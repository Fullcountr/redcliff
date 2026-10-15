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
  // The Python sources come as JavaScript modules (path -> text), not archives: see build-engine.mjs.
  const site = py.runPython("import site; site.getsitepackages()[0]") as string;
  for (const file of ["sqlglot.js", "redcliff.js"]) {
    let files: Record<string, string>;
    try {
      files = (await import(/* @vite-ignore */ `${base}engine/${file}`)).default;
    } catch (e) {
      const res = await fetch(`${base}engine/${file}`, { method: "HEAD" }).catch(() => null);
      if (res && [401, 403, 407, 451].includes(res.status)) {
        throw new Error(`blocked: the network refused engine/${file} (HTTP ${res.status})`);
      }
      if (!res) throw new Error(`blocked: couldn't download engine/${file}; is the network up?`);
      throw new Error(`could not load engine/${file}: ${(e as Error).message}`);
    }
    for (const [path, text] of Object.entries(files)) {
      const full = `${site}/${path}`;
      py.FS.mkdirTree(full.slice(0, full.lastIndexOf("/")));
      py.FS.writeFile(full, text);
    }
  }
  py.runPython("import importlib; importlib.invalidate_caches(); import redcliff.web");
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
