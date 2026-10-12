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
from redcliff.spec import Pipeline, validate


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
