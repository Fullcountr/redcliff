// End-to-end test of the playground in a real Chromium.
//
//   APP_URL=http://localhost:4173/ CHROMIUM_PATH=/path/to/chrome node playground.e2e.mjs
//
// Drives the full loop: engine boot, row counts, lineage hover, blast radius, SQL import,
// PowerCenter import with its scorecard, data preview, generated code and share links.
// Screenshots go to SCREENSHOT_DIR if set.
import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { chromium } from "playwright-core";

const url = process.env.APP_URL || "http://localhost:4173/";
const shots = process.env.SCREENSHOT_DIR;
if (shots) mkdirSync(shots, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  headless: true,
});
const errors = [];
let failures = 0;

async function step(name, fn) {
  const started = Date.now();
  try {
    await fn();
    console.log(`  ok   ${name} (${Date.now() - started} ms)`);
  } catch (e) {
    failures++;
    console.log(`  FAIL ${name}\n       ${String(e.message || e).split("\n").slice(0, 4).join("\n       ")}`);
  }
}

async function shot(page, name) {
  if (shots) await page.screenshot({ path: `${shots}/${name}.png` });
}

const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

const col = (node, column) =>
  page.locator(`.react-flow__node[data-id="${node}"] button.col`, { hasText: new RegExp(`^${column}$`) });
const rowsOf = (node) => page.locator(`.react-flow__node[data-id="${node}"] .step-rows`);
// Zoomed out, cards show a big title instead of columns; double-clicking one zooms to it.
async function zoomTo(node) {
  await page.locator(`.react-flow__node[data-id="${node}"]`).dblclick();
  await page.waitForFunction((id) => !document.querySelector(`.react-flow__node[data-id="${id}"] .step.is-far`), node);
  await page.waitForTimeout(350);
  await page.mouse.move(5, 300); // the zoom puts a column under the still pointer; move so hovers register
}

console.log(`Redcliff playground e2e against ${url}`);

await step("the example is interactive before the engine has loaded", async () => {
  const started = Date.now();
  await page.goto(url);
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="revenue_by_region"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  const interactive = Date.now() - started;
  const status = await page.locator('[data-testid="engine-status"]').textContent();
  await page.locator('.react-flow__node[data-id="revenue_by_region"] button.col', { hasText: /^revenue$/ }).hover();
  await page.waitForSelector('.react-flow__node[data-id="orders"] button.col.is-lit');
  await page.mouse.move(5, 5);
  console.log(`       canvas interactive after ${interactive} ms (engine: "${status}")`);
  assert.ok(!status.startsWith("Engine ready") || interactive < 3000, `canvas waited for the engine (${interactive} ms)`);
});

await step("engine boots in the browser", async () => {
  const started = Date.now();
  await page.waitForFunction(
    () => document.querySelector('[data-testid="engine-status"]')?.textContent?.startsWith("Engine ready"),
    null, { timeout: 120000 });
  console.log(`       engine ready ${Date.now() - started} ms later`);
});

await step("default example renders with live row counts", async () => {
  await page.waitForSelector('.react-flow__node[data-id="revenue_by_region"]');
  assert.equal(await page.locator(".react-flow__node").count(), 6);
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="revenue_by_region"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  assert.equal(await rowsOf("orders").textContent(), "8 rows");
  assert.equal(await rowsOf("shipped").textContent(), "6 rows");
  await shot(page, "01-default");
});

await step("hovering a column traces it back to its source", async () => {
  await col("revenue_by_region", "revenue").hover();
  await page.waitForSelector('.react-flow__node[data-id="orders"] button.col.is-lit');
  const lit = await page.locator("button.col.is-lit").allTextContents();
  assert.ok(lit.includes("amount"), `amount should be lit, got ${lit}`);
  assert.ok(!lit.includes("region"), "region does not feed revenue");
  assert.ok(await page.locator('.react-flow__node[data-id="customers"] .step.is-dimmed').count() === 1);
  await shot(page, "02-trace");
  await page.mouse.move(5, 5);
});

await step("clicking a column shows its blast radius", async () => {
  await col("orders", "status").click();
  await page.waitForSelector(".impact");
  const text = await page.locator(".impact").innerText();
  assert.match(text, /If orders\.status changes/);
  assert.match(text, /A rename breaks\s+1\s+shipped/i);
  assert.ok(await page.locator('.react-flow__node[data-id="revenue_by_region"] .step.rows-change').count() === 1);
  assert.match(await page.locator('.react-flow__node[data-id="shipped"]').innerText(), /Breaks on rename/);
  await shot(page, "03-impact");
  await page.keyboard.press("Escape");
  await page.waitForSelector(".impact", { state: "detached" });
});

