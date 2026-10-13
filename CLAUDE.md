# Redcliff: notes for Claude

Redcliff is an Apache 2.0 visual data pipeline designer. Tagline: "Legacy ETL in, clean Spark out."

## Architecture rules (non-negotiable)

- The pipeline spec (YAML/JSON) is the single source of truth. The canvas edits the spec;
  code and lineage are generated from it. Never parse generated code back into the canvas.
- `core/` must stay pure Python. It runs in CPython and in Pyodide in the browser.
  Ask before adding any dependency to `core/` that is not pure Python.
- Use sqlglot for parsing, transpiling and expression analysis.
- Use dataclasses + JSON Schema for the spec, not compiled libraries such as pydantic-core.

## Spec v0

Node types: source (csv, parquet, table), filter, select, derive, join, aggregate, union, target.
Each node has: id, type, inputs (node ids), params. Expressions are SQL strings.

## Compiler and lineage

- Compile a spec to DuckDB SQL and Spark SQL as a chain of named CTEs, one per node,
  plus PySpark DataFrame code. Output must be readable: stable names, a comment per node.
- Lineage marks each output column's inputs as DIRECT (derived from) or INDIRECT
  (filters, join keys, group-bys). Export as the OpenLineage ColumnLineageDatasetFacet.
- The SQL importer turns unsupported constructs into a raw SQL node plus a warning,
  never a silent drop.

## Engineering rules

- Clean room: no employer code, schemas or data anywhere in this repo. Sample data is
  generated or from public datasets.
- Golden tests: compiled DuckDB SQL runs on sample data and must match expected results.
  Lineage tests compare against expected column maps.
- Keep changes small and reviewable. Run `pytest` in `core/` and `npm run build` in `web/`
  before committing.

## Layout

- `core/src/redcliff/`: the Python package. Tests in `core/tests/`.
- `web/`: Vite + React + TypeScript app (`npm run build:vscode` builds the VS Code webview).
- `vscode/`: the VS Code extension; it runs `redcliff bridge`. `npm test` and `npm run build` there.
- `examples/`, `docs/`, `spikes/`: see README.
