"""Data models: the tables a pipeline should produce, as a spec of their own (``model.yaml``).

A model names entities (dimensions, facts, Data Vault hubs, links and satellites, or plain tables),
their attributes and keys, and the relationships between them. Pipelines load it; it doesn't load
anything itself. This module reads, checks, imports (SQL DDL, DBML) and exports (DDL, DBML, Mermaid,
dbt ``schema.yml``) models. See docs/modeling.md for the design.

Pure Python: it runs in the browser too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from redcliff.spec import ID_PATTERN, Issue, SpecError

MODEL_VERSION = 0
STYLES = ("kimball", "data_vault")
KINDS = ("dimension", "fact", "hub", "link", "satellite", "table")
ROLES = ("surrogate_key", "durable_key", "business_key", "measure")
HISTORY = ("scd1", "scd2")
KEY_ROLES = ("surrogate_key", "durable_key", "business_key")


@dataclass
class Attribute:
    name: str
    type: str
    role: str | None = None
    history: str | None = None  # dimension attributes: scd1 overwrites every version
    track: bool = True  # dimension attributes: false = a change doesn't make a new version
    description: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Attribute:
        return cls(name=d.get("name"), type=d.get("type"), role=d.get("role"), history=d.get("history"),
                   track=d.get("track", True), description=d.get("description"))

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.role:
            out["role"] = self.role
        if self.history:
            out["history"] = self.history
        if self.track is not True:
            out["track"] = self.track
        if self.description:
            out["description"] = self.description
        return out


@dataclass
class Entity:
    id: str
    kind: str = "table"
    attributes: list[Attribute] = field(default_factory=list)
    description: str | None = None
    grain: str | None = None  # facts: what one row is
    history: str | None = None  # dimensions: scd2 (default) or scd1

    @classmethod
    def from_dict(cls, d: dict) -> Entity:
        return cls(id=d.get("id"), kind=d.get("kind", "table"), description=d.get("description"),
                   grain=d.get("grain"), history=d.get("history"),
                   attributes=[Attribute.from_dict(a) for a in d.get("attributes") or [] if isinstance(a, dict)])

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind}
        for key in ("description", "grain", "history"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        out["attributes"] = [a.to_dict() for a in self.attributes]
        return out

    def attribute(self, name: str) -> Attribute | None:
        return next((a for a in self.attributes if a.name == name), None)

    @property
    def scd2(self) -> bool:
        return self.kind == "dimension" and (self.history or "scd2") == "scd2"

    def key(self) -> list[str]:
        """The columns that identify a row: the surrogate key, else the business key."""
        for role in ("surrogate_key", "business_key"):
            names = [a.name for a in self.attributes if a.role == role]
            if names:
                return names
        return []


@dataclass
class Relationship:
    source: str  # entity.attribute holding the reference (many side)
    target: str  # entity.attribute referenced (one side)

    @classmethod
    def from_dict(cls, d: dict) -> Relationship:
        return cls(source=d.get("from"), target=d.get("to"))

    def to_dict(self) -> dict:
        return {"from": self.source, "to": self.target}


@dataclass
class Model:
    name: str
    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    style: str = "kimball"
    description: str | None = None
    version: int = MODEL_VERSION

    @classmethod
    def from_dict(cls, d: dict) -> Model:
        if not isinstance(d, dict):
            raise SpecError([Issue(None, "a model must be a mapping")])
        return cls(name=d.get("name"), style=d.get("style", "kimball"), description=d.get("description"),
                   version=d.get("version", MODEL_VERSION),
                   entities=[Entity.from_dict(e) for e in d.get("entities") or [] if isinstance(e, dict)],
                   relationships=[Relationship.from_dict(r) for r in d.get("relationships") or []
                                  if isinstance(r, dict)])

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"version": self.version, "name": self.name, "style": self.style}
        if self.description:
            out["description"] = self.description
        out["entities"] = [e.to_dict() for e in self.entities]
        if self.relationships:
            out["relationships"] = [r.to_dict() for r in self.relationships]
        return out

    def entity(self, entity_id: str) -> Entity | None:
        return next((e for e in self.entities if e.id == entity_id), None)

    def resolve(self, ref: str) -> tuple[Entity | None, Attribute | None]:
        entity_id, _, name = (ref or "").partition(".")
        entity = self.entity(entity_id)
        return entity, entity.attribute(name) if entity else None


# --------------------------------------------------------------------------- reading and writing


def json_schema() -> dict[str, Any]:
    """The JSON Schema for model files, for editors and the web app."""
    return json.loads((Path(__file__).parent / "model.schema.json").read_text(encoding="utf-8"))


def parse_model(text: str) -> Model:
    """A model from YAML or JSON text."""
    if text.lstrip().startswith("{"):
        return Model.from_dict(json.loads(text))
    import yaml

    return Model.from_dict(yaml.safe_load(text))


def load_model(path: str | Path) -> Model:
    return parse_model(Path(path).read_text(encoding="utf-8"))


def dump_model(model: Model, fmt: str = "yaml") -> str:
    data = model.to_dict()
    if fmt == "json":
        return json.dumps(data, indent=2) + "\n"
    import yaml

    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


# --------------------------------------------------------------------------- validation


def _type_ok(type_name: Any) -> bool:
    if not isinstance(type_name, str) or not type_name.strip():
        return False
    try:
        exp.DataType.build(type_name)
    except (ParseError, ValueError):
        return False
    return True


def _family(type_name: str) -> str:
    """A coarse type family, to compare the two ends of a relationship."""
    t = exp.DataType.build(type_name)
    if t.is_type(*exp.DataType.INTEGER_TYPES):
        return "integer"
    if t.is_type(*exp.DataType.TEXT_TYPES):
        return "text"
    return t.this.name.lower()


def validate_model(model: Model) -> list[Issue]:
    """Every problem in the model. An empty list means it's valid."""
    issues: list[Issue] = []
    if model.version != MODEL_VERSION:
        issues.append(Issue(None, f"unsupported model version {model.version!r}; expected {MODEL_VERSION}"))
    if not isinstance(model.name, str) or not model.name.strip() or any(ord(c) < 32 for c in model.name):
        issues.append(Issue(None, "'name' is required and must be one line"))
    if model.style not in STYLES:
        issues.append(Issue(None, f"'style' must be one of {list(STYLES)}"))
    if not model.entities:
        issues.append(Issue(None, "a model needs at least one entity"))

    seen: set[str] = set()
    for e in model.entities:
        if not isinstance(e.id, str) or not ID_PATTERN.match(e.id):
            issues.append(Issue(None, f"entity id {e.id!r} must match {ID_PATTERN.pattern}"))
            continue
        if e.id in seen:
            issues.append(Issue(e.id, "duplicate entity id"))
        seen.add(e.id)
        if e.kind not in KINDS:
            issues.append(Issue(e.id, f"'kind' must be one of {list(KINDS)}"))
        if not e.attributes:
            issues.append(Issue(e.id, "an entity needs at least one attribute"))
        if e.kind == "fact" and not (isinstance(e.grain, str) and e.grain.strip()):
            issues.append(Issue(e.id, "a fact needs a 'grain': what one row is"))
        if e.history is not None and (e.kind != "dimension" or e.history not in HISTORY):
            issues.append(Issue(e.id, f"'history' applies to dimensions and is one of {list(HISTORY)}"))
        names: set[str] = set()
        for a in e.attributes:
            if not isinstance(a.name, str) or not ID_PATTERN.match(a.name):
                issues.append(Issue(e.id, f"attribute name {a.name!r} must match {ID_PATTERN.pattern}"))
                continue
            if a.name in names:
                issues.append(Issue(e.id, f"duplicate attribute {a.name!r}"))
            names.add(a.name)
            if not _type_ok(a.type):
                issues.append(Issue(e.id, f"{a.name}: {a.type!r} is not a SQL type"))
            if a.role is not None and a.role not in ROLES:
                issues.append(Issue(e.id, f"{a.name}: 'role' must be one of {list(ROLES)}"))
            if a.history is not None and (e.kind != "dimension" or a.history not in HISTORY):
                issues.append(Issue(e.id, f"{a.name}: 'history' applies to dimension attributes and is one of "
                                          f"{list(HISTORY)}"))
            if a.track is not True and (e.kind != "dimension" or a.track is not False):
                issues.append(Issue(e.id, f"{a.name}: 'track: false' applies to dimension attributes"))
            if a.role in KEY_ROLES and (a.history or a.track is not True):
                issues.append(Issue(e.id, f"{a.name}: a key isn't tracked or overwritten; drop 'history'/'track'"))
        if sum(a.role == "surrogate_key" for a in e.attributes) > 1:
            issues.append(Issue(e.id, "an entity has at most one surrogate key"))
        if e.kind in ("dimension", "hub") and not any(a.role == "business_key" for a in e.attributes):
            issues.append(Issue(e.id, f"a {e.kind} needs a business key (role: business_key)"))

    for r in model.relationships:
        label = f"relationship {r.source} -> {r.target}"
        ends = []
        for ref in (r.source, r.target):
            entity, attr = model.resolve(ref) if isinstance(ref, str) else (None, None)
            if attr is None:
                issues.append(Issue(None, f"{label}: {ref!r} is not an entity.attribute in this model"))
            ends.append((entity, attr))
        if any(a is None for _, a in ends):
            continue
        (_, src), (tgt_entity, tgt) = ends
        if _type_ok(src.type) and _type_ok(tgt.type) and _family(src.type) != _family(tgt.type):
            issues.append(Issue(None, f"{label}: types differ ({src.type} vs {tgt.type})"))
        if tgt_entity.key() != [tgt.name]:
            issues.append(Issue(None, f"{label}: {r.target} isn't {tgt_entity.id}'s key"))
    return issues