await step("selecting a step previews its data", async () => {
  await page.locator('.react-flow__node[data-id="revenue_by_region"] .step-id').click();
  await page.getByRole("tab", { name: "Data" }).click();
  await page.waitForSelector(".data table tbody tr");
  const rows = await page.locator(".data table tbody tr").allInnerTexts();
  assert.equal(rows.length, 2);
  assert.ok(rows.some((r) => r.includes("South") && r.includes("330.50")), rows.join(" | "));
  await shot(page, "04-data");
});

await step("expectations run and show on the cards and in the Data tab", async () => {
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="orders"] .step-checks')?.textContent === "3 checks passed", null, { timeout: 30000 });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="revenue_by_region"] .step-checks')?.textContent === "2 checks passed");
  const names = await page.locator(".checks .check .name").allTextContents();
  assert.deepEqual(names, ["revenue_is_positive", "row_count"]);
});

await step("clicking a row traces it to the source rows behind it", async () => {
  await page.locator(".data table tbody tr", { hasText: "South" }).first().click();
  await page.waitForSelector(".behind-source");
  const titles = await page.locator(".behind-title").allInnerTexts();
  assert.deepEqual(titles.map((t) => t.replace(/\s+/g, " ").trim()), ["orders 3 rows", "customers 2 rows"]);
  const orders = await page.locator(".behind-source").first().locator("tbody tr").allInnerTexts();
  assert.ok(orders[0].includes("120.50") && orders[2].includes("10.00"), orders.join(" | "));
  await shot(page, "04-trace");
  await page.getByRole("button", { name: "Clear" }).click();
});

await step("code tab generates Spark SQL and PySpark", async () => {
  await page.getByRole("tab", { name: "Code" }).click();
  await page.waitForFunction(() => document.querySelector(".code-block")?.textContent?.includes("INSERT OVERWRITE"));
  await page.getByRole("button", { name: "PySpark" }).click();
  await page.waitForFunction(() => document.querySelector(".code-block")?.textContent?.includes("spark.read"));
  await shot(page, "05-code");
});

await step("code tab generates dbt and Lakeflow projects", async () => {
  await page.getByRole("button", { name: "dbt" }).click();
  await page.waitForFunction(() => document.querySelector(".code-block")?.textContent?.includes("config(materialized="));
  const names = await page.locator(".file-select option").allInnerTexts();
  assert.ok(names.includes("dbt_project.yml") && names.includes("models/schema.yml"), names.join(", "));
  await shot(page, "05-dbt");
  await page.getByRole("button", { name: "Lakeflow" }).click();
  await page.waitForFunction(() => document.querySelector(".code-block")?.textContent?.includes("@dp.materialized_view"));
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download .zip" }).click()]);
  assert.match(download.suggestedFilename(), /_lakeflow\.zip$/);
  await page.getByRole("button", { name: "Spark SQL" }).click();
});

await step("editing a step updates the pipeline", async () => {
  await page.locator('.react-flow__node[data-id="shipped"] .step-id').click();
  await page.getByRole("tab", { name: "Step" }).click();
  const box = page.locator("textarea.code-input");
  await box.fill("condition: amount > 50");
  await page.getByRole("button", { name: "Apply changes" }).click();
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="shipped"] .step-rows')?.textContent === "5 rows", null, { timeout: 30000 });
});

await step("the Changes tab shows what an edit does, side by side", async () => {
  await page.getByRole("tab", { name: "Changes" }).click();
  await page.waitForFunction(() => /1 step changed/.test(document.querySelector('[data-testid="changes-summary"]')?.textContent ?? ""), null, { timeout: 30000 });
  await page.waitForSelector('[data-testid="changes-rows"]', { timeout: 30000 });
  assert.ok(await page.locator('[data-testid="changes-diff"] .sbs-row.mod').count() >= 1);
  await shot(page, "05b-changes");
  await page.getByRole("tab", { name: "Step" }).click();
});

await step("a broken step is flagged, not fatal", async () => {
  const box = page.locator("textarea.code-input");
  await box.fill("condition: amout > 50");
  await page.getByRole("button", { name: "Apply changes" }).click();
  await page.waitForSelector('.react-flow__node[data-id="shipped"] .step.has-issues');
  assert.match(await page.locator('.react-flow__node[data-id="shipped"] .step-issue').innerText(), /unknown column 'amout'/);
  await shot(page, "06-issue");
});

async function openImport(target, item) {
  await target.locator(".topbar").getByRole("button", { name: "Import" }).click();
  await target.getByRole("menuitem", { name: new RegExp(item) }).click();
  await target.waitForSelector("dialog[open]");
}

