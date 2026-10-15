"""Generate a dbt project from a pipeline spec.

The mapping is direct:

- table sources become dbt ``source()``\\s; CSV sources become seeds; Parquet sources a model
- every step becomes an ephemeral model, so dbt compiles the chain into one query per target
- every target becomes a model: ``table``, or ``incremental`` for ``mode: append``; file targets
  use dbt-duckdb's ``external`` materialization on DuckDB
- a target's ``pre_sql`` / ``post_sql`` become dbt ``pre_hook`` / ``post_hook``
- each model's columns are documented with their lineage, so ``dbt docs`` shows where every
  column comes from

The output is a dict of relative path -> file content; :func:`write_dbt_project` writes it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from redcliff.columns import infer_columns
from redcliff.expect import expectations
from redcliff.compile import _literal, _node_body, _one_line
from redcliff.lineage import Ref, build_graph, trace_to_sources
from redcliff.spec import Node, Pipeline, bind

ADAPTERS = {"duckdb": "duckdb", "spark": "spark", "databricks": "databricks", "snowflake": "snowflake",
            "bigquery": "bigquery", "postgres": "postgres", "redshift": "redshift"}
SOURCE_GROUP = "raw"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "pipeline"
    return s if s[0].isalpha() else "p_" + s


_JINJA_OPEN = re.compile(r"\{[{%#]")


def _jinja_literal(text: str) -> str:
    """Spec-derived text that dbt's Jinja renders back as itself: '{{', '{%' and '{#' become
    {{ '{{' }} and so on. Redcliff's own ref/source/var tokens hold no braces and are swapped in after."""
    return _JINJA_OPEN.sub(lambda m: "{{ '" + m.group(0) + "' }}", text)


def _yaml_str(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _table_name(table: str) -> tuple[str | None, str]:
    parts = table.split(".")
    return (parts[-2] if len(parts) > 1 else None), parts[-1]


def _lineage_text(graph, node_id: str, column: str) -> str:
    traced = trace_to_sources(graph, Ref(node_id, column))
    direct = sorted(f"{r} ({s.lower()})" for r, (t, s) in traced.items() if t == "DIRECT")
    shaping = sorted({str(r) for r, (t, _s) in traced.items() if t == "INDIRECT"})
    text = f"From {', '.join(direct)}." if direct else \
        "Computed without reading a source column (a count, a constant or a placeholder)."
    if shaping:
        text += f" Shaped by {', '.join(shaping)}."
    return text


def compile_dbt(pipeline: Pipeline, adapter: str = "duckdb", base_dir: str | Path | None = None) -> dict[str, str]:
    """A dbt project as {relative path: content}. ``base_dir`` lets CSV seeds be copied in."""
    if adapter not in ADAPTERS:
        raise ValueError(f"unsupported dbt adapter {adapter!r}; expected one of {sorted(ADAPTERS)}")
    dialect = ADAPTERS[adapter]
    project = _slug(pipeline.name)
    # Variables stay variables: dbt's var(), with the spec's values as defaults. SQL is generated
    # from a copy holding a token per variable; analysis uses the real values.
    variables = dict(pipeline.variables)
    templated = bind(pipeline, {k: f"__rcvar_{k}__" for k in variables})
    pipeline = bind(pipeline)
    cols = infer_columns(pipeline)
    graph = build_graph(pipeline)
    by_id = {n.id: n for n in templated.nodes}
    files: dict[str, str] = {}

    # dbt conventions: intermediate steps are int_ models; a target's model is named after its table.
    tables = [_table_name(n.params["table"])[1] for n in pipeline.nodes
              if n.type == "target" and n.params["format"] == "table"]
    model_name: dict[str, str] = {}
    for n in pipeline.nodes:
        if n.type == "source":
            model_name[n.id] = n.id if n.params["format"] == "csv" else f"src_{n.id}"
        elif n.type == "target":
            table = _table_name(n.params["table"])[1] if n.params["format"] == "table" else None
            model_name[n.id] = table if table and tables.count(table) == 1 else n.id
        else:
            model_name[n.id] = f"int_{n.id}"

    written = {n.params["table"]: n.id for n in pipeline.nodes if n.type == "target" and n.params.get("format") == "table"}

    def relation(node_id: str) -> str:
        node = by_id[node_id]
        if node.type == "source" and node.params["format"] == "table":
            if node.params["table"] in written:  # this pipeline writes it: ref() it, so dbt builds it first
                return f"{{{{ ref('{model_name[written[node.params['table']]]}') }}}}"
            return f"{{{{ source('{SOURCE_GROUP}', '{_table_name(node.params['table'])[1]}') }}}}"
        return f"{{{{ ref('{model_name[node_id]}') }}}}"

    def model_sql(node: Node) -> str:
        """The step's SELECT in the adapter's dialect, reading its inputs through ref()/source()."""
        body = _node_body(node, cols, dialect)
        tree = sqlglot.parse_one(body)
        refs: dict[str, str] = {}

        def swap(n: exp.Expression) -> exp.Expression:
            if isinstance(n, exp.Table) and not n.db and n.name in node.inputs:
                token = f"__redcliff_ref_{len(refs)}__"
                refs[token] = relation(n.name)
                alias = n.alias or n.name
                return exp.Table(this=exp.to_identifier(token), alias=exp.TableAlias(this=exp.to_identifier(alias)))
            return n

        text = _jinja_literal(tree.transform(swap).sql(dialect, pretty=True))
        for token, rel in refs.items():
            text = re.sub(rf'["`]?{token}["`]?', lambda _m, r=rel: r, text)
        return text

    # ------------------------------------------------------------------ project files
    files["dbt_project.yml"] = "\n".join([
        f"# Generated by Redcliff from the pipeline spec {pipeline.name!r}. Edit the spec, not these files.",
        f"name: {project}",
        "version: '1.0.0'",
        "config-version: 2",
        f"profile: {project}",
        "model-paths: [models]",
        "seed-paths: [seeds]",
        "test-paths: [tests]",
        *(["snapshot-paths: [snapshots]"] if any(n.type == "target" and n.params.get("mode") == "scd2"
                                                 for n in pipeline.nodes) else []),
        "models:",
        f"  {project}:",
        "    +materialized: ephemeral  # steps compile into their targets' queries",
        *(["vars:"] + [f"  {k}: {json.dumps(v)}" for k, v in variables.items()] if variables else []),
        "",
    ])
    if adapter == "duckdb":
        files["profiles.yml"] = "\n".join([
            "# A local DuckDB profile so the project runs as is: dbt build --profiles-dir .",
            f"{project}:",
            "  target: dev",
            "  outputs:",
            "    dev:",
            "      type: duckdb",
            f"      path: {project}.duckdb",
            "",
        ])

    # ------------------------------------------------------------------ sources and seeds
    table_sources = [n for n in pipeline.nodes if n.type == "source" and n.params["format"] == "table"]
    if table_sources:
        lines = ["version: 2", "sources:", f"  - name: {SOURCE_GROUP}",
                 "    description: Tables the pipeline reads. Set schema (and database) to where they live.",
                 "    schema: \"{{ target.schema }}\"",
                 "    tables:"]
        for n in table_sources:
            schema, name = _table_name(n.params["table"])
            lines.append(f"      - name: {name}")
            if schema:
                lines.append(f"        schema: {schema}")
            if n.params.get("columns"):
                lines.append("        columns:")
                lines += [f"          - name: {c}\n            data_type: {t}" for c, t in n.params["columns"].items()]
        files["models/sources.yml"] = "\n".join(lines) + "\n"

    seeds = [n for n in pipeline.nodes if n.type == "source" and n.params["format"] == "csv"]
    if seeds:
        lines = ["version: 2", "seeds:"]
        for n in seeds:
            lines.append(f"  - name: {n.id}")
            lines.append(f"    description: {_yaml_str(_jinja_literal('Loaded from ' + _one_line(n.params['path'])))}")
            if n.params.get("columns") or n.params.get("delimiter"):
                lines.append("    config:")
            if n.params.get("delimiter"):
                lines.append(f"      delimiter: {json.dumps(n.params['delimiter'])}")
            if n.params.get("columns"):
                lines.append("      column_types:")
                lines += [f"        {c}: {exp.DataType.build(t).sql(dialect)}" for c, t in n.params["columns"].items()]
            if base_dir is not None:
                path = Path(base_dir) / n.params["path"]
                if path.exists():
                    files[f"seeds/{n.id}.csv"] = path.read_text(encoding="utf-8")
        files["seeds/seeds.yml"] = "\n".join(lines) + "\n"

    for n in pipeline.nodes:
        if n.type == "source" and n.params["format"] in ("parquet", "json"):
            path, fmt = n.params["path"], n.params["format"]
            read = (f"read_{'parquet' if fmt == 'parquet' else 'json_auto'}({_literal(path)})" if dialect == "duckdb"
                    else f"{fmt}.`{path.replace('`', '``')}`")
            view = "{{ config(materialized='view') }}\n" if n.expect else ""
            files[f"models/{model_name[n.id]}.sql"] = f"-- {n.id}: source ({fmt})\n{view}SELECT * FROM {_jinja_literal(read)}\n"

    # ------------------------------------------------------------------ steps and targets
    docs = ["version: 2", "models:"]
    for node in templated.topo_order():
        if node.type == "source":
            continue
        if node.type != "target":
            view = "{{ config(materialized='view') }}  -- it has data tests, which need a relation\n" \
                if node.expect else ""
            files[f"models/{model_name[node.id]}.sql"] = f"-- {node.id}: {node.type}\n{view}{model_sql(node)}\n"
            documented = node.id
        else:
            p = node.params
            config: list[str] = []
            mode = p.get("mode")
            if mode == "scd2":
                files[f"snapshots/{model_name[node.id]}.sql"] = _snapshot(node, model_name[node.id], relation(node.inputs[0]),
                                                                          cols.get(node.inputs[0]) or [], adapter)
                continue
            history_note = ""
            if mode in ("scd1", "insert_new", "insert_changed"):
                keys = list(p["keys"])
                if mode == "scd1":
                    strategy = "merge" if adapter in ("databricks", "spark", "snowflake", "bigquery") else "delete+insert"
                    config += ["materialized='incremental'", f"unique_key={keys!r}", f"incremental_strategy='{strategy}'"]
                    sk = p.get("surrogate_key")
                    if sk and sk not in (cols.get(node.inputs[0]) or []):
                        history_note = (f"-- Redcliff generates {sk}; dbt keys these rows by {', '.join(keys)} instead "
                                        "(add a surrogate key with dbt_utils.generate_surrogate_key if you need one).\n")
                else:
                    config += ["materialized='incremental'", "incremental_strategy='append'"]
            elif mode == "merge":
                strategy = "merge" if adapter in ("databricks", "spark", "snowflake", "bigquery") else "delete+insert"
                config += ["materialized='incremental'", f"unique_key={list(p['keys'])!r}",
                           f"incremental_strategy='{strategy}'"]
            elif p["format"] == "table":
                config.append("materialized='incremental'" if p.get("mode") == "append" else "materialized='table'")
            if p["format"] == "table":
                if model_name[node.id] != _table_name(p["table"])[1]:
                    config.append(f"alias='{_table_name(p['table'])[1]}'")
                if _table_name(p["table"])[0]:
                    config.append(f"schema='{_table_name(p['table'])[0]}'")
            elif adapter == "duckdb":
                config += ["materialized='external'", f"location={p['path']!r}", f"format='{p['format']}'"]
                folder = Path(pipeline.node(node.id).params["path"]).parent  # with variables at their defaults
                if str(folder) not in ("", ".") and not folder.is_absolute() and ".." not in folder.parts:
                    files[f"{folder.as_posix()}/.gitkeep"] = ""  # DuckDB writes files but not folders
            else:
                config.append("materialized='table'")
            for key, hook in (("pre_sql", "pre_hook"), ("post_sql", "post_hook")):
                statements = [_jinja_literal(sqlglot.transpile(s, write=dialect)[0]) for s in p.get(key) or []]
                if statements:
                    config.append(f"{hook}={statements!r}")
            note = "" if p["format"] == "table" or adapter == "duckdb" else \
                f"-- Redcliff writes {_jinja_literal(_one_line(p['path']))}; dbt materializes a table here. " \
                "Unload it with your platform's tools.\n"
            body = f"SELECT * FROM {relation(node.inputs[0])}\n"
            note += history_note
            if mode in ("insert_new", "insert_changed"):
                body = f"SELECT * FROM {relation(node.inputs[0])} AS incoming\n" + _insert_filter(p)
            if p.get("mode") == "merge" and p.get("delete_when"):
                cond = _jinja_literal(sqlglot.parse_one(p["delete_when"]).sql(dialect))
                note += (f"-- Redcliff deletes matched rows where {cond}; dbt's incremental merge doesn't delete,\n"
                         "-- so those rows are left out here. Delete them with a post_hook or a separate job.\n")
                body += f"WHERE NOT ({cond})\n"
            files[f"models/{model_name[node.id]}.sql"] = (
                f"-- {node.id}: target\n{note}{{{{ config({', '.join(config)}) }}}}\n\n{body}")
            documented = node.inputs[0]
        docs.append(f"  - name: {model_name[node.id]}")
        docs.append(f"    description: {_yaml_str(f'Redcliff step {node.id} ({node.type}).')}")
        names = cols.get(documented) or []
        if names:
            docs.append("    columns:")
            for c in names:
                docs.append(f"      - name: {c}")
                docs.append(f"        description: {_yaml_str(_lineage_text(graph, documented, c))}")
    files["models/schema.yml"] = "\n".join(docs) + "\n"
    # Expectations become singular data tests: each returns the rows that break it.
    for e in expectations(pipeline):
        node = by_id[e.node]
        rel = relation(e.node) if node.type != "target" else f"{{{{ ref('{model_name[e.node]}') }}}}"
        sql = _jinja_literal(sqlglot.transpile(e.failing_sql("__rc_relation__"), write=dialect, pretty=True)[0])
        sql = re.sub(r'["`]?__rc_relation__["`]?', lambda _m, r=rel: r, sql)
        config = "{{ config(severity='warn') }}\n" if e.severity == "warn" else ""
        files[f"tests/{e.node}__{e.name}.sql"] = \
            f"-- {e.node}: expect {_jinja_literal(_one_line(e.describe()))}\n{config}{sql}\n"

    if variables:
        token = re.compile(r'["`]?__rcvar_([A-Za-z0-9_]+?)__["`]?', re.IGNORECASE)
        by_lower = {k.lower(): k for k in variables}
        files = {path: token.sub(lambda m: f'{{{{ var("{by_lower.get(m.group(1).lower(), m.group(1))}") }}}}', text)
                 for path, text in files.items()}
    return files


_HASH64 = {  # a 64-bit integer hash of a row's text, per adapter
    "duckdb": "CAST(hash({x}) % 9223372036854775807 AS BIGINT)",
    "databricks": "xxhash64({x})", "spark": "xxhash64({x})",
    "snowflake": "HASH({x})",
    "bigquery": "FARM_FINGERPRINT({x})",
    "postgres": "('x' || SUBSTR(MD5({x}), 1, 15))::BIT(60)::BIGINT",
    "redshift": "STRTOL(SUBSTRING(MD5({x}), 1, 15), 16)",
}


def _snapshot(node: Node, name: str, rel: str, columns: list[str], adapter: str = "duckdb") -> str:
    """An SCD2 target as a dbt snapshot (dbt 1.9+): check strategy on the tracked columns.

    The surrogate key is a 64-bit hash of the business key and the tracked columns: one value per
    version, which the facts look up like Redcliff's own."""
    p = node.params
    keys = list(p["keys"])
    sk = p.get("surrogate_key")
    skip = set(keys) | set(p.get("scd1") or []) | set(p.get("ignore") or []) | {sk}
    tracked = [c for c in columns if c not in skip]
    unique = keys[0] if len(keys) == 1 else keys
    lost = [what for what, on in (("type-1 overwrites", p.get("scd1")), ("the unknown member", p.get("unknown_member")))
            if on]
    select = "*"
    if sk and sk not in columns:
        text = " || '||' || ".join(f"COALESCE(CAST({c} AS VARCHAR), '')" for c in keys + tracked)
        hashed = _HASH64.get(adapter)
        select = f"{hashed.format(x=text)} AS {sk}, *" if hashed else "*"
        if not hashed:
            lost.append(f"the surrogate key {sk} (no 64-bit hash for {adapter}; dbt_scd_id identifies a version)")
    note = ("-- dbt keeps the history: a change to a tracked column closes the version (valid_to) and adds one.\n"
            + (f"-- Not in a dbt snapshot: {', '.join(lost)}.\n" if lost else "")
            + "-- First versions start when the snapshot first runs, not at the beginning of time.\n")
    config = (f"target_schema=target.schema, unique_key={unique!r}, strategy='check', check_cols={tracked!r}, "
              "snapshot_meta_column_names={'dbt_valid_from': 'valid_from', 'dbt_valid_to': 'valid_to'}")
    return (f"-- {node.id}: target (SCD2)\n{note}{{% snapshot {name} %}}\n{{{{ config({config}) }}}}\n\n"
            f"SELECT {select} FROM {rel}\n{{% endsnapshot %}}\n")


def _insert_filter(p: dict) -> str:
    """Incremental runs insert only new keys (insert_new) or rows that changed since the latest (insert_changed)."""
    keys = list(p["keys"])
    on = " AND ".join(f"existing.{k} = incoming.{k}" for k in keys)
    if p["mode"] == "insert_new":
        existing = "{{ this }} AS existing"
        cond = on
    else:
        part = ", ".join(keys)
        existing = (f"(SELECT {part}, {p['compare']}, ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {p['order_by']} DESC) "
                    "AS rn FROM {{ this }}) AS existing")
        cond = f"existing.rn = 1 AND {on} AND existing.{p['compare']} = incoming.{p['compare']}"
    return ("{% if is_incremental() %}\n"
            f"WHERE NOT EXISTS (SELECT 1 FROM {existing} WHERE {cond})\n"
            "{% endif %}\n")


def project_path(out_dir: str | Path, rel: str) -> Path:
    """Where a generated project file goes; refuses names that would land outside ``out_dir``."""
    root = Path(out_dir).resolve()
    path = (root / rel).resolve()
    if Path(rel).is_absolute() or (path != root and root not in path.parents):
        raise ValueError(f"refusing to write {rel!r}: it is outside {out_dir}")
    return path


def write_dbt_project(files: dict[str, str], out_dir: str | Path) -> list[Path]:
    """Write a project from :func:`compile_dbt` into ``out_dir``. Returns the paths written."""
    written = []
    for rel in files:
        project_path(out_dir, rel)  # check every name before writing anything
    for rel, content in files.items():
        path = project_path(out_dir, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
