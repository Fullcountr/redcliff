// Runs the extension in a real VS Code (downloaded by @vscode/test-electron), with a real
// `redcliff bridge` on PATH. CI runs it under xvfb; failures are also printed as GitHub annotations.
import { cpSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { runTests } from "@vscode/test-electron";

const here = dirname(fileURLToPath(import.meta.url));
const extension = resolve(here, "../..");
const workspace = mkdtempSync(join(tmpdir(), "redcliff-vscode-"));
cpSync(resolve(extension, "../examples/specs/orders_revenue.yaml"), join(workspace, "orders_revenue.yaml"));

try {
  await runTests({
    extensionDevelopmentPath: extension,
    extensionTestsPath: join(here, "suite.cjs"),
    launchArgs: [workspace, "--disable-extensions", "--disable-workspace-trust", "--no-sandbox"],
  });
} catch (e) {
  console.log(`::error title=VS Code extension test::${String(e.message || e).replace(/\n/g, "%0A")}`);
  process.exit(1);
}