await step("importing a legacy Teradata job builds the pipeline", async () => {
  await openImport(page, "SQL script");
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.waitForSelector('.react-flow__node[data-id="regional_revenue"]');
  assert.equal(await page.locator(".react-flow__node").count(), 10);
  const notes = await page.locator(".notes").innerText();
  assert.match(notes, /QUALIFY kept as a SQL step/);
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="regional_revenue"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  await shot(page, "07-imported");
});

await step("lineage works on the imported job", async () => {
  await zoomTo("regional_revenue");
  await col("regional_revenue", "revenue").hover();
  await page.waitForSelector('.react-flow__node[data-id="orders"] button.col.is-lit');
  assert.deepEqual(await page.locator('.react-flow__node[data-id="orders"] button.col.is-lit').allTextContents(), ["amount"]);
  await shot(page, "08-imported-trace");
  await page.mouse.move(5, 5);
});

await step("importing a PowerCenter mapping shows its migration scorecard", async () => {
  await openImport(page, "Informatica PowerCenter");
  await shot(page, "10-import-dialog");
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.waitForSelector('.react-flow__node[data-id="agg_region_month"]');
  assert.equal(await page.locator(".react-flow__node").count(), 19);
  await page.waitForSelector(".score-hero");
  assert.match(await page.locator(".ring-label").innerText(), /86%/);
  assert.match(await page.locator('.react-flow__node[data-id="agg_region_month"]').innerText(), /Review/);
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="agg_region_month"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  await page.locator(".score-item", { hasText: "LKP_REGION" }).click();
  await page.waitForFunction(() => document.querySelectorAll(".step.is-scored").length === 3, null, { timeout: 5000 });
  await shot(page, "11-scorecard");
  await page.getByRole("tab", { name: "Data" }).click();
  await page.waitForSelector(".data table tbody tr");
  await page.keyboard.press("Escape");
  assert.ok(await page.locator(".step.is-far").count() > 0, "a 19-step mapping starts zoomed out, with big titles");
  await shot(page, "12-far");
  await zoomTo("agg_region_month");
  await col("agg_region_month", "revenue").hover();
  await page.waitForSelector('.react-flow__node[data-id="orders"] button.col.is-lit');
  await page.mouse.move(5, 5);
});

await step("a PowerCenter workflow imports with its mapplet, session and parameter file", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /PowerCenter workflow/ }).click();
  await page.waitForSelector("dialog[open]");
  assert.match(await page.locator('textarea[aria-label="Parameter file"]').inputValue(), /\$\$MIN_ORDER_DATE=2026-09-02/);
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.waitForSelector('.react-flow__node[data-id="mplt_clean__exp_clean_customer"]');
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="agg_region_month"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  assert.match(await page.locator('.react-flow__node[data-id="region_monthly_revenue"]').innerText(), /post-SQL/);
  const items = await page.locator(".score-item .name").allTextContents();
  assert.ok(items.includes("MPLT_CLEAN/EXP_CLEAN_CUSTOMER"), items.join(", "));
  assert.ok(items.includes("wf_customer_revenue/cmd_check_landing"), items.join(", "));
  await shot(page, "13-workflow");
});

await step("changing a variable re-runs the pipeline", async () => {
  const box = page.getByLabel("Variable min_order_date");
  assert.equal(await box.inputValue(), "2026-09-02");
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="sq_orders"] .step-rows')?.textContent === "6 rows", null, { timeout: 30000 });
  await box.fill("2026-01-01");
  await box.press("Enter");
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="sq_orders"] .step-rows')?.textContent === "8 rows", null, { timeout: 30000 });
  await shot(page, "13-variables");
  await box.fill("2026-09-02");
  await box.press("Enter");
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="sq_orders"] .step-rows')?.textContent === "6 rows", null, { timeout: 30000 });
});

await step("a share link restores the pipeline", async () => {
  await page.getByRole("button", { name: "Share link" }).click();
  await page.waitForFunction(() => location.hash.startsWith("#p="));
  const link = page.url();
  const fresh = await context.newPage();
  await fresh.goto(link);
  await fresh.waitForSelector('.react-flow__node[data-id="mplt_clean__exp_clean_customer"]', { timeout: 120000 });
  assert.equal(await fresh.locator(".react-flow__node").count(), 19);
  await fresh.close();
});

