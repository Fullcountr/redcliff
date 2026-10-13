// Entry for the VS Code webview (vite.vscode.config.ts builds it into ../vscode/media).
// Engine calls go to the extension, which runs them in `redcliff bridge`; nothing runs in Pyodide.
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@xyflow/react/dist/style.css";
import "../index.css";
import "./vscode.css";
import { ReactFlowProvider } from "@xyflow/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { useTransport } from "../engine/engine";
import { PipelineView, type Host } from "./PipelineView";

declare function acquireVsCodeApi(): Host;

const host: Host = acquireVsCodeApi();
let nextId = 1;
const pending = new Map<number, (value: unknown) => void>();

window.addEventListener("message", (e: MessageEvent) => {
  const msg = e.data;
  if (msg?.type === "result" && pending.has(msg.id)) {
    pending.get(msg.id)!(msg.result);
    pending.delete(msg.id);
  }
});

useTransport((fn, args) => new Promise((resolve) => {
  const id = nextId++;
  pending.set(id, resolve);
  host.postMessage({ type: "call", id, fn, args });
}));

// Follow VS Code's theme: it puts vscode-light, vscode-dark or vscode-high-contrast on <body>.
const followTheme = () => {
  const dark = document.body.classList.contains("vscode-dark") || document.body.classList.contains("vscode-high-contrast")
    && !document.body.classList.contains("vscode-high-contrast-light");
  document.documentElement.dataset.theme = dark ? "dark" : "light";
};
followTheme();
new MutationObserver(followTheme).observe(document.body, { attributes: true, attributeFilter: ["class"] });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ReactFlowProvider>
      <PipelineView host={host} />
    </ReactFlowProvider>
  </StrictMode>,
);
