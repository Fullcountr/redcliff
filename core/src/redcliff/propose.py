"""Profile tables and propose a data model from them (M2 in docs/modeling.md).

Two halves, so the logic stays pure Python and runs in the browser too:

- profiling SQL (:func:`profile_query`, :func:`inclusion_query`) that DuckDB runs, and
  :func:`read_profile` to turn a result row into a :class:`TableProfile`;
- :func:`propose_model`, which turns profiles (and the foreign keys that held) into a Kimball
  model, with the reason for every choice, for a person to confirm.

:func:`profile_files` does the whole round trip with a DuckDB connection, for the CLI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlglot import exp

from redcliff.model import Attribute, Entity, Model, Relationship

NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
           "FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC", "INT", "INT8", "INT4", "INT2", "LONG", "SHORT")
DATES = ("DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ", "DATETIME")
NOISE = re.compile(r"(^(etl|load|dw|audit)_|(_at|_ts|_timestamp)$|^(updated|modified|created|inserted)_"
                   r"|^last_(updated|modified|login|seen|run|changed))")
CORRECTABLE = re.compile(r"(^|_)(name|first_name|last_name|full_name|title|description|label)$")
QUANTITY = re.compile(r"(qty|quantity|count|units|amount|total|price|cost|revenue|sales|value)$")


@dataclass
class ColumnProfile:
    name: str
    type: str  # the engine's type, e.g. BIGINT, DECIMAL(10,2), VARCHAR, DATE
    nulls: int = 0
    distinct: int = 0

    @property
    def base(self) -> str:
        return self.type.upper().split("(")[0].strip()

    @property
    def numeric(self) -> bool:
        return self.base in NUMERIC

    @property
    def integer(self) -> bool:
        return self.numeric and self.base not in ("FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC")

    @property
    def date(self) -> bool:
        return self.base in DATES


@dataclass
class TableProfile:
    name: str  # a model-friendly name, e.g. customers
    source: str  # the path or table it was read from
    rows: int
    columns: list[ColumnProfile] = field(default_factory=list)
    # Evidence from elsewhere than the data (a legacy job): keys and measures with the reason for each.
    # ``keys`` set (even empty) replaces the profile's candidate keys.
    keys: dict[str, str] | None = None
    measures: dict[str, str] = field(default_factory=dict)

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.columns if c.name == name), None)

    def key_reason(self, key: str) -> str:
        if self.keys is not None and key in self.keys:
            return self.keys[key]
        return f"{key} is unique and never null in {self.rows:,} rows"

    def candidate_keys(self) -> list[str]:
        """Columns that are never null and never repeat: what could identify a row."""
        if self.keys is not None:
            return list(self.keys)
        if not self.rows:
            return []
        return [c.name for c in self.columns if c.nulls == 0 and c.distinct == self.rows and not c.date
                and (c.integer or c.base in ("VARCHAR", "TEXT", "STRING", "CHAR", "UUID"))]


# --------------------------------------------------------------------------- profiling SQL


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def profile_query(relation: str, columns: list[str]) -> str:
    """One query that returns the row count and, per column, its nulls and distinct values."""
    parts = ["COUNT(*) AS __rows"]
    for i, c in enumerate(columns):
        parts.append(f"COUNT(*) - COUNT({_q(c)}) AS __n{i}")
        parts.append(f"COUNT(DISTINCT {_q(c)}) AS __d{i}")
    return f"SELECT {', '.join(parts)} FROM {relation}"


def read_profile(name: str, source: str, types: dict[str, str], row: list[Any] | tuple) -> TableProfile:
    """A :class:`TableProfile` from :func:`profile_query`'s single result row."""
    cols = list(types)
    profile = TableProfile(name=name, source=source, rows=int(row[0] or 0))
    for i, c in enumerate(cols):
        profile.columns.append(ColumnProfile(c, types[c], nulls=int(row[1 + 2 * i] or 0), distinct=int(row[2 + 2 * i] or 0)))
    return profile


