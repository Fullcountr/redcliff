# redcliff

The Python engine behind [Redcliff](https://github.com/Fullcountr/redcliff), an open-source visual
pipeline tool with lineage built in: **legacy ETL in, clean Spark out.**

```text
pip install "redcliff[cli]"
```

- **Import** legacy SQL (11 dialects, BTEQ scripts) and Informatica PowerCenter exports (mappings,
  mapplets, sessions, workflows, parameter files) into a small YAML pipeline spec, with a
  migration scorecard.
- **Compile** the spec to Spark SQL, PySpark, DuckDB SQL, a dbt project or a Lakeflow
  Declarative Pipeline.
- **Trace** column-level lineage, a column's blast radius, and the source rows behind any row;
  export OpenLineage.
- **Check** data quality expectations, **diff** two versions of a pipeline, and let AI agents
  edit specs through an **MCP server** that shows a diff before anything is written.

```text
redcliff import job.sql --dialect teradata -o pipeline.yaml
redcliff import wf_export.xml --params wf.par -o pipeline.yaml --scorecard scorecard.md
redcliff compile pipeline.yaml --to spark          # or pyspark, duckdb, dbt, lakeflow
redcliff lineage pipeline.yaml --impact orders.status
redcliff test pipeline.yaml                        # data quality expectations, in DuckDB
redcliff run pipeline.yaml --var start_date=2026-09-01
redcliff diff --git origin/main --data
redcliff mcp --root .
```

The core depends only on [sqlglot](https://github.com/tobymao/sqlglot), so it also runs in the
browser through Pyodide: try the [playground](https://fullcountr.github.io/redcliff/). The `cli`
extra adds PyYAML and DuckDB for running pipelines locally.

Documentation: [spec](https://github.com/Fullcountr/redcliff/blob/main/docs/spec.md),
[outputs](https://github.com/Fullcountr/redcliff/blob/main/docs/outputs.md),
[PowerCenter](https://github.com/Fullcountr/redcliff/blob/main/docs/powercenter.md),
[pull request bot](https://github.com/Fullcountr/redcliff/blob/main/docs/pr-bot.md),
[MCP server](https://github.com/Fullcountr/redcliff/blob/main/docs/mcp.md).
Apache 2.0.
