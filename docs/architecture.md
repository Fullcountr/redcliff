# Architecture

Redcliff has one source of truth: the **pipeline spec**, a YAML/JSON document stored in Git.

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

## Compiler output

| Target | What you get | Notes |
| --- | --- | --- |
| DuckDB SQL | A CTE per node; `COPY ... TO` or `CREATE TABLE` per target | Output folders must exist first |
| Spark SQL | A CTE per node; `INSERT OVERWRITE` per target | File sources are registered as temp views named after their node |
| PySpark | A DataFrame script, one variable per node | Uses `withColumns`, so Spark 3.3 or later |

Golden tests run the DuckDB output on the sample data. Spark SQL is checked by parsing and
PySpark by compiling; running them against real Spark is a later CI addition.
