# Outputs: code, dbt projects and Lakeflow pipelines

The spec compiles to five outputs. All of them are generated: edit the spec, not the output.

| `--to` | You get | Runs on |
| --- | --- | --- |
| `spark` | one Spark SQL script: a CTE per step, `INSERT OVERWRITE` / `INSERT INTO` per target | any Spark 3.4+ / Databricks SQL |
| `pyspark` | one PySpark script: a DataFrame per step | any Spark |
| `duckdb` | one DuckDB SQL script | DuckDB (what the playground previews with) |
| `dbt` | a dbt project | dbt-core with any adapter Redcliff writes SQL for |
| `lakeflow` | a Lakeflow Declarative Pipeline (`pyspark.pipelines`) | Databricks; open-source Spark 4.1+ (`spark-pipelines run`) |

```text
$ redcliff compile pipeline.yaml --to dbt -o revenue_dbt
wrote 9 files to revenue_dbt
$ redcliff compile pipeline.yaml --to lakeflow --storage s3a://bucket/checkpoints -o revenue_lakeflow
wrote 2 files to revenue_lakeflow
```

Without `-o`, project outputs print every file to stdout, each under a `# ==== path` header.
The playground's **Code** tab shows the same projects file by file and downloads them as a zip.

## dbt

```text
dbt_project.yml          every model ephemeral unless it's a target
profiles.yml             DuckDB only: a local profile so the project runs as is
seeds/orders.csv         CSV sources become seeds (copied in when the CLI can find them)
models/sources.yml       table sources become source('raw', ...)
models/int_<step>.sql    one ephemeral model per step
models/<table>.sql       one model per target, named after its table
models/schema.yml        every model's columns, described with their lineage
```

| Spec | dbt |
| --- | --- |
| table source | `{{ source('raw', 'orders') }}`; `schema` defaults to `{{ target.schema }}`, so set it to where the tables live |
| CSV source | a seed with the spec's column types |
| Parquet source | a model reading the file (`read_parquet` on DuckDB, `` parquet.`path` `` elsewhere) |
| step | an ephemeral `int_` model, so dbt compiles the chain into one query per target |
| target, `mode: overwrite` | `materialized='table'` (a `schema.table` name sets `schema=`) |
| target, `mode: append` | `materialized='incremental'` |
| file target | `materialized='external'` on dbt-duckdb; elsewhere a table, with a comment |
| `pre_sql` / `post_sql` | `pre_hook` / `post_hook`, transpiled to the adapter's dialect |
| column lineage | each column's `description`, e.g. *From orders.amount (aggregation). Shaped by orders.status.* |

`--adapter` picks the dialect: `duckdb` (default on the CLI), `databricks`, `spark`, `snowflake`,
`bigquery`, `postgres`, `redshift`. The playground generates for Databricks.

## Lakeflow Declarative Pipelines

```text
spark-pipeline.yml                 name, storage (checkpoints) and the transformations folder
transformations/<pipeline>.py      the datasets
```

| Spec | Lakeflow |
| --- | --- |
| table source | read in place with `spark.read.table(...)` |
| CSV / Parquet source | a `@dp.temporary_view` reading the file |
| step | a `@dp.temporary_view` returning a DataFrame (the same code `--to pyspark` writes) |
| target, table | a `@dp.materialized_view` named after the table |
| file target | a materialized view named after the target step, with a note to export it |
| `pre_sql` / `post_sql` | listed at the top of the module as job tasks to run around the pipeline |

`storage` must be an absolute URI. Pass `--storage`; without it the YAML gets a local
`file:///tmp/...` placeholder with a comment. A step named like a table it feeds gets a `_step`
suffix, because datasets and tables share one namespace in a pipeline.

## How they're tested

CI builds every example (hand-written specs, the imported Teradata job and the imported
PowerCenter workflow) both ways and compares every target, row for row, with the compiled DuckDB
query:

- **dbt**: `dbt build` with dbt-duckdb, seeds, hooks and external file targets included.
- **Lakeflow**: `spark-pipelines run` on a local Spark 4, then the materialized views are read
  back from the warehouse folder.
