# Redcliff™ for VS Code

The extension in [`vscode/`](../vscode) shows a pipeline spec as a canvas beside its YAML. The YAML
stays the source of truth: edit it and the canvas follows as you type.

- Hover a column for its lineage back to the sources; click it for its blast radius.
- Click a step, or an issue, to jump to its lines in the editor.
- Problems (unknown columns, broken references) are underlined on the step that has them.
- The Code tab shows Spark SQL, PySpark or DuckDB SQL for the pipeline or one step.
- It follows VS Code's light, dark and high-contrast themes.

## Install

1. `pip install redcliff` (0.3.0 or later): the extension runs `redcliff bridge` for the analysis.
2. Install the extension: from the VS Code Marketplace or Open VSX once it's published there, or
   from a `.vsix`: every CI run uploads `redcliff-vsix`, and releases attach `redcliff.vsix`. Then
   **Extensions → … → Install from VSIX**.
3. Open a spec and click the canvas icon in the editor's title bar, or run **Redcliff: Show Pipeline
   Canvas**.

If `redcliff` isn't on VS Code's PATH, set `redcliff.command` to its full path, or to
`python3 -m redcliff.cli` with the Python that has it. The **Redcliff** output channel logs the bridge.

## How it's built

| Piece | Where | What |
| --- | --- | --- |
| `redcliff bridge` | `core/src/redcliff/cli.py` | JSON lines over stdio: `{"id", "fn", "args"}` in, `{"id", "result"}` out, for the browser engine's functions that take spec text (analyze, code, impact, diff, model analysis and export). None read or write files. |
| the webview | `web/src/vscode/`, built by `npm run build:vscode` into `vscode/media/` | the playground's step cards, layout and lineage, with engine calls sent to the extension instead of Pyodide |
| the extension | `vscode/src/extension.ts` | the panel, the bridge process, diagnostics, and jumping to a step's lines |

Tests: `npm test` in `vscode/` (the helpers), `web/e2e/vscode-webview.e2e.mjs` (the webview in
Chromium with a real bridge: canvas, lineage, blast radius, jump requests, code, broken YAML,
themes), and `npm run test:vscode` (the extension in a real VS Code: activation, the panel,
diagnostics appearing and clearing). CI runs all three and packages the `.vsix`.

## Build it yourself

```bash
cd web && npm ci && npm run build:vscode
cd ../vscode && npm ci && npm run build && npm run package   # redcliff.vsix
```
