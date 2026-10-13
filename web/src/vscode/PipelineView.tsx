// The pipeline canvas inside VS Code: the spec open in the editor drawn as steps, with column
// lineage on hover, blast radius on click, issues and generated code. The editor holds the spec;
// this view only reads it (edit the YAML, the canvas follows) and jumps to a step's lines on click.
import {
  applyNodeChanges, Background, BackgroundVariant, ReactFlow, useReactFlow, type Edge, type NodeChange,
} from "@xyflow/react";
import * as yaml from "js-yaml";
import { CircleAlert, CircleCheck, Code2, Copy, Maximize, Minus, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { StepNode, type StepNodeType } from "../components/StepNode";
import { engine } from "../engine/engine";
import { layout } from "../layout";
import { fromImpact, trace, type Highlight } from "../lineage";
import { refParts, type Analysis, type Impact, type Spec } from "../model";
import { highlight as colour } from "../ui";

export type Host = { postMessage: (message: unknown) => void };

const nodeTypes = { step: StepNode };
const TARGETS = [["spark", "Spark SQL"], ["pyspark", "PySpark"], ["duckdb", "DuckDB SQL"]] as const;

/** A spec from YAML text, or why it isn't one. */
export function readSpec(text: string): { spec: Spec } | { error: string } {
  try {
    const data = yaml.load(text) as Spec | null;
    if (!data || typeof data !== "object" || !Array.isArray((data as Spec).nodes)) {
      return { error: "This file isn't a Redcliff pipeline spec (it has no nodes list)." };
    }
    return { spec: data };
  } catch (e) {
    return { error: `The YAML doesn't parse: ${(e as Error).message.split("\n")[0]}` };
  }
}

export function PipelineView({ host }: { host: Host }) {
  const flow = useReactFlow();
  const [file, setFile] = useState("");
  const [spec, setSpec] = useState<Spec | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const [impact, setImpact] = useState<Impact | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<"issues" | "code">("issues");
  const [target, setTarget] = useState<string>("spark");
  const [code, setCode] = useState<string>("");

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const msg = e.data;
      if (msg?.type === "spec") {
        setFile(msg.file ?? "");
        const read = readSpec(msg.text ?? "");
        if ("spec" in read) { setSpec(read.spec); setProblem(null); } else setProblem(read.error);
      } else if (msg?.type === "bridge-error") {
        setEngineError(msg.message);
      }
    };
    window.addEventListener("message", onMessage);
    host.postMessage({ type: "ready" });
    return () => window.removeEventListener("message", onMessage);
  }, [host]);

  useEffect(() => {
    if (!spec) return;
    let live = true;
    const id = window.setTimeout(() => {
      engine.analyze(spec).then((r) => {
        if (!live) return;
        if (r.ok) { setAnalysis(r); setEngineError(null); } else setEngineError(r.error);
      });
    }, 120);
    return () => { live = false; window.clearTimeout(id); };
  }, [spec]);

  useEffect(() => {
    if (!pinned || !spec) { setImpact(null); return; }
    const [node, column] = refParts(pinned);
    let live = true;
    engine.impact(spec, node, column).then((r) => { if (live && r.ok) setImpact(r); });
    return () => { live = false; };
  }, [pinned, spec]);

  useEffect(() => {
    if (tab !== "code" || !spec) return;
    let live = true;
    engine.code(spec, target, selectedId).then((r) => { if (live) setCode(r.ok ? r.code : `-- ${r.error}`); });
    return () => { live = false; };
  }, [tab, spec, target, selectedId]);

  const highlight: Highlight | null = useMemo(() => {
    if (!analysis) return null;
    if (pinned && impact && impact.column === pinned) {
      const h = fromImpact(analysis, impact);
      for (const n of spec?.nodes ?? []) {
        for (const i of n.inputs ?? []) if (h.nodes.has(i) && h.rowNodes.has(n.id)) h.links.add(`${i}->${n.id}`);
      }
      return h;
    }
    if (hovered && !pinned) return trace(analysis, hovered);
    return null;
  }, [analysis, hovered, pinned, impact, spec]);

  const positions = useMemo(() => (spec ? layout(spec, analysis?.columns ?? {}) : {}), [spec, analysis]);
  const layoutKey = JSON.stringify(positions);
  useEffect(() => {
    const id = window.requestAnimationFrame(() => flow.fitView({ padding: 0.12, maxZoom: 1.05 }));
    return () => window.cancelAnimationFrame(id);
  }, [layoutKey, flow]);

  const issues = useMemo(() => analysis?.issues ?? [], [analysis]);
  const derived: StepNodeType[] = useMemo(() => {
    if (!spec) return [];
    const byNode: Record<string, string[]> = {};
    for (const i of issues) if (i.node) (byNode[i.node] ??= []).push(i.message);
    return spec.nodes.map((step) => ({
      id: step.id,
      type: "step" as const,
      position: positions[step.id] ?? { x: 0, y: 0 },
      selected: step.id === selectedId,
      data: {
        step, columns: analysis?.columns[step.id] ?? [], rowCount: undefined, checks: null, issues: byNode[step.id] ?? [],
        highlight, selected: step.id === selectedId, score: null, scored: null,
        onHoverColumn: setHovered, onPinColumn: (ref: string) => setPinned((p) => (p === ref ? null : ref)),
      },
    }));
  }, [spec, analysis, issues, positions, selectedId, highlight]);

  // Keep each node's measurements across re-renders, as the playground does (React Flow hides unmeasured nodes).
  const [nodes, setNodes] = useState<StepNodeType[]>([]);
  useEffect(() => {
    setNodes((prev) => {
      const old = new Map(prev.map((n) => [n.id, n]));
      return derived.map((n) => {
        const p = old.get(n.id);
        return p ? { ...p, ...n, measured: p.measured, width: p.width, height: p.height } : n;
      });
    });
  }, [derived]);

  const edges: Edge[] = useMemo(() => (spec?.nodes ?? []).flatMap((n) => (n.inputs ?? []).map((i) => {
    const key = `${i}->${n.id}`;
    const lit = highlight?.links.has(key);
    return { id: key, source: i, target: n.id,
             className: lit ? (highlight!.mode === "impact" ? "edge-impact" : "edge-trace") : highlight ? "edge-dim" : "" };
  })), [spec, highlight]);

  const reveal = useCallback((node: string) => host.postMessage({ type: "reveal", node }), [host]);
  const coloured = useMemo(() => colour(code, target === "pyspark"), [code, target]);

  if (problem && !spec) {
    return <div className="vs-empty" data-testid="vs-problem"><CircleAlert />{problem}</div>;
  }
  return (
    <div className="vs-root">
      <main className="canvas vs-canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={(changes: NodeChange<StepNodeType>[]) => {
            const kept = changes.filter((c) => c.type !== "remove" && c.type !== "select");  // the YAML owns the steps
            if (kept.length) setNodes((prev) => applyNodeChanges(kept, prev));
          }}
          nodesDraggable={false}
          nodesConnectable={false}
          onNodeClick={(_, n) => { setSelectedId(n.id); reveal(n.id); }}
          onPaneClick={() => { setSelectedId(null); setPinned(null); setHovered(null); }}
          minZoom={0.1}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
        </ReactFlow>
        <div className="zoom glass vs-zoom" role="group" aria-label="Zoom">
          <button className="btn ghost icon" onClick={() => flow.zoomIn({ duration: 150 })} aria-label="Zoom in"><Plus /></button>
          <button className="btn ghost icon" onClick={() => flow.zoomOut({ duration: 150 })} aria-label="Zoom out"><Minus /></button>
          <button className="btn ghost icon" onClick={() => flow.fitView({ padding: 0.12, maxZoom: 1.05, duration: 250 })}
            aria-label="Fit to screen"><Maximize /></button>
        </div>
      </main>
      <aside className="vs-panel" aria-label="Pipeline">
        <div className="vs-head">
          <strong className="mono" title={file}>{spec?.name ?? "pipeline"}</strong>
          <span className="quiet">{spec?.nodes.length ?? 0} steps{problem ? " · showing the last version that parsed" : ""}</span>
        </div>
        {engineError && <p className="error vs-error" data-testid="vs-engine-error">{engineError}</p>}
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "issues"} className={`tab${tab === "issues" ? " is-active" : ""}`}
            onClick={() => setTab("issues")}>
            {issues.length ? <CircleAlert /> : <CircleCheck />}Issues{issues.length ? ` ${issues.length}` : ""}
          </button>
          <button role="tab" aria-selected={tab === "code"} className={`tab${tab === "code" ? " is-active" : ""}`}
            onClick={() => setTab("code")}><Code2 />Code</button>
        </div>
        <div className="panel vs-body">
          {tab === "issues" ? (
            !analysis ? <p className="quiet">Reading the pipeline…</p>
              : issues.length === 0 ? (
                <p className="ok-line" data-testid="vs-valid"><CircleCheck />No issues. Hover a column for its lineage; click
                  one for its blast radius; click a step to jump to it.</p>
              ) : (
                <ul className="issue-list" data-testid="vs-issues">
                  {issues.map((i, k) => (
                    <li key={k}>
                      <CircleAlert />
                      {i.node ? <button className="link-btn mono" onClick={() => { setSelectedId(i.node); reveal(i.node!); }}>
                        {i.node}</button> : null}{i.node ? ": " : ""}{i.message}
                    </li>
                  ))}
                </ul>
              )
          ) : (
            <>
              <div className="row between">
                <div className="segmented small" role="group" aria-label="Language">
                  {TARGETS.map(([id, label]) => (
                    <button key={id} aria-pressed={target === id} onClick={() => setTarget(id)}>{label}</button>
                  ))}
                </div>
                <button className="btn sm ghost" onClick={() => navigator.clipboard?.writeText(code)}><Copy />Copy</button>
              </div>
              <p className="quiet">{selectedId ? <>The code for <span className="mono">{selectedId}</span>; click the canvas
                background for the whole pipeline.</> : "The whole pipeline; click a step for its code alone."}</p>
              <div className="code-shell">
                <pre className="code-block" data-testid="vs-code"><code>{coloured}</code></pre>
              </div>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
