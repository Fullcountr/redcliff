// Records the README demo: a scripted ~30 second tour of the playground.
//
//   APP_URL=http://localhost:4173/ CHROMIUM_PATH=/path/to/chrome OUT_DIR=/tmp/demo node demo.mjs
//
// Writes OUT_DIR/demo.webm and OUT_DIR/trim.json (where the tour starts and ends in the video).
// scripts/make-demo-gif.sh turns them into docs/images/demo.gif.
import { mkdirSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import { chromium } from "playwright-core";

const url = process.env.APP_URL || "http://localhost:4173/";
const out = process.env.OUT_DIR || "/tmp/redcliff-demo";
const theme = process.env.THEME || "light";
const size = { width: 1200, height: 760 };
mkdirSync(out, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

// Load once without recording so the engine is cached and the video starts on a ready canvas.
const warm = await browser.newContext({ viewport: size });
const w = await warm.newPage();
await w.goto(url);
await w.waitForFunction(() => document.querySelector('[data-testid="engine-status"]')?.textContent?.includes("Engine ready"), null, { timeout: 120000 });
await warm.close();

const context = await browser.newContext({
  viewport: size,
  deviceScaleFactor: 1,
  colorScheme: theme,
  recordVideo: { dir: out, size },
});
// Headless video has no pointer, so draw one that follows the mouse.
await context.addInitScript(() => {
  addEventListener("DOMContentLoaded", () => {
    const dot = document.createElement("div");
    dot.style.cssText = "position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;" +
      "transform:translate(-100px,-100px);transition:transform 0.02s linear";
    dot.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M4 2l15 9-6.5 1.5L9.5 19z" ' +
      'fill="#1d1916" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.body.appendChild(dot);
    addEventListener("mousemove", (e) => { dot.style.transform = `translate(${e.clientX - 3}px, ${e.clientY - 2}px)`; }, true);
    addEventListener("mousedown", () => { dot.firstChild.style.transform = "scale(0.85)"; }, true);
    addEventListener("mouseup", () => { dot.firstChild.style.transform = ""; }, true);
  });
});
const page = await context.newPage();
const t0 = Date.now(); // the video starts when the page is created
const pause = (ms) => page.waitForTimeout(ms);
let at = { x: 640, y: 400 };

async function glide(locator, { dx = 0, dy = 0, steps = 28 } = {}) {
  const box = await locator.boundingBox();
  const target = { x: box.x + box.width / 2 + dx, y: box.y + box.height / 2 + dy };
  await page.mouse.move(target.x, target.y, { steps });
  at = target;
}
async function click(locator, opts) {
  await glide(locator, opts);
  await pause(120);
  await page.mouse.down();
  await page.mouse.up();
}
const col = (node, c) => page.locator(`.react-flow__node[data-id="${node}"] button.col`, { hasText: new RegExp(`^${c}$`) });

await page.goto(url);
await page.mouse.move(at.x, at.y);
await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="revenue_by_region"] .step-rows')?.textContent === "2 rows", null, { timeout: 120000 });
const start = (Date.now() - t0) / 1000;
await pause(1200);

// 1. Trace a column back to its source.
await glide(col("revenue_by_region", "revenue"));
await pause(2200);
await glide(col("revenue_by_region", "order_count"), { steps: 10 });
await pause(1200);

// 2. Blast radius of a source column.
await click(col("orders", "status"));
await pause(2800);
await page.keyboard.press("Escape");
await pause(400);

// 3. Import an Informatica PowerCenter mapping.
await click(page.locator(".topbar").getByRole("button", { name: "Import" }));
await pause(700);
await click(page.getByRole("menuitem", { name: /PowerCenter/ }));
await pause(900);
await click(page.locator("dialog").getByRole("button", { name: "Import", exact: true }));
await page.waitForSelector(".score-hero");
await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="agg_region_month"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
await pause(2200);

// 4. The scorecard: pick the item that needs review.
await click(page.locator(".score-item", { hasText: "AGG_REGION_MONTH" }));
await pause(2400);

// 5. Zoom into the aggregate and trace revenue through the whole mapping.
await glide(page.locator('.react-flow__node[data-id="agg_region_month"]'), { dy: -20 });
await page.mouse.dblclick(at.x, at.y);
await pause(700);
await page.mouse.move(at.x - 120, at.y - 160, { steps: 8 });
await glide(col("agg_region_month", "revenue"), { steps: 18 });
await pause(2600);
await page.mouse.move(at.x, at.y - 240, { steps: 14 });

// 6. The code it compiles to.
await click(page.getByRole("tab", { name: "Code" }));
await pause(900);
await click(page.getByRole("button", { name: "PySpark" }));
await pause(2600);
const end = (Date.now() - t0) / 1000;

await context.close();
await browser.close();
const video = readdirSync(out).filter((f) => f.endsWith(".webm") && f !== "demo.webm").pop();
renameSync(`${out}/${video}`, `${out}/demo.webm`);
writeFileSync(`${out}/trim.json`, JSON.stringify({ start, end }));
console.log(`wrote ${out}/demo.webm`);
