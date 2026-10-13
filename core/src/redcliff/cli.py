"""The ``redcliff`` command line: import, validate, compile, inspect lineage and run pipelines.

    redcliff import legacy.sql --dialect teradata --schema schema.json -o pipeline.yaml
    redcliff import mapping.xml -o pipeline.yaml --scorecard scorecard.md   (Informatica PowerCenter)
    redcliff import package.dtsx -o pipeline.yaml --scorecard scorecard.md  (SSIS)
    redcliff validate pipeline.yaml
    redcliff compile pipeline.yaml --to spark
    redcliff compile pipeline.yaml --to dbt -o my_dbt_project       (or --to lakeflow)
    redcliff lineage pipeline.yaml --impact orders.status
    redcliff run pipeline.yaml --db warehouse.duckdb
    redcliff test pipeline.yaml                       (data quality expectations)
    redcliff trace pipeline.yaml out --where "region = 'South'"
    redcliff diff old.yaml new.yaml --data            (or: redcliff diff --git origin/main)
    redcliff mcp --root .                             (an MCP server for AI agents, over stdio)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from redcliff.compile import CompileError, compile_pyspark, compile_query, compile_script
from redcliff.importer import SQLImportError, import_sql
from redcliff.lineage import Ref, blast_radius, build_graph, openlineage_facet, trace_to_sources
from redcliff.powercenter import import_powercenter
from redcliff.spec import SpecError, dump_spec, load_spec


def _read_mapping(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def _write_spec(data: dict, path: str | None) -> str:
    text = dump_spec(data, "yaml" if path is None or path.endswith((".yaml", ".yml")) else "json")
    if path:
        Path(path).write_text(text, encoding="utf-8")
    return text


def _spec(args):
    """Load the spec, with --var overrides applied to its variables."""
    pipeline = load_spec(args.spec)
    for item in getattr(args, "var", None) or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise SystemExit(f"--var expects NAME=VALUE, got {item!r}")
        if name.strip() not in pipeline.variables:
            _err(f"note: {name.strip()} isn't declared in the spec's variables; adding it")
        pipeline.variables[name.strip()] = value
    return pipeline


def _ref(text: str) -> Ref:
    node, _, column = text.partition(".")
    if not node or not column:
        raise SystemExit(f"expected NODE.COLUMN, got {text!r}")
    return Ref(node, column)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- commands


def _import_file(path: str, args, name: str | None = None):
    """Import legacy SQL, a PowerCenter export or an SSIS package (the format is detected)."""
    text_in = Path(path).read_text(encoding="utf-8")
    from redcliff.ssis import import_ssis, is_ssis

    if is_ssis(text_in, path):
        return import_ssis(text_in, name=name)
    if path.lower().endswith(".xml") or text_in.lstrip().startswith("<"):
        params = Path(args.params).read_text(encoding="utf-8") if getattr(args, "params", None) else None
        return import_powercenter(text_in, mapping=getattr(args, "mapping", None) or None, name=name, params=params)
    schema = _read_mapping(args.schema) if getattr(args, "schema", None) else None
    return import_sql(text_in, dialect=getattr(args, "dialect", None), name=name or Path(path).stem, schema=schema)


def _load_job(path: str, args):
    """A pipeline spec as it is, or a legacy job imported (warnings go to stderr)."""
    if path.lower().endswith((".yaml", ".yml", ".json")):
        return load_spec(path)
    result = _import_file(path, args)
    for w in result.warnings:
        _err(f"warning: {w}")
    return result.pipeline


def cmd_import(args) -> int:
    result = _import_file(args.sql, args, args.name)
    text = _write_spec(result.pipeline.to_dict(), args.output)
    if not args.output:
        print(text, end="")
    nodes = result.pipeline.nodes
    _err(f"imported {len(nodes)} nodes ({sum(n.type == 'target' for n in nodes)} targets)"
         + (f" to {args.output}" if args.output else ""))
    for w in result.warnings:
        _err(f"warning: {w}")
    card = result.scorecard
    if card is not None:
        c = card.counts()
        _err(f"scorecard: {c['converted']} of {len(card.items)} converted ({card.automated():.0%}), "
             f"{c['review']} to review, {c['manual']} to rewrite by hand")
        if args.scorecard:
            out = Path(args.scorecard)
            out.write_text(json.dumps(card.to_dict(), indent=2) + "\n" if out.suffix == ".json"
                           else card.to_markdown(), encoding="utf-8")
            _err(f"scorecard written to {args.scorecard}")
    return 0


def cmd_validate(args) -> int:
    pipeline = load_spec(args.spec)
    issues = build_graph(pipeline).issues
    for i in issues:
        _err(f"column issue: {i}")
    if issues:
        return 1
    print(f"{args.spec}: valid ({len(pipeline.nodes)} nodes)")
    return 0


def _write_project(files: dict[str, str], out_dir: str | None) -> None:
    if not out_dir:  # one stream, a header per file
        for rel, content in files.items():
            print(f"# ==== {rel}\n{content}", end="" if content.endswith("\n") else "\n")
        return
    from redcliff.dbt import project_path

    try:
        paths = {rel: project_path(out_dir, rel) for rel in files}  # check every name before writing anything
    except ValueError as e:
        raise SystemExit(f"redcliff: {e}") from None
    for rel, content in files.items():
        paths[rel].parent.mkdir(parents=True, exist_ok=True)
        paths[rel].write_text(content, encoding="utf-8")
    _err(f"wrote {len(files)} files to {out_dir}")


def cmd_compile(args) -> int:
    pipeline = _spec(args)
    base = Path(args.spec).parent if args.resolve_paths else None
    if args.to == "dbt":
        from redcliff.dbt import compile_dbt

        _write_project(compile_dbt(pipeline, args.adapter, base_dir=Path(args.spec).parent), args.output)
        return 0
    if args.to == "lakeflow":
        from redcliff.lakeflow import compile_lakeflow

        _write_project(compile_lakeflow(pipeline, base_dir=base, storage=args.storage), args.output)
        return 0
    if args.to == "pyspark":
        out = compile_pyspark(pipeline, base_dir=base)
    elif args.node:
        out = compile_query(pipeline, args.node, args.to, base_dir=base) + "\n"
    else:
        out = compile_script(pipeline, args.to, base_dir=base)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        _err(f"wrote {args.output}")
    else:
        print(out, end="")
    return 0


def cmd_lineage(args) -> int:
    graph = build_graph(_spec(args))
    for i in graph.issues:
        _err(f"column issue: {i}")
    if args.openlineage:
        print(json.dumps(openlineage_facet(graph, args.openlineage), indent=2))
    elif args.impact:
        impact = blast_radius(graph, _ref(args.impact)).to_dict()
        print(f"If {impact['column']} changes:")
        print(f"  values change in: {', '.join(impact['values']) or 'nothing'}")
        print(f"  rows change in:   {', '.join(impact['rows']) or 'nothing'}")
        print(f"  a rename breaks:  {', '.join(impact['breaks']) or 'nothing'}")
    elif args.column:
        ref = _ref(args.column)
        traced = trace_to_sources(graph, ref)
        print(f"{ref} comes from:")
        for src, (kind, sub) in sorted(traced.items()):
            print(f"  {src}  ({kind.lower()}, {sub.lower()})")
        if not traced:
            print("  no source columns (a literal or COUNT(*))")
    else:
        for node, cols in graph.columns.items():
            for col in cols:
                srcs = ", ".join(str(s) for s in sorted(trace_to_sources(graph, Ref(node, col))))
                print(f"{node}.{col} <- {srcs or '-'}")
    return 0


def cmd_run(args) -> int:
    try:
        import duckdb
    except ImportError:
        _err("run needs DuckDB: pip install duckdb")
        return 2
    pipeline = _spec(args)
    base = Path(args.spec).parent
    con = duckdb.connect(args.db) if args.db else duckdb.connect()
    if args.node:
        rel = con.sql(compile_query(pipeline, args.node, "duckdb", base_dir=base))
        print(" | ".join(rel.columns))
        for row in rel.fetchmany(args.limit):
            print(" | ".join("" if v is None else str(v) for v in row))
        return 0
    if not args.skip_checks:
        from redcliff.expect import run_checks

        results = run_checks(con, pipeline, base_dir=base)
        _report_checks(results, quiet=True)
        if any(not r.passed and r.expectation.severity == "error" for r in results):
            _err("not writing: an expectation failed (see above); fix the data or pass --skip-checks")
            return 1
    for target in (n for n in pipeline.nodes if n.type == "target" and n.params["format"] != "table"):
        (base / target.params["path"]).parent.mkdir(parents=True, exist_ok=True)
    emitter = None
    if args.openlineage:
        import os

        from redcliff.openlineage import Emitter, run_event

        emitter = Emitter(args.openlineage, api_key=os.environ.get("OPENLINEAGE_API_KEY"))

        def emit(kind: str, error: str | None = None) -> None:
            try:
                emitter.send(run_event(pipeline, kind, emitter.run_id, base, args.namespace, error))
            except Exception as e:  # noqa: BLE001 - lineage must never fail the run
                _err(f"warning: couldn't send the OpenLineage {kind} event: {e}")

        emit("START")
    try:
        con.execute(compile_script(pipeline, "duckdb", base_dir=base))
    except duckdb.Error as e:
        message = str(e).splitlines()[0]
        if emitter:
            emit("FAIL", message)
        _err(f"error: the run failed: {message}")
        return 1
    if emitter:
        emit("COMPLETE")
    targets = [n.params.get("table") or n.params.get("path") for n in pipeline.nodes if n.type == "target"]
    print(f"ran {pipeline.name}: wrote {', '.join(targets)}")
    return 0


def _report_checks(results, quiet: bool = False) -> None:
    """Print expectation results; with quiet, only the failures."""
    for r in results:
        e = r.expectation
        if r.passed:
            if not quiet:
                print(f"pass  {e.node}.{e.name}  ({e.describe()})")
            continue
        label = "warn " if e.severity == "warn" else "FAIL "
        what = r.error or f"{r.failures} failing row{'s' if r.failures != 1 else ''}"
        print(f"{label} {e.node}.{e.name}  ({e.describe()}): {what}")
        if r.sample:
            print("        " + " | ".join(r.columns))
            for row in r.sample:
                print("        " + " | ".join("" if v is None else str(v) for v in row))


def cmd_test(args) -> int:
    try:
        import duckdb
    except ImportError:
        _err("test needs DuckDB: pip install duckdb")
        return 2
    from redcliff.expect import run_checks

    pipeline = _spec(args)
    con = duckdb.connect(args.db, read_only=True) if args.db else duckdb.connect()
    results = run_checks(con, pipeline, args.node, Path(args.spec).parent)
    if not results:
        print("no expectations to check; add 'expect' to a step (see docs/spec.md)")
        return 0
    _report_checks(results)
    failed = [r for r in results if not r.passed and r.expectation.severity == "error"]
    warned = [r for r in results if not r.passed and r.expectation.severity == "warn"]
    print(f"\n{len(results) - len(failed) - len(warned)} passed, {len(failed)} failed, {len(warned)} warned")
    return 1 if failed else 0


def _cell(v) -> str:
    return "" if v is None else str(v)


def cmd_trace(args) -> int:
    try:
        import duckdb
    except ImportError:
        _err("trace needs DuckDB: pip install duckdb")
        return 2
    from redcliff.trace import trace_query, trace_rows

    pipeline = _spec(args)
    base = Path(args.spec).parent
    con = duckdb.connect(args.db, read_only=True) if args.db else duckdb.connect()
    tq = trace_query(pipeline, args.node, base)
    for step in tq.opaque:
        _err(f"note: rows pass through the sql step {step} untraced")
    rows = trace_rows(con, pipeline, args.node, args.where, base)
    if not rows:
        print("no rows" + (f" where {args.where}" if args.where else ""))
    for i, traced in enumerate(rows[: args.limit]):
        if i:
            print()
        print(f"{tq.node}: " + ", ".join(f"{k}={_cell(v)}" for k, v in traced["row"].items()))
        for source, src_rows in traced["sources"].items():
            print(f"  from {source} ({len(src_rows)} row{'s' if len(src_rows) != 1 else ''}):")
            for r in src_rows:
                print(f"    #{r['row']}: " + ", ".join(f"{k}={_cell(v)}" for k, v in r.items() if k != "row"))
    if len(rows) > args.limit:
        print(f"\n... {len(rows) - args.limit} more rows; narrow them with --where")
    return 0


MARKER = "<!-- redcliff-diff -->"


def _git(*args: str, cwd: str | None = None) -> str:
    import subprocess

    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if done.returncode:
        raise FileNotFoundError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def _as_model(text: str | None):
    """A model file's text as a Model, or None when it isn't a data model."""
    from redcliff.model import Model

    if text is None:
        return None
    try:
        data = json.loads(text) if text.lstrip().startswith("{") else __import__("yaml").safe_load(text)
        if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
            return None
        return Model.from_dict(data)
    except Exception:  # noqa: BLE001 - not a model
        return None


def _as_pipeline(text: str | None):
    """A spec's text as a Pipeline, or None when it isn't a pipeline spec."""
    from redcliff.spec import Pipeline

    if text is None:
        return None
    try:
        if text.lstrip().startswith("{"):
            data = json.loads(text)
        else:
            import yaml

            data = yaml.safe_load(text)
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            return None
        return Pipeline.from_dict(data)
    except Exception:  # noqa: BLE001 - not a spec, or not parseable: skip it
        return None


def _unreadable(rel: str):
    from redcliff.diff import PipelineDiff

    diff = PipelineDiff(name=Path(rel).stem)
    diff.issues.append(f"{rel} looks like a pipeline spec but isn't valid YAML or JSON")
    return diff


def _diff_one(base, head, args, base_dir, head_dir, root):
    from redcliff.confine import OutsideRoot, check_paths, lock_duckdb
    from redcliff.diff import data_diff, diff_pipelines
    from redcliff.spec import Pipeline

    base = base or Pipeline(name=head.name, nodes=[])
    diff = diff_pipelines(base, head)
    if args.data and not diff.issues:
        # The specs may come from a pull request: they may read only files of the repository.
        try:
            check_paths(head, head_dir, root)
            check_paths(base, base_dir, root)
        except OutsideRoot as e:
            diff.issues.append(f"{e}; data diffs read only files in the repository")
    if args.data and not diff.issues:
        try:
            import duckdb
        except ImportError:
            _err("--data needs DuckDB: pip install duckdb")
        else:
            con = duckdb.connect(args.db, read_only=True) if args.db else duckdb.connect()
            diff.data = data_diff(base, head, lock_duckdb(con, root), base_dir, head_dir)
    return diff


def _diff_report(results, args) -> int:
    """Write the diff report (Markdown or JSON) and return the exit code."""
    if args.format == "json":
        text = json.dumps({label: (d.to_dict() if d else {"removed": True}) for label, d in results}, indent=2) + "\n"
    else:
        from redcliff.diff import ModelDiff

        models = " and models" if any(isinstance(d, ModelDiff) for _l, d in results) else ""
        lines = [MARKER, f"## Redcliff: what this change does to the pipelines{models}", ""]
        if not results:
            lines.append("No pipeline specs changed.")
        for label, d in results:
            if d is None:
                lines += [f"### `{label}`", "", "**removed**", ""]
            else:
                lines.append(d.to_markdown(title=f"`{label}`", level=3))
        lines += ["<sub>Generated by [Redcliff](https://github.com/Fullcountr/redcliff) from the pipeline specs; "
                  "data compares each target on the sample data in the repository.</sub>" if args.data else
                  "<sub>Generated by [Redcliff](https://github.com/Fullcountr/redcliff) from the pipeline specs.</sub>"]
        text = "\n".join(lines).rstrip() + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        _err(f"wrote {args.output}")
    else:
        print(text, end="")
    has_issues = any(d is not None and d.issues for _l, d in results)
    return 1 if has_issues and args.fail_on_issues else 0


def cmd_diff(args) -> int:
    results = []  # (label, diff)
    if args.git:
        root = _git("rev-parse", "--show-toplevel").strip()
        # -z: NUL-separated and unquoted, so names with spaces or non-ASCII letters arrive whole
        names = _git("diff", "-z", "--name-only", "--diff-filter=ACMRD", args.git, "--", *(args.paths or ["."]),
                     cwd=None)
        prefix = _git("rev-parse", "--show-prefix").strip()
        for rel in filter(None, names.split("\0")):
            if not rel.endswith((".yaml", ".yml", ".json")):
                continue
            path = Path(root) / rel
            head_text = path.read_text(encoding="utf-8") if path.exists() else None
            try:
                base_text = _git("show", f"{args.git}:{rel}", cwd=root)
            except FileNotFoundError:
                base_text = None
            label = rel[len(prefix):] if prefix and rel.startswith(prefix) else rel
            base_model, head_model = _as_model(base_text), _as_model(head_text)
            if base_model is not None or head_model is not None:  # a data model: entities, not steps
                from redcliff.diff import diff_models

                results.append((label, diff_models(base_model, head_model) if head_model else None))
                continue
            base, head = _as_pipeline(base_text), _as_pipeline(head_text)
            if head is None and base is None:
                if head_text is not None and rel.endswith((".yaml", ".yml")) and "nodes:" in head_text:
                    results.append((rel, _unreadable(rel)))  # looks like a spec but doesn't parse: say so
                continue
            label = rel[len(prefix):] if prefix and rel.startswith(prefix) else rel
            if head is None:
                results.append((label, None))
                continue
            results.append((label, _diff_one(base, head, args, path.parent, path.parent, root)))
    else:
        if not args.paths or len(args.paths) != 2:
            _err("give two specs (BASE HEAD), or --git REF")
            return 2
        a, b = (Path(p) for p in args.paths)
        base_model, head_model = _as_model(a.read_text(encoding="utf-8")), _as_model(b.read_text(encoding="utf-8"))
        if base_model is not None and head_model is not None:
            from redcliff.diff import diff_models

            args.paths = []
            results.append((str(b), diff_models(base_model, head_model)))
            return _diff_report(results, args)
        base = _as_pipeline(a.read_text(encoding="utf-8"))
        head = _as_pipeline(b.read_text(encoding="utf-8"))
        if base is None or head is None:
            _err("both files must be pipeline specs")
            return 1
        try:
            root = _git("rev-parse", "--show-toplevel").strip()
        except FileNotFoundError:
            root = str(Path.cwd())
        results.append((str(b), _diff_one(base, head, args, a.parent, b.parent, root)))

    return _diff_report(results, args)


def cmd_model(args) -> int:
    from redcliff import model as m

    if args.action == "propose":
        from redcliff.propose import to_data_vault

        base = Path(args.output).parent if args.output else Path.cwd()
        data = [f for f in args.file if f.lower().endswith((".csv", ".parquet", ".tsv"))]
        job_map = None
        if data and len(data) != len(args.file):
            _err("propose from data files (CSV, Parquet) or from one legacy job, not both")
            return 2
        if data:
            try:
                import duckdb
            except ImportError:
                _err("propose needs DuckDB: pip install 'redcliff[cli]'")
                return 2
            from redcliff.propose import profile_files

            proposal = profile_files(duckdb.connect(), args.file, base_dir=base)
        else:
            if len(args.file) != 1:
                _err("propose from one legacy job at a time")
                return 2
            from redcliff.jobmodel import map_job, propose_from_job

            job = _load_job(args.file[0], args)
            proposal, _ev = propose_from_job(job, base_dir=base, job_dir=Path(args.file[0]).parent)
            job_map = map_job(job, proposal.model, base, Path(args.file[0]).parent)
        if args.style == "data_vault":
            proposal = to_data_vault(proposal)
            if job_map is not None:
                job_map = map_job(job, proposal.model, base, Path(args.file[0]).parent)
        proposal.model.name = args.name or proposal.model.name
        for ref, why in proposal.reasons.items():
            for w in why:
                _err(f"{ref}: {w}")
        if job_map is not None:
            _err(f"{job_map.to_dict()['covered']} of {len(job_map.columns)} columns the job writes "
                 f"({job_map.coverage():.0%}) come from model attributes; details: redcliff model map MODEL JOB")
        out = m.dump_model(proposal.model, "json" if (args.output or "").endswith(".json") else "yaml")
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8")
            _err(f"wrote {args.output}; review it, then: redcliff model validate {args.output}")
        else:
            print(out, end="")
        return 0
    if args.action == "map":
        if len(args.file) != 2:
            _err("model map takes a model and a job: redcliff model map model.yaml job.sql")
            return 2
        from redcliff.jobmodel import map_job

        model = m.load_model(args.file[0])
        job = _load_job(args.file[1], args)
        result = map_job(job, model, Path(args.file[0]).parent, Path(args.file[1]).parent)
        out = (json.dumps(result.to_dict(), indent=2) + "\n" if (args.output or "").endswith(".json")
               else result.to_markdown(f"{Path(args.file[1]).name} mapped onto {Path(args.file[0]).name}"))
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8")
            _err(f"wrote {args.output}")
        else:
            print(out, end="")
        _err(f"{result.coverage():.0%} of the columns the job writes come from model attributes")
        return 0
    if len(args.file) != 1:
        _err(f"model {args.action} takes one file")
        return 2
    args.file = args.file[0]
    if args.action == "import":
        text = Path(args.file).read_text(encoding="utf-8")
        name = args.name or Path(args.file).stem
        result = m.from_dbml(text, name) if args.file.endswith(".dbml") else m.from_ddl(text, args.dialect, name)
        for w in result.warnings:
            _err(f"warning: {w}")
        out = m.dump_model(result.model, "json" if (args.output or "").endswith(".json") else "yaml")
    else:
        model = m.load_model(args.file)
        issues = m.validate_model(model)
        for i in issues:
            _err(f"error: {i}")
        if issues:
            return 1
        if args.action == "validate":
            print(f"{args.file}: valid ({len(model.entities)} entities, {len(model.relationships)} relationships)")
            return 0
        if args.action == "pipeline":
            from redcliff.generate import model_pipeline
            from redcliff.spec import dump_spec

            spec_dir = Path(args.output).parent if args.output else Path(args.file).parent
            pipeline = model_pipeline(model, Path(args.file).parent, spec_dir, name=args.name)
            out = dump_spec(pipeline, "json" if (args.output or "").endswith(".json") else "yaml")
            out = f"# Generated by Redcliff from {Path(args.file).name}: edit the model, then generate again.\n" + out
            if args.output:
                Path(args.output).write_text(out, encoding="utf-8")
                _err(f"wrote {args.output}; run it with: redcliff run {args.output} --db warehouse.duckdb")
            else:
                print(out, end="")
            return 0
        if args.action == "test":
            try:
                import duckdb
            except ImportError:
                _err("model test needs DuckDB: pip install 'redcliff[cli]'")
                return 2
            from redcliff.generate import run_model_checks

            con = duckdb.connect(args.db, read_only=True) if args.db else duckdb.connect()
            results = run_model_checks(con, model)
            for r in results:
                status = "pass " if r["passed"] else "FAIL "
                if r["error"]:
                    detail = f"  ({r['error']})"
                else:
                    detail = "" if r["passed"] else f"  ({r['failing']} rows, e.g. {r['sample'][0]})"
                print(f"{status} {r['entity']}: {r['check']}{detail}")
            failed = sum(not r["passed"] for r in results)
            print(f"{len(results) - failed} passed, {failed} failed")
            return 1 if failed else 0
        fn = m.EXPORTS[args.to]
        out = fn(model, args.dialect or "duckdb") if args.to == "ddl" else fn(model)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        _err(f"wrote {args.output}")
    else:
        print(out, end="")
    return 0


def cmd_mcp(args) -> int:
    from redcliff.mcp import serve

    serve(args.root, args.db)
    return 0


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    from redcliff import __version__

    parser = argparse.ArgumentParser(prog="redcliff", description="Legacy ETL in, clean Spark out.")
    parser.add_argument("--version", action="version", version=f"redcliff {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="turn legacy SQL, a PowerCenter export or an SSIS package into a pipeline spec")
    p.add_argument("sql", help="SQL file (one or more statements), a PowerCenter XML export or an SSIS .dtsx")
    p.add_argument("--dialect", help="sqlglot dialect, e.g. teradata, oracle, tsql, snowflake")
    p.add_argument("--schema", help="JSON/YAML mapping of table -> {column: type}")
    p.add_argument("--name", help="pipeline name (default: the file name)")
    p.add_argument("--mapping", action="append", help="PowerCenter: import only this mapping (repeatable)")
    p.add_argument("--scorecard", help="write the migration scorecard here (.md or .json)")
    p.add_argument("--params", help="PowerCenter: a parameter file, for $$PARAMS in mappings and SQL")
    p.add_argument("-o", "--output", help="write the spec here (.yaml or .json); default: stdout")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("validate", help="check a spec, including column-level checks")
    p.add_argument("spec")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("compile", help="generate DuckDB SQL, Spark SQL, PySpark, a dbt project or a Lakeflow pipeline")
    p.add_argument("spec")
    p.add_argument("--var", action="append", metavar="NAME=VALUE", help="set a pipeline variable (repeatable)")
    p.add_argument("--to", choices=["duckdb", "spark", "pyspark", "dbt", "lakeflow"], default="duckdb")
    p.add_argument("--adapter", default="duckdb", help="dbt: the adapter to write SQL for (duckdb, databricks, "
                   "spark, snowflake, bigquery, postgres, redshift)")
    p.add_argument("--storage", help="lakeflow: checkpoint root, an absolute URI (file://..., s3a://...)")
    p.add_argument("--node", help="compile a SELECT for one node instead of the full script")
    p.add_argument("--resolve-paths", action="store_true", help="make file paths absolute")
    p.add_argument("-o", "--output", help="a file; for dbt and lakeflow, a directory")
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("lineage", help="column lineage, blast radius or OpenLineage export")
    p.add_argument("spec")
    p.add_argument("--var", action="append", metavar="NAME=VALUE", help="set a pipeline variable (repeatable)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--column", help="NODE.COLUMN: where does it come from?")
    g.add_argument("--impact", help="NODE.COLUMN: what does a change to it touch?")
    g.add_argument("--openlineage", metavar="NODE", help="print the OpenLineage column facet for NODE")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("run", help="run the pipeline locally with DuckDB")
    p.add_argument("spec")
    p.add_argument("--var", action="append", metavar="NAME=VALUE", help="set a pipeline variable (repeatable)")
    p.add_argument("--db", help="DuckDB database file holding table sources and targets")
    p.add_argument("--node", help="preview one node's rows instead of writing targets")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--skip-checks", action="store_true", help="write even if an expectation fails")
    p.add_argument("--openlineage", metavar="FILE_OR_URL",
                   help="send OpenLineage run events (START, COMPLETE or FAIL, with column lineage): append them to a "
                        "file as JSON lines, or POST them to a URL such as http://localhost:5000/api/v1/lineage "
                        "(a bearer token from OPENLINEAGE_API_KEY)")
    p.add_argument("--namespace", default="redcliff", help="OpenLineage namespace for the job and table datasets")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("test", help="run the pipeline's expectations (data quality checks) in DuckDB")
    p.add_argument("spec")
    p.add_argument("--var", action="append", metavar="NAME=VALUE", help="set a pipeline variable (repeatable)")
    p.add_argument("--node", help="check only this step")
    p.add_argument("--db", help="DuckDB database file holding table sources")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("trace", help="row-level trace: the source rows behind a step's rows")
    p.add_argument("spec")
    p.add_argument("--var", action="append", metavar="NAME=VALUE", help="set a pipeline variable (repeatable)")
    p.add_argument("node", help="the step (or target) whose rows to trace")
    p.add_argument("--where", help="SQL condition picking the rows, e.g. \"region = 'South'\"")
    p.add_argument("--db", help="DuckDB database file holding table sources")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("diff", help="what a change does: steps, columns, lineage and data")
    p.add_argument("paths", nargs="*", help="BASE HEAD specs; with --git, paths to limit the search to")
    p.add_argument("--git", metavar="REF", help="compare every changed spec with its version at REF")
    p.add_argument("--data", action="store_true", help="also run each target on sample data, both ways")
    p.add_argument("--db", help="DuckDB database file holding table sources")
    p.add_argument("--format", choices=["md", "json"], default="md")
    p.add_argument("--fail-on-issues", action="store_true", help="exit 1 if a new spec has problems")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("model", help="data models: propose one from files, validate, export (DDL, DBML, Mermaid, dbt), "
                                     "import (DDL, DBML), generate the pipeline that loads it, test the loaded tables")
    p.add_argument("action", choices=["validate", "export", "import", "propose", "map", "pipeline", "test"])
    p.add_argument("file", nargs="+", help="model.yaml to validate or export; a .sql or .dbml file to import; "
                                           "CSV or Parquet files, or a legacy job (SQL, PowerCenter XML, SSIS .dtsx "
                                           "or a pipeline spec), to propose a model from; for map, the model then the job")
    p.add_argument("--to", choices=["ddl", "dbml", "mermaid", "dbt"], default="ddl", help="export format")
    p.add_argument("--dialect", help="SQL dialect for DDL export or import (default: duckdb)")
    p.add_argument("--name", help="model name for an import or propose; pipeline name for pipeline")
    p.add_argument("--db", help="model test: the DuckDB file the model was loaded into")
    p.add_argument("--style", choices=["kimball", "data_vault"], default="kimball", help="propose: the model style")
    p.add_argument("--schema", help="propose/map from legacy SQL: JSON/YAML mapping of table -> {column: type}")
    p.add_argument("--params", help="propose/map from PowerCenter: a parameter file")
    p.add_argument("--mapping", action="append", help="propose/map from PowerCenter: only this mapping (repeatable)")
    p.add_argument("-o", "--output", help="write to this file instead of printing")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("mcp", help="run an MCP server over stdio, so AI agents can read and edit pipelines")
    p.add_argument("--root", default=".", help="the folder whose specs agents may read and edit (default: here)")
    p.add_argument("--db", help="DuckDB database file holding table sources, for previews and traces")
    p.set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SpecError, SQLImportError, CompileError, FileNotFoundError, KeyError, ValueError) as e:
        _err(f"error: {e}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
