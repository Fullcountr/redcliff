"""Grow a data model with new tables, without touching what is already there (docs/modeling.md).

Adding a file to a model shouldn't mean proposing the whole model again: the entities someone
renamed, the history they changed, the relationships they drew are theirs. :func:`extend_model`
proposes the model the tables suggest (with :func:`~redcliff.propose.propose_model`), keeps every
entity the model already has, and returns only what is new, as separate changes a person accepts
or skips:

- ``entity``: a new entity for a new table (a dimension, a fact; ``dim_date`` if new facts need one);
- ``relationship``: a reference the data shows, new to existing or existing to new, with the
  attributes it brings (the surrogate key a fact looks up, the durable key it keeps);
- ``append``: a new table with exactly the columns of one the model loads already (a month per
  file, say), loaded with it through the entity's ``append`` list. Its alternative, a separate
  entity, comes as an ``entity`` change that is off by default and excludes it.

:func:`apply_changes` applies the accepted ones. Both are pure Python: the browser profiles its
tables with DuckDB-Wasm and calls the same functions the CLI does.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from redcliff.model import Attribute, Entity, Model, Relationship
from redcliff.propose import ModelProposal, TableProfile, propose_model, to_data_vault


@dataclass
class Change:
    id: str
    kind: str  # entity | relationship | append
    label: str
    why: list[str] = field(default_factory=list)
    default: bool = True  # accepted unless the person unticks it
    needs: list[str] = field(default_factory=list)  # changes that must be accepted too
    excludes: list[str] = field(default_factory=list)  # changes it can't go with
    entity: Entity | None = None  # entity: the new entity
    reasons: dict[str, list[str]] = field(default_factory=dict)  # entity: why, per entity and attribute
    target: str | None = None  # relationship: the entity that gets the attributes; append: the entity
    attributes: list[Attribute] = field(default_factory=list)  # relationship: attributes to add to target
    roles: dict[str, dict[str, str]] = field(default_factory=dict)  # relationship: attribute -> role, description
    relationship: Relationship | None = None
    path: str | None = None  # append: the file or table
    alongside: str | None = None  # append: the source it loads with (every entity reading it gets it)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "label": self.label, "why": self.why,
                               "default": self.default, "needs": self.needs, "excludes": self.excludes}
        if self.entity is not None:
            out["entity"] = self.entity.to_dict()
            out["reasons"] = self.reasons
        if self.target is not None:
            out["target"] = self.target
        if self.attributes:
            out["attributes"] = [a.to_dict() for a in self.attributes]
        if self.roles:
            out["roles"] = self.roles
        if self.relationship is not None:
            out["relationship"] = self.relationship.to_dict()
        if self.path is not None:
            out["path"] = self.path
        if self.alongside is not None:
            out["alongside"] = self.alongside
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Change:
        return cls(id=d["id"], kind=d["kind"], label=d.get("label", ""), why=list(d.get("why") or []),
                   default=d.get("default", True), needs=list(d.get("needs") or []),
                   excludes=list(d.get("excludes") or []),
                   entity=Entity.from_dict(d["entity"]) if d.get("entity") else None,
                   reasons=dict(d.get("reasons") or {}), target=d.get("target"),
                   attributes=[Attribute.from_dict(a) for a in d.get("attributes") or []],
                   roles=dict(d.get("roles") or {}),
                   relationship=Relationship.from_dict(d["relationship"]) if d.get("relationship") else None,
                   path=d.get("path"), alongside=d.get("alongside"))


@dataclass
class Extension:
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what was looked at and left alone, and why

    def to_dict(self) -> dict[str, Any]:
        return {"changes": [c.to_dict() for c in self.changes], "notes": self.notes}


def _same_source(a: str | None, b: str | None) -> bool:
    """Two sources name the same file or table: equal, or the same file name (a model may keep
    ``data/orders.csv`` where the browser has ``orders.csv``)."""
    if not a or not b:
        return False
    if a == b:
        return True
    pa, pb = PurePosixPath(a.replace("\\", "/")), PurePosixPath(b.replace("\\", "/"))
    return pa.name == pb.name or (not pa.suffix and pa.name == pb.stem) or (not pb.suffix and pb.name == pa.stem)


def _sources(e: Entity) -> list[str]:
    return [s for s in [e.source, *(e.append or [])] if s]


def _fresh(wanted: str, taken: set[str]) -> str:
    name, i = wanted, 2
    while name in taken:
        name, i = f"{wanted}_{i}", i + 1
    taken.add(name)
    return name


def _describe(e: Entity) -> str:
    what = {"dimension": "a dimension", "fact": "a fact", "hub": "a hub", "link": "a link",
            "satellite": "a satellite"}.get(e.kind, "a table")
    return f"{e.id}: {what}, {len(e.attributes)} attributes"


def extend_model(model: Model, tables: list[TableProfile], new: list[str] | set[str],
                 references: list[tuple[str, str, str, str]], sources: dict[str, str] | None = None) -> Extension:
    """What ``new`` tables add to ``model``.

    ``tables`` are profiles of every table to consider: the new ones and the ones the model already
    loads (so references can be tested both ways). ``references`` are the (child, column, parent,
    key) pairs that held in the data, as for :func:`~redcliff.propose.propose_model`.
    """
    sources = sources or {}
    src_of = {t.name: sources.get(t.name, t.source) for t in tables}
    by_name = {t.name: t for t in tables}
    ext = Extension()
    new = set(new)
    for name in sorted(new):  # a table the model loads already isn't new
        holder = next((e for e in model.entities if any(_same_source(s, src_of.get(name)) for s in _sources(e))), None)
        if holder is not None:
            new.discard(name)
            ext.notes.append(f"{src_of[name]} is in the model already ({holder.id})")
    if not new:
        return ext

    # A new table with exactly the columns, in order, of another (one the model loads, or an earlier
    # new one): offer to load it with that one.
    loaded = {t.name for t in tables if t.name not in new
              and any(_same_source(s, src_of[t.name]) for e in model.entities for s in _sources(e))}
    appendable: dict[str, str] = {}  # new table -> the table it has the shape of
    for name in sorted(new):
        shape = [(c.name, c.base) for c in by_name[name].columns]
        for other in [*sorted(loaded), *sorted(new)]:
            if other == name or other in appendable or (other in new and other > name):
                continue
            if [(c.name, c.base) for c in by_name[other].columns] == shape:
                appendable[name] = other
                break

    proposal: ModelProposal = propose_model(tables, references, name=model.name, sources=sources)
    if model.style == "data_vault":
        proposal = to_data_vault(proposal)
    proposed = proposal.model

    # Which proposed entity is which existing one: the same source (and kind), or dim_date by name.
    existing_of: dict[str, str] = {}
    new_entities: list[Entity] = []
    for pe in proposed.entities:
        table = next((t for t, s in src_of.items() if pe.source and _same_source(s, pe.source)), None)
        if pe.source and table not in new:
            match = [e for e in model.entities if any(_same_source(s, pe.source) for s in _sources(e))]
            same_kind = [e for e in match if e.kind == pe.kind]
            if same_kind or len(match) == 1:
                existing_of[pe.id] = (same_kind or match)[0].id
            elif not match:
                ext.notes.append(f"{pe.source} isn't in the model any more; it was left out")
            continue
        if not pe.source:  # the date dimension
            date = model.entity(pe.id) or next(
                (e for e in model.entities if e.kind == "dimension" and not e.source
                 and any(a.role == "business_key" and a.type.lower().startswith("date") for a in e.attributes)), None)
            if date:
                existing_of[pe.id] = date.id
                continue
        new_entities.append(pe)

    taken = {e.id for e in model.entities}
    final: dict[str, str] = dict(existing_of)
    for pe in new_entities:
        final[pe.id] = _fresh(pe.id, taken)

    def key_of(entity_id: str) -> str | None:
        e = model.entity(entity_id)
        if e is None:
            pe = proposed.entity(next(k for k, v in final.items() if v == entity_id))
            keys = pe.key()
        else:
            keys = e.key()
        return keys[0] if len(keys) == 1 else None

    # Relationships the proposal draws that touch something new; their attributes travel with them.
    entity_change: dict[str, Change] = {}
    for pe in new_entities:
        e = copy.deepcopy(pe)
        e.id = final[pe.id]
        entity_change[pe.id] = Change(id=f"entity:{e.id}", kind="entity", entity=e, label=_describe(e),
                                      why=list(proposal.reasons.get(pe.id, [])))
    rel_changes: list[Change] = []
    moved: dict[str, set[str]] = {}  # new entity -> attributes that travel with a relationship
    for r in proposed.relationships:
        (src_e, src_a), (tgt_e, tgt_a) = r.source.split(".", 1), r.target.split(".", 1)
        if src_e in existing_of and tgt_e in existing_of:
            continue  # between tables the model has: the model already says what it wants
        source_entity, target_entity = final[src_e], final[tgt_e]
        target_key = key_of(target_entity) if tgt_e in existing_of else tgt_a
        if target_key is None:
            ext.notes.append(f"{r.source} -> {target_entity}: {target_entity} has no single key to reference")
            continue
        if tgt_e in existing_of and model.entity(target_entity).kind != proposed.entity(tgt_e).kind:
            ext.notes.append(f"{r.source} -> {target_entity}: the model has {target_entity} as a "
                             f"{model.entity(target_entity).kind}, not a {proposed.entity(tgt_e).kind}; link it by hand")
            continue
        pe = proposed.entity(src_e)
        attr = copy.deepcopy(pe.attribute(src_a))
        if attr is None:
            continue
        # A fact's lookup attribute is named after the dimension's key: follow the existing key's name.
        name = target_key if (tgt_e in existing_of and src_a == tgt_a and attr.role != "surrogate_key") else src_a
        holder = model.entity(source_entity) if src_e in existing_of else None
        if holder is not None and holder.attribute(name) is not None and holder.attribute(name).source != attr.source:
            name = _fresh(name, {a.name for a in holder.attributes})
        attr.name = name
        change = Change(id=f"relationship:{source_entity}.{name}", kind="relationship", target=source_entity,
                        relationship=Relationship(f"{source_entity}.{name}", f"{target_entity}.{target_key}"),
                        label=f"{source_entity}.{name} -> {target_entity}.{target_key}",
                        why=list(proposal.reasons.get(r.source, [])))
        for side in (src_e, tgt_e):
            if side in entity_change:
                change.needs.append(entity_change[side].id)
        if attr.role != "surrogate_key":  # a satellite's or link's own key stays with its entity
            if holder is None or holder.attribute(name) is None:
                change.attributes.append(attr)
            moved.setdefault(src_e, set()).add(src_a)
            durable = next((a for a in pe.attributes if a.role == "durable_key" and a.name == attr.source), None)
            if durable is not None:
                target_bk = next((a.name for a in (model.entity(target_entity) or proposed.entity(tgt_e)).attributes
                                  if a.role == "business_key"), None)
                has = holder.attribute(durable.name) if holder is not None else True
                if has is not None and (holder is None or has.role is None):
                    change.roles[durable.name] = {
                        "role": "durable_key",
                        "description": f"{target_entity}.{target_bk}, for as-is reporting (Type 7)"}
                moved.setdefault(src_e, set()).add(f"role:{durable.name}")
                change.why += proposal.reasons.get(f"{src_e}.{durable.name}", [])
        rel_changes.append(change)

    # New entities carry their plain columns; lookups and durable-key roles come with relationships.
    for pid, change in entity_change.items():
        e = change.entity
        for a in list(e.attributes):
            if a.name in moved.get(pid, set()):
                e.attributes.remove(a)
            elif f"role:{a.name}" in moved.get(pid, set()):
                a.role, a.description = None, None
        change.reasons = {k.replace(pid, e.id, 1): v for k, v in proposal.reasons.items()
                          if k == pid or k.startswith(f"{pid}.")}

    for name, other in appendable.items():
        shape = len(by_name[name].columns)
        into = next((e.id for e in model.entities if any(_same_source(s, src_of[other]) for s in _sources(e))), None)
        needs = []
        if into is None:  # the other is new too: its entity has to be accepted
            pid = next(p for p, c in entity_change.items() if _same_source(proposed.entity(p).source, src_of[other]))
            into, needs = entity_change[pid].entity.id, [entity_change[pid].id]
        append = Change(id=f"append:{name}", kind="append", target=into, path=src_of[name], alongside=src_of[other],
                        needs=needs, label=f"{src_of[name]} loads into {into} with {src_of[other]}",
                        why=[f"{name} has exactly the columns of {other} ({shape}, in the same order), so it reads "
                             f"as more rows of the same table"])
        ext.changes.append(append)
        for pid, change in entity_change.items():
            if _same_source(proposed.entity(pid).source, src_of[name]):
                change.default = False
                change.excludes.append(append.id)
                append.excludes.append(change.id)
                change.why.insert(0, f"instead of loading {name} into {into}: a separate entity")
    off = {c.id for c in entity_change.values() if not c.default}
    for rc in rel_changes:
        if off & set(rc.needs):
            rc.default = False

    ext.changes += list(entity_change.values()) + rel_changes
    return ext


def apply_changes(model: Model, changes: list[Change], accepted: list[str] | set[str]) -> Model:
    """``model`` with the accepted changes (and only those whose needs are accepted too)."""
    out = copy.deepcopy(model)
    ids = set(accepted)
    by_id = {c.id: c for c in changes}

    def ok(c: Change) -> bool:
        if c.id not in ids or any(n in by_id and not ok(by_id[n]) for n in c.needs) \
                or any(n not in by_id and n not in ids for n in c.needs):
            return False
        return c.default or not any(x in ids for x in c.excludes)  # the default wins a clash

    for c in changes:
        if c.kind == "entity" and ok(c) and c.entity is not None and out.entity(c.entity.id) is None:
            e = copy.deepcopy(c.entity)
            if e.kind == "dimension":  # dimensions before facts, as a proposal orders them
                at = max((i + 1 for i, x in enumerate(out.entities) if x.kind == "dimension"), default=0)
                out.entities.insert(at, e)
            else:
                out.entities.append(e)
    for c in changes:
        if c.kind == "append" and ok(c) and c.path:
            for e in out.entities:
                reads = any(_same_source(s, c.alongside or "") for s in _sources(e)) or (not c.alongside and e.id == c.target)
                if reads and not any(_same_source(s, c.path) for s in _sources(e)):
                    e.append = [*(e.append or []), c.path]
    for c in changes:
        if c.kind != "relationship" or not ok(c) or c.relationship is None:
            continue
        holder = out.entity(c.target)
        if holder is None:
            continue
        for a in c.attributes:
            if holder.attribute(a.name) is None:
                holder.attributes.append(copy.deepcopy(a))
        for name, settings in c.roles.items():
            a = holder.attribute(name)
            if a is not None and a.role is None:
                a.role = settings.get("role")
                a.description = a.description or settings.get("description")
        if not any(r.source == c.relationship.source and r.target == c.relationship.target for r in out.relationships):
            out.relationships.append(copy.deepcopy(c.relationship))
    return out


def link_check_query(child_relation: str, column: str, parent_relation: str, key: str) -> str:
    """For a relationship drawn by hand: the child's distinct non-null values and how many of them
    the parent key has (``total``, ``found``)."""
    q = lambda n: '"' + n.replace('"', '""') + '"'  # noqa: E731
    return (f"SELECT COUNT(*) AS total, COUNT(p.k) AS found FROM (SELECT DISTINCT {q(column)} AS v FROM "
            f"{child_relation} WHERE {q(column)} IS NOT NULL) c LEFT JOIN (SELECT DISTINCT {q(key)} AS k FROM "
            f"{parent_relation}) p ON CAST(c.v AS VARCHAR) = CAST(p.k AS VARCHAR)")


def link(model: Model, source: str, target: str) -> tuple[Model, list[str]]:
    """``model`` with a relationship from ``source`` (entity.attribute) to ``target`` (an entity, or
    entity.attribute: its key). A dimension target gets a lookup the Kimball way: a new attribute
    named after its surrogate key, read from the source attribute's column, which becomes the
    durable key. Returns the model and what was done."""
    out = copy.deepcopy(model)
    src_e, src_a = source.split(".", 1)
    child = out.entity(src_e)
    tgt_id = target.split(".", 1)[0]
    parent = out.entity(tgt_id)
    if child is None or parent is None or child.attribute(src_a) is None:
        raise ValueError(f"{source} -> {target}: no such entity or attribute")
    if out.style == "data_vault":
        raise ValueError("in a Data Vault model, links are entities of their own: add them in the YAML")
    if parent.kind != "dimension":
        raise ValueError(f"{parent.id} is a {parent.kind}: a relationship points at a dimension")
    if child.kind == "dimension":
        raise ValueError(f"{child.id} is a dimension too: link facts to dimensions (dimension to dimension, "
                         f"an outrigger, isn't loaded by Redcliff)")
    keys = parent.key()
    if len(keys) != 1:
        raise ValueError(f"{parent.id} has no single key to reference; mark one")
    key = keys[0]
    col = child.attribute(src_a)
    done = []
    sk = parent.attribute(key)
    if parent.kind == "dimension" and sk.role == "surrogate_key" and col.role != "surrogate_key" and src_a != key:
        name = key if child.attribute(key) is None else f"{src_a}_{key}"
        child.attributes.insert(child.attributes.index(col), Attribute(name, sk.type, source=col.source or col.name))
        done.append(f"{child.id}.{name} looks up {parent.id}'s version by {col.name}")
        if col.role is None:
            bk = next((a.name for a in parent.attributes if a.role == "business_key"), key)
            col.role, col.description = "durable_key", f"{parent.id}.{bk}, for as-is reporting (Type 7)"
            done.append(f"{child.id}.{col.name} is kept as the durable key")
        src_a = name
    rel = Relationship(f"{child.id}.{src_a}", f"{parent.id}.{key}")
    if not any(r.source == rel.source and r.target == rel.target for r in out.relationships):
        out.relationships.append(rel)
        done.append(f"{rel.source} -> {rel.target}")
    return out, done


def extend_files(con, model: Model, model_dir, paths: list) -> Extension:
    """Profile the files a model loads and ``paths`` with DuckDB, test the references that touch a
    new file, and extend the model with them (for the CLI). Sources are written relative to
    ``model_dir``, like the model's own."""
    import os
    from pathlib import Path

    from redcliff.propose import _model_name, candidate_references, inclusion_query, profile_query, read_profile

    model_dir = Path(model_dir)
    tables: list[TableProfile] = []
    relation: dict[str, str] = {}
    sources: dict[str, str] = {}
    taken: set[str] = set()

    def add(path: Path, source: str) -> str:
        literal = "'" + str(path).replace("'", "''") + "'"
        rel = f"read_parquet({literal})" if path.suffix.lower() == ".parquet" else f"read_csv_auto({literal}, header = true)"
        name = _fresh(_model_name(path.stem), taken)
        types = {row[0]: str(row[1]) for row in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()}
        tables.append(read_profile(name, source, types, con.execute(profile_query(rel, list(types))).fetchone()))
        relation[name], sources[name] = rel, source
        return name

    for e in model.entities:
        if e.source and Path(e.source).suffix.lower() in (".csv", ".parquet") and (model_dir / e.source).exists():
            add(model_dir / e.source, e.source)
    new = []
    for p in paths:
        path = Path(p)
        try:
            source = path.resolve().relative_to(model_dir.resolve()).as_posix()
        except ValueError:
            source = os.path.relpath(path.resolve(), model_dir.resolve()).replace(os.sep, "/")
        new.append(add(path, source))
    held = [r for r in candidate_references(tables) if (r[0] in new or r[2] in new)
            and con.execute(inclusion_query(relation[r[0]], r[1], relation[r[2]], r[3])).fetchone()[0] == 0]
    return extend_model(model, tables, new, held, sources)
