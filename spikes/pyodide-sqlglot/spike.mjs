// Redcliff spike: can sqlglot load and work inside Pyodide?
import { loadPyodide } from "pyodide";

const t0 = performance.now();
const py = await loadPyodide();
const t1 = performance.now();
console.log(`Pyodide ${py.version} loaded in ${((t1 - t0) / 1000).toFixed(2)}s`);

// The sandbox blocks the Pyodide CDN, so install the pure-Python wheel by hand:
// copy it into Pyodide's virtual filesystem and unzip it into site-packages.
import { readFileSync, readdirSync } from "node:fs";
const whl = readdirSync("wheels").find((f) => f.startsWith("sqlglot-"));
py.FS.writeFile(`/tmp/${whl}`, readFileSync(`wheels/${whl}`));
py.runPython(`
import zipfile, site
zipfile.ZipFile("/tmp/${whl}").extractall(site.getsitepackages()[0])
`);
const t2 = performance.now();
console.log(`sqlglot installed in ${((t2 - t1) / 1000).toFixed(2)}s`);

const out = py.runPython(`
import json, sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage

legacy = """
SELECT c.region, SUM(o.amount) AS revenue
FROM orders o JOIN customers c ON o.customer_id = c.id
WHERE o.status = 'shipped'
GROUP BY c.region
"""

# 1. transpile across dialects
spark = sqlglot.transpile(legacy, read="teradata", write="spark", pretty=True)[0]
duck = sqlglot.transpile("SELECT TOP 5 * FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1",
                         read="teradata", write="duckdb")[0]

# 2. column lineage for 'revenue'
schema = {"orders": {"customer_id": "int", "amount": "double", "status": "text"},
          "customers": {"id": "int", "region": "text"}}
node = lineage("revenue", legacy, schema=schema)
sources = sorted({n.name for n in node.walk() if not n.downstream})

# 3. columns used anywhere (for indirect lineage: filters, join keys)
tree = sqlglot.parse_one(legacy)
cols = sorted({c.sql() for c in tree.find_all(exp.Column)})

json.dumps({"sqlglot": sqlglot.__version__, "spark": spark, "duckdb_qualify": duck,
            "revenue_sources": sources, "all_columns": cols})
`);
const t3 = performance.now();
const r = JSON.parse(out);
console.log(`sqlglot ${r.sqlglot}, first queries ran in ${((t3 - t2) / 1000).toFixed(2)}s`);
console.log("\n-- Teradata to Spark SQL --\n" + r.spark);
console.log("\n-- Teradata QUALIFY to DuckDB --\n" + r.duckdb_qualify);
console.log("\n-- Lineage of revenue -->", r.revenue_sources.join(", "));
console.log("-- All columns referenced -->", r.all_columns.join(", "));
console.log(`\nTotal: ${((t3 - t0) / 1000).toFixed(2)}s`);
