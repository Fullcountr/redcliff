"""An MCP server, so AI agents can read, explain and edit pipelines, with a diff you accept first.

    redcliff mcp --root path/to/repo

It speaks the Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0) with nothing but
the standard library. Tools read specs under ``--root``: describe a pipeline, trace a column's
lineage, show a change's blast radius, compile code, preview and trace rows. Edits are two steps:
``propose_edit`` validates the change and returns what it does (steps, columns, lineage, a text
diff) without touching the file; ``apply_edit`` writes it once the person has accepted. A proposal
goes stale if the file changes in between.
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any
from collections.abc import Callable

from redcliff import __version__
from redcliff.compile import compile_pyspark, compile_query, compile_script
from redcliff.diff import data_diff, diff_pipelines
from redcliff.lineage import Ref, blast_radius, build_graph, trace_to_sources
from redcliff.spec import Pipeline, SpecError, dump_spec, edit_spec_text, validate

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
VERSION = __version__
INSTRUCTIONS = """Redcliff pipelines are YAML specs: a list of steps (source, filter, select, derive,
join, aggregate, union, sql, target) whose expressions are SQL. The spec is the source of truth;
code and lineage are generated from it, so edit specs, never generated code.
Start with list_pipelines and describe_pipeline. Before changing a step, check impact on the
columns it touches. To change a spec, call propose_edit, show the person the summary and the
diff it returns, and call apply_edit only after they accept.
Data models (model.yaml: entities, keys, relationships) are the target the loads produce:
propose_model profiles data files (or reads a legacy job) and proposes one with reasons,
map_job shows how much of a legacy job a model covers, generate_load proposes the pipeline
that loads a model, describe_model reads one. Proposals go through apply_edit."""


class ToolError(Exception):
    """A tool failed in a way the agent should read and act on."""


# --------------------------------------------------------------------------- helpers


def _ref(text: str) -> Ref:
    node, _, column = (text or "").partition(".")
    if not node or not column:
        raise ToolError(f"expected STEP.COLUMN, got {text!r}")
    return Ref(node, column)


def _rows(rel, limit: int) -> dict[str, Any]:
    rows = rel.fetchmany(limit)
    return {"columns": rel.columns, "rows": [[None if v is None else v if isinstance(v, (int, float, bool, str))
                                              else str(v) for v in r] for r in rows]}


def _summary(step: dict) -> str:
    p = step.get("params") or {}
    t = step.get("type")
    if t == "source":
        return f"reads {p.get('table') or p.get('path')} ({p.get('format')})"
    if t == "target":
        where = p.get("table") or p.get("path")
        return f"writes {where} ({p.get('format')}, {p.get('mode', 'overwrite')})"
    if t == "filter":
        return f"keeps rows where {p.get('condition')}"
    if t == "select":
        return f"keeps {', '.join(p.get('columns') or [])}"
    if t == "derive":
        return "; ".join(f"{c} = {e}" for c, e in (p.get("columns") or {}).items())
    if t == "join":
        return f"{p.get('how', 'inner')} join on {p.get('condition')}"
    if t == "aggregate":
        by = ", ".join(p.get("group_by") or []) or "everything"
        return f"by {by}: " + "; ".join(f"{c} = {e}" for c, e in (p.get("measures") or {}).items())
    if t == "union":
        return "union" + (" (distinct)" if p.get("distinct") else " all")
    if t == "sql":
        return " ".join(str(p.get("query", "")).split())[:200]
    return ""


def _merge(params: dict, changes: dict) -> dict:
    out = dict(params)
    for k, v in changes.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_operations(data: dict, operations: list[dict]) -> dict:
    """Apply edit operations to a spec dict (a copy). See the propose_edit tool for the format."""
    data = copy.deepcopy(data)
    nodes: list[dict] = data.setdefault("nodes", [])

    def find(nid: str) -> dict:
        for n in nodes:
            if n.get("id") == nid:
                return n
        raise ToolError(f"no step named {nid!r}; steps are {', '.join(n.get('id', '?') for n in nodes)}")

    for i, op in enumerate(operations or []):
        kind = op.get("op")
        if kind == "add_step":
            step = op.get("step")
            if not isinstance(step, dict) or not step.get("id"):
                raise ToolError(f"operation {i}: add_step needs a step with an id")
            if any(n.get("id") == step["id"] for n in nodes):
                raise ToolError(f"operation {i}: a step named {step['id']!r} already exists")
            at = len(nodes)
            if op.get("after"):
                at = nodes.index(find(op["after"])) + 1
            nodes.insert(at, step)
            for nid in op.get("feeds") or []:  # rewire these steps to read the new one
                target = find(nid)
                target["inputs"] = [step["id"] if x in (step.get("inputs") or []) else x for x in target.get("inputs", [])]
        elif kind == "update_step":
            n = find(op.get("id", ""))
            if "type" in op:
                n["type"] = op["type"]
            if "inputs" in op:
                n["inputs"] = list(op["inputs"])
            if "params" in op:
                n["params"] = _merge(n.get("params") or {}, op["params"] or {})
        elif kind == "remove_step":
            n = find(op.get("id", ""))
            nodes.remove(n)
            if op.get("rewire_to"):
                for other in nodes:
                    other["inputs"] = [op["rewire_to"] if x == n["id"] else x for x in other.get("inputs", [])]
        elif kind == "rename_step":
            n = find(op.get("id", ""))
            new = op.get("new_id")
            if not new:
                raise ToolError(f"operation {i}: rename_step needs new_id")
            n["id"] = new
            for other in nodes:
                other["inputs"] = [new if x == op["id"] else x for x in other.get("inputs", [])]
        elif kind == "set_description":
            data["description"] = op.get("description", "")
        elif kind == "set_expectations":
            n = find(op.get("id", ""))
            if op.get("expect"):
                n["expect"] = op["expect"]
            else:
                n.pop("expect", None)
        else:
            raise ToolError(f"operation {i}: unknown op {kind!r}; use add_step, update_step, remove_step, "
                            "rename_step, set_description or set_expectations")
    return data


# --------------------------------------------------------------------------- the server


class Server:
    def __init__(self, root: str | Path = ".", db: str | None = None):
        self.root = Path(root).resolve()
        self.db = db  # a DuckDB file holding table sources, for previews, traces and data diffs
        self.proposals: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, tuple[dict, Callable[..., Any]]] = {}
        self._register()

    # ------------------------------------------------------------------ files

    def path(self, spec: str) -> Path:
        p = (self.root / spec).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"{spec} is outside the workspace {self.root}")
        return p

    def load(self, spec: str) -> tuple[Path, dict]:
        p = self.path(spec)
        if not p.exists():
            raise ToolError(f"no such spec: {spec}")
        text = p.read_text(encoding="utf-8")
        data = json.loads(text) if text.lstrip().startswith("{") else _yaml().safe_load(text)
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            raise ToolError(f"{spec} isn't a pipeline spec (no list of nodes)")
        return p, data

    def pipeline(self, spec: str, valid: bool = True) -> tuple[Path, Pipeline]:
        p, data = self.load(spec)
        pipeline = Pipeline.from_dict(data)
        if valid:
            issues = validate(pipeline)
            if issues:
                raise ToolError("the spec has problems:\n" + "\n".join(f"- {i}" for i in issues))
        from redcliff.confine import OutsideRoot, check_paths

        try:
            check_paths(pipeline, p.parent, self.root)
        except OutsideRoot as e:
            raise ToolError(f"{spec}: {e}; specs here may only use files in the workspace") from None
        return p, pipeline

    def duck(self):
        try:
            import duckdb
        except ImportError as e:
            raise ToolError("previews need DuckDB: pip install 'redcliff[cli]'") from e
        from redcliff.confine import lock_duckdb

        con = duckdb.connect(self.db, read_only=True) if self.db else duckdb.connect()
        return lock_duckdb(con, self.root)  # spec SQL may touch only workspace files, and no network

    # ------------------------------------------------------------------ tools

    def _register(self) -> None:
        S = {"type": "string"}  # noqa: N806
        spec = {"spec": {**S, "description": "Spec path, relative to the workspace root"}}

        def tool(name, title, description, props, required, read_only=True, destructive=False):
            def deco(fn):
                self.tools[name] = ({
                    "name": name, "title": title, "description": description,
                    "inputSchema": {"type": "object", "properties": props, "required": required},
                    "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                                    "openWorldHint": False},
                }, fn)
                return fn
            return deco

        @tool("list_pipelines", "List pipelines", "Every pipeline spec under the workspace root, with its steps "
              "and targets.", {}, [])
        def list_pipelines():
            out = []
            for p in sorted(self.root.rglob("*")):
                if p.suffix not in (".yaml", ".yml", ".json") or any(part.startswith(".") or part == "node_modules"
                                                                     for part in p.relative_to(self.root).parts):
                    continue
                try:
                    _, data = self.load(str(p.relative_to(self.root)))
                except Exception:  # noqa: BLE001 - not a spec
                    continue
                nodes = data["nodes"]
                out.append({"spec": str(p.relative_to(self.root)), "name": data.get("name"), "steps": len(nodes),
                            "targets": [n.get("id") for n in nodes if n.get("type") == "target"]})
            return {"pipelines": out}

        @tool("describe_pipeline", "Describe a pipeline", "The pipeline's steps in run order: type, inputs, what "
              "each does in one line, and its output columns. Also lists problems.", spec, ["spec"])
        def describe_pipeline(spec):
            _, data = self.load(spec)
            pipeline = Pipeline.from_dict(data)
            issues = [str(i) for i in validate(pipeline)]
            graph = None if issues else build_graph(pipeline)
            steps = []
            order = pipeline.topo_order() if not issues else pipeline.nodes
            for n in order:
                steps.append({"id": n.id, "type": n.type, "inputs": n.inputs, "does": _summary(n.to_dict()),
                              "columns": graph.columns.get(n.id) if graph else None})
            issues += [str(i) for i in (graph.issues if graph else [])]
            return {"name": pipeline.name, "description": pipeline.description, "steps": steps, "issues": issues}

        col = {"column": {**S, "description": "STEP.COLUMN, e.g. revenue_by_region.revenue"}}

        @tool("column_lineage", "Column lineage", "Where a column comes from: every source column behind it, "
              "DIRECT (its value is derived from it) or INDIRECT (it filters, joins or groups the rows).",
              {**spec, **col}, ["spec", "column"])
        def column_lineage(spec, column):
            _, pipeline = self.pipeline(spec)
            traced = trace_to_sources(build_graph(pipeline), _ref(column))
            return {"column": column, "sources": [{"column": str(r), "kind": k.lower(), "how": s.lower()}
                                                  for r, (k, s) in sorted(traced.items())]}

        @tool("impact", "Blast radius", "What a change to a column touches: columns whose values change, steps whose "
              "rows change, and steps that break if it is renamed or dropped. Check this before editing a step.",
              {**spec, **col}, ["spec", "column"])
        def impact(spec, column):
            _, pipeline = self.pipeline(spec)
            return blast_radius(build_graph(pipeline), _ref(column)).to_dict()

        @tool("compile", "Generate code", "Code generated from the spec: duckdb or spark SQL, pyspark, or a whole "
              "dbt or lakeflow project as {path: content}. Pass step to get one step's SELECT (SQL only).",
              {**spec, "to": {"type": "string", "enum": ["duckdb", "spark", "pyspark", "dbt", "lakeflow"]},
               "step": S}, ["spec", "to"])
        def compile_(spec, to, step=None):
            p, pipeline = self.pipeline(spec)
            if to == "dbt":
                from redcliff.dbt import compile_dbt

                return {"files": compile_dbt(pipeline, "databricks", p.parent)}
            if to == "lakeflow":
                from redcliff.lakeflow import compile_lakeflow

                return {"files": compile_lakeflow(pipeline)}
            if to == "pyspark":
                return {"code": compile_pyspark(pipeline)}
            if to not in ("duckdb", "spark"):
                raise ToolError(f"unknown target {to!r}")
            if step:
                return {"code": compile_query(pipeline, step, to)}
            return {"code": compile_script(pipeline, to)}

        rows_props = {"limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20}}

        @tool("preview", "Preview rows", "Run the pipeline up to a step in DuckDB and return its first rows. File "
              "sources are read relative to the spec.", {**spec, "step": S, **rows_props}, ["spec", "step"])
        def preview(spec, step, limit=20):
            p, pipeline = self.pipeline(spec)
            node = pipeline.node(step)
            sql = compile_query(pipeline, node.inputs[0] if node.type == "target" else step, "duckdb", p.parent)
            return _rows(self.duck().sql(sql), limit)

        @tool("trace_rows", "Trace rows", "Row-level trace: rows of a step (optionally only those matching a SQL "
              "condition) with the source rows behind each one.",
              {**spec, "step": S, "where": {**S, "description": "SQL condition, e.g. region = 'South'"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5}}, ["spec", "step"])
        def trace_rows_(spec, step, where=None, limit=5):
            from redcliff.trace import trace_rows

            p, pipeline = self.pipeline(spec)
            rows = trace_rows(self.duck(), pipeline, step, where, p.parent)[:limit]
            return json.loads(json.dumps({"rows": rows}, default=str))

        @tool("run_checks", "Run expectations", "Run the pipeline's data quality expectations in DuckDB: each "
              "check's result, with a few failing rows. Pass step to check one step.",
              {**spec, "step": S}, ["spec"])
        def run_checks_(spec, step=None):
            from redcliff.expect import run_checks

            p, pipeline = self.pipeline(spec)
            results = [r.to_dict() for r in run_checks(self.duck(), pipeline, step, p.parent)]
            return {"checks": results, "passed": sum(r["passed"] for r in results),
                    "failed": sum(not r["passed"] and r["severity"] == "error" for r in results),
                    "warned": sum(not r["passed"] and r["severity"] == "warn" for r in results)}

        ops_schema = {
            "type": "array",
            "description": (
                "Operations, applied in order. "
                "{op: add_step, step: {id, type, inputs, params}, after?: STEP, feeds?: [STEP]} adds a step "
                "(feeds rewires those steps to read it instead of its inputs); "
                "{op: update_step, id, type?, inputs?, params?} merges params (a null value removes a key); "
                "{op: remove_step, id, rewire_to?}; {op: rename_step, id, new_id}; "
                "{op: set_description, description}; {op: set_expectations, id, expect: [...]} replaces a step's "
                "data quality checks."),
            "items": {"type": "object", "properties": {"op": {"type": "string", "enum": [
                "add_step", "update_step", "remove_step", "rename_step", "set_description", "set_expectations"]}},
                      "required": ["op"]},
        }

        @tool("propose_edit", "Propose an edit", "Validate a change to a spec and return what it does (steps, "
              "columns, lineage, rows on sample data) and a text diff, WITHOUT writing it. Show the person the "
              "summary and diff, then call apply_edit with the proposal id if they accept. Give either operations "
              "or a complete new spec. A spec path that doesn't exist yet creates a new pipeline.",
              {**spec, "operations": ops_schema,
               "new_spec": {**S, "description": "The complete new spec as YAML or JSON, instead of operations"},
               "data": {"type": "boolean", "default": True, "description": "Compare rows on sample data"}},
              ["spec"])
        def propose_edit(spec, operations=None, new_spec=None, data=True):
            p = self.path(spec)
            exists = p.exists()
            before_text = p.read_text(encoding="utf-8") if exists else ""
            if new_spec is not None:
                after = json.loads(new_spec) if new_spec.lstrip().startswith("{") else _yaml().safe_load(new_spec)
                if not isinstance(after, dict) or not isinstance(after.get("nodes"), list):
                    raise ToolError("new_spec must be a pipeline spec with a list of nodes")
            elif operations:
                base_data = self.load(spec)[1] if exists else {"version": 0, "name": p.stem, "nodes": []}
                after = apply_operations(base_data, operations)
            else:
                raise ToolError("give operations or new_spec")
            try:
                head = Pipeline.from_dict(after)
            except SpecError as e:
                raise ToolError(str(e)) from e
            base = Pipeline.from_dict(self.load(spec)[1]) if exists else Pipeline(name=head.name, nodes=[])
            diff = diff_pipelines(base, head)
            if data and not diff.issues:
                with contextlib.suppress(ToolError):  # DuckDB isn't installed: no data section
                    diff.data = data_diff(base, head, self.duck(), p.parent, p.parent)
            after_text = edit_spec_text(before_text, after) if exists else \
                dump_spec(after, "json" if p.suffix == ".json" else "yaml")
            text_diff = "".join(difflib.unified_diff(before_text.splitlines(True), after_text.splitlines(True),
                                                     f"a/{spec}", f"b/{spec}"))
            pid = uuid.uuid4().hex[:12]
            self.proposals[pid] = {"spec": spec, "text": after_text, "base": _hash(before_text if exists else None)}
            return {"proposal_id": pid, "spec": spec, "new_file": not exists, "summary": diff.summary(),
                    "issues": diff.issues, "can_apply": not diff.issues,
                    "report": diff.to_markdown(title=f"Proposed change to `{spec}`", level=3), "diff": text_diff}

        @tool("apply_edit", "Apply an edit", "Write a proposal from propose_edit to its spec file. Only call this after "
              "the person has seen the proposal and accepted it.",
              {"proposal_id": S, "force": {"type": "boolean", "default": False,
                                           "description": "Write even though the new spec has problems"}},
              ["proposal_id"], read_only=False, destructive=True)
        def apply_edit(proposal_id, force=False):
            prop = self.proposals.get(proposal_id)
            if prop is None:
                raise ToolError(f"no proposal {proposal_id!r}; call propose_edit first")
            p = self.path(prop["spec"])
            current = p.read_text(encoding="utf-8") if p.exists() else None
            if _hash(current) != prop["base"]:
                raise ToolError(f"{prop['spec']} changed since the proposal; propose the edit again")
            if prop.get("model"):
                issues = _text_issues(prop["text"])
            else:
                text = prop["text"]
                data = json.loads(text) if text.lstrip().startswith("{") else _yaml().safe_load(text)
                issues = _issues(Pipeline.from_dict(data))
            if issues and not force:
                raise ToolError("the proposed spec has problems; fix them, or pass force: true:\n" +
                                "\n".join(f"- {i}" for i in issues))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(prop["text"], encoding="utf-8")
            del self.proposals[proposal_id]
            return {"written": prop["spec"]}

        @tool("describe_model", "Describe a data model", "A data model's entities (kind, keys, grain, history), "
              "relationships and problems.", {"model": {**S, "description": "Model path, relative to the workspace root"}},
              ["model"])
        def describe_model(model):
            from redcliff.model import load_model, validate_model

            m = load_model(self.path(model))
            return {"name": m.name, "style": m.style, "issues": [str(i) for i in validate_model(m)],
                    "entities": [{"id": e.id, "kind": e.kind, "key": e.key(), "grain": e.grain,
                                  "history": ("scd2" if e.scd2 else "scd1") if e.kind == "dimension" else None,
                                  "attributes": [a.to_dict() for a in e.attributes]} for e in m.entities],
                    "relationships": [r.to_dict() for r in m.relationships]}

        def _propose_text(path: str, text: str, model: bool, report: str, summary: str, issues: list[str]) -> dict:
            p = self.path(path)
            before = p.read_text(encoding="utf-8") if p.exists() else ""
            text_diff = "".join(difflib.unified_diff(before.splitlines(True), text.splitlines(True), f"a/{path}", f"b/{path}"))
            pid = uuid.uuid4().hex[:12]
            self.proposals[pid] = {"spec": path, "text": text, "base": _hash(before if p.exists() else None), "model": model}
            return {"proposal_id": pid, "spec": path, "new_file": not p.exists(), "summary": summary, "issues": issues,
                    "can_apply": not issues, "report": report, "diff": text_diff}

        def _job(path: str, dialect: str | None = None, params: str | None = None):
            """A pipeline spec as it is, or a legacy job imported: (pipeline, import result or None)."""
            src = self.path(path)
            if src.suffix.lower() in (".yaml", ".yml", ".json"):
                return self.pipeline(path)[1], None
            text = src.read_text(encoding="utf-8")
            from redcliff.ssis import import_ssis, is_ssis

            if is_ssis(text, src.name):
                return None, import_ssis(text)
            if src.suffix.lower() == ".xml" or text.lstrip().startswith("<"):
                from redcliff.powercenter import import_powercenter

                return None, import_powercenter(text, params=self.path(params).read_text(encoding="utf-8") if params else None)
            from redcliff.importer import import_sql

            return None, import_sql(text, dialect=dialect, name=src.stem)

        def _job_pipeline(path: str, dialect: str | None = None, params: str | None = None):
            pipeline, result = _job(path, dialect, params)
            return pipeline if pipeline is not None else result.pipeline

        @tool("propose_model", "Propose a data model", "Propose a Kimball or Data Vault 2.0 model, with the reason for "
              "every choice, as a proposal for a model file (apply it with apply_edit). Either profile CSV or Parquet "
              "files in the workspace (keys, nulls, references between them), or read a legacy job (a pipeline spec, "
              "SQL script, PowerCenter XML or SSIS .dtsx): the columns it joins and looks up on become keys, the ones "
              "it sums become measures, and the result says how much of what the job writes the model covers.",
              {"files": {"type": "array", "items": S, "description": "Data files, relative to the root"},
               "job": {**S, "description": "A legacy job or pipeline spec, relative to the root (instead of files)"},
               "out": {**S, "description": "Where the model goes, e.g. models/sales.yaml"},
               "name": S, "style": {"type": "string", "enum": ["kimball", "data_vault"], "default": "kimball"},
               "dialect": {**S, "description": "SQL dialect of a SQL job, e.g. teradata"}},
              ["out"])
        def propose_model(out, files=None, job=None, name=None, style="kimball", dialect=None):
            from redcliff.diff import diff_models
            from redcliff.model import dump_model, load_model
            from redcliff.propose import profile_files, to_data_vault

            if bool(files) == bool(job):
                raise ToolError("pass either files (data to profile) or job (a legacy job), not both")
            target = self.path(out)
            pipeline = None
            if job:
                from redcliff.jobmodel import propose_from_job

                pipeline = _job_pipeline(job, dialect)
                proposal, _ev = propose_from_job(pipeline, base_dir=target.parent, job_dir=self.path(job).parent)
            else:
                proposal = profile_files(self.duck(), [self.path(f) for f in files], base_dir=target.parent)
            if style == "data_vault":
                proposal = to_data_vault(proposal)
            proposal.model.name = name or target.stem
            base = load_model(target) if target.exists() else None
            diff = diff_models(base, proposal.model)
            result = _propose_text(out, dump_model(proposal.model), True, diff.to_markdown(f"Proposed model `{out}`", 3),
                                   diff.summary(), diff.issues)
            result["reasons"] = proposal.reasons
            if pipeline is not None:
                from redcliff.jobmodel import map_job

                result["coverage"] = map_job(pipeline, proposal.model, target.parent, self.path(job).parent).to_markdown()
            return result

        @tool("map_job", "Map a job onto a model", "For every column a legacy job (or pipeline spec) writes, the model "
              "attributes its source columns land in, and the columns the model is missing: how much of the job a model "
              "covers.",
              {"model": {**S, "description": "Model path, relative to the root"},
               "job": {**S, "description": "A legacy job or pipeline spec, relative to the root"},
               "dialect": {**S, "description": "SQL dialect of a SQL job, e.g. teradata"}},
              ["model", "job"])
        def map_job(model, job, dialect=None):
            from redcliff.jobmodel import map_job as _map
            from redcliff.model import load_model

            result = _map(_job_pipeline(job, dialect), load_model(self.path(model)), self.path(model).parent,
                          self.path(job).parent)
            return {**result.to_dict(), "report": result.to_markdown(f"`{job}` mapped onto `{model}`")}

        @tool("generate_load", "Generate a model's load", "The pipeline that loads a data model (SCD2 dimensions, "
              "point-in-time facts; or Data Vault hubs, links and satellites), as a proposal for a pipeline spec "
              "(apply it with apply_edit).",
              {"model": {**S, "description": "Model path, relative to the root"},
               "out": {**S, "description": "Where the pipeline goes, e.g. pipelines/load_sales.yaml"}},
              ["model", "out"])
        def generate_load(model, out):
            from redcliff.confine import OutsideRoot, check_paths
            from redcliff.generate import GenerateError, model_pipeline
            from redcliff.model import load_model

            m_path, target = self.path(model), self.path(out)
            try:
                pipeline = model_pipeline(load_model(m_path), m_path.parent, target.parent)
                check_paths(pipeline, target.parent, self.root)
            except (GenerateError, SpecError, OutsideRoot) as e:
                raise ToolError(str(e)) from None
            return propose_edit(out, new_spec=dump_spec(pipeline, "yaml"), data=False)

        @tool("import_legacy", "Import legacy ETL", "Turn a legacy SQL script, an Informatica PowerCenter XML export "
              "or an SSIS .dtsx package "
              "into a pipeline, as a proposal for a new spec file (apply it with apply_edit). PowerCenter imports "
              "come with a migration scorecard.",
              {"source": {**S, "description": "Path of the .sql, .xml or .dtsx file, relative to the root"},
               "out": {**S, "description": "Where the new spec goes, e.g. pipelines/orders.yaml"},
               "dialect": {**S, "description": "SQL dialect, e.g. teradata, oracle, tsql"},
               "params": {**S, "description": "PowerCenter parameter file path"}},
              ["source", "out"])
        def import_legacy(source, out, dialect=None, params=None):
            _pipeline, result = _job(source, dialect, params)
            if result is None:
                raise ToolError(f"{source} is already a pipeline spec")
            proposal = propose_edit(out, new_spec=dump_spec(result.pipeline, "yaml"), data=False)
            proposal["warnings"] = result.warnings
            if result.scorecard is not None:
                proposal["scorecard"] = result.scorecard.to_markdown()
            return proposal

    # ------------------------------------------------------------------ protocol

    def call(self, name: str, arguments: dict | None) -> dict[str, Any]:
        if name not in self.tools:
            return _error_result(f"unknown tool {name!r}")
        _, fn = self.tools[name]
        try:
            result = fn(**(arguments or {}))
        except TypeError as e:
            return _error_result(f"bad arguments for {name}: {e}")
        except (ToolError, SpecError, KeyError, ValueError, FileNotFoundError) as e:
            msg = str(e) if not isinstance(e, KeyError) else f"no step named {e.args[0]!r}"
            return _error_result(msg)
        text = result.get("report") if isinstance(result, dict) and "report" in result else None
        if text:
            text += ("\n\n```diff\n" + result["diff"] + "```" if "diff" in result else "") + "\n\n" + json.dumps(
                {k: v for k, v in result.items() if k not in ("report", "diff")}, indent=2, default=str)
        else:
            text = json.dumps(result, indent=2, default=str)
        return {"content": [{"type": "text", "text": text}], "structuredContent": result, "isError": False}

    def handle(self, message: dict) -> dict | None:
        """One JSON-RPC message in, the response out (None for notifications)."""
        method = message.get("method")
        mid = message.get("id")
        params = message.get("params") or {}
        if mid is None:
            return None  # notifications/initialized, notifications/cancelled, ...
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "redcliff", "title": "Redcliff", "version": VERSION},
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [schema for schema, _ in self.tools.values()]}
        elif method == "tools/call":
            result = self.call(params.get("name", ""), params.get("arguments"))
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as e:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}}
            else:
                try:
                    reply = self.handle(message)
                except Exception as e:  # noqa: BLE001 - never die on one message
                    reply = {"jsonrpc": "2.0", "id": message.get("id"),
                             "error": {"code": -32603, "message": f"internal error: {e}"}}
            if reply is not None:
                stdout.write(json.dumps(reply, default=str) + "\n")
                stdout.flush()


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _issues(pipeline: Pipeline) -> list[str]:
    """Structural problems, then column problems (an unknown column, a clash)."""
    issues = [str(i) for i in validate(pipeline)]
    return issues or [str(i) for i in build_graph(pipeline).issues]


def _text_issues(text: str) -> list[str]:
    from redcliff.model import parse_model, validate_model

    return [str(i) for i in validate_model(parse_model(text))]


def _hash(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def _yaml():
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise ToolError("YAML specs need PyYAML: pip install 'redcliff[cli]'") from e
    return yaml


def serve(root: str | Path = ".", db: str | None = None) -> None:
    Server(root, db).serve()
