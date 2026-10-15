// End-to-end test of the playground in a real Chromium.
//
//   APP_URL=http://localhost:4173/ CHROMIUM_PATH=/path/to/chrome node playground.e2e.mjs
//
// Drives the full loop: engine boot, row counts, lineage hover, blast radius, SQL import,
// PowerCenter import with its scorecard, data preview, generated code and share links.
// Screenshots go to SCREENSHOT_DIR if set.
import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
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

await step("a PowerCenter mapping becomes a proposed model, with what it covers", async () => {
  await page.getByRole("tab", { name: /Migration/ }).click();
  await page.locator('[data-testid="model-from-job"]').getByRole("button", { name: "Kimball model" }).click();
  for (const id of ["dim_customer", "dim_region", "dim_date", "fct_orders"]) await page.waitForSelector(`[data-testid="entity-${id}"]`);
  await page.waitForFunction(() => /13 of 13/.test(document.querySelector('[data-testid="job-map-summary"]')?.textContent ?? ""), null, { timeout: 30000 });
  await shot(page, "12b-model-from-job");
  await page.locator('[data-testid="entity-dim_region"]').click();
  assert.match(await page.locator('[data-testid="model-why"]').textContent(), /looks regions rows up by region \(step lkp_region_unique\)/);
  await page.locator(".topbar").getByRole("tab", { name: "Pipeline" }).click();
  await page.waitForSelector('.react-flow__node[data-id="agg_region_month"]');
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

await step("a Talend job imports with its tMap lookups, rejects and scorecard", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /Talend job/ }).click();
  await page.waitForSelector("dialog[open]");
  assert.match(await page.getByLabel("Talend job").inputValue(), /componentName="tMap"/);
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="revenue_by_region"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="map_orders_unknown_customers"] .step-rows')?.textContent === "1 row");
  await page.waitForSelector(".score-hero");
  const items = await page.locator(".score-item .name").allTextContents();
  assert.ok(items.includes("tMap_1") && items.includes("tJava_1"), items.join(", "));
  assert.equal(await page.getByLabel("Variable fx_rate").inputValue(), "1.0");
  await shot(page, "15b-talend");
});

await step("a DataStage parallel job imports with its Transformer, lookup reject and scorecard", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /DataStage parallel job/ }).click();
  await page.waitForSelector("dialog[open]");
  assert.match(await page.getByLabel("DataStage export").inputValue(), /CTransformerStage/);
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="agg_region"] .step-rows')?.textContent === "2 rows", null, { timeout: 30000 });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="lkp_customer_reject"] .step-rows')?.textContent === "1 row");
  await page.waitForSelector(".score-hero");
  const items = await page.locator(".score-item .name").allTextContents();
  assert.ok(items.includes("xfm_prepare") && items.includes("before-job subroutine"), items.join(", "));
  assert.equal(await page.getByLabel("Variable min_amount").inputValue(), "0");
  await shot(page, "15c-datastage");
});

await step("a data model shows as a diagram, exports DDL and Mermaid, and imports DDL", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /^sales Kimball/ }).click();
  for (const id of ["dim_customer", "dim_date", "fct_orders"]) await page.waitForSelector(`[data-testid="entity-${id}"]`);
  await page.waitForFunction(() => document.querySelectorAll(".react-flow__edge.rel").length === 2);  // drawn once measured
  await page.waitForSelector('[data-testid="model-valid"]', { timeout: 30000 });
  await page.locator('[data-testid="entity-fct_orders"]').click();
  assert.match(await page.locator(".panel").textContent(), /Grain: one order/);
  await page.getByRole("tab", { name: "Export" }).click();
  await page.waitForFunction(() => /FOREIGN KEY \(customer_key\) REFERENCES dim_customer/.test(document.querySelector('[data-testid="model-export"]')?.textContent ?? ""));
  await page.getByRole("button", { name: "Mermaid", exact: true }).click();
  await page.waitForFunction(() => (document.querySelector('[data-testid="model-export"]')?.textContent ?? "").startsWith("erDiagram"));
  await shot(page, "16-model");
  await page.locator(".topbar").getByRole("button", { name: "Import" }).click();
  await page.getByRole("menuitem", { name: /Data model/ }).click();
  await page.waitForSelector('[data-testid="model-start"]');
  await page.locator(".start-body").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector('[data-testid="entity-fct_sales"]');
  await page.waitForSelector('[data-testid="entity-dim_product"]');
  await page.locator(".topbar").getByRole("tab", { name: "Pipeline" }).click();
  await page.waitForSelector(".react-flow__node .step");
});