await step("a PowerCenter store mart imports its Normalizer, Ranks and worklet", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /PowerCenter store mart/ }).click();
  await page.waitForSelector("dialog[open]");
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="nrm_quarters"] .step-rows')?.textContent === "16 rows", null, { timeout: 30000 });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="rnk_best_quarters"] .step-rows')?.textContent === "9 rows", null, { timeout: 30000 });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="rnk_top_store"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  const items = await page.locator(".score-item .name").allTextContents();
  assert.ok(items.includes("wf_store_mart/wklt_load/cmd_archive_extract"), items.join(", "));
  await shot(page, "14-store-mart");
});

await step("an SSIS package imports with its data flow and control flow", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /SSIS package/ }).click();
  await page.waitForSelector("dialog[open]");
  assert.match(await page.getByLabel("SSIS package").inputValue(), /Microsoft\.ConditionalSplit/);
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="agg_region"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="cspl_status_cancelled"] .step-rows')?.textContent === "1 row");
  await page.getByRole("tab", { name: /Migration/ }).click();
  const items = await page.locator(".score-item .name").allTextContents();
  assert.ok(items.includes("MJ Orders Customers") && items.includes("LoadRegionRevenue/Send Done Mail"), items.join(", "));
  assert.equal(await page.getByLabel("Variable fx_rate").inputValue(), "1.0");
  await shot(page, "15-ssis");
});

await step("a data model shows as a diagram, exports DDL and Mermaid, and imports DDL", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /^sales Kimball/ }).click();
  for (const id of ["dim_customer", "dim_date", "fct_orders"]) await page.waitForSelector(`[data-testid="entity-${id}"]`);
  assert.equal(await page.locator(".react-flow__edge.rel").count(), 2);
  await page.waitForSelector('[data-testid="model-valid"]', { timeout: 30000 });
  await page.locator('[data-testid="entity-fct_orders"]').click();
  assert.match(await page.locator(".panel").textContent(), /Grain: one order/);
  await page.getByRole("tab", { name: "Export" }).click();
  await page.waitForFunction(() => /FOREIGN KEY \(customer_key\) REFERENCES dim_customer/.test(document.querySelector('[data-testid="model-export"]')?.textContent ?? ""));
  await page.getByRole("button", { name: "Mermaid", exact: true }).click();
  await page.waitForFunction(() => (document.querySelector('[data-testid="model-export"]')?.textContent ?? "").startsWith("erDiagram"));
  await shot(page, "16-model");
  await page.getByRole("tab", { name: "Import" }).click();
  await page.locator(".panel").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector('[data-testid="entity-fct_sales"]');
  await page.waitForSelector('[data-testid="entity-dim_product"]');
  await page.locator(".topbar").getByRole("button", { name: "Pipelines" }).click();
  await page.waitForSelector(".react-flow__node .step");
});

await step("a model proposed from the sample data opens its load as a pipeline", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Import" }).click();
  await page.getByRole("menuitem", { name: /Data model/ }).click();
  await page.getByRole("button", { name: /Propose a model from 2 tables/ }).click();
  for (const id of ["dim_customer", "dim_date", "fct_orders"]) await page.waitForSelector(`[data-testid="entity-${id}"]`);
  await page.locator('[data-testid="entity-dim_customer"]').click();
  assert.match(await page.locator('[data-testid="model-why"]').textContent(), /unique and never null/);
  await page.getByRole("tab", { name: "Load" }).click();
  await page.waitForFunction(() => /fct_orders/.test(document.querySelector('[data-testid="model-load-targets"]')?.textContent ?? ""));
  await shot(page, "17-model-load");
  await page.getByRole("button", { name: "Open as a pipeline" }).click();
  await page.waitForSelector('.react-flow__node[data-id="keyed_fct_orders"]');
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="stg_dim_customer"] .step-rows')?.textContent === "5 rows", null, { timeout: 30000 });
});

await step("works on a phone-sized screen and in dark mode", async () => {
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
  const p = await phone.newPage();
  await p.goto(url);
  await p.waitForFunction(() => document.querySelector('.react-flow__node[data-id="revenue_by_region"] .step-rows')?.textContent === "2 rows", null, { timeout: 120000 });
  await p.waitForTimeout(500);
  const overflow = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert.ok(overflow <= 1, `page scrolls sideways by ${overflow}px`);
  if (shots) await p.screenshot({ path: `${shots}/09-phone-dark.png`, fullPage: true });
  await phone.close();
});

await browser.close();
const relevant = errors.filter((e) => !/favicon|sourcemap/i.test(e));
if (relevant.length) {
  console.log(`\nConsole errors (${relevant.length}):\n  ${relevant.slice(0, 5).join("\n  ")}`);
  failures++;
}
console.log(failures ? `\n${failures} failed` : "\nall passed");
process.exit(failures ? 1 : 0);
