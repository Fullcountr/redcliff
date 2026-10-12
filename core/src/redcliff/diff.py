"""What a change to a pipeline does: steps, columns, lineage and, optionally, data.

    diff = diff_pipelines(base, head)
    diff.data = data_diff(base, head, con, base_dir, base_dir)   # optional, needs DuckDB
    print(diff.to_markdown())

A text diff of two specs shows which lines moved. This shows what they mean: which steps changed
and how, which columns appear or disappear, which target columns now come from somewhere else,
and how many rows each target gains or loses on sample data. It powers ``redcliff diff``, the pull
request bot and the MCP server's edit proposals.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from redcliff.columns import infer_columns
from redcliff.lineage import Ref, build_graph, trace_to_sources
from redcliff.spec import Pipeline, validate

SAMPLE_ROWS = 5


@dataclass
class StepChange:
    id: str
    type: str
    change: str  # added | removed | changed
    details: list[str] = field(default_factory=list)


@dataclass
class ColumnChange:
    step: str
    added: list[str]
    removed: list[str]


@dataclass
class LineageChange:
    target: str
    column: str
    before: list[str]  # "orders.amount" or "orders.status (shapes)"; empty when the column is new
    after: list[str]   # empty when the column is gone
    change: str        # added | removed | changed


@dataclass
class DataChange:
    target: str
    rows_before: int | None
    rows_after: int | None
    added: int | None = None    # rows in head, not in base (None: not comparable)
    removed: int | None = None
    columns: list[str] = field(default_factory=list)
    added_sample: list[list[Any]] = field(default_factory=list)
    removed_sample: list[list[Any]] = field(default_factory=list)
    note: str = ""


@dataclass
class PipelineDiff:
    name: str
    steps: list[StepChange] = field(default_factory=list)
    columns: list[ColumnChange] = field(default_factory=list)
    lineage: list[LineageChange] = field(default_factory=list)
    affected_targets: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)  # "name: 'a' → 'b'", "name 'a' added", ...
    issues: list[str] = field(default_factory=list)  # problems in the head spec
    data: list[DataChange] | None = None

    @property
    def empty(self) -> bool:
        return not (self.steps or self.columns or self.lineage or self.issues or self.variables
                    or any(d.added or d.removed or d.rows_before != d.rows_after or d.note for d in self.data or []))

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), default=str))

    def summary(self) -> str:
        parts = []
        for kind in ("changed", "added", "removed"):
            n = sum(s.change == kind for s in self.steps)
            if n:
                parts.append(f"{n} step{'s' if n != 1 else ''} {kind}")
        if self.variables:
            n = len(self.variables)
            parts.append(f"{n} variable{'s' if n != 1 else ''} changed")
        if self.lineage:
            n = len(self.lineage)
            parts.append(f"{n} target column{'s' if n != 1 else ''} with new lineage")
        for d in self.data or []:
            if d.rows_before != d.rows_after:
                parts.append(f"{d.target}: {_n(d.rows_before)} → {_n(d.rows_after)} rows")
            elif d.added or d.removed:
                parts.append(f"{d.target}: {d.added + d.removed} rows differ")
        if self.issues:
            parts.append(f"{len(self.issues)} issue{'s' if len(self.issues) != 1 else ''}")
        return ", ".join(parts) or "no change to what the pipeline does"

    def to_markdown(self, title: str | None = None, level: int = 2) -> str:
        h = "#" * level
        out = [f"{h} {title or f'Pipeline `{self.name}`'}", "", f"**{self.summary()}**", ""]
        if self.issues:
            out += [f"{h}# Issues in the new spec", ""] + [f"- {i}" for i in self.issues] + [""]
        if self.variables:
            out += [f"{h}# Variables", ""] + [f"- {_cell(v)}" for v in self.variables] + [""]
        if self.steps:
            out += [f"{h}# Steps", "", "| Step | Change |", "| --- | --- |"]
            for s in self.steps:
                what = "<br>".join(_cell(d) for d in s.details) if s.details else s.change
                out.append(f"| `{s.id}` ({s.type}) | **{s.change}**{': ' + what if s.details else ''} |")
            out.append("")
        if self.affected_targets:
            out += [f"Targets downstream of a change: {', '.join(f'`{t}`' for t in self.affected_targets)}", ""]
        if self.columns:
            out += [f"{h}# Columns", "", "| Step | Added | Removed |", "| --- | --- | --- |"]
            for c in self.columns:
                out.append(f"| `{c.step}` | {_codes(c.added)} | {_codes(c.removed)} |")
            out.append("")
        if self.lineage:
            out += [f"{h}# Lineage of target columns", "", "| Column | Before | After |", "| --- | --- | --- |"]
            for ch in self.lineage:
                out.append(f"| `{ch.target}.{ch.column}` | {_codes(ch.before) if ch.change != 'added' else '*new*'} | "
                           f"{_codes(ch.after) if ch.change != 'removed' else '*gone*'} |")
            out.append("")
        if self.data:
            out += [f"{h}# Data (sample data, DuckDB)", "", "| Target | Rows before | Rows after | Added | Removed |",
                    "| --- | ---: | ---: | ---: | ---: |"]
            for d in self.data:
                out.append(f"| `{d.target}` | {_n(d.rows_before)} | {_n(d.rows_after)} | {_n(d.added)} | {_n(d.removed)} |")
            out.append("")
            for d in self.data:
                if d.note:
                    out.append(f"- `{d.target}`: {d.note}")
                for label, rows in (("added", d.added_sample), ("removed", d.removed_sample)):
                    if rows:
                        summary = f"<details><summary><code>{d.target}</code>: rows {label}</summary>"
                        out += ([""] if out[-1] else []) + [summary, "",
                                "| " + " | ".join(d.columns) + " |", "|" + " --- |" * len(d.columns)]
                        out += ["| " + " | ".join(_cell(_fmt(v)) for v in r) + " |" for r in rows]
                        out += ["", "</details>"]
            out.append("")
        return "\n".join(out).rstrip() + "\n"


def _n(v) -> str:
    return "–" if v is None else f"{v:,}"


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _codes(items: list[str]) -> str:
    return ", ".join(f"`{_cell(i)}`" for i in items) if items else "–"


def _fmt(v) -> str:
    return "null" if v is None else str(v)


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return f"`{text if len(text) <= 120 else text[:117] + '...'}`"


def _param_changes(before: dict, after: dict, prefix: str = "") -> list[str]:
    out = []
    for key in list(before) + [k for k in after if k not in before]:
        name = f"{prefix}{key}"
        if key not in after:
            out.append(f"{name} removed (was {_short(before[key])})")
        elif key not in before:
            out.append(f"{name} {_short(after[key])} added")
        elif before[key] != after[key]:
            if isinstance(before[key], dict) and isinstance(after[key], dict):
                out += _param_changes(before[key], after[key], f"{name}.")
            else:
                out.append(f"{name}: {_short(before[key])} → {_short(after[key])}")
    return out


def _sources(graph, node_id: str, column: str) -> list[str]:
    traced = trace_to_sources(graph, Ref(node_id, column))
    direct = sorted(str(r) for r, (t, _s) in traced.items() if t == "DIRECT")
    shaping = sorted(f"{r} (shapes)" for r, (t, _s) in traced.items() if t == "INDIRECT")
    return direct + shaping


def _target_columns(pipeline: Pipeline, graph) -> dict[tuple[str, str], list[str]]:
    out = {}
    for n in pipeline.nodes:
        if n.type == "target" and n.inputs:
            for c in graph.columns.get(n.inputs[0]) or []:
                out[(n.id, c)] = _sources(graph, n.inputs[0], c)
    return out


def diff_pipelines(base: Pipeline, head: Pipeline) -> PipelineDiff:
    """Structural, column and lineage changes from ``base`` to ``head``."""
    diff = PipelineDiff(name=head.name or base.name)
    diff.issues = [str(i) for i in validate(head)]
    if isinstance(base.variables, dict) and isinstance(head.variables, dict):
        diff.variables = _param_changes(base.variables, head.variables)
    before = {n.id: n for n in base.nodes}
    after = {n.id: n for n in head.nodes}
    changed: list[str] = []
    for nid, n in after.items():
        if nid not in before:
            details = [f"reads {', '.join(n.inputs)}"] if n.inputs else []
            details += [f"{k} {_short(v)}" for k, v in n.params.items()]
            diff.steps.append(StepChange(nid, n.type, "added", details))
            changed.append(nid)
            continue
        old = before[nid]
        details = []
        if old.type != n.type:
            details.append(f"type: {old.type} → {n.type}")
        if old.inputs != n.inputs:
            details.append(f"inputs: {', '.join(old.inputs) or '–'} → {', '.join(n.inputs) or '–'}")
        details += _param_changes(old.params, n.params)
        if details:
            diff.steps.append(StepChange(nid, n.type, "changed", details))
            changed.append(nid)
    for nid, n in before.items():
        if nid not in after:
            diff.steps.append(StepChange(nid, n.type, "removed"))
            changed += [d.id for d in head.nodes if nid in d.inputs]
    if diff.issues:
        return diff  # columns and lineage need a valid spec

    cols_before, cols_after = infer_columns(base), infer_columns(head)
    for nid in after:
        if nid in before and after[nid].type != "target":
            a, b = cols_before.get(nid), cols_after.get(nid)
            if a is not None and b is not None and a != b:
                added, removed = [c for c in b if c not in a], [c for c in a if c not in b]
                if added or removed:
                    diff.columns.append(ColumnChange(nid, added, removed))

    g_before, g_after = build_graph(base), build_graph(head)
    old_issues = {str(i) for i in g_before.issues}
    diff.issues += [str(i) for i in g_after.issues if str(i) not in old_issues]
    lin_before, lin_after = _target_columns(base, g_before), _target_columns(head, g_after)
    for key in list(lin_after) + [k for k in lin_before if k not in lin_after]:
        if key[0] not in after or after[key[0]].type != "target":
            continue  # a removed target is already listed as a removed step
        a, b = lin_before.get(key), lin_after.get(key)
        if a == b:
            continue
        kind = "added" if a is None else "removed" if b is None else "changed"
        diff.lineage.append(LineageChange(key[0], key[1], a or [], b or [], kind))

    downstream: set[str] = set()
    stack = [c for c in changed if c in after]
    while stack:
        nid = stack.pop()
        if nid in downstream:
            continue
        downstream.add(nid)
        stack += head.downstream(nid)
    diff.affected_targets = [n.id for n in head.nodes if n.type == "target" and n.id in downstream]
    return diff


def data_diff(base: Pipeline, head: Pipeline, con, base_dir=None, head_dir=None,
              sample: int = SAMPLE_ROWS) -> list[DataChange]:
    """Run every target's query on both sides in DuckDB and compare the rows."""
    from redcliff.compile import compile_query

    out = []
    targets = [n for n in head.nodes if n.type == "target"]
    base_ids = {n.id for n in base.nodes if n.type == "target"}
    for t in targets:
        d = DataChange(t.id, None, None)
        try:
            head_sql = compile_query(head, t.inputs[0], "duckdb", head_dir)
            d.rows_after = con.sql(f"SELECT COUNT(*) FROM ({head_sql})").fetchone()[0]
        except Exception as e:  # noqa: BLE001 - report, don't fail the whole diff
            d.note = f"the new pipeline doesn't run: {str(e).splitlines()[0]}"
            out.append(d)
            continue
        if t.id not in base_ids:
            d.note = "new target"
            out.append(d)
            continue
        try:
            base_sql = compile_query(base, base.node(t.id).inputs[0], "duckdb", base_dir)
            d.rows_before = con.sql(f"SELECT COUNT(*) FROM ({base_sql})").fetchone()[0]
        except Exception as e:  # noqa: BLE001
            d.note = f"the old pipeline doesn't run here: {str(e).splitlines()[0]}"
            out.append(d)
            continue
        cols_head = con.sql(head_sql).columns
        cols_base = con.sql(base_sql).columns
        common = [c for c in cols_head if c in cols_base]
        if not common:
            d.note = "no column is in both versions, so rows aren't compared"
            out.append(d)
            continue
        if common != cols_head or common != cols_base:
            d.note = f"compared on the {len(common)} columns both versions have"
            pick = ", ".join(f'"{c}"' for c in common)
            head_sql, base_sql = f"SELECT {pick} FROM ({head_sql})", f"SELECT {pick} FROM ({base_sql})"
        d.columns = common
        added = f"SELECT * FROM ({head_sql}) EXCEPT ALL SELECT * FROM ({base_sql})"
        removed = f"SELECT * FROM ({base_sql}) EXCEPT ALL SELECT * FROM ({head_sql})"
        try:
            d.added = con.sql(f"SELECT COUNT(*) FROM ({added})").fetchone()[0]
            d.removed = con.sql(f"SELECT COUNT(*) FROM ({removed})").fetchone()[0]
            d.added_sample = [list(r) for r in con.sql(f"{added} LIMIT {sample}").fetchall()] if d.added else []
            d.removed_sample = [list(r) for r in con.sql(f"{removed} LIMIT {sample}").fetchall()] if d.removed else []
        except Exception as e:  # noqa: BLE001 - e.g. a column changed type
            d.note = f"rows not compared: {str(e).splitlines()[0]}"
        out.append(d)
    return out


