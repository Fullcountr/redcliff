// Vendors everything the browser engine needs into public/, so the app runs offline and on
// static hosting with no CDN: Pyodide's core files, the sqlglot wheel, the redcliff package
// and the sample data. Run automatically before `npm run dev` and `npm run build`.
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import * as yaml from "js-yaml";

const web = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repo = resolve(web, "..");
const pub = join(web, "public");
const python = process.env.PYTHON || "python3";

function fresh(dir) {
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
  return dir;
}

// 1. Pyodide core (no package downloads at runtime: we install wheels ourselves)
const pyo = fresh(join(pub, "pyodide"));
for (const f of ["pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"]) {
  copyFileSync(join(web, "node_modules", "pyodide", f), join(pyo, f));
}

// 2. The redcliff package as a zip with redcliff/ at its root
const engine = fresh(join(pub, "engine"));
execFileSync(python, ["-c", `
import pathlib, zipfile
src = pathlib.Path(${JSON.stringify(join(repo, "core", "src"))})
with zipfile.ZipFile(${JSON.stringify(join(engine, "redcliff.zip"))}, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted((src / "redcliff").rglob("*")):
        if f.suffix in (".py", ".json"):
            z.write(f, f.relative_to(src).as_posix())
`]);

// 3. The sqlglot wheel (pure Python), same major version the core requires
const tmp = fresh(join(engine, "tmp"));
execFileSync(python, ["-m", "pip", "download", "sqlglot>=30", "--no-deps", "--only-binary=:all:",
  "--quiet", "-d", tmp], { stdio: "inherit" });
const wheel = readdirSync(tmp).find((f) => f.startsWith("sqlglot-") && f.endsWith(".whl"));
renameSync(join(tmp, wheel), join(engine, "sqlglot.whl"));
rmSync(tmp, { recursive: true, force: true });
writeFileSync(join(engine, "manifest.json"), JSON.stringify({ sqlglot: wheel }, null, 2));

// 4. Samples: CSVs, example specs as JSON, and the legacy Teradata job
const samples = fresh(join(pub, "samples"));
const data = join(repo, "examples", "data");
const tables = [];
for (const f of readdirSync(data).filter((f) => f.endsWith(".csv"))) {
  copyFileSync(join(data, f), join(samples, f));
  tables.push(f);
}
const specsDir = join(repo, "examples", "specs");
const specs = {};
for (const f of readdirSync(specsDir).filter((f) => f.endsWith(".yaml"))) {
  const spec = yaml.load(readFileSync(join(specsDir, f), "utf8"));
  specs[spec.name] = spec;
}
const legacy = readFileSync(join(repo, "examples", "legacy", "teradata_revenue.sql"), "utf8");
const pcDir = join(repo, "examples", "legacy", "powercenter");
const powercenter = readFileSync(join(pcDir, "m_customer_revenue.xml"), "utf8");
const workflow = { xml: readFileSync(join(pcDir, "wf_customer_revenue.xml"), "utf8"),
  params: readFileSync(join(pcDir, "wf_customer_revenue.par"), "utf8") };
const storeMart = readFileSync(join(pcDir, "wf_store_mart.xml"), "utf8");
const ssis = readFileSync(join(repo, "examples", "legacy", "ssis", "LoadRegionRevenue.dtsx"), "utf8");
writeFileSync(join(samples, "samples.json"), JSON.stringify({ tables, specs, legacy, powercenter, workflow, storeMart, ssis }, null, 2));

// 5. Precomputed lineage, row counts and blast radius for the examples, so the canvas is
//    interactive while Python and DuckDB load. Optional: needs sqlglot and duckdb in $PYTHON.
let precomputed = "skipped (install core/[cli] in $PYTHON to enable)";
try {
  execFileSync(python, [join(web, "scripts", "precompute.py"), samples, join(samples, "precomputed.json")], {
    env: { ...process.env, PYTHONPATH: join(repo, "core", "src") },
    stdio: ["ignore", "pipe", "pipe"],
  });
  precomputed = "precomputed examples";
} catch (e) {
  writeFileSync(join(samples, "precomputed.json"), "{}");
  console.warn(`precompute ${precomputed}: ${String(e.stderr || e).split("\n").filter(Boolean).pop()}`);
}

console.log(`engine ready: pyodide, ${wheel}, redcliff.zip, ${tables.length} sample tables, ${Object.keys(specs).length} specs, ${precomputed}`);
