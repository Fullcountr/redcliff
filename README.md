# Redcliff

**Legacy ETL in, clean Spark out.**

Redcliff is an open-source visual pipeline designer with lineage built in.
Paste legacy SQL (and soon, import Informatica PowerCenter mappings), see it as an
editable graph with column-level lineage, preview it on sample data, and generate
clean Spark SQL, PySpark or DuckDB SQL that you own.

> Status: early development. Nothing here is ready for production use yet.

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
