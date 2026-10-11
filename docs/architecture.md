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
