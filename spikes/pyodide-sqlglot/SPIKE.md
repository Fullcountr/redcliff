# Spike: sqlglot inside Pyodide

**Result: pass.** Redcliff's core can run fully in the browser.

Run on Oct 11, 2026 with Node 22, Pyodide 314.0.7 and sqlglot 30.22.0.

## Timings (Node, files already on disk)

| Step | Time |
| --- | --- |
| Load Pyodide | 2.4 s |
| Install sqlglot wheel | 0.1 s |
| First import + 3 operations | 1.4 s |
| Total | 3.9 s |

A first browser visit will also download Pyodide from its CDN, so expect a few extra
seconds there. Show a loading state, and cache the runtime with a service worker.

## What was proven

1. **Transpile:** a Teradata query became clean, pretty-printed Spark SQL.
2. **Dialect quirks:** Teradata `TOP 5 ... QUALIFY` became DuckDB `QUALIFY ... LIMIT 5`.
3. **Column lineage:** `revenue` traced back to `o.amount` (direct dependency).
4. **Indirect lineage input:** every referenced column was found, including join keys
   and filter columns (`c.id`, `o.customer_id`, `o.status`).

## Notes for the build

- sqlglot is a pure-Python wheel, so it can be unzipped straight into Pyodide's
  site-packages. `micropip.install("sqlglot")` is the normal route in the browser.
- Direct vs indirect lineage needs our own pass: sqlglot's `lineage()` gives direct
  sources; filters, join keys and group-bys come from walking the AST.

## How to rerun

```bash
npm install pyodide
pip download sqlglot --no-deps --only-binary=:all: -d wheels
node spike.mjs
```
