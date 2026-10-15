"""Generate a Lakeflow Declarative Pipeline (Spark Declarative Pipelines) from a pipeline spec.

The code uses ``pyspark.pipelines``, the open API that Databricks Lakeflow Declarative Pipelines
and open-source Spark (``spark-pipelines run``) share:

- each step is a ``@dp.temporary_view``; each target a ``@dp.materialized_view`` named after its
  table, so the platform tracks dependencies, retries and lineage
- table sources are read where they are; CSV and Parquet sources become temporary views
- pre/post SQL and file targets have no declarative form, so they're noted for a job task

The output is {relative path: content}: ``spark-pipeline.yml`` and one Python file.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from redcliff.columns import infer_columns
from redcliff.expect import expectations
from redcliff.compile import spark_reader, variables_note, _dataframe, _doc_text, _one_line, _path, _statement, _transpile
from redcliff.spec import Pipeline, bind


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "pipeline"
    return s if s[0].isalpha() else "p_" + s


def compile_lakeflow(pipeline: Pipeline, base_dir: str | Path | None = None,
                     storage: str | None = None) -> dict[str, str]:
    """{relative path: content}. ``storage`` is the checkpoint root, an absolute URI such as
    ``file:///tmp/x`` or ``s3a://bucket/x``; without one the YAML gets a local placeholder."""
    variables = dict(pipeline.variables)
    pipeline = bind(pipeline)
    cols = infer_columns(pipeline)
    name = _slug(pipeline.name)

    # Dataset names share one namespace with the target tables, so a step named like a table it
    # feeds (regional_revenue -> table regional_revenue) gets a suffix.
    tables = {n.params["table"] for n in pipeline.nodes if n.type == "target" and n.params["format"] == "table"}
    dataset = {n.id: f"{n.id}_step" if n.type != "target" and n.id in tables and
               not (n.type == "source" and n.params.get("table") == n.id) else n.id for n in pipeline.nodes}

    def read(node_id: str) -> str:
        # Every step reads its inputs by name: a dataset defined here, or a table read in place.
        return f"spark.read.table({dataset[node_id]!r})"

    def sql_query(node) -> str:
        renamed = {i: dataset[i] for i in node.inputs if dataset[i] != i}
        tree = sqlglot.parse_one(_transpile(node.params["query"], "spark"), read="spark")
        if renamed:
            tree = tree.transform(lambda e: exp.Table(this=exp.to_identifier(renamed[e.name]),
                                                      alias=exp.TableAlias(this=exp.to_identifier(e.alias or e.name)))
                                  if isinstance(e, exp.Table) and not e.db and e.name in renamed else e)
        return tree.sql("spark")

    lines = [
        f'"""Redcliff pipeline: {_doc_text(pipeline.name)}, as a Lakeflow Declarative Pipeline.',
        "",
        "Generated from the pipeline spec; edit the spec, not this file. Runs on Databricks Lakeflow",
        'Declarative Pipelines and on open-source Spark 4.1+ ("spark-pipelines run").',
        *([""] + variables_note(variables, "").rstrip("\n").splitlines() if variables else []),
        '"""',
        "",
        "from pyspark import pipelines as dp",
        "from pyspark.sql import DataFrame",
        "from pyspark.sql import functions as F",
    ]
    job_notes: list[str] = []
    checks = {}
    for e in expectations(pipeline):
        checks.setdefault(e.node, []).append(e)

    def expect_lines(node) -> list[str]:
        """Row-level expectations as decorators; the rest become job notes."""
        out = []
        for e in checks.get(node.id, []):
            if e.row_level:
                cond = sqlglot.parse_one(e.condition).sql("spark")
                out.append(f"@expect({e.name!r}, {cond!r}" + (", fail=False)" if e.severity == "warn" else ")"))
            else:
                job_notes.append(f"{node.id}: check {e.describe()} ({e.name}) after the pipeline, e.g. "
                                 "with redcliff test")
        return out

    for node in pipeline.topo_order():
        p, t = node.params, node.type
        fn = node.id + "_" if node.id in ("dp", "spark", "F", "DataFrame", "expect") else node.id
        if t == "source":
            if p["format"] == "table":
                if p["table"] == node.id:
                    for e in checks.get(node.id, []):
                        job_notes.append(f"{node.id}: check {e.describe()} ({e.name}) on the table it reads")
                    continue  # read in place: steps refer to the table by this name
                body = f"spark.read.table({p['table']!r})"
            else:
                body = f"{spark_reader(p)}.{p['format']}({_path(node, base_dir)!r})"
            lines += ["", "", *expect_lines(node), f"@dp.temporary_view(name={dataset[node.id]!r})",
                      f"def {fn}() -> DataFrame:", f'    """{node.id}: source"""', f"    return {body}"]
        elif t == "target":
            src = read(node.inputs[0])
            if p.get("mode") in ("scd1", "scd2", "insert_new", "insert_changed"):
                table = f"{p['table'].split('.')[-1]}_incoming"
                what = {"scd1": "overwrite (SCD1)", "scd2": "keep history of (SCD2)", "insert_new": "insert new keys into",
                        "insert_changed": "insert changed rows into"}[p["mode"]]
                comment = (f"the rows to {what} {p['table']}; a declarative pipeline recomputes datasets, so the load "
                           "runs as a job task")
                job_notes.append(f"{node.id}: after the pipeline, {what} {p['table']} from {table} on "
                                 f"{', '.join(p['keys'])} (redcliff compile --dialect spark prints the statements)")
            elif p.get("mode") == "merge":
                table = f"{p['table'].split('.')[-1]}_incoming"
                comment = (f"the rows to merge into {p['table']} on {', '.join(p['keys'])}; a declarative pipeline "
                           "recomputes datasets, so the MERGE itself runs as a job task")
                job_notes.append(f"{node.id}: after the pipeline, MERGE {table} INTO {p['table']} on "
                                 f"{', '.join(p['keys'])}" + (f", deleting matched rows where {p['delete_when']}"
                                                              if p.get("delete_when") else ""))
            elif p["format"] == "table":
                table = p["table"]
                comment = "rows are appended in Redcliff; a materialized view recomputes them" \
                    if p.get("mode") == "append" else None
            else:
                table = node.id
                comment = f"Redcliff writes {p['path']}; export this view with a job task if you need the file"
                job_notes.append(f"{node.id}: {comment}")
            lines += ["", ""]
            if comment:
                lines.append(f"# {_one_line(comment)}")
            lines += [*expect_lines(node), f"@dp.materialized_view(name={table!r})", f"def {fn}() -> DataFrame:",
                      f'    """{node.id}: target"""', f"    return {src}"]
            for key, when in (("pre_sql", "before"), ("post_sql", "after")):
                for sql in p.get(key) or []:
                    job_notes.append(f"{node.id}: run {when} the pipeline: {_statement(sql, 'spark')}")
        else:
            if t == "sql":
                body = f"spark.sql({sql_query(node)!r})"
            else:
                body = _dataframe(node, [read(i) for i in node.inputs], cols)
            lines += ["", "", *expect_lines(node), f"@dp.temporary_view(name={dataset[node.id]!r})",
                      f"def {fn}() -> DataFrame:", f'    """{node.id}: {t}"""', f"    return {body}"]
    if checks and any(e.row_level for es in checks.values() for e in es):
        at = next(i for i, line in enumerate(lines) if line.startswith("from pyspark.sql import functions"))
        lines[at + 1:at + 1] = [
            "", "",
            "def expect(name: str, condition: str, fail: bool = True):",
            '    """A Lakeflow expectation where the platform has them (Databricks); open-source Spark 4.1',
            '    has no expectations yet, so there it changes nothing. Check with `redcliff test`."""',
            '    decorate = getattr(dp, "expect_or_fail" if fail else "expect", None)',
            "    return decorate(name, condition) if decorate else (lambda f: f)",
        ]
    if job_notes:
        at = lines.index('"""')
        lines[at:at] = ["", "Steps with no declarative form; run them as job tasks around the pipeline:"] + \
            [f"  - {_doc_text(n)}" for n in job_notes]
    files = {
        "spark-pipeline.yml": "\n".join([
            f"# Generated by Redcliff from the pipeline spec {pipeline.name!r}.",
            f"name: {name}",
            f"storage: {storage or f'file:///tmp/{name}-pipeline-storage'}"
            + ("" if storage else "  # checkpoints: an absolute URI; use cloud storage on a cluster"),
            "libraries:",
            "  - glob:",
            "      include: transformations/**",
            "",
        ]),
        f"transformations/{name}.py": "\n".join(lines) + "\n",
    }
    return files
