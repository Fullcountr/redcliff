"""JSON-in, JSON-out functions the browser app calls through Pyodide.

Every function takes plain JSON-compatible values and returns a dict with ``ok`` plus either
results or ``error``. The web app never sees Python exceptions.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from redcliff.compile import compile_pyspark, compile_query, compile_script
from redcliff.importer import import_sql as _import_sql
from redcliff.lineage import Ref, blast_radius, build_graph, trace_to_sources
from redcliff.powercenter import import_powercenter as _import_powercenter
from redcliff.powercenter import mapping_names
from redcliff.spec import Pipeline, bind, validate


def _load(spec: Any) -> Pipeline:
    data = json.loads(spec) if isinstance(spec, str) else spec
    return Pipeline.from_dict(data)


def _wrap(fn):
    def inner(*args, **kwargs) -> str:
        try:
            result = fn(*args, **kwargs)
            return json.dumps({"ok": True, **result})
        except Exception as e:  # report every failure to the UI as data
            issues = getattr(e, "issues", None)
            return json.dumps({
                "ok": False,
                "error": str(e).splitlines()[0] if not issues else "the spec has problems",
                "issues": [str(i) for i in issues] if issues else [],
            })

    inner.__name__ = fn.__name__
    inner.__doc__ = fn.__doc__
    return inner


def _browser_paths(pipeline: Pipeline) -> Pipeline:
    """File sources read the file by its base name, which is how the browser registers files."""
    data = pipeline.to_dict()
    for node in data["nodes"]:
        params = node.get("params", {})
        if node["type"] in ("source", "target") and "path" in params:
            params["path"] = PurePosixPath(params["path"]).name
    return Pipeline.from_dict(data)


@_wrap
def analyze(spec: Any) -> dict[str, Any]:
    """Everything the canvas needs at once: issues, columns per node and the lineage graph."""
    pipeline = _load(spec)
    structural = validate(pipeline)
    if structural:
        return {"valid": False, "issues": [_issue(i) for i in structural], "columns": {}, "edges": [],
                "dataset_edges": []}
    graph = build_graph(pipeline)
    return {
        "valid": True,
        "issues": [_issue(i) for i in graph.issues],
        "columns": graph.columns,
        "edges": [{"from": str(e.src), "to": str(e.dst), "type": e.type, "subtype": e.subtype,
                   "explicit": e.explicit} for e in graph.edges],
        "dataset_edges": [{"from": str(d.src), "node": d.node, "subtype": d.subtype}
                          for d in graph.dataset_edges],
        "order": [n.id for n in pipeline.topo_order()],
    }


def _issue(issue) -> dict[str, Any]:
    return {"node": issue.node_id, "message": issue.message}


@_wrap
def preview_query(spec: Any, node_id: str) -> dict[str, Any]:
    """DuckDB SQL returning one node's rows, with file sources read by base name."""
    pipeline = _browser_paths(_load(spec))
    node = pipeline.node(node_id)
    if node.type == "target":
        node_id = node.inputs[0]
    return {"sql": compile_query(pipeline, node_id, "duckdb"), "node": node_id}


@_wrap
def trace_query(spec: Any, node_id: str) -> dict[str, Any]:
    """DuckDB SQL for a step's rows, each with the source row numbers behind it."""
    from redcliff.trace import trace_query as _trace_query

    return _trace_query(_browser_paths(_load(spec)), node_id).to_dict()


@_wrap
def source_rows(spec: Any, source_id: str, rows: Any) -> dict[str, Any]:
    """DuckDB SQL for some rows of a source, by row number."""
    from redcliff.trace import source_rows_query

    ids = json.loads(rows) if isinstance(rows, str) else list(rows)
    return {"sql": source_rows_query(_browser_paths(_load(spec)), source_id, ids)}


@_wrap
def checks(spec: Any, node_id: str | None = None) -> dict[str, Any]:
    """The step's expectations (or all of them), each with DuckDB SQL returning its failing rows."""
    from redcliff.expect import check_queries

    out = []
    # Pyodide passes JavaScript null as a falsy jsnull, not None.
    for e, sql in check_queries(_browser_paths(_load(spec)), node_id or None):
        out.append({"node": e.node, "name": e.name, "kind": e.kind, "severity": e.severity,
                    "describe": e.describe(), "sql": sql})
    return {"checks": out}


