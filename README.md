# Redcliff

**Legacy ETL in, clean Spark out.**

Redcliff is an open-source visual pipeline designer with lineage built in.
Paste legacy SQL (and soon, import Informatica PowerCenter mappings), see it as an
editable graph with column-level lineage, preview it on sample data, and generate
clean Spark SQL, PySpark or DuckDB SQL that you own.

> Status: early development. The engine, CLI and browser playground work; the PowerCenter
> importer is next. Not ready for production use yet.

## Why

- Every serious visual-to-code ETL tool is paid or locked to one platform.
- Lineage is usually reverse-engineered after the fact. Redcliff owns the graph, so
  column-level lineage comes by construction.
- The first release runs entirely in your browser: no install, no account, and your
  data never leaves your machine.

## How it works

```
Canvas / SQL importer  ->  Pipeline spec (YAML, in Git)  ->  Compiler -> Spark SQL, PySpark, DuckDB SQL
                                                         ->  Lineage  -> column graph, OpenLineage events
```

The pipeline spec is the single source of truth. The canvas edits it; code and lineage
are generated from it.

## Playground

The web app runs the whole engine in your browser: Python (via Pyodide) for the pipeline
logic and DuckDB-Wasm for previews. Nothing is uploaded.

- Paste legacy SQL (Teradata, Oracle, SQL Server, Snowflake and more) and get an editable pipeline.
- Hover a column to trace it back to its sources; click it to see its blast radius.
- Preview every step's rows, edit steps, add your own CSV files.
- Generate Spark SQL, PySpark or DuckDB SQL, and share a pipeline as a link.

```bash
cd web && npm install && npm run dev   # needs Python 3 with pip for the engine build step
```

## Try the CLI

```bash
cd core && pip install -e ".[cli]"

# Turn a legacy Teradata BTEQ job into a pipeline
redcliff import ../examples/legacy/teradata_revenue.sql --dialect teradata \
  --schema ../examples/legacy/schema.json -o pipeline.yaml

redcliff validate pipeline.yaml                       # structure and column checks
redcliff compile  pipeline.yaml --to spark            # or duckdb, pyspark
redcliff lineage  pipeline.yaml --impact orders.status
redcliff run      pipeline.yaml --db warehouse.duckdb # run it locally on DuckDB
```

The schema file maps each table to its columns (see `examples/legacy/schema.json`). It is
optional, but it makes lineage and join handling exact. `run --db` expects a DuckDB file that
already holds the source tables.

## Repository layout

| Folder | What it holds |
| --- | --- |
| `core/` | Python package `redcliff`: spec model, compiler, lineage, SQL importer. Pure Python so it also runs in the browser via Pyodide. |
| `web/` | Browser app: Vite, React, TypeScript, React Flow, Pyodide and DuckDB-Wasm. |
| `examples/` | Small generated sample datasets and example pipeline specs. |
| `docs/` | Spec reference and architecture notes. |
| `spikes/` | Throwaway experiments that proved an approach. |

## Development

```bash
# core
cd core
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest

# web
cd web
npm install
npm run dev
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