def _singular(name: str) -> str:
    if name.endswith("ies") and len(name) > 4:
        return name[:-3] + "y"
    if name.endswith(("sses", "xes", "ches", "shes", "uses")):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss") and len(name) > 2:
        return name[:-1]
    return name


def _model_name(text: str) -> str:
    out = re.sub(r"\W+", "_", text.strip().lower()).strip("_") or "t"
    return out if re.match(r"[a-z_]", out) else f"t_{out}"


def candidate_references(tables: list[TableProfile]) -> list[tuple[str, str, str, str]]:
    """(child table, column, parent table, parent key) pairs worth testing for inclusion.

    Names must match, so two unrelated id columns are never linked by accident: the child column is
    the parent's key name (``customer_id`` to ``customer_id``) or the parent's singular name plus the
    key (``customer_id`` to ``customers.id``)."""
    out = []
    for parent in tables:
        for key in parent.candidate_keys():
            names = {key, f"{_singular(parent.name)}_{key}"} if key != "id" else {f"{_singular(parent.name)}_id"}
            for child in tables:
                if child is parent:
                    continue
                for col in child.columns:
                    pk = parent.column(key)
                    if col.name in names and pk and col.integer == pk.integer and not col.date:
                        out.append((child.name, col.name, parent.name, key))
    return out


def inclusion_query(child_relation: str, column: str, parent_relation: str, key: str) -> str:
    """How many distinct non-null values of the child column are missing from the parent key."""
    return (f"SELECT COUNT(*) FROM (SELECT DISTINCT {_q(column)} AS v FROM {child_relation} "
            f"WHERE {_q(column)} IS NOT NULL EXCEPT SELECT {_q(key)} FROM {parent_relation})")


# --------------------------------------------------------------------------- the proposal


@dataclass
class ModelProposal:
    model: Model
    reasons: dict[str, list[str]] = field(default_factory=dict)  # "entity" or "entity.attribute" -> why

    def why(self, ref: str, reason: str) -> None:
        self.reasons.setdefault(ref, []).append(reason)

    def to_dict(self) -> dict:
        return {"model": self.model.to_dict(), "reasons": self.reasons}


def _type(col: ColumnProfile) -> str:
    t = col.type.lower()
    if col.base in ("VARCHAR", "TEXT", "STRING"):
        return "varchar"
    try:
        exp.DataType.build(t)
        return t
    except Exception:  # noqa: BLE001 - an engine type sqlglot doesn't know
        return "varchar"