@_wrap
def code(spec: Any, target: str, node_id: str | None = None) -> dict[str, Any]:
    """Generated code for the whole pipeline (or one node) in duckdb, spark or pyspark; or a whole
    project for dbt / lakeflow, as {"files": {path: content}}."""
    pipeline = _load(spec)
    if target in ("dbt", "lakeflow"):
        from redcliff.dbt import compile_dbt
        from redcliff.lakeflow import compile_lakeflow

        files = compile_dbt(pipeline, "databricks") if target == "dbt" else compile_lakeflow(pipeline)
        return {"code": "", "files": files}
    if target == "pyspark":
        return {"code": compile_pyspark(pipeline)}
    if node_id and pipeline.node(node_id).type != "target":
        return {"code": compile_query(pipeline, node_id, target) + "\n"}
    if not any(n.type == "target" for n in pipeline.nodes):
        last = pipeline.topo_order()[-1].id
        return {"code": compile_query(pipeline, last, target) + "\n"}
    return {"code": compile_script(pipeline, target)}


@_wrap
def import_sql(sql: str, dialect: str | None = None, schema: Any = None, name: str = "imported") -> dict[str, Any]:
    """Legacy SQL in, spec out, with warnings for anything kept as raw SQL."""
    schema_map = json.loads(schema) if isinstance(schema, str) else schema
    result = _import_sql(sql, dialect=dialect or None, name=name, schema=schema_map or None)
    return {"spec": result.pipeline.to_dict(), "warnings": result.warnings, "outputs": result.outputs,
            "scorecard": result.scorecard.to_dict()}


@_wrap
def import_powercenter(xml: str, mapping: Any = None, params: str | None = None) -> dict[str, Any]:
    """A PowerCenter export (and optionally its parameter file) in, spec and migration scorecard out."""
    result = _import_powercenter(xml, mapping=mapping or None, params=params or None)
    return {"spec": result.pipeline.to_dict(), "warnings": result.warnings, "outputs": result.outputs,
            "scorecard": result.scorecard.to_dict(), "mappings": mapping_names(xml)}


@_wrap
def import_ssis(xml: str) -> dict[str, Any]:
    """An SSIS package (.dtsx) in, spec and migration scorecard out."""
    from redcliff.ssis import import_ssis as _import_ssis

    result = _import_ssis(xml)
    return {"spec": result.pipeline.to_dict(), "warnings": result.warnings, "outputs": result.outputs,
            "scorecard": result.scorecard.to_dict()}


@_wrap
def import_talend(xml: str, name: str | None = None) -> dict[str, Any]:
    """A Talend job (.item) in, spec and migration scorecard out."""
    from redcliff.talend import import_talend as _import_talend

    result = _import_talend(xml, name=name or None)
    return {"spec": result.pipeline.to_dict(), "warnings": result.warnings, "outputs": result.outputs,
            "scorecard": result.scorecard.to_dict()}


@_wrap
def import_datastage(text: str) -> dict[str, Any]:
    """A DataStage export (.dsx or XML) in, spec and migration scorecard out."""
    from redcliff.datastage import import_datastage as _import_datastage

    result = _import_datastage(text)
    return {"spec": result.pipeline.to_dict(), "warnings": result.warnings, "outputs": result.outputs,
            "scorecard": result.scorecard.to_dict()}


@_wrap
def impact(spec: Any, node_id: str, column: str) -> dict[str, Any]:
    """Blast radius of one column."""
    graph = build_graph(_load(spec))
    return blast_radius(graph, Ref(node_id, column)).to_dict()


@_wrap
def trace(spec: Any, node_id: str, column: str) -> dict[str, Any]:
    """Source columns feeding one column, with how they are used."""
    graph = build_graph(_load(spec))
    traced = trace_to_sources(graph, Ref(node_id, column))
    return {"sources": [{"column": str(r), "type": t, "subtype": s} for r, (t, s) in sorted(traced.items())]}


# --------------------------------------------------------------------------- data models


def _model(model: Any):
    from redcliff.model import Model

    return Model.from_dict(json.loads(model) if isinstance(model, str) else model)


@_wrap
def model_analyze(model: Any) -> dict[str, Any]:
    """A model's problems, its normalized form, and each entity's key and outgoing relationships."""
    from redcliff.model import validate_model

    m = _model(model)
    return {
        "valid": not (issues := validate_model(m)),
        "issues": [_issue(i) for i in issues],
        "model": m.to_dict(),
        "keys": {e.id: e.key() for e in m.entities},
        "scd2": [e.id for e in m.entities if e.scd2],
    }


@_wrap
def model_export(model: Any, fmt: str, dialect: str | None = None) -> dict[str, Any]:
    """DDL (in a dialect), DBML, Mermaid or a dbt schema.yml for a valid model."""
    from redcliff.model import EXPORTS, validate_model
    from redcliff.spec import SpecError

    m = _model(model)
    issues = validate_model(m)
    if issues:
        raise SpecError(issues)
    fn = EXPORTS[fmt]
    return {"code": fn(m, dialect or "duckdb") if fmt == "ddl" else fn(m)}