await step("a model proposed from the sample data opens its load as a pipeline", async () => {
  await page.locator(".topbar").getByRole("tab", { name: "Model" }).click();
  await page.getByRole("button", { name: "New model" }).click();
  await page.waitForSelector('[data-testid="model-start"]');
  await shot(page, "17a-model-start");
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
  await page.waitForFunction(() => /No issues/.test(document.querySelector(".statusbar")?.textContent ?? ""), null, { timeout: 15000 });
  await page.locator('[data-testid="loads-model"]').click();
  await page.waitForSelector('[data-testid="entity-fct_orders"]');
});

const csv = (name, text) => ({ name, mimeType: "text/csv", buffer: Buffer.from(text) });
const drag = async (from, to) => {
  const a = await page.locator(from).boundingBox();
  const b = await page.locator(to).boundingBox();
  await page.mouse.move(a.x + 4, a.y + 4);
  await page.mouse.down();
  await page.mouse.move((a.x + b.x) / 2, (a.y + b.y) / 2, { steps: 8 });
  await page.mouse.move(b.x + 4, b.y + 4, { steps: 8 });
  await page.mouse.up();
};

await step("adding CSVs grows the model: new entities, references both ways, a month appended", async () => {
  await page.locator(".topbar input[type=file]").setInputFiles([
    csv("statuses.csv", "status,description\nshipped,Left the warehouse\npending,Waiting for payment\ncancelled,Cancelled\n"),
    csv("orders_october.csv", "order_id,customer_id,order_date,status,amount\n20,101,2026-10-01,shipped,40.00\n21,103,2026-10-02,cancelled,50.00\n"),
  ]);
  await page.waitForSelector('[data-testid="model-review"]', { timeout: 30000 });
  const ids = await page.locator('[data-testid^="change-"]').evaluateAll((els) => els.map((e) => e.dataset.testid));
  assert.deepEqual(ids, ["change-append:orders_october", "change-entity:dim_status", "change-entity:fct_orders_october",
    "change-relationship:fct_orders.status_key"]);
  assert.ok(!(await page.locator('[data-testid="change-entity:fct_orders_october"] input').isChecked()));
  await page.waitForSelector('[data-testid="entity-dim_status"] .new-badge');
  await shot(page, "17b-model-review");
  await page.locator('[data-testid="review-accept"]').click();
  await page.waitForSelector('[data-testid="model-review"]', { state: "detached" });
  await page.waitForSelector('[data-testid="entity-dim_status"]');
  assert.equal(await page.locator('[data-testid="entity-dim_status"] .new-badge').count(), 0);
  await page.locator('[data-testid="entity-fct_orders"]').click();
  assert.match(await page.locator('[data-testid="entity-source"]').textContent(), /orders\.csv \+ orders_october\.csv/);
  await page.getByRole("tab", { name: "Load" }).click();
  await page.getByRole("button", { name: "Open as a pipeline" }).click();
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="src_fct_orders"] .step-rows')?.textContent === "10 rows",
    null, { timeout: 30000 });
  await page.waitForFunction(() => document.querySelector('.react-flow__node[data-id="stg_dim_status"] .step-rows')?.textContent === "3 rows",
    null, { timeout: 30000 });
  await page.locator('[data-testid="loads-model"]').click();
  await page.waitForSelector('[data-testid="entity-dim_status"]');
});

await step("a relationship drawn by dragging is checked against the data", async () => {
  await page.locator(".topbar input[type=file]").setInputFiles([
    csv("products.csv", "product_id,product_name\n10,Lamp\n11,Notebook\n"),
    csv("order_lines.csv", "line_id,order_id,item,quantity\n1,1,10,2\n2,2,11,1\n3,3,12,5\n"),
  ]);
  await page.waitForSelector('[data-testid="model-review"]', { timeout: 30000 });
  await page.locator('[data-testid="review-accept"]').click();
  await page.waitForSelector('[data-testid="entity-fct_order_lines"]');
  await page.waitForTimeout(800);  // the canvas refits
  await drag('.react-flow__node[data-id="fct_order_lines"] [data-handleid="r-item"]',
    '.react-flow__node[data-id="dim_product"] [data-handleid="l-product_key"]');
  await page.waitForSelector('[data-testid="link-check"]', { timeout: 15000 });
  assert.match(await page.locator('[data-testid="link-check"]').textContent(), /2 of 3 values found \(1 without a match/);
  await shot(page, "17c-model-link");
  await page.locator('[data-testid="link-card"]').getByRole("button", { name: "Link" }).click();
  await page.waitForFunction(() => /fct_order_lines\.product_key → dim_product\.product_key/.test(document.querySelector(".panel")?.textContent ?? ""));
});

await step("a CSV source in a pipeline can be modelled, and a model travels in a link", async () => {
  await page.locator(".topbar").getByRole("tab", { name: "Pipeline" }).click();
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /orders revenue/ }).click();
  await page.locator('.react-flow__node[data-id="customers"] .step-id').click();
  await page.locator('[data-testid="model-this"]').getByRole("button", { name: "Model this data" }).click();
  await page.waitForFunction(() => /customers is in the model already/.test(document.querySelector(".toast")?.textContent ?? ""));
  await page.locator(".topbar").getByRole("button", { name: "Share link" }).click();
  await page.waitForFunction(() => /#m=/.test(location.href));
  const link = await page.evaluate(() => location.href);
  const other = await context.newPage();
  await other.goto(link);
  await other.waitForSelector('[data-testid="entity-fct_order_lines"]', { timeout: 60000 });
  await other.close();
});

