// The README's screenshots of the playground and the VS Code webview, in light and dark.
//
//   APP_URL=http://localhost:4173/ REDCLIFF=redcliff CHROMIUM_PATH=/path/to/chrome \
//     node readme-shots.mjs ../../docs/images
//
// 1600 x 1000 PNGs (1280 x 800 at 1.25x), named <shot>-light.png and <shot>-dark.png, like the
// existing ones. Needs the built playground (vite preview) and the built webview (vscode/media).
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(process.argv[2] || resolve(here, "../../docs/images"));
const url = process.env.APP_URL || "http://localhost:4173/";
const size = { viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1.25 };
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  headless: true,
});

async function playground(theme) {
  const context = await browser.newContext({ ...size, colorScheme: theme });
  await context.addInitScript((t) => localStorage.setItem("redcliff-theme", t), theme);
  const page = await context.newPage();
  await page.goto(url);
  await page.waitForFunction(() => /Engine ready/.test(document.body.textContent ?? ""), null, { timeout: 120000 });
  await page.addStyleTag({ content: ".toast { display: none !important; }" });
  return page;
}

async function examples(page, item) {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

async function shot(page, name, theme) {
  await page.mouse.move(2, 790);
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(out, `${name}-${theme}.png`) });
  console.log(`wrote ${name}-${theme}.png`);
}

for (const theme of ["light", "dark"]) {
  // The Changes tab after an edit.
  let page = await playground(theme);
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="shipped"] .step-rows')?.textContent);
  await page.locator('.react-flow__node[data-id="shipped"] .step-id').click();
  await page.getByRole("tab", { name: "Step" }).click();
  await page.locator("textarea.code-input").fill("condition: amount > 50");
  await page.getByRole("button", { name: "Apply changes" }).click();
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="shipped"] .step-rows')?.textContent === "5 rows");
  await page.getByRole("tab", { name: "Changes" }).click();
  await page.waitForSelector('[data-testid="changes-rows"]');
  await shot(page, "changes", theme);

  // A data model.
  await examples(page, /^sales Kimball/);
  await page.waitForSelector('[data-testid="model-valid"]', { timeout: 30000 });
  await page.waitForFunction(() => document.querySelectorAll(".react-flow__edge.rel").length === 2);
  await shot(page, "data-model", theme);
  await page.context().close();

  // A legacy job proposed as a model, with what the model covers.
  page = await playground(theme);
  await examples(page, /PowerCenter mapping/);
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.getByRole("tab", { name: /Migration/ }).click();
  await page.locator('[data-testid="model-from-job"]').getByRole("button", { name: "Kimball model" }).click();
  await page.waitForFunction(() => /13 of 13/.test(document.querySelector('[data-testid="job-map-summary"]')?.textContent ?? ""),
    null, { timeout: 30000 });
  await page.evaluate(() => document.querySelector('[data-testid="job-map"]')?.scrollIntoView());
  await shot(page, "model-from-job", theme);
  await page.context().close();
}

// The VS Code webview, with a real `redcliff bridge`, a column's lineage traced.
const media = resolve(here, "../../vscode/media");
const spec = readFileSync(resolve(here, "../../examples/specs/orders_revenue.yaml"), "utf8");
const [program, ...args] = (process.env.REDCLIFF || "redcliff").split(" ");
const bridge = spawn(program, [...args, "bridge"]);
const waiting = new Map();
let nextId = 1;
let bridgeReady;
const ready = new Promise((r) => { bridgeReady = r; });
createInterface({ input: bridge.stdout }).on("line", (line) => {
  const msg = JSON.parse(line);
  if (msg.ready) return bridgeReady();
  waiting.get(msg.id)?.(msg.result);
  waiting.delete(msg.id);
});
const call = async (fn, callArgs) => {
  await ready;
  const id = nextId++;
  return new Promise((r) => { waiting.set(id, r); bridge.stdin.write(JSON.stringify({ id, fn, args: callArgs }) + "\n"); });
};
const types = { ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2" };
const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  if (path === "/") {
    const theme = new URL(req.url, "http://x").searchParams.get("theme");
    res.writeHead(200, { "content-type": "text/html" });
    res.end(`<!doctype html><html><head><meta charset="UTF-8"><link rel="stylesheet" href="/webview.css"></head>
<body class="vscode-${theme}"><div id="root"></div><script>window.acquireVsCodeApi = () => ({ postMessage: (m) => window.__post(m) });</script>
<script type="module" src="/webview.js"></script></body></html>`);
    return;
  }
  try {
    const body = readFileSync(join(media, path));
    res.writeHead(200, { "content-type": types[extname(path)] || "application/octet-stream" });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
}).listen(0);
for (const theme of ["light", "dark"]) {
  const page = await browser.newPage(size);
  await page.exposeFunction("__post", async (msg) => {
    if (msg.type === "ready") {
      await page.evaluate((text) => window.postMessage({ type: "spec", text, file: "specs/orders_revenue.yaml" }, "*"), spec);
    } else if (msg.type === "call") {
      const result = await call(msg.fn, msg.args);
      await page.evaluate((m) => window.postMessage(m, "*"), { type: "result", id: msg.id, result });
    }
  });
  await page.goto(`http://localhost:${server.address().port}/?theme=${theme}`);
  await page.waitForSelector('[data-testid="vs-valid"]', { timeout: 30000 });
  await page.getByRole("tab", { name: "Code" }).click();
  await page.waitForFunction(() => /FROM/.test(document.querySelector('[data-testid="vs-code"]')?.textContent ?? ""));
  await page.locator('.react-flow__node[data-id="revenue_by_region"] button.col', { hasText: "revenue" }).first().hover();
  await page.waitForSelector('.react-flow__node[data-id="orders"] button.col.is-lit');
  await page.waitForTimeout(300);
  await page.screenshot({ path: join(out, `vscode-${theme}.png`) });
  console.log(`wrote vscode-${theme}.png`);
  await page.close();
}
server.close();
bridge.kill();
await browser.close();