@_wrap
def model_import(text: str, fmt: str, dialect: str | None = None, name: str = "imported") -> dict[str, Any]:
    """A model from CREATE TABLE statements (fmt ``ddl``) or DBML (fmt ``dbml``)."""
    from redcliff.model import from_dbml, from_ddl

    result = from_dbml(text, name) if fmt == "dbml" else from_ddl(text, dialect or None, name)
    return {"model": result.model.to_dict(), "warnings": result.warnings}


@_wrap
def model_profile_queries(tables: Any) -> dict[str, Any]:
    """Profiling SQL for each browser table: {table: {column: type}} -> {table: sql}."""
    from redcliff.propose import profile_query

    tables = json.loads(tables) if isinstance(tables, str) else tables
    return {"queries": {t: profile_query(f'"{t}"', list(cols)) for t, cols in tables.items()}}


def _profiles(tables: Any, rows: Any, files: Any):
    from redcliff.propose import read_profile

    tables = json.loads(tables) if isinstance(tables, str) else tables
    rows = json.loads(rows) if isinstance(rows, str) else rows
    files = json.loads(files) if isinstance(files, str) else (files or {})
    return [read_profile(t, files.get(t, f"{t}.csv"), cols, rows[t]) for t, cols in tables.items() if t in rows]


@_wrap
def model_reference_queries(tables: Any, rows: Any, files: Any = None) -> dict[str, Any]:
    """The foreign keys worth testing, each with the SQL that counts its missing values."""
    from redcliff.propose import candidate_references, inclusion_query

    refs = candidate_references(_profiles(tables, rows, files))
    return {"checks": [{"ref": list(r), "sql": inclusion_query(f'"{r[0]}"', r[1], f'"{r[2]}"', r[3])} for r in refs]}


@_wrap
def model_propose(tables: Any, rows: Any, held: Any, name: str = "proposed", files: Any = None) -> dict[str, Any]:
    """A Kimball model from the profiles and the foreign keys that held, with the reason for each choice."""
    from redcliff.propose import propose_model

    held = json.loads(held) if isinstance(held, str) else held
    proposal = propose_model(_profiles(tables, rows, files), [tuple(r) for r in held], name=name)
    return proposal.to_dict()


@_wrap
def model_from_job(spec: Any, style: str = "kimball") -> dict[str, Any]:
    """A model proposed from what a legacy job does (joins, lookups, merges, sums), with the step
    behind every reason, and how much of what the job writes it covers."""
    from redcliff.jobmodel import map_job, propose_from_job
    from redcliff.propose import to_data_vault

    pipeline = _load(spec)
    proposal, _ev = propose_from_job(pipeline)
    if style == "data_vault":
        proposal = to_data_vault(proposal)
    return {**proposal.to_dict(), "map": map_job(pipeline, proposal.model).to_dict()}


@_wrap
def model_map(model: Any, spec: Any) -> dict[str, Any]:
    """For every column a job writes, the model attributes its source columns land in."""
    from redcliff.jobmodel import map_job

    return map_job(_load(spec), _model(model)).to_dict()


@_wrap
def model_load_pipeline(model: Any) -> dict[str, Any]:
    """The pipeline that loads a model (Kimball: SCD2 dimensions and point-in-time facts; Data Vault:
    hubs, links, satellites and current-state marts)."""
    from redcliff.generate import model_pipeline

    pipeline = model_pipeline(_model(model))
    return {"spec": pipeline.to_dict(), "targets": [n.id for n in pipeline.nodes if n.type == "target"]}


@_wrap
def diff(base: Any, head: Any) -> dict[str, Any]:
    """What a change does (the pull request bot's report) plus, per target in both versions, the DuckDB
    queries the browser runs to count rows before and after, and the rows added and removed."""
    from redcliff.columns import infer_columns
    from redcliff.diff import diff_pipelines

    b, h = _browser_paths(_load(base)), _browser_paths(_load(head))
    d = diff_pipelines(b, h)
    queries = []
    if not d.issues:
        b_cols, h_cols = infer_columns(bind(b)), infer_columns(bind(h))
        b_targets = {n.id: n for n in b.nodes if n.type == "target"}
        for t in (n for n in h.nodes if n.type == "target"):
            if t.id not in b_targets:
                continue
            try:
                queries.append({
                    "target": t.id,
                    "base": compile_query(b, b_targets[t.id].inputs[0], "duckdb"),
                    "head": compile_query(h, t.inputs[0], "duckdb"),
                    "comparable": b_cols.get(b_targets[t.id].inputs[0]) == h_cols.get(t.inputs[0]) is not None,
                })
            except Exception:  # noqa: BLE001 - a target that doesn't compile simply isn't compared
                continue
    return {"summary": d.summary(), "empty": d.empty, **d.to_dict(), "queries": queries}