await step("Add file reads TSV, Parquet, JSON and Excel, and routes a legacy job to the import", async () => {
  const fixture = (name, as = name) => ({ name: as, mimeType: "application/octet-stream",
    buffer: readFileSync(new URL(`./fixtures/${name}`, import.meta.url)) });
  await page.locator(".topbar").getByRole("tab", { name: "Pipeline" }).click();
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.getByRole("menuitem", { name: /orders revenue/ }).click();
  await page.locator('[data-testid="add-file"]').setInputFiles([
    csv("skus.tsv", "sku\tlabel\tprice\nA\tLamp, desk\t3.50\nB\tMug\t7.25\n"),
    { name: "events.ndjson", mimeType: "application/x-ndjson", buffer: Buffer.from('{"id": 1, "kind": "view"}\n{"id": 2, "kind": "buy"}\n') },
    fixture("products.parquet", "catalog.parquet"), fixture("stores.xlsx"),
  ]);
  await page.waitForSelector('[data-testid="sheet-dialog"]');
  assert.match(await page.locator('[data-testid="sheet-dialog"]').innerText(), /stores[\s\S]*targets/);
  assert.doesNotMatch(await page.locator('[data-testid="sheet-dialog"]').innerText(), /notes/);  // an empty sheet
  await shot(page, "18-sheets");
  await page.locator('[data-testid="add-sheets"]').click();
  for (const [node, n] of [["skus", 2], ["events", 2], ["catalog", 3], ["stores_stores", 3], ["stores_targets", 4]]) {
    await page.waitForFunction(([id, rows]) => document.querySelector(`.react-flow__node[data-id="${id}"] .step-rows`)?.textContent === `${rows} rows`,
      [node, n], { timeout: 30000 });
  }
  await page.locator('.react-flow__node[data-id="skus"] .step-id').click();
  assert.match(await page.locator("textarea.code-input").inputValue(), /delimiter: "\\t"/);
  await page.locator(".react-flow__pane").click({ position: { x: 700, y: 820 } });  // the whole pipeline's code
  await page.getByRole("tab", { name: "Code" }).click();
  await page.getByRole("button", { name: "PySpark", exact: true }).click();
  await page.waitForFunction(() => /spark\.read\.parquet\('catalog\.parquet'\)/.test(document.querySelector(".code-block")?.textContent ?? ""));
  assert.match(await page.locator(".code-block").textContent(), /spark\.read\.schema\('id BIGINT, kind STRING'\)\.json\('events\.ndjson'\)/);
  assert.match(await page.locator(".code-block").textContent(), /stores_targets = spark\.table\('stores_targets'\)/);
  await shot(page, "18b-file-sources");

  await page.locator('[data-testid="add-file"]').setInputFiles([csv("notes.pdf", "%PDF-1.4")]);
  await page.waitForFunction(() => /notes\.pdf isn't a file Redcliff reads/.test(document.querySelector(".toast")?.textContent ?? ""));
  await page.locator('[data-testid="add-file"]').setInputFiles([csv("job.sql", "CREATE TABLE out_t AS SELECT region, COUNT(*) AS n FROM customers GROUP BY region;")]);
  await page.waitForSelector("dialog[open]");
  assert.match(await page.locator("dialog textarea").first().inputValue(), /CREATE TABLE out_t/);
  await page.locator("dialog").getByRole("button", { name: "Close" }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
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

await step("a network that blocks the engine download says so", async () => {
  const blocked = await browser.newContext();
  await blocked.route(/engine\/redcliff\.js$/, (route) => route.fulfill({ status: 403, body: "Blocked by policy" }));
  const p = await blocked.newPage();
  await p.goto(url);
  await p.waitForSelector('[data-testid="engine-error"]', { timeout: 60000 });
  assert.match(await p.locator(".boot-card").innerText(), /network refused engine\/redcliff\.js \(HTTP 403\)[\s\S]*firewall or proxy/);
  if (shots) await p.screenshot({ path: `${shots}/10-engine-blocked.png` });
  await blocked.close();
});

await browser.close();
const relevant = errors.filter((e) => !/favicon|sourcemap/i.test(e));
if (relevant.length) {
  console.log(`\nConsole errors (${relevant.length}):\n  ${relevant.slice(0, 5).join("\n  ")}`);
  failures++;
}
console.log(failures ? `\n${failures} failed` : "\nall passed");
process.exit(failures ? 1 : 0);