# --------------------------------------------------------------------------- exports


def _keywords() -> set[str]:
    from redcliff.compile import _keywords as kw

    return kw()


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=name.upper() in _keywords())


def _order(model: Model) -> list[Entity]:
    """Entities with the ones others reference first, so DDL constraints resolve."""
    deps = {e.id: {r.target.split(".")[0] for r in model.relationships if r.source.split(".")[0] == e.id} - {e.id}
            for e in model.entities}
    done: list[Entity] = []
    pending = list(model.entities)
    while pending:
        ready = [e for e in pending if deps[e.id] <= {d.id for d in done}] or pending[:1]  # cycles: keep order
        done.append(ready[0])
        pending.remove(ready[0])
    return done


def to_ddl(model: Model, dialect: str = "duckdb") -> str:
    """CREATE TABLE statements, with primary and foreign keys where the dialect takes them."""
    constraints = dialect not in ("spark", "databricks", "hive")
    out = [f"-- Redcliff model: {' '.join(model.name.split())} ({model.style}, {dialect})"]
    for e in _order(model):
        cols: list[exp.Expression] = [exp.ColumnDef(this=_ident(a.name), kind=exp.DataType.build(a.type))
                                      for a in e.attributes]
        key = e.key()
        if constraints and key and not (e.scd2 and e.key() == [a.name for a in e.attributes
                                                                   if a.role == "business_key"]):
            cols.append(exp.PrimaryKey(expressions=[_ident(k) for k in key]))
        create = exp.Create(kind="TABLE", this=exp.Schema(this=exp.to_table(e.id), expressions=cols))
        text = create.sql(dialect, pretty=True)
        if constraints:
            fks = [f"  FOREIGN KEY ({_ident(r.source.split('.')[1]).sql(dialect)}) REFERENCES "
                   f"{r.target.split('.')[0]} ({_ident(r.target.split('.')[1]).sql(dialect)})"
                   for r in model.relationships if r.source.split(".")[0] == e.id]
            if fks:
                text = text[:text.rindex("\n)")] + ",\n" + ",\n".join(fks) + "\n)"
        head = f"-- {e.id}: {e.kind}" + (f" (grain: {e.grain})" if e.grain else "")
        out.append(" ".join(head.split()) + "\n" + text + ";")
        if not constraints:
            for r in model.relationships:
                if r.source.split(".")[0] == e.id:
                    out.append(f"-- {r.source} references {r.target}")
    return "\n\n".join(out) + "\n"


