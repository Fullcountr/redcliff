// Screenshots of every view and state, for a visual review (not a test).
//   APP_URL=http://localhost:4173/ CHROMIUM_PATH=... node audit-shots.mjs OUT_DIR
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright-core";

const out = process.argv[2] || "/tmp/audit";
mkdirSync(out, { recursive: true });
const url = process.env.APP_URL || "http://localhost:4173/";
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });

async function open(theme, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport, colorScheme: theme });
  await context.addInitScript((t) => localStorage.setItem("redcliff-theme", t), theme);
  const page = await context.newPage();
  page.on("pageerror", (e) => console.log("PAGE ERROR", String(e)));
  page.on("console", (m) => { if (m.type() === "error" || m.type() === "warning") console.log(`console ${m.type()}: ${m.text().slice(0, 200)}`); });
  await page.goto(url);
  await page.waitForFunction(() => /Engine ready/.test(document.body.textContent ?? ""), null, { timeout: 120000 });
  return page;
}
const snap = async (page, name) => { await page.waitForTimeout(500); await page.screenshot({ path: join(out, `${name}.png`) }); console.log(name); };
const examples = async (page, item) => {
  await page.locator(".topbar").getByRole("button", { name: "Examples" }).click();
  await page.waitForTimeout(200);
  if (!item) return;
  await page.getByRole("menuitem", { name: item }).click();
};

for (const theme of ["light", "dark"]) {
  const page = await open(theme);
  await snap(page, `${theme}-01-home`);
  for (const tab of ["Step", "Data", "Code", "Spec", "Changes"]) {
    if (tab === "Step" || tab === "Data") await page.locator('.react-flow__node[data-id="shipped"] .step-id').click();
    await page.getByRole("tab", { name: tab }).click();
    await snap(page, `${theme}-02-tab-${tab.toLowerCase()}`);
  }
  await examples(page);
  await snap(page, `${theme}-03-examples-menu`);
  await page.keyboard.press("Escape");
  await page.locator(".topbar").getByRole("button", { name: "Import" }).click();
  await snap(page, `${theme}-04-import-menu`);
  await page.getByRole("menuitem", { name: /Talend job/ }).click();
  await page.waitForSelector("dialog[open]");
  await snap(page, `${theme}-05-import-dialog`);
  await page.locator("dialog").getByRole("button", { name: "Import", exact: true }).click();
  await page.waitForSelector("dialog[open]", { state: "detached" });
  await page.waitForTimeout(1500);
  await snap(page, `${theme}-06-talend`);
  await page.locator(".topbar").getByRole("button", { name: "Import" }).click();
  await page.getByRole("menuitem", { name: /Data model/ }).click();
  await snap(page, `${theme}-07-model-import-tab`);
  await page.getByRole("button", { name: /Propose a model from/ }).click();
  await page.waitForTimeout(1500);
  await snap(page, `${theme}-08-model-proposed`);
  await page.locator('[data-testid="entity-dim_customer"]').click();
  await snap(page, `${theme}-09-entity-selected`);
  for (const tab of ["Load", "Export", "YAML"]) {
    await page.getByRole("tab", { name: tab }).click();
    await page.waitForTimeout(800);
    await snap(page, `${theme}-10-model-${tab.toLowerCase()}`);
  }
  await page.context().close();
}
const phone = await open("light", { width: 390, height: 844 });
await snap(phone, "phone-01-home");
await phone.locator('.react-flow__node[data-id="shipped"]').click();
await snap(phone, "phone-02-step");
await examples(phone, /^sales Kimball/);
await phone.waitForTimeout(1500);
await snap(phone, "phone-03-model");
await browser.close();