def propose_model(tables: list[TableProfile], references: list[tuple[str, str, str, str]], name: str = "proposed",
                  sources: dict[str, str] | None = None) -> ModelProposal:
    """A Kimball model for ``tables``.

    ``references`` are the (child, column, parent, key) pairs whose values all appear in the
    parent: foreign keys that held in the data. ``sources`` maps a table name to the path or table
    the entity should load from (default: the profile's source).
    """
    sources = sources or {}
    refs_from: dict[str, list[tuple[str, str, str]]] = {}
    referenced: set[str] = set()
    for child, col, parent, key in references:
        refs_from.setdefault(child, []).append((col, parent, key))
        referenced.add(parent)

    def measures(t: TableProfile, skip: set[str]) -> list[ColumnProfile]:
        return [c for c in t.columns if c.name not in skip and c.numeric and not c.date
                and (not c.integer or QUANTITY.search(c.name) or c.name in t.measures)]

    kinds: dict[str, str] = {}
    proposal = ModelProposal(Model(name=name, style="kimball"))
    for t in tables:
        fk_cols = {c for c, _p, _k in refs_from.get(t.name, [])}
        if fk_cols and measures(t, fk_cols):
            kinds[t.name] = "fact"
        elif t.name in referenced or t.candidate_keys():
            kinds[t.name] = "dimension"
        else:
            kinds[t.name] = "fact" if measures(t, set()) else "table"

    dims: dict[str, Entity] = {}
    date_refs: list[tuple[str, str, str]] = []  # (fact entity, attribute, source column)
    for t in tables:
        kind = kinds[t.name]
        keys = t.candidate_keys()
        source = sources.get(t.name, t.source)
        if kind == "dimension":
            stem = _singular(t.name)
            e = Entity(id=f"dim_{stem}", kind="dimension", source=source)
            if not keys:
                proposal.why(e.id, f"nothing in the job identifies a row of {t.name}; mark its business key"
                             if t.keys is not None else
                             f"{t.name} has no column that is unique and never null; mark its business key")
            bk = keys[0] if keys else None
            if bk:
                why_key = t.key_reason(bk)
                also = ", and other tables reference it" if t.name in referenced else ""
                proposal.why(e.id, f"a dimension: {why_key}{also}")
            e.attributes.append(Attribute(f"{stem}_key", "bigint", role="surrogate_key"))
            proposal.why(f"{e.id}.{stem}_key", "a surrogate key, so history (SCD2) can keep several versions of a row")
            for c in t.columns:
                attr_name = f"{stem}_id" if c.name == bk and bk == "id" else c.name
                a = Attribute(attr_name, _type(c), source=None if attr_name == c.name else c.name)
                if c.name == bk:
                    a.role = "business_key"
                    proposal.why(f"{e.id}.{attr_name}", why_key)
                elif NOISE.search(c.name):
                    a.track = False
                    proposal.why(f"{e.id}.{attr_name}", "looks like an audit or load column: a change doesn't make a new version")
                elif CORRECTABLE.search(c.name):
                    a.history = "scd1"
                    proposal.why(f"{e.id}.{attr_name}",
                                 "a name: a change is usually a correction, so it overwrites every version")
                e.attributes.append(a)
            dims[t.name] = e
            proposal.model.entities.append(e)

    for t in tables:
        kind = kinds[t.name]
        if kind == "dimension":
            continue
        source = sources.get(t.name, t.source)
        keys = t.candidate_keys()
        prefix = "fct_" if kind == "fact" else ""
        e = Entity(id=f"{prefix}{_model_name(t.name)}", kind=kind, source=source)
        fks: dict[str, tuple[str, str]] = {}
        for c, p, k in refs_from.get(t.name, []):  # a column that fits several parents: the dimension's
            if c not in fks or (kinds.get(fks[c][0]) != "dimension" and kinds.get(p) == "dimension"):
                fks[c] = (p, k)
        ms = {c.name for c in measures(t, set(fks))}
        if kind == "fact":
            e.grain = f"one {_singular(t.name)} ({keys[0]})" if keys else f"one row of {t.name}"
            parents = ", ".join(sorted({p for p, _k in fks.values()}))
            listed = ", ".join(sorted(ms))
            proposal.why(e.id, f"a fact: it references {parents} and has measures ({listed})" if fks
                         else f"a fact: it has measures ({listed})")
        for c in t.columns:
            if c.name in fks and fks[c.name][0] in dims:
                parent, key = fks[c.name]
                dim = dims[parent]
                sk = next(a.name for a in dim.attributes if a.role == "surrogate_key")
                bk = next(a for a in dim.attributes if a.role == "business_key")
                e.attributes.append(Attribute(sk, "bigint", source=c.name))  # looked up by this column
                proposal.why(f"{e.id}.{sk}", f"every {c.name} in {t.name} appears as {parent}.{key}: looked up as "
                                             f"{dim.id}'s version on the row's date (as it was)")
                proposal.model.relationships.append(Relationship(f"{e.id}.{sk}", f"{dim.id}.{sk}"))
                durable = Attribute(c.name, _type(c), role="durable_key",
                                    description=f"{dim.id}.{bk.name}, for as-is reporting (Type 7)")
                e.attributes.append(durable)
                proposal.why(f"{e.id}.{c.name}", "kept as the durable key, so reports can also use today's version")
            elif kind == "fact" and c.date:
                key_name = f"{c.name}_key"
                e.attributes.append(Attribute(key_name, "integer",
                                              source=f"YEAR({c.name}) * 10000 + MONTH({c.name}) * 100 + DAY({c.name})"))
                e.attributes.append(Attribute(c.name, _type(c)))
                date_refs.append((e.id, key_name, c.name))
                proposal.why(f"{e.id}.{key_name}", f"{c.name} is a date: it points at the date dimension")
            else:
                a = Attribute(c.name, _type(c))
                if keys and c.name == keys[0]:
                    a.role = "business_key"
                    proposal.why(f"{e.id}.{c.name}", f"{t.key_reason(c.name)}: identifies a row")
                elif c.name in ms:
                    a.role = "measure"
                    proposal.why(f"{e.id}.{c.name}", t.measures.get(c.name) or "numeric and not a key: a measure")
                e.attributes.append(a)
        proposal.model.entities.append(e)

    if date_refs:
        date = Entity(id="dim_date", kind="dimension", history="scd1",
                      description="One row per calendar day the facts refer to.")
        date.attributes = [
            Attribute("date_key", "integer", role="surrogate_key",
                      source="YEAR(calendar_date) * 10000 + MONTH(calendar_date) * 100 + DAY(calendar_date)"),
            Attribute("calendar_date", "date", role="business_key"),
            Attribute("year", "integer"), Attribute("quarter", "integer"),
            Attribute("month", "integer"), Attribute("day", "integer"),
        ]
        proposal.why("dim_date", "the facts have dates (" + ", ".join(f"{f}.{c}" for f, _k, c in date_refs)
                     + "); built from the dates they use")
        proposal.model.entities.insert(len(dims), date)
        for fact, key, _col in date_refs:
            proposal.model.relationships.append(Relationship(f"{fact}.{key}", "dim_date.date_key"))
    return proposal