def _quote_note(text: str) -> str:
    return "'" + " ".join(text.split()).replace("\\", "\\\\").replace("'", "\\'") + "'"


def to_dbml(model: Model) -> str:
    """DBML (dbdiagram.io): tables, keys, notes and references."""
    lines = [f"// Redcliff model: {' '.join(model.name.split())} ({model.style})", ""]
    for e in model.entities:
        settings = [f"note: {_quote_note(e.description or e.grain or e.kind)}"]
        lines.append(f"Table {e.id} [{', '.join(settings)}] {{")
        key = e.key()
        for a in e.attributes:
            flags = []
            if len(key) == 1 and a.name == key[0] and not (e.scd2 and a.role == "business_key"):
                flags.append("pk")
            note = ", ".join(x for x in (a.role and a.role.replace("_", " "), a.history,
                                         "not tracked" if a.track is False else None, a.description) if x)
            if note:
                flags.append(f"note: {_quote_note(note)}")
            lines.append(f"  {a.name} {a.type.replace(' ', '')}" + (f" [{', '.join(flags)}]" if flags else ""))
        if len(key) > 1:
            lines += ["  indexes {", f"    ({', '.join(key)}) [pk]", "  }"]
        lines += ["}", ""]
    lines += [f"Ref: {r.source} > {r.target}" for r in model.relationships]
    return "\n".join(lines).rstrip() + "\n"


