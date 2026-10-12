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
const powercenter = readFileSync(join(repo, "examples", "legacy", "powercenter", "m_customer_revenue.xml"), "utf8");
writeFileSync(join(samples, "samples.json"), JSON.stringify({ tables, specs, legacy, powercenter }, null, 2));

console.log(`engine ready: pyodide, ${wheel}, redcliff.zip, ${tables.length} sample tables, ${Object.keys(specs).length} specs`);
