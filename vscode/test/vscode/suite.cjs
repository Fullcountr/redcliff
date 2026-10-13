// Runs inside VS Code's extension host.
const assert = require("node:assert/strict");
const path = require("node:path");
const vscode = require("vscode");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function until(what, check, timeout = 30000) {
  const started = Date.now();
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() - started > timeout) throw new Error(`timed out waiting for ${what}`);
    await wait(250);
  }
}

async function run() {
  const failures = [];
  const step = async (name, fn) => {
    try {
      await fn();
      console.log(`  ok   ${name}`);
    } catch (e) {
      failures.push(`${name}: ${e.message}`);
      console.log(`::error title=${name}::${String(e.stack || e).replace(/\n/g, "%0A")}`);
    }
  };
  const folder = vscode.workspace.workspaceFolders[0].uri.fsPath;
  const uri = vscode.Uri.file(path.join(folder, "orders_revenue.yaml"));
  const doc = await vscode.workspace.openTextDocument(uri);
  const editor = await vscode.window.showTextDocument(doc);

  await step("the extension activates on a YAML file", async () => {
    const ext = vscode.extensions.getExtension("fullcountr.redcliff");
    assert.ok(ext, "extension not found");
    await until("activation", () => ext.isActive);
  });

  await step("the command opens the canvas beside the spec", async () => {
    await vscode.commands.executeCommand("redcliff.showPipeline");
    await until("the webview tab", () => vscode.window.tabGroups.all.flatMap((g) => g.tabs)
      .some((t) => t.input instanceof vscode.TabInputWebview && t.label === "Pipeline: orders_revenue.yaml"));
  });

  await step("a broken column shows as a problem on its step", async () => {
    await editor.edit((e) => {
      const text = doc.getText();
      const at = text.indexOf("status = 'shipped'");
      e.replace(new vscode.Range(doc.positionAt(at), doc.positionAt(at + "status".length)), "statuss");
    });
    const found = await until("a diagnostic", () =>
      vscode.languages.getDiagnostics(uri).find((d) => d.source === "redcliff" && /statuss/.test(d.message)));
    const line = doc.lineAt(found.range.start.line).text;
    assert.match(line, /id:\s*shipped/, `diagnostic on: ${line}`);
  });

  await step("fixing it clears the problem", async () => {
    await vscode.commands.executeCommand("undo");
    await until("no diagnostics", () => vscode.languages.getDiagnostics(uri).filter((d) => d.source === "redcliff").length === 0);
  });

  if (failures.length) throw new Error(`${failures.length} failed:\n${failures.join("\n")}`);
}

module.exports = { run };