# --------------------------------------------------------------------------- the CLI round trip


def profile_files(con, paths: list[str | Path], base_dir: str | Path | None = None) -> ModelProposal:
    """Profile CSV/Parquet files with DuckDB, test the foreign keys their names suggest, and propose.

    Entity sources are written relative to ``base_dir`` (the folder the model will be saved in)."""
    tables: list[TableProfile] = []
    relation: dict[str, str] = {}
    sources: dict[str, str] = {}
    for p in paths:
        path = Path(p)
        literal = "'" + str(path).replace("'", "''") + "'"
        rel = f"read_parquet({literal})" if path.suffix.lower() == ".parquet" else f"read_csv_auto({literal}, header = true)"
        name = _model_name(path.stem)
        types = {row[0]: str(row[1]) for row in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()}
        row = con.execute(profile_query(rel, list(types))).fetchone()
        tables.append(read_profile(name, str(path), types, row))
        relation[name] = rel
        if base_dir is not None:
            try:
                sources[name] = Path(path).resolve().relative_to(Path(base_dir).resolve()).as_posix()
            except ValueError:
                import os

                sources[name] = os.path.relpath(Path(path).resolve(), Path(base_dir).resolve()).replace(os.sep, "/")
    held = [r for r in candidate_references(tables)
            if con.execute(inclusion_query(relation[r[0]], r[1], relation[r[2]], r[3])).fetchone()[0] == 0]
    return propose_model(tables, held, sources=sources)


