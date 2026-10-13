"""Legacy jobs and data models: propose a model from what a job does, and map a job onto a model.

An imported job (SQL, PowerCenter, SSIS, Talend or a pipeline spec) knows things about the tables it reads
that a profile of the data can't tell you, and it knows them without any data:

- the columns it joins on, and which side of each join is the one side (a key);
- the tables it looks rows up in, deduplicated on a key;
- the targets it merges on a key;
- the columns it sums or averages (measures).

:func:`job_evidence` collects that, :func:`propose_from_job` turns it into a Kimball proposal with the
step behind every reason, and :func:`map_job` shows, for every column the job writes, the model
attributes it comes from, so you can see how much of the legacy job a model covers.

Pure Python: it runs in the browser too.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import sqlglot
from sqlglot import exp

from redcliff.lineage import DIRECT, Ref, build_graph, trace_to_sources
from redcliff.model import Model
from redcliff.propose import ColumnProfile, ModelProposal, TableProfile, _model_name, _singular, propose_model
from redcliff.spec import KEYED_MODES, Node, Pipeline, bind

DEDUPE = ("ANY_VALUE", "FIRST", "LAST", "MIN", "MAX", "ARBITRARY", "FIRST_VALUE", "LAST_VALUE")
SUMMED = (exp.Sum, exp.Avg)


@dataclass
class JobEvidence:
    """What a job says about the tables it reads. Table names are model-friendly (``orders``)."""

    tables: dict[str, dict[str, str]] = field(default_factory=dict)  # table -> {column: type}
    sources: dict[str, str] = field(default_factory=dict)  # table -> the table name or file path it reads
    keys: dict[str, dict[str, str]] = field(default_factory=dict)  # table -> {key column: reason}
    references: dict[tuple[str, str, str, str], str] = field(default_factory=dict)  # (child, col, parent, key) -> why
    joins: list[tuple[str, str, str, str, str]] = field(default_factory=list)  # undirected: (t, c, t, c, step)
    measures: dict[str, dict[str, str]] = field(default_factory=dict)  # table -> {column: reason}
    used: dict[str, set[str]] = field(default_factory=dict)  # table -> columns the job reads
    files: set[str] = field(default_factory=set)  # tables read from files rather than a database

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables": self.tables, "sources": self.sources, "keys": self.keys,
            "references": [{"from": f"{c}.{col}", "to": f"{p}.{k}", "why": w} for (c, col, p, k), w in self.references.items()],
            "measures": self.measures, "used": {t: sorted(c) for t, c in self.used.items()},
        }


def _table_of(node: Node) -> tuple[str, str]:
    """(model table name, what the entity should read) for a source node."""
    p = node.params
    if p.get("format") == "table":
        return _model_name(str(p["table"]).split(".")[-1]), str(p["table"])
    return _model_name(PurePosixPath(str(p["path"]).replace("\\", "/")).stem), str(p["path"])


def _columns(node: Node) -> dict[str, str]:
    cols = node.params.get("columns")
    if isinstance(cols, dict):
        return {str(c): str(t) for c, t in cols.items()}
    if isinstance(cols, list):
        return {str(c): "varchar" for c in cols}
    return {}


def _looks_like_key(table: str, column: str, other: str | None = None) -> bool:
    """Named like the key of ``table``: ``id``, ``customer_id`` in customers, or ``x`` where the
    other side of the join is ``customer_x``."""
    stem = _singular(table)
    if column == "id" or column in (f"{stem}_id", f"{stem}_key", f"{stem}_code", f"{stem}_no", f"{table}_id"):
        return True
    return other is not None and other == f"{stem}_{column}"


def job_evidence(pipeline: Pipeline) -> JobEvidence:
    """Keys, references and measures the job implies for each table it reads."""
    pipeline = bind(pipeline)
    graph = build_graph(pipeline)
    ev = JobEvidence()
    fed = {e.dst.node for e in graph.edges}
    src_table: dict[str, str] = {}  # source node id -> table
    for n in pipeline.nodes:
        if n.type != "source" or n.id in fed:
            continue  # a source that reads back a table the job writes isn't an input
        table, source = _table_of(n)
        src_table[n.id] = table
        ev.sources.setdefault(table, source)
        if n.params.get("format") != "table":
            ev.files.add(table)
        cols = ev.tables.setdefault(table, {})
        for c, t in (_columns(n) or {c: "varchar" for c in graph.columns.get(n.id, [])}).items():
            cols.setdefault(c, t)

    def origins(ref: Ref, aggregated: bool = False) -> list[tuple[str, str]]:
        """Source (table, column) a column carries directly: its value, not just its rows."""
        out = []
        for src, (typ, sub) in trace_to_sources(graph, ref).items():
            if typ == DIRECT and (aggregated or sub != "AGGREGATION") and src.node in src_table:
                out.append((src_table[src.node], src.column))
        return sorted(set(out))

    def key(table: str, column: str, why: str) -> None:
        ev.keys.setdefault(table, {}).setdefault(column, why)

    # What the job reads: the columns behind what it writes, and the ones that decide its rows.
    for n in pipeline.nodes:
        if n.type == "target":
            for c in graph.columns.get(n.id, []):
                for t, col in origins(Ref(n.id, c), aggregated=True):
                    ev.used.setdefault(t, set()).add(col)
    for de in graph.dataset_edges:
        for t, col in origins(de.src):
            ev.used.setdefault(t, set()).add(col)

    # Lookups: a table deduplicated on one column (and only picked from) before a join is keyed on it.
    for n in pipeline.nodes:
        if n.type != "aggregate" or len(n.params.get("group_by") or []) != 1:
            continue
        measures = n.params.get("measures") or {}
        picks = [_parse(sql) for sql in measures.values()]
        if not picks or not all(e is not None and _picked(e) for e in picks):
            continue
        col = n.params["group_by"][0]
        for t, c in origins(Ref(n.id, col)):
            key(t, c, f"the job looks {t} rows up by {c} (step {n.id})")

    # Targets merged on a key, with no aggregate on the way: the key identifies the table's rows.
    for n in pipeline.nodes:
        if n.type == "target" and n.params.get("mode") in KEYED_MODES and not _aggregated(pipeline, n.id):
            for k in n.params.get("keys") or []:
                for t, c in origins(Ref(n.id, k)):
                    key(t, c, f"the job matches {n.params.get('table') or n.id} rows on {c} (step {n.id})")

    # Joins: each equality between two tables' columns; the side named like (or used as) a key is the one side.
    for n in pipeline.nodes:
        if n.type != "join":
            continue
        cond = _parse(n.params.get("condition") or "")
        if cond is None:
            continue
        for eq in [*cond.find_all(exp.EQ), *cond.find_all(exp.NullSafeEQ)]:
            sides = []
            for side in (eq.this, eq.expression):
                if not isinstance(side, exp.Column):
                    break
                ref = _resolve(n, side, graph.columns)
                found = origins(ref) if ref else []
                if len(found) != 1:
                    break
                sides.append(found[0])
            if len(sides) != 2 or sides[0][0] == sides[1][0]:
                continue
            (ta, ca), (tb, cb) = sides
            ev.joins.append((ta, ca, tb, cb, n.id))

    for _ in range(2):  # a key found from one join can orient another
        for ta, ca, tb, cb, step in ev.joins:
            a_key = ca in ev.keys.get(ta, {}) or _looks_like_key(ta, ca, cb)
            b_key = cb in ev.keys.get(tb, {}) or _looks_like_key(tb, cb, ca)
            if a_key == b_key:
                continue  # can't tell which side is the one side
            (child, ccol, parent, pkey) = (tb, cb, ta, ca) if a_key else (ta, ca, tb, cb)
            key(parent, pkey, f"{child}.{ccol} is joined to it (step {step}) and it is named like a key of {parent}")
            ev.references.setdefault((child, ccol, parent, pkey), f"joined on {child}.{ccol} = {parent}.{pkey} (step {step})")

    # Otherwise a column named like the table's own key (order_id in orders) is taken as its key.
    for table, cols in ev.tables.items():
        if table not in ev.keys:
            named = next((c for c in cols if _looks_like_key(table, c)), None)
            if named:
                key(table, named, f"named like the key of {table}; nothing in the job contradicts it")

    # Measures: numeric columns the job sums or averages.
    for n in pipeline.nodes:
        if n.type != "aggregate":
            continue
        for name, sql in (n.params.get("measures") or {}).items():
            e = _parse(sql)
            if e is None:
                continue
            for agg in e.find_all(*SUMMED):
                for col in agg.find_all(exp.Column):
                    ref = _resolve_input(n, col.name, graph.columns)
                    for t, c in origins(ref) if ref else []:
                        fn = "summed" if isinstance(agg, exp.Sum) else "averaged"
                        ev.measures.setdefault(t, {}).setdefault(c, f"{fn} by the job ({name} in step {n.id}): a measure")
    return ev


def _parse(sql: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001 - an expression the parser can't read tells us nothing
        return None


def _picked(e: exp.Expression) -> bool:
    """True for ANY_VALUE(x), MAX(x) and the like: a value picked from the rows, not computed over them."""
    while isinstance(e, (exp.IgnoreNulls, exp.RespectNulls)):
        e = e.this
    if isinstance(e, (exp.AnyValue, exp.Max, exp.Min, exp.First, exp.Last)):
        return True
    return isinstance(e, exp.Anonymous) and str(e.this).upper() in DEDUPE


def _aggregated(pipeline: Pipeline, node_id: str) -> bool:
    seen, todo = set(), [node_id]
    while todo:
        n = pipeline.node(todo.pop())
        if n.id in seen:
            continue
        seen.add(n.id)
        if n.type in ("aggregate", "sql"):
            return True
        todo += n.inputs
    return False


def _resolve(node: Node, col: exp.Column, columns: dict[str, list[str]]) -> Ref | None:
    if col.table and col.table in node.inputs:
        return Ref(col.table, col.name) if col.name in columns.get(col.table, []) else None
    return _resolve_input(node, col.name, columns)


def _resolve_input(node: Node, name: str, columns: dict[str, list[str]]) -> Ref | None:
    for inp in node.inputs:
        if name in columns.get(inp, []):
            return Ref(inp, name)
    return None


# --------------------------------------------------------------------------- propose


def propose_from_job(pipeline: Pipeline, name: str | None = None, base_dir: str | Path | None = None,
                     job_dir: str | Path | None = None) -> tuple[ModelProposal, JobEvidence]:
    """A Kimball model for the tables a job reads, from what the job does with them.

    File sources are rewritten relative to ``base_dir`` (where the model will be saved) when the job's
    folder ``job_dir`` is given; table sources are kept as they are."""
    ev = job_evidence(pipeline)
    profiles = []
    for table, cols in ev.tables.items():
        t = TableProfile(name=table, source=ev.sources[table], rows=0,
                         columns=[ColumnProfile(c, _engine_type(ty)) for c, ty in cols.items()],
                         keys=dict(ev.keys.get(table, {})), measures=dict(ev.measures.get(table, {})))
        profiles.append(t)
    sources = {t: _relocate(s, job_dir, base_dir) if t in ev.files else s for t, s in ev.sources.items()}
    refs = list(ev.references)
    proposal = propose_model(profiles, refs, name=name or _model_name(pipeline.name or "proposed"), sources=sources)
    proposal.model.description = f"Proposed from the legacy job {pipeline.name}."
    for (child, col, _parent, _key), why in ev.references.items():
        for e in proposal.model.entities:
            if e.source == sources.get(child):
                for a in e.attributes:
                    if (a.source or a.name) == col and a.role != "durable_key":
                        proposal.why(f"{e.id}.{a.name}", why + ("; it names a row of another dimension (a snowflake): "
                                                                "copy what reports need onto this one for a star"
                                                                if e.kind == "dimension" else ""))
    for e in proposal.model.entities:
        table = next((t for t, s in sources.items() if s == e.source), None)
        unused = [c for c in ev.tables.get(table, {}) if c not in ev.used.get(table, set())] if table else []
        if unused:
            proposal.why(e.id, "the job never reads " + ", ".join(unused) + "; keep them only if reports need them")
    return proposal, ev


def _engine_type(t: str) -> str:
    """Spec column types (``text``, ``int``) as the engine names propose expects."""
    low = t.strip().lower()
    return {"text": "VARCHAR", "string": "VARCHAR", "int": "INTEGER", "long": "BIGINT"}.get(low, t.upper())


def _relocate(source: str, job_dir: str | Path | None, base_dir: str | Path | None) -> str:
    path = Path(source)
    if job_dir is None or base_dir is None or path.is_absolute():
        return source
    return os.path.relpath((Path(job_dir) / path).resolve(), Path(base_dir).resolve()).replace(os.sep, "/")


# --------------------------------------------------------------------------- map a job onto a model


@dataclass
class ColumnMap:
    target: str  # the job's target step
    table: str | None  # the table or file it writes
    column: str
    inputs: list[dict[str, Any]]  # {"table", "column", "kind": "direct"|"indirect", "attributes": [entity.attr]}

    @property
    def covered(self) -> bool:
        direct = [i for i in self.inputs if i["kind"] == "direct"]
        return all(i["attributes"] for i in direct)


@dataclass
class JobMap:
    columns: list[ColumnMap] = field(default_factory=list)
    unmapped: dict[str, list[str]] = field(default_factory=dict)  # table -> source columns the job uses, not in the model
    tables: dict[str, list[str]] = field(default_factory=dict)  # table -> the entities that load from it

    def coverage(self) -> float:
        return sum(c.covered for c in self.columns) / len(self.columns) if self.columns else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage(),
            "covered": sum(c.covered for c in self.columns), "total": len(self.columns),
            "columns": [{"target": c.target, "table": c.table, "column": c.column, "covered": c.covered,
                         "inputs": c.inputs} for c in self.columns],
            "unmapped": self.unmapped, "tables": self.tables,
        }

    def to_markdown(self, title: str = "Legacy job mapped onto the model") -> str:
        lines = [f"## {title}", "",
                 f"{sum(c.covered for c in self.columns)} of {len(self.columns)} columns the job writes "
                 f"({self.coverage():.0%}) come from model attributes.", ""]
        if self.tables:
            lines += ["| Table the job reads | Loaded into |", "| --- | --- |"]
            lines += [f"| `{t}` | {', '.join(f'`{e}`' for e in es) or '—'} |" for t, es in self.tables.items()]
            lines.append("")
        lines += ["| Writes | From (model attributes) | Covered |", "| --- | --- | --- |"]
        for c in self.columns:
            direct = [i for i in c.inputs if i["kind"] == "direct"]
            parts = [f"`{i['table']}.{i['column']}` → " + (", ".join(f"`{a}`" for a in i["attributes"]) or "not in the model")
                     for i in direct]
            source = "; ".join(parts) or "computed by the job (reads no column)"
            lines.append(f"| `{c.table or c.target}.{c.column}` | {source} | "
                         f"{'yes' if c.covered else '**no**'} |")
        if self.unmapped:
            lines += ["", "Columns the job reads that the model doesn't hold:", ""]
            lines += [f"- `{t}`: " + ", ".join(f"`{c}`" for c in cols) for t, cols in self.unmapped.items()]
        return "\n".join(lines) + "\n"


def _same_source(entity_source: str | None, job_source: str, table: str, model_dir: Path | None,
                 job_dir: Path | None) -> bool:
    if not entity_source:
        return False
    if entity_source == job_source:
        return True
    a, b = PurePosixPath(entity_source.replace("\\", "/")), PurePosixPath(job_source.replace("\\", "/"))
    if not a.suffix and not b.suffix:  # tables: match on the name without schema, any case
        return entity_source.split(".")[-1].lower() == job_source.split(".")[-1].lower()
    if a.suffix and b.suffix and model_dir is not None and job_dir is not None:
        return (model_dir / a).resolve() == (job_dir / b).resolve()
    if a.suffix and b.suffix:
        return a.name == b.name
    # A file on one side and a table on the other (orders.csv, orders): the same data by name.
    file, table_name = (a, job_source) if a.suffix else (b, entity_source)
    return file.stem.lower() == table_name.split(".")[-1].lower()


def _attribute_columns(source: str | None, name: str) -> set[str]:
    if not source:
        return {name}
    e = _parse(source)
    return {c.name for c in e.find_all(exp.Column)} if e is not None else set()


def map_job(pipeline: Pipeline, model: Model, model_dir: str | Path | None = None,
            job_dir: str | Path | None = None) -> JobMap:
    """For every column the job writes, the model attributes its source columns land in."""
    pipeline = bind(pipeline)
    graph = build_graph(pipeline)
    ev = job_evidence(pipeline)
    mdir = Path(model_dir) if model_dir is not None else None
    jdir = Path(job_dir) if job_dir is not None else None
    lands: dict[tuple[str, str], list[str]] = {}
    out = JobMap()
    for table, source in ev.sources.items():
        entities = [e for e in model.entities if _same_source(e.source, source, table, mdir, jdir)]
        out.tables[table] = [e.id for e in entities]
        for e in entities:
            for a in e.attributes:
                if a.role == "surrogate_key" and not a.source:
                    continue
                for col in _attribute_columns(a.source, a.name):
                    if col in ev.tables[table]:
                        lands.setdefault((table, col), []).append(f"{e.id}.{a.name}")
    src_table = {}
    fed = {e.dst.node for e in graph.edges}
    for n in pipeline.nodes:
        if n.type == "source" and n.id not in fed:
            src_table[n.id] = _table_of(n)[0]
    for n in pipeline.nodes:
        if n.type != "target":
            continue
        for c in graph.columns.get(n.id, []):
            inputs = []
            for src, (typ, _sub) in sorted(trace_to_sources(graph, Ref(n.id, c)).items()):
                table = src_table.get(src.node)
                if table is None:
                    continue
                inputs.append({"table": table, "column": src.column, "kind": "direct" if typ == DIRECT else "indirect",
                               "attributes": sorted(set(lands.get((table, src.column), [])))})
            out.columns.append(ColumnMap(n.id, n.params.get("table") or n.params.get("path"), c, inputs))
    for table, cols in ev.used.items():
        missing = sorted(c for c in cols if not lands.get((table, c)))
        if missing:
            out.unmapped[table] = missing
    return out