def to_mermaid(model: Model) -> str:
    """A Mermaid erDiagram: entities, attributes with PK/FK marks, and relationships."""
    fks = {r.source for r in model.relationships}
    lines = ["erDiagram"]
    for e in model.entities:
        key = set(e.key())
        lines.append(f"  {e.id} {{")
        for a in e.attributes:
            base = re.sub(r"\W", "_", exp.DataType.build(a.type).this.name.lower())
            marks = ",".join(m for m, on in (("PK", a.name in key), ("FK", f"{e.id}.{a.name}" in fks)) if on)
            comment = a.role.replace("_", " ") if a.role else ("SCD1" if a.history == "scd1" else "")
            comment = comment.replace('"', "'")
            lines.append(f"    {base} {a.name}" + (f" {marks}" if marks else "") + (f' "{comment}"' if comment else ""))
        lines.append("  }")
    for r in model.relationships:
        src = r.source.split(".")[0]
        tgt = r.target.split(".")[0]
        lines.append(f'  {tgt} ||--o{{ {src} : "{r.source.split(".")[1]}"')
    return "\n".join(lines) + "\n"


def to_dbt_schema(model: Model) -> str:
    """A dbt ``schema.yml``: each entity as a model with key and relationship tests."""
    rels = {r.source: r.target for r in model.relationships}
    lines = ["version: 2", "", "models:"]
    for e in model.entities:
        lines.append(f"  - name: {e.id}")
        about = e.description or (f"{e.kind.capitalize()}. Grain: {e.grain}" if e.grain else e.kind.capitalize())
        lines.append(f"    description: {json.dumps(' '.join(about.split()))}")
        lines.append("    columns:")
        key = e.key()
        for a in e.attributes:
            lines.append(f"      - name: {a.name}")
            if a.description:
                lines.append(f"        description: {json.dumps(' '.join(a.description.split()))}")
            tests: list[str] = []
            if a.role in KEY_ROLES:
                tests.append("not_null")
                # an SCD2 dimension repeats its business key across versions
                if key == [a.name] and not (e.scd2 and a.role == "business_key"):
                    tests.append("unique")
            target = rels.get(f"{e.id}.{a.name}")
            if target:
                t_entity, _, t_col = target.partition(".")
                tests.append(f"relationships:\n              to: ref('{t_entity}')\n              field: {t_col}")
            if tests:
                lines.append("        tests:")
                lines += [f"          - {t}" for t in tests]
    return "\n".join(lines) + "\n"


EXPORTS = {"ddl": to_ddl, "dbml": to_dbml, "mermaid": to_mermaid, "dbt": to_dbt_schema}


# --------------------------------------------------------------------------- imports

_PREFIXES = (("dim_", "dimension"), ("d_", "dimension"), ("fct_", "fact"), ("fact_", "fact"), ("f_", "fact"),
             ("hub_", "hub"), ("h_", "hub"), ("lnk_", "link"), ("link_", "link"), ("l_", "link"),
             ("sat_", "satellite"), ("s_", "satellite"))


def _kind_of(name: str) -> str:
    return next((kind for prefix, kind in _PREFIXES if name.startswith(prefix)), "table")


def _name(text: str) -> str:
    """A model id from a table or column name: lower case, non-word characters to underscores."""
    out = re.sub(r"\W+", "_", text.strip().lower()).strip("_") or "x"
    return out if re.match(r"[a-z_]", out) else f"_{out}"


def _key_role(name: str, type_name: str) -> str:
    integer = exp.DataType.build(type_name).is_type(*exp.DataType.INTEGER_TYPES)
    return "surrogate_key" if integer and re.search(r"(_key|_sk|_hk)$", name) else "business_key"


@dataclass
class ModelImport:
    model: Model
    warnings: list[str] = field(default_factory=list)


