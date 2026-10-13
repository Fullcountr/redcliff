// Redcliff for VS Code: the pipeline canvas next to the spec, and the spec's problems in the editor.
//
// The YAML in the editor is the source of truth. The canvas reads it as you type, and clicking a
// step reveals its lines. Analysis runs in `redcliff bridge` (pip install redcliff), a Python process
// speaking JSON lines; the webview is the playground's canvas, built by web/vite.vscode.config.ts.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomBytes } from "node:crypto";
import * as readline from "node:readline";
import * as yaml from "js-yaml";
import * as vscode from "vscode";
import { looksLikeSpec, placeIssues, rangeOfNode, splitCommand, type Issue } from "./specLines";

type Pending = { resolve: (value: unknown) => void };

/** One `redcliff bridge` process, started on first use and restarted if it exits. */
class Bridge {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<number, Pending>();
  private nextId = 1;
  private ready: Promise<void> | null = null;
  lastError: string | null = null;

  constructor(private readonly log: vscode.OutputChannel) {}

  private start(): Promise<void> {
    if (this.ready) return this.ready;
    const [program, args] = splitCommand(vscode.workspace.getConfiguration("redcliff").get("command", "redcliff"));
    this.ready = new Promise((resolve, reject) => {
      let proc: ChildProcessWithoutNullStreams;
      try {
        proc = spawn(program, [...args, "bridge"], { cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath });
      } catch (e) {
        reject(e);
        return;
      }
      this.proc = proc;
      const lines = readline.createInterface({ input: proc.stdout });
      lines.on("line", (line) => {
        let msg: { ready?: boolean; version?: string; id?: number; result?: unknown };
        try {
          msg = JSON.parse(line);
        } catch {
          this.log.appendLine(`bridge: ${line}`);
          return;
        }
        if (msg.ready) {
          this.log.appendLine(`redcliff ${msg.version} bridge started`);
          this.lastError = null;
          resolve();
          return;
        }
        const p = msg.id !== undefined ? this.pending.get(msg.id) : undefined;
        if (p) {
          this.pending.delete(msg.id!);
          p.resolve(msg.result);
        }
      });
      proc.stderr.on("data", (d) => this.log.append(String(d)));
      proc.on("error", (e) => {
        this.lastError = `Couldn't run "${program}": ${e.message}. Install Redcliff (pip install redcliff) or set ` +
          "redcliff.command.";
        this.log.appendLine(this.lastError);
        reject(new Error(this.lastError));
      });
      proc.on("exit", (code) => {
        this.log.appendLine(`bridge exited (${code})`);
        for (const p of this.pending.values()) p.resolve({ ok: false, error: "the Redcliff bridge stopped" });
        this.pending.clear();
        this.proc = null;
        this.ready = null;
        if (code && !this.lastError) {
          this.lastError = `"${program} bridge" exited with code ${code}; is Redcliff 0.2.0 or later installed?`;
          reject(new Error(this.lastError));
        }
      });
    });
    return this.ready;
  }

  async call(fn: string, args: unknown[]): Promise<unknown> {
    try {
      await this.start();
    } catch (e) {
      return { ok: false, error: (e as Error).message };
    }
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.pending.set(id, { resolve });
      this.proc?.stdin.write(JSON.stringify({ id, fn, args }) + "\n");
    });
  }

  restart(): void {
    this.dispose();
    this.lastError = null;
  }

  dispose(): void {
    this.proc?.kill();
    this.proc = null;
    this.ready = null;
  }
}

/** The canvas for one spec document, beside its editor. */
class PipelinePanel {
  static current: PipelinePanel | null = null;
  private disposables: vscode.Disposable[] = [];
  private timer: NodeJS.Timeout | null = null;

  private constructor(private readonly panel: vscode.WebviewPanel, private doc: vscode.TextDocument,
                      private readonly bridge: Bridge, extensionUri: vscode.Uri) {
    const media = vscode.Uri.joinPath(extensionUri, "media");
    panel.webview.options = { enableScripts: true, localResourceRoots: [media] };
    panel.webview.html = html(panel.webview, media);
    panel.onDidDispose(() => this.dispose(), null, this.disposables);
    panel.webview.onDidReceiveMessage((msg) => this.onMessage(msg), null, this.disposables);
    vscode.workspace.onDidChangeTextDocument((e) => {
      if (e.document === this.doc) this.schedule();
    }, null, this.disposables);
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      if (editor && editor.document !== this.doc && looksLikeSpec(editor.document.getText())) this.show(editor.document);
    }, null, this.disposables);
  }

  static open(doc: vscode.TextDocument, bridge: Bridge, extensionUri: vscode.Uri): void {
    if (PipelinePanel.current) {
      PipelinePanel.current.panel.reveal(vscode.ViewColumn.Beside, true);
      PipelinePanel.current.show(doc);
      return;
    }
    const panel = vscode.window.createWebviewPanel("redcliff.pipeline", title(doc), {
      viewColumn: vscode.ViewColumn.Beside, preserveFocus: true,
    }, { retainContextWhenHidden: true });
    panel.iconPath = vscode.Uri.joinPath(extensionUri, "icon.png");
    PipelinePanel.current = new PipelinePanel(panel, doc, bridge, extensionUri);
  }

  show(doc: vscode.TextDocument): void {
    this.doc = doc;
    this.panel.title = title(doc);
    this.send();
  }

  private schedule(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.send(), 250);
  }

  private send(): void {
    void this.panel.webview.postMessage({ type: "spec", text: this.doc.getText(), file: vscode.workspace.asRelativePath(this.doc.uri) });
  }

  private async onMessage(msg: { type: string; id?: number; fn?: string; args?: unknown[]; node?: string }): Promise<void> {
    if (msg.type === "ready") {
      this.send();
    } else if (msg.type === "call" && msg.fn) {
      const result = await this.bridge.call(msg.fn, msg.args ?? []);
      if (this.bridge.lastError) void this.panel.webview.postMessage({ type: "bridge-error", message: this.bridge.lastError });
      void this.panel.webview.postMessage({ type: "result", id: msg.id, result });
    } else if (msg.type === "reveal" && msg.node) {
      const [start, end] = rangeOfNode(this.doc.getText(), msg.node);
      if (start < 0) return;
      const editor = await vscode.window.showTextDocument(this.doc, { viewColumn: vscode.ViewColumn.One, preserveFocus: false });
      const range = new vscode.Range(start, 0, end, this.doc.lineAt(end).text.length);
      editor.selection = new vscode.Selection(range.start, range.start);
      editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
      editor.setDecorations(flash, [range]);
      setTimeout(() => editor.setDecorations(flash, []), 900);
    }
  }

  dispose(): void {
    PipelinePanel.current = null;
    if (this.timer) clearTimeout(this.timer);
    for (const d of this.disposables) d.dispose();
  }
}