# --------------------------------------------------------------------------- data models


@dataclass
class ModelDiff:
    """What a change to a data model does: entities, attributes and relationships."""

    name: str
    entities: list[tuple[str, str]] = field(default_factory=list)  # (entity, change)
    attributes: list[tuple[str, str, str]] = field(default_factory=list)  # (entity, attribute, change)
    relationships: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)  # problems in the new model
    data: None = None  # a model has no rows to compare; keeps the same shape as PipelineDiff

    @property
    def empty(self) -> bool:
        return not (self.entities or self.attributes or self.relationships or self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "model", **asdict(self)}

    def summary(self) -> str:
        parts = []
        for label, n in (("entity change", len(self.entities)), ("attribute change", len(self.attributes)),
                         ("relationship change", len(self.relationships)), ("issue", len(self.issues))):
            if n:
                parts.append(f"{n} {label}{'s' if n != 1 else ''}")
        return ", ".join(parts) or "no changes"

    def to_markdown(self, title: str | None = None, level: int = 2) -> str:
        h = "#" * level
        out = [f"{h} {title or self.name} (data model)", "", f"**{self.summary()}**", ""]
        if self.entities:
            out += ["| Entity | Change |", "| --- | --- |"] + [f"| `{e}` | {_cell(c)} |" for e, c in self.entities] + [""]
        if self.attributes:
            out += ["| Attribute | Change |", "| --- | --- |"]
            out += [f"| `{e}.{a}` | {_cell(c)} |" for e, a, c in self.attributes] + [""]
        if self.relationships:
            out += [f"{h}# Relationships", ""] + [f"- {r}" for r in self.relationships] + [""]
        if self.issues:
            out += [f"{h}# Issues in the new model", ""] + [f"- {i}" for i in self.issues] + [""]
        if self.empty:
            out += ["No changes.", ""]
        return "\n".join(out).rstrip() + "\n"


