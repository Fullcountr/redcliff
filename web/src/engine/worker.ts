/// <reference lib="webworker" />
// Runs the Redcliff Python engine in Pyodide, off the main thread.
// Everything it needs is served from this site (see scripts/build-engine.mjs); no CDN.

type PyFn = (...args: unknown[]) => string;
let web: Record<string, PyFn> | null = null;

async function boot(): Promise<number> {
  const started = performance.now();
  const base = new URL(import.meta.env.BASE_URL, self.location.origin).href;
  const { loadPyodide } = await import(/* @vite-ignore */ `${base}pyodide/pyodide.mjs`);
  const py = await loadPyodide({ indexURL: `${base}pyodide/` });
  for (const file of ["sqlglot.bin", "redcliff.bin"]) {
    let res: Response;
    try {
      res = await fetch(`${base}engine/${file}`);
    } catch {
      throw new Error(`blocked: couldn't download engine/${file}; is the network up?`);
    }
    if ([401, 403, 407, 451].includes(res.status)) {
      throw new Error(`blocked: the network refused engine/${file} (HTTP ${res.status})`);
    }
    if (!res.ok) throw new Error(`could not load engine/${file} (HTTP ${res.status})`);
    py.FS.writeFile(`/tmp/${file}`, new Uint8Array(await res.arrayBuffer()));
  }
  py.runPython(`
import site, zipfile
target = site.getsitepackages()[0]
for f in ("/tmp/sqlglot.bin", "/tmp/redcliff.bin"):
    zipfile.ZipFile(f).extractall(target)
import redcliff.web
`);
  web = py.pyimport("redcliff.web");
  return performance.now() - started;
}

const ready = boot();

ready.then(
  (ms) => self.postMessage({ type: "ready", ms }),
  (err) => self.postMessage({ type: "failed", error: err instanceof Error ? err.message : String(err) }),
);

self.onmessage = async (event: MessageEvent<{ id: number; fn: string; args: unknown[] }>) => {
  const { id, fn, args } = event.data;
  try {
    await ready;
    const raw = web![fn](...args);
    self.postMessage({ id, result: JSON.parse(raw) });
  } catch (err) {
    self.postMessage({ id, result: { ok: false, error: String(err) } });
  }
};