def _finish(name: str, tables: dict[str, Entity], keys: dict[str, list[str]],
            refs: list[tuple[str, str, str, str]], warnings: list[str]) -> ModelImport:
    for table, cols in keys.items():
        entity = tables.get(table)
        if entity is None:
            continue
        for col in cols:
            attr = entity.attribute(col)
            if attr:
                attr.role = attr.role or (_key_role(attr.name, attr.type) if len(cols) == 1 else "business_key")
    kinds = {e.kind for e in tables.values()}
    style = "data_vault" if kinds & {"hub", "link", "satellite"} else "kimball"
    model = Model(name=name, style=style, entities=list(tables.values()))
    for src_t, src_c, tgt_t, tgt_c in refs:
        if src_t in tables and tgt_t in tables and tables[src_t].attribute(src_c) and tables[tgt_t].attribute(tgt_c):
            model.relationships.append(Relationship(f"{src_t}.{src_c}", f"{tgt_t}.{tgt_c}"))
        else:
            warnings.append(f"reference {src_t}.{src_c} -> {tgt_t}.{tgt_c} skipped: a table or column isn't in the file")
    for e in model.entities:
        if e.kind == "fact":
            if not e.grain:
                warnings.append(f"{e.id}: set the fact's grain")
            for a in e.attributes:
                t = exp.DataType.build(a.type)
                quantity = t.is_type(*exp.DataType.INTEGER_TYPES) and \
                    re.search(r"(qty|quantity|count|units|amount|total)$", a.name)
                if a.role is None and f"{e.id}.{a.name}" not in {r.source for r in model.relationships} and \
                        (t.is_type(*exp.DataType.REAL_TYPES) or quantity):
                    a.role = "measure"
        if e.kind in ("dimension", "hub") and not any(a.role == "business_key" for a in e.attributes):
            warnings.append(f"{e.id}: mark its business key (role: business_key)")
    return ModelImport(model, warnings)


def from_ddl(sql: str, dialect: str | None = None, name: str = "imported") -> ModelImport:
    """A model from CREATE TABLE statements: columns, primary keys and foreign keys.

    Table names give the kind (``dim_``, ``fct_``, ``hub_``, ...). Anything else in the script is
    skipped with a warning; nothing is dropped silently."""
    warnings: list[str] = []
    tables: dict[str, Entity] = {}
    keys: dict[str, list[str]] = {}
    refs: list[tuple[str, str, str, str]] = []
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except ParseError as e:
        raise SpecError([Issue(None, f"not valid SQL: {str(e).splitlines()[0]}")]) from None
    for st in statements:
        if st is None:
            continue
        if not (isinstance(st, exp.Create) and (st.args.get("kind") or "").upper() == "TABLE"
                and isinstance(st.this, exp.Schema)):
            warnings.append(f"skipped: {' '.join(st.sql(dialect)[:60].split())}...")
            continue
        table = _name(st.this.this.name)
        entity = Entity(id=table, kind=_kind_of(table))
        tables[table] = entity
        for item in st.this.expressions:
            if isinstance(item, exp.ColumnDef):
                col = _name(item.name)
                kind = item.args.get("kind")
                entity.attributes.append(Attribute(col, kind.sql().lower() if kind else "varchar"))
                for c in item.args.get("constraints") or []:
                    k = c.args.get("kind")
                    if isinstance(k, exp.PrimaryKeyColumnConstraint):
                        keys[table] = [col]
                    elif isinstance(k, exp.Reference):
                        target = k.this
                        cols = [x.name for x in target.expressions] if isinstance(target, exp.Schema) else []
                        t_name = target.this.name if isinstance(target, exp.Schema) else target.name
                        refs.append((table, col, _name(t_name), _name(cols[0]) if cols else col))
            elif isinstance(item, exp.PrimaryKey):
                keys[table] = [_name(x.name) for x in item.expressions]
            elif isinstance(item, exp.ForeignKey):
                ref = item.args.get("reference")
                target = ref.this if ref else None
                if isinstance(target, exp.Schema):
                    for s, t in zip(item.expressions, target.expressions):
                        refs.append((table, _name(s.name), _name(target.this.name), _name(t.name)))
            else:
                warnings.append(f"{table}: skipped {' '.join(item.sql(dialect)[:50].split())}")
    if not tables:
        raise SpecError([Issue(None, "no CREATE TABLE statements found")])
    return _finish(name, tables, keys, refs, warnings)


_DBML_TABLE = re.compile(r"^\s*Table\s+(?:\"?[\w]+\"?\.)?\"?([\w]+)\"?(?:\s+as\s+\w+)?\s*(?:\[[^\]]*\])?\s*\{", re.I)
_DBML_REF = re.compile(r"^\s*Ref(?:\s+\w+)?\s*:\s*\"?(\w+)\"?\.\"?(\w+)\"?\s*([<>-]|<>)\s*\"?(\w+)\"?\.\"?(\w+)\"?", re.I)
_DBML_COL = re.compile(r"^\s*\"?(\w+)\"?\s+(\"[^\"]+\"|[\w]+(?:\s*\([^)]*\))?)\s*(?:\[(.*)\])?\s*$")