const flash = vscode.window.createTextEditorDecorationType({
  backgroundColor: new vscode.ThemeColor("editor.findMatchHighlightBackground"), isWholeLine: true,
});

function title(doc: vscode.TextDocument): string {
  return `Pipeline: ${doc.uri.path.split("/").pop()}`;
}

function html(webview: vscode.Webview, media: vscode.Uri): string {
  const nonce = randomBytes(16).toString("base64");
  const js = webview.asWebviewUri(vscode.Uri.joinPath(media, "webview.js"));
  const css = webview.asWebviewUri(vscode.Uri.joinPath(media, "webview.css"));
  const csp = [
    "default-src 'none'",
    `style-src ${webview.cspSource} 'unsafe-inline'`,
    `font-src ${webview.cspSource}`,
    `img-src ${webview.cspSource} data:`,
    `script-src 'nonce-${nonce}'`,
  ].join("; ");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${css}">
<title>Pipeline</title>
</head>
<body>
<div id="root"></div>
<script nonce="${nonce}" type="module" src="${js}"></script>
</body>
</html>`;
}

/** Problems in the spec, from the same analysis as the canvas, on the steps' lines. */
class Diagnostics {
  private collection = vscode.languages.createDiagnosticCollection("redcliff");
  private timers = new Map<string, NodeJS.Timeout>();

  constructor(private readonly bridge: Bridge) {}

  schedule(doc: vscode.TextDocument): void {
    if (doc.languageId !== "yaml") return;
    const key = doc.uri.toString();
    clearTimeout(this.timers.get(key));
    this.timers.set(key, setTimeout(() => void this.check(doc), 400));
  }

  async check(doc: vscode.TextDocument): Promise<void> {
    const text = doc.getText();
    const isSpec = looksLikeSpec(text);
    if (vscode.window.activeTextEditor?.document === doc) {
      void vscode.commands.executeCommand("setContext", "redcliff.isSpec", isSpec);
    }
    if (!isSpec || !vscode.workspace.getConfiguration("redcliff").get("diagnostics", true)) {
      this.collection.delete(doc.uri);
      return;
    }
    let data: unknown;
    try {
      data = yaml.load(text);
    } catch {
      return;  // the YAML extension reports syntax errors
    }
    const result = await this.bridge.call("analyze", [JSON.stringify(data)]) as
      { ok: boolean; issues?: Issue[]; error?: string };
    if (!result.ok) {
      this.collection.set(doc.uri, [new vscode.Diagnostic(new vscode.Range(0, 0, 0, 0),
        result.error ?? "Redcliff couldn't read this spec", vscode.DiagnosticSeverity.Warning)]);
      return;
    }
    this.collection.set(doc.uri, placeIssues(text, result.issues ?? []).map(({ line, message }) => {
      const d = new vscode.Diagnostic(doc.lineAt(Math.min(line, doc.lineCount - 1)).range, message,
        vscode.DiagnosticSeverity.Error);
      d.source = "redcliff";
      return d;
    }));
  }

  clear(doc: vscode.TextDocument): void {
    this.collection.delete(doc.uri);
  }

  dispose(): void {
    this.collection.dispose();
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const log = vscode.window.createOutputChannel("Redcliff");
  const bridge = new Bridge(log);
  const diagnostics = new Diagnostics(bridge);
  context.subscriptions.push(
    log,
    { dispose: () => bridge.dispose() },
    diagnostics,
    vscode.commands.registerCommand("redcliff.showPipeline", () => {
      const doc = vscode.window.activeTextEditor?.document;
      if (!doc || !looksLikeSpec(doc.getText())) {
        void vscode.window.showInformationMessage("Open a Redcliff pipeline spec (YAML with version and nodes) first.");
        return;
      }
      PipelinePanel.open(doc, bridge, context.extensionUri);
    }),
    vscode.workspace.onDidOpenTextDocument((d) => diagnostics.schedule(d)),
    vscode.workspace.onDidChangeTextDocument((e) => diagnostics.schedule(e.document)),
    vscode.workspace.onDidCloseTextDocument((d) => diagnostics.clear(d)),
    vscode.window.onDidChangeActiveTextEditor((e) => e && diagnostics.schedule(e.document)),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("redcliff.command")) bridge.restart();
    }),
  );
  for (const doc of vscode.workspace.textDocuments) diagnostics.schedule(doc);
}

export function deactivate(): void {
  PipelinePanel.current?.dispose();
}
