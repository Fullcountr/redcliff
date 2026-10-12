"""The ``redcliff`` command line: import, validate, compile, inspect lineage and run pipelines.

    redcliff import legacy.sql --dialect teradata --schema schema.json -o pipeline.yaml
    redcliff import mapping.xml -o pipeline.yaml --scorecard scorecard.md   (Informatica PowerCenter)
    redcliff validate pipeline.yaml
    redcliff compile pipeline.yaml --to spark
    redcliff lineage pipeline.yaml --impact orders.status
    redcliff run pipeline.yaml --db warehouse.duckdb
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
from redcliff.spec import SpecError, load_spec


def _read_mapping(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def _write_spec(data: dict, path: str | None) -> str:
    use_yaml = path is None or path.endswith((".yaml", ".yml"))
    text = None
    if use_yaml:
        try:
            import yaml

            class _Dumper(yaml.SafeDumper):
                pass

            def short_lists_inline(dumper, items):  # inputs: [orders], not one line per item
                flow = all(isinstance(i, (str, int, float, bool)) for i in items)
                return dumper.represent_sequence("tag:yaml.org,2002:seq", items, flow_style=flow)

            _Dumper.add_representer(list, short_lists_inline)
            text = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
        except ImportError:
            pass
    if text is None:
        text = json.dumps(data, indent=2) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    return text


def _ref(text: str) -> Ref:
    node, _, column = text.partition(".")
    if not node or not column:
        raise SystemExit(f"expected NODE.COLUMN, got {text!r}")
    return Ref(node, column)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- commands


def cmd_import(args) -> int:
    text_in = Path(args.sql).read_text(encoding="utf-8")
    if args.sql.lower().endswith(".xml") or text_in.lstrip().startswith("<"):
        params = Path(args.params).read_text(encoding="utf-8") if args.params else None
        result = import_powercenter(text_in, mapping=args.mapping or None, name=args.name, params=params)
    else:
        schema = _read_mapping(args.schema) if args.schema else None
        name = args.name or Path(args.sql).stem
        result = import_sql(text_in, dialect=args.dialect, name=name, schema=schema)
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


def cmd_compile(args) -> int:
    pipeline = load_spec(args.spec)
    base = Path(args.spec).parent if args.resolve_paths else None
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
    graph = build_graph(load_spec(args.spec))
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
    pipeline = load_spec(args.spec)
    base = Path(args.spec).parent
    con = duckdb.connect(args.db) if args.db else duckdb.connect()
    if args.node:
        rel = con.sql(compile_query(pipeline, args.node, "duckdb", base_dir=base))
        print(" | ".join(rel.columns))
        for row in rel.fetchmany(args.limit):
            print(" | ".join("" if v is None else str(v) for v in row))
        return 0
    for target in (n for n in pipeline.nodes if n.type == "target" and n.params["format"] != "table"):
        (base / target.params["path"]).parent.mkdir(parents=True, exist_ok=True)
    con.execute(compile_script(pipeline, "duckdb", base_dir=base))
    targets = [n.params.get("table") or n.params.get("path") for n in pipeline.nodes if n.type == "target"]
    print(f"ran {pipeline.name}: wrote {', '.join(targets)}")
    return 0


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="redcliff", description="Legacy ETL in, clean Spark out.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="turn legacy SQL or a PowerCenter mapping into a pipeline spec")
    p.add_argument("sql", help="SQL file (one or more statements), or a PowerCenter XML export")
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

    p = sub.add_parser("compile", help="generate DuckDB SQL, Spark SQL or PySpark")
    p.add_argument("spec")
    p.add_argument("--to", choices=["duckdb", "spark", "pyspark"], default="duckdb")
    p.add_argument("--node", help="compile a SELECT for one node instead of the full script")
    p.add_argument("--resolve-paths", action="store_true", help="make file paths absolute")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("lineage", help="column lineage, blast radius or OpenLineage export")
    p.add_argument("spec")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--column", help="NODE.COLUMN: where does it come from?")
    g.add_argument("--impact", help="NODE.COLUMN: what does a change to it touch?")
    g.add_argument("--openlineage", metavar="NODE", help="print the OpenLineage column facet for NODE")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("run", help="run the pipeline locally with DuckDB")
    p.add_argument("spec")
    p.add_argument("--db", help="DuckDB database file holding table sources and targets")
    p.add_argument("--node", help="preview one node's rows instead of writing targets")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SpecError, SQLImportError, CompileError, FileNotFoundError, KeyError) as e:
        _err(f"error: {e}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