def from_dbml(text: str, name: str = "imported") -> ModelImport:
    """A model from DBML: tables, columns, ``pk``, inline ``ref:`` and ``Ref:`` lines, notes."""
    warnings: list[str] = []
    tables: dict[str, Entity] = {}
    keys: dict[str, list[str]] = {}
    refs: list[tuple[str, str, str, str]] = []
    current: Entity | None = None
    depth = 0

    def add_ref(a_t, a_c, op, b_t, b_c):
        # `>` many-to-one: a references b; `<` the reverse; `-` one-to-one, a references b
        if op == "<":
            refs.append((_name(b_t), _name(b_c), _name(a_t), _name(a_c)))
        elif op == "<>":
            warnings.append(f"many-to-many {a_t}.{a_c} <> {b_t}.{b_c} skipped: model it with a bridge or link")
        else:
            refs.append((_name(a_t), _name(a_c), _name(b_t), _name(b_c)))

    # one brace per line, so `Table t { id int [pk] }` reads like the multi-line form
    text = re.sub(r"\{([^\n]*?)\}\s*$", lambda m: "{\n" + m.group(1) + "\n}", text, flags=re.M)
    for raw in text.splitlines():
        line = raw.split("//")[0].rstrip()
        if not line.strip():
            continue
        if current is None:
            m = _DBML_TABLE.match(line)
            if m:
                current = Entity(id=_name(m.group(1)), kind=_kind_of(_name(m.group(1))))
                tables[current.id] = current
                note = re.search(r"note:\s*'([^']*)'", line, re.I)
                if note:
                    current.description = note.group(1)
                depth = 1
                continue
            m = _DBML_REF.match(line)
            if m:
                add_ref(*m.groups())
            elif not re.match(r"^\s*(Project|Enum|TableGroup|Note)\b|^\s*[}{]|^\s*\w+\s*$|^\s*\w+:", line):
                warnings.append(f"skipped: {line.strip()[:60]}")
            continue
        stripped = line.strip()
        if stripped.startswith("}"):
            depth -= 1
            if depth <= 0:
                current = None
            continue
        if stripped.endswith("{"):  # indexes { ... } or Note { ... }
            depth += 1
            continue
        if depth > 1:
            pk = re.match(r"^\(([^)]*)\)\s*\[[^\]]*\bpk\b", stripped, re.I)
            if pk:
                keys[current.id] = [_name(c) for c in pk.group(1).split(",")]
            continue
        if re.match(r"^note\s*:", stripped, re.I):
            note = re.search(r"'([^']*)'", stripped)
            current.description = note.group(1) if note else current.description
            continue
        m = _DBML_COL.match(line)
        if not m:
            warnings.append(f"{current.id}: skipped {stripped[:60]}")
            continue
        col = _name(m.group(1))
        type_name = m.group(2).strip('"')
        if not _type_ok(type_name):
            warnings.append(f"{current.id}.{col}: type {type_name!r} isn't a SQL type; using varchar")
            type_name = "varchar"
        attr = Attribute(col, type_name.lower())
        current.attributes.append(attr)
        settings = m.group(3) or ""
        if re.search(r"(^|,)\s*(pk|primary key)\s*(,|$)", settings, re.I):
            keys[current.id] = [col]
        note = re.search(r"note:\s*'((?:[^'\\]|\\.)*)'", settings, re.I)
        if note:  # to_dbml writes role, history and tracking into the note; read them back
            rest = []
            for part in (p.strip() for p in note.group(1).replace("\\'", "'").split(",")):
                if part.replace(" ", "_") in ROLES:
                    attr.role = part.replace(" ", "_")
                elif part in HISTORY:
                    attr.history = part
                elif part == "not tracked":
                    attr.track = False
                elif part:
                    rest.append(part)
            attr.description = ", ".join(rest) or None
        ref = re.search(r"ref:\s*([<>-]|<>)\s*\"?(\w+)\"?\.\"?(\w+)\"?", settings, re.I)
        if ref:
            add_ref(current.id, col, ref.group(1), ref.group(2), ref.group(3))
    if not tables:
        raise SpecError([Issue(None, "no DBML tables found")])
    return _finish(name, tables, keys, refs, warnings)
