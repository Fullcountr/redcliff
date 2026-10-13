// End-to-end test of the VS Code extension's webview (vscode/media) in a real Chromium, with a
// real `redcliff bridge` behind it, standing in for VS Code.
//
//   REDCLIFF="redcliff" CHROMIUM_PATH=/path/to/chrome node vscode-webview.e2e.mjs
//
// The page gets the same messages VS Code sends (the spec text), and its engine calls go to the
// Python bridge through the same JSON lines the extension uses. Screenshots go to SCREENSHOT_DIR.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const here = dirname(fileURLToPath(import.meta.url));
const media = resolve(here, "../../vscode/media");
const spec = readFileSync(resolve(here, "../../examples/specs/orders_revenue.yaml"), "utf8");
const shots = process.env.SCREENSHOT_DIR;
if (shots) mkdirSync(shots, { recursive: true });

// The bridge, as the extension runs it.
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
bridge.stderr.on("data", (d) => process.stderr.write(d));
const call = async (fn, callArgs) => {
  await ready;
  const id = nextId++;
  return new Promise((r) => { waiting.set(id, r); bridge.stdin.write(JSON.stringify({ id, fn, args: callArgs }) + "\n"); });
};

// A page like the extension's: the bundle, and acquireVsCodeApi() posting to the test.
const HARNESS = `<!doctype html><html><head><meta charset="UTF-8"><link rel="stylesheet" href="/webview.css"></head>
<body class="vscode-light"><div id="root"></div><script>
window.acquireVsCodeApi = () => ({ postMessage: (m) => window.__post(m) });
</script><script type="module" src="/webview.js"></script></body></html>`;
const types = { ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2" };
const server = createServer((req, res) => {
  const path = req.url.split("?")[0];
  if (path === "/") { res.writeHead(200, { "content-type": "text/html" }); res.end(HARNESS); return; }
  try {
    const body = readFileSync(join(media, path));
    res.writeHead(200, { "content-type": types[extname(path)] || "application/octet-stream" });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
}).listen(0);
const url = `http://localhost:${server.address().port}/`;

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1200, height: 760 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
const posted = [];
await page.exposeFunction("__post", async (msg) => {
  posted.push(msg);
  if (msg.type === "ready") {
    await page.evaluate((text) => window.postMessage({ type: "spec", text, file: "specs/orders_revenue.yaml" }, "*"), spec);
  } else if (msg.type === "call") {
    const result = await call(msg.fn, msg.args);
    await page.evaluate((m) => window.postMessage(m, "*"), { type: "result", id: msg.id, result });
  }
});

let failures = 0;
async function step(name, fn) {
  const started = Date.now();
  try {
    await fn();
    console.log(`  ok   ${name} (${Date.now() - started} ms)`);
  } catch (e) {
    failures++;
    console.log(`  FAIL ${name}\n       ${String(e.message || e).split("\n").join("\n       ")}`);
  }
}

console.log(`Redcliff VS Code webview e2e (bundle in ${media})`);
await page.goto(url);

await step("the spec from the editor renders as a canvas", async () => {
  await page.waitForSelector('.react-flow__node[data-id="revenue_by_region"]', { timeout: 30000 });
  assert.equal(await page.locator(".react-flow__node").count(), 6);
  await page.waitForSelector('[data-testid="vs-valid"]');
});

await step("hovering a column traces it, clicking pins its blast radius", async () => {
  await page.locator('.react-flow__node[data-id="revenue_by_region"] button.col', { hasText: "revenue" }).first().hover();
  await page.waitForSelector('.react-flow__node[data-id="orders"] button.col.is-lit');
  await page.locator('.react-flow__node[data-id="orders"] button.col', { hasText: "status" }).first().click();
  await page.waitForSelector(".step.rows-change", { timeout: 10000 });
  if (shots) await page.screenshot({ path: `${shots}/vscode-01-canvas.png` });
});

await step("clicking a step asks the editor to reveal it", async () => {
  await page.locator('.react-flow__node[data-id="shipped"] .step-id').click();
  await page.waitForFunction(() => true);
  assert.ok(posted.some((m) => m.type === "reveal" && m.node === "shipped"), JSON.stringify(posted.slice(-3)));
});

await step("the code tab shows Spark SQL for the selected step and PySpark", async () => {
  await page.getByRole("tab", { name: "Code" }).click();
  await page.waitForFunction(() => /FROM/.test(document.querySelector('[data-testid="vs-code"]')?.textContent ?? ""),
    null, { timeout: 15000 });
  await page.getByRole("button", { name: "PySpark" }).click();
  await page.waitForFunction(() => /spark\./.test(document.querySelector('[data-testid="vs-code"]')?.textContent ?? ""),
    null, { timeout: 15000 });
});

await step("an edit with a broken column shows the issue, and broken YAML keeps the last canvas", async () => {
  const broken = spec.replace("condition: status = 'shipped'", "condition: statuss = 'shipped'");
  await page.evaluate((text) => window.postMessage({ type: "spec", text }, "*"), broken);
  await page.getByRole("tab", { name: /Issues/ }).click();
  await page.waitForFunction(() => /statuss/.test(document.querySelector('[data-testid="vs-issues"]')?.textContent ?? ""),
    null, { timeout: 15000 });
  await page.evaluate(() => window.postMessage({ type: "spec", text: "nodes: [unclosed" }, "*"));
  await page.waitForSelector(".vs-head", { timeout: 5000 });
  assert.match(await page.locator(".vs-head").innerText(), /showing the last version that parsed/);
  assert.equal(await page.locator(".react-flow__node").count(), 6);
});

await step("dark VS Code themes switch the canvas to dark", async () => {
  await page.evaluate(() => { document.body.className = "vscode-dark"; });
  await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  if (shots) await page.screenshot({ path: `${shots}/vscode-02-dark.png` });
});

assert.deepEqual(errors, [], "no page errors");
await browser.close();
server.close();
bridge.kill();
console.log(failures ? `\n${failures} failed` : "\nall passed");
process.exit(failures ? 1 : 0);