def to_data_vault(kimball: ModelProposal) -> ModelProposal:
    """The same data as a Data Vault 2.0 model: a hub and a satellite per dimension, and per fact a
    hub (on its business key), a link to the hubs it references and a satellite of its measures.

    Hash keys are MD5 (``char(32)``); satellites carry ``load_dts``, ``record_source`` and a ``hashdiff``.
    """
    k = kimball.model
    out = ModelProposal(Model(name=k.name, style="data_vault", description=k.description))
    meta = [Attribute("load_dts", "timestamp"), Attribute("record_source", "varchar")]
    hub_of: dict[str, tuple[str, str, str]] = {}  # kimball entity -> (hub id, hash key, business key name)
    sat_skip = {"surrogate_key", "durable_key", "business_key"}
    for e in k.entities:
        if e.kind != "dimension" or not e.source:
            continue
        stem = e.id[4:] if e.id.startswith("dim_") else e.id
        bk = next(a for a in e.attributes if a.role == "business_key")
        hub = Entity(id=f"hub_{stem}", kind="hub", source=e.source, attributes=[
            Attribute(f"{stem}_hk", "char(32)", role="surrogate_key"),
            Attribute(bk.name, bk.type, role="business_key", source=bk.source), *[Attribute(m.name, m.type) for m in meta]])
        out.why(hub.id, f"a hub for the {bk.name} business key of {e.id}")
        sat = Entity(id=f"sat_{stem}", kind="satellite", source=e.source, attributes=[
            Attribute(f"{stem}_hk", "char(32)", role="surrogate_key", source=bk.source or bk.name),
            Attribute("load_dts", "timestamp"), Attribute("hashdiff", "char(32)"),
            *[Attribute(a.name, a.type, source=a.source) for a in e.attributes if a.role not in sat_skip],
            Attribute("record_source", "varchar")])
        out.why(sat.id, f"the descriptive columns of {e.id}, a new row whenever their hashdiff changes")
        out.model.entities += [hub, sat]
        out.model.relationships.append(Relationship(f"{sat.id}.{stem}_hk", f"{hub.id}.{stem}_hk"))
        hub_of[e.id] = (hub.id, f"{stem}_hk", bk.name)
    for e in k.entities:
        if e.kind != "fact" or not e.source:
            continue
        stem = _singular(e.id[4:] if e.id.startswith("fct_") else e.id)
        bk = next((a for a in e.attributes if a.role == "business_key"), None)
        if bk is None:
            out.why(e.id, "left out: a fact without a business key has no hub; mark one and propose again")
            continue
        hub = Entity(id=f"hub_{stem}", kind="hub", source=e.source, attributes=[
            Attribute(f"{stem}_hk", "char(32)", role="surrogate_key"),
            Attribute(bk.name, bk.type, role="business_key", source=bk.source), *[Attribute(m.name, m.type) for m in meta]])
        out.model.entities.append(hub)
        refs = [(r, k.entity(r.target.split(".")[0])) for r in k.relationships if r.source.startswith(f"{e.id}.")]
        hubs = [(e.attribute(r.source.split(".")[1]), hub_of[d.id]) for r, d in refs if d and d.id in hub_of]
        if hubs:
            link = Entity(id=f"lnk_{stem}_" + "_".join(h[0][4:] for _a, h in hubs), kind="link", source=e.source)
            link.attributes = [Attribute(f"{link.id[4:]}_hk", "char(32)", role="surrogate_key"),
                               Attribute(f"{stem}_hk", "char(32)", source=bk.source or bk.name)]
            out.model.relationships.append(Relationship(f"{link.id}.{stem}_hk", f"{hub.id}.{stem}_hk"))
            for attr, (hub_id, hub_hk, _bk) in hubs:
                link.attributes.append(Attribute(hub_hk, "char(32)", source=attr.source or attr.name))
                out.model.relationships.append(Relationship(f"{link.id}.{hub_hk}", f"{hub_id}.{hub_hk}"))
            link.attributes += [Attribute(m.name, m.type) for m in meta]
            out.why(link.id, f"links {hub.id} to " + ", ".join(h[0] for _a, h in hubs))
            out.model.entities.append(link)
        skip = {a.name for a, _h in hubs} | {r.source.split(".")[1] for r, _d in refs}
        sat = Entity(id=f"sat_{stem}", kind="satellite", source=e.source, attributes=[
            Attribute(f"{stem}_hk", "char(32)", role="surrogate_key", source=bk.source or bk.name),
            Attribute("load_dts", "timestamp"), Attribute("hashdiff", "char(32)"),
            *[Attribute(a.name, a.type, source=a.source) for a in e.attributes
              if a.role not in sat_skip and a.name not in skip],
            Attribute("record_source", "varchar")])
        out.model.entities.append(sat)
        out.model.relationships.append(Relationship(f"{sat.id}.{stem}_hk", f"{hub.id}.{stem}_hk"))
        out.why(sat.id, f"the measures and descriptive columns of {e.id}")
    return out
