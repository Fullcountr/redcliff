# Architecture

Redcliff™ has one source of truth: the **pipeline spec**, a YAML/JSON document stored in Git.

```
Writers                      Source of truth            Readers
-------                      ---------------            -------
Canvas UI (React Flow)  -->                        -->  Compiler -> Spark SQL, PySpark, DuckDB SQL
SQL importer (sqlglot)  -->  Pipeline spec (YAML)  -->  Lineage  -> column graph, OpenLineage events
PowerCenter importer    -->                        -->  Preview  -> DuckDB-Wasm on sample rows
```

## Decisions

- **Spec, not code, is canonical.** Generated code is never parsed back into the canvas.
  Custom logic lives in dedicated SQL or Python nodes.
- **Browser first.** `core/` is pure Python so the whole engine runs in Pyodide; previews run
  in DuckDB-Wasm. Proven in `spikes/pyodide-sqlglot` (about 4 seconds to load and run).
- **Lineage by construction.** Each node's expressions are parsed with sqlglot. Direct sources
  come from `sqlglot.lineage`; indirect ones (filters, join keys, group-bys) from an AST walk.

## Browser limits

DuckDB-Wasm and Pyodide get one thread each and about 4 GB of memory. Redcliff previews on
samples; full runs happen in the user's own Spark or DuckDB environment with the generated code.

Files added in the playground stay in the browser. Delimited text (CSV, TSV, pipes; the separator
is sniffed) goes straight to DuckDB. JSON and Parquet are read in JavaScript (Parquet with
[hyparquet](https://github.com/hyparam/hyparquet)) and handed to DuckDB as CSV, because DuckDB-Wasm
would download its readers for them from extensions.duckdb.org, which work networks often block;
nested values become JSON text in previews, and the generated code reads the real format. An Excel
workbook ([read-excel-file](https://gitlab.com/catamphetamine/read-excel-file)) asks which sheets to
add; each becomes a table, read as a table by pipelines and models.

## Compiler output

| Target | What you get | Notes |
| --- | --- | --- |
| DuckDB SQL | A CTE per node; `COPY ... TO` or `CREATE TABLE` per target | Output folders must exist first |
| Spark SQL | A CTE per node; `INSERT OVERWRITE` per target | File sources are registered as temp views named after their node |
| PySpark | A DataFrame script, one variable per node | Uses `withColumns`, so Spark 3.3 or later |

Golden tests run the DuckDB output on the sample data. Spark SQL is checked by parsing and
PySpark by compiling; running them against real Spark is a later CI addition.

## Lineage

| Kind | Subtype | Example |
| --- | --- | --- |
| DIRECT | IDENTITY | a column passed through or selected |
| DIRECT | TRANSFORMATION | `LOWER(TRIM(email))` |
| DIRECT | AGGREGATION | `SUM(amount)` |
| INDIRECT (column) | CONDITIONAL, WINDOW | a CASE condition; a window's partition or order |
| INDIRECT (rows) | FILTER, JOIN, GROUP_BY | decides which rows a node has |

`blast_radius` answers "what if this column changes?": columns whose **values** change,
nodes whose **rows** change, and nodes that would **break** on a rename (they name the
column, or a same-named pass-through copy of it). `openlineage_facet` exports a node's
lineage as an OpenLineage ColumnLineageDatasetFacet 1-2-0.