def diff_models(base, head) -> ModelDiff:
    """Compare two :class:`redcliff.model.Model`. ``base`` may be None (a new model)."""
    from redcliff.model import Model, validate_model

    base = base or Model(name=head.name, entities=[])
    d = ModelDiff(head.name, issues=[str(i) for i in validate_model(head)])
    before = {e.id: e for e in base.entities}
    after = {e.id: e for e in head.entities}
    for eid in after.keys() - before.keys():
        e = after[eid]
        d.entities.append((eid, f"**added**: {e.kind}, {len(e.attributes)} attributes"))
    for eid in before.keys() - after.keys():
        d.entities.append((eid, f"**removed** ({before[eid].kind})"))
    for eid in [e.id for e in head.entities if e.id in before]:
        old, new = before[eid].to_dict(), after[eid].to_dict()
        changes = [f"{k}: {_short(old.get(k))} → {_short(new.get(k))}" for k in ("kind", "grain", "history", "source")
                   if old.get(k) != new.get(k)]
        if changes:
            d.entities.append((eid, "**changed**: " + "; ".join(changes)))
        oa = {a.name: a.to_dict() for a in before[eid].attributes}
        na = {a.name: a.to_dict() for a in after[eid].attributes}
        for name in [a.name for a in after[eid].attributes]:
            if name not in oa:
                d.attributes.append((eid, name, f"**added**: {na[name]['type']}"
                                     + (f", {na[name]['role'].replace('_', ' ')}" if na[name].get("role") else "")))
            elif oa[name] != na[name]:
                keys = ("type", "role", "history", "track", "source", "description")
                d.attributes.append((eid, name, "**changed**: " + "; ".join(
                    f"{k}: {_short(oa[name].get(k))} → {_short(na[name].get(k))}" for k in keys
                    if oa[name].get(k) != na[name].get(k))))
        for name in oa.keys() - na.keys():
            d.attributes.append((eid, name, "**removed**"))
    old_r = {(r.source, r.target) for r in base.relationships}
    new_r = {(r.source, r.target) for r in head.relationships}
    d.relationships += [f"**added**: `{s}` → `{t}`" for s, t in sorted(new_r - old_r)]
    d.relationships += [f"**removed**: `{s}` → `{t}`" for s, t in sorted(old_r - new_r)]
    return d
