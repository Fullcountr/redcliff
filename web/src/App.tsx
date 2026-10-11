import {
  Background, Controls, ReactFlow, type Connection, type Edge, type EdgeChange, type NodeChange,
  applyNodeChanges, useReactFlow,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { addCsv, count, loadSamples, startDuck, tables } from "./engine/duck";
import { engine, engineReady, startEngine } from "./engine/engine";
import { ImportDialog } from "./components/ImportDialog";
import { Inspector, type Tab } from "./components/Inspector";
import { StepNode, type StepNodeType } from "./components/StepNode";
import { layout } from "./layout";
import { fromImpact, trace, type Highlight } from "./lineage";
import { defaultParams, refParts, type Analysis, type Impact, type Spec, type Step, type StepType } from "./model";
import { shareLink, specFromHash } from "./share";

type Samples = { tables: string[]; specs: Record<string, Spec>; legacy: string };
type EngineState = { state: "loading" | "ready" | "failed"; ms?: number; error?: string };

const nodeTypes = { step: StepNode };
const BASE = import.meta.env.BASE_URL;

function uniqueId(spec: Spec, base: string): string {
  const taken = new Set(spec.nodes.map((n) => n.id));
  let id = base;
  let i = 2;
  while (taken.has(id)) id = `${base}_${i++}`;
  return id;
}

export default function App() {
  const [samples, setSamples] = useState<Samples | null>(null);
  const [spec, setSpec] = useState<Spec | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [eng, setEng] = useState<EngineState>({ state: "loading" });
  const [dataReady, setDataReady] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const [impact, setImpact] = useState<Impact | null>(null);
  const [counts, setCounts] = useState<Record<string, number | null>>({});
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [notes, setNotes] = useState<string[]>([]);
  const [tab, setTab] = useState<Tab>("step");
  const [importing, setImporting] = useState<{ open: boolean; busy: boolean; error: string | null }>({
    open: false, busy: false, error: null,
  });
  const [toast, setToast] = useState<string | null>(null);
  const [example, setExample] = useState("orders_revenue");
  const fileInput = useRef<HTMLInputElement>(null);
  const flow = useReactFlow();
  const ready = eng.state === "ready";
  const runnable = ready && dataReady;

  const say = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast((t) => (t === message ? null : t)), 2800);
  }, []);

  // ---------------------------------------------------------------- boot
  useEffect(() => {
    startEngine();
    engineReady.then(
      (ms) => setEng({ state: "ready", ms }),
      (err) => setEng({ state: "failed", error: String(err) }),
    );
    (async () => {
      const s: Samples = await (await fetch(`${BASE}samples/samples.json`)).json();
      setSamples(s);
      const shared = await specFromHash(window.location.hash);
      setSpec(shared ?? s.specs.orders_revenue);
      if (shared) setExample("");
      await startDuck();
      await loadSamples(BASE, s.tables);
      setDataReady(true);
    })().catch((e) => setEng({ state: "failed", error: String(e) }));
  }, []);

  // ---------------------------------------------------------------- analysis and row counts
  useEffect(() => {
    if (!ready || !spec) return;
    let live = true;
    engine.analyze(spec).then((a) => {
      if (!live) return;
      if (a.ok) setAnalysis(a);
      else setAnalysis({ valid: false, issues: [{ node: null, message: a.error }], columns: {}, edges: [], dataset_edges: [] });
    });
    return () => { live = false; };
  }, [spec, ready]);

  useEffect(() => {
    if (!runnable || !spec || !analysis?.valid) return;
    let live = true;
    (async () => {
      const next: Record<string, number | null> = {};
      for (const id of analysis.order ?? []) {
        const step = spec.nodes.find((n) => n.id === id);
        if (!step || step.type === "target") continue;
        try {
          const q = await engine.previewQuery(spec, id);
          next[id] = q.ok ? await count(q.sql) : null;
        } catch {
          next[id] = null;
        }
        if (!live) return;
        setCounts({ ...next });
      }
    })();
    return () => { live = false; };
  }, [analysis, spec, runnable]);

  // ---------------------------------------------------------------- layout
  const autoPositions = useMemo(() => (spec ? layout(spec, analysis?.columns ?? {}) : {}), [spec, analysis]);

  // Refit when the automatic layout changes (new pipeline, new steps, columns arriving).
  const layoutKey = Object.keys(positions).length ? "manual" : JSON.stringify(autoPositions);
  useEffect(() => {
    if (layoutKey === "manual") return;
    const id = window.requestAnimationFrame(() => flow.fitView({ padding: 0.12, maxZoom: 1.1 }));
    return () => window.cancelAnimationFrame(id);
  }, [layoutKey, flow]);

  // ---------------------------------------------------------------- lineage focus
  useEffect(() => {
    if (!pinned || !spec) { setImpact(null); return; }
    const [node, column] = refParts(pinned);
    let live = true;
    engine.impact(spec, node, column).then((r) => { if (live && r.ok) setImpact(r); });
    return () => { live = false; };
  }, [pinned, spec]);

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

  const clearFocus = useCallback(() => { setPinned(null); setHovered(null); }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") clearFocus(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [clearFocus]);

  // ---------------------------------------------------------------- spec edits
  const replaceSpec = useCallback((next: Spec, keepPositions = true) => {
    setSpec(next);
    if (!keepPositions) setPositions({});
    setPinned(null);
    setHovered(null);
  }, []);

  const updateStep = useCallback((id: string, params: Record<string, unknown>) => {
    if (!spec) return;
    replaceSpec({ ...spec, nodes: spec.nodes.map((n) => (n.id === id ? { ...n, params } : n)) });
  }, [spec, replaceSpec]);

  const deleteStep = useCallback((id: string) => {
    if (!spec) return;
    const nodes = spec.nodes
      .filter((n) => n.id !== id)
      .map((n) => ({ ...n, inputs: (n.inputs ?? []).filter((i) => i !== id) }));
    replaceSpec({ ...spec, nodes });
    setSelectedId(null);
  }, [spec, replaceSpec]);

  const addStep = useCallback((type: StepType) => {
    if (!spec || !selectedId) return;
    const id = uniqueId(spec, type === "sql" ? "sql_step" : type);
    const params = defaultParams(type, analysis?.columns[selectedId] ?? []);
    if (type === "sql") params.query = `SELECT * FROM ${selectedId}`;
    const step: Step = { id, type, inputs: [selectedId], params };
    replaceSpec({ ...spec, nodes: [...spec.nodes, step] });
    setSelectedId(id);
    setTab("step");
  }, [spec, selectedId, analysis, replaceSpec]);

  const onConnect = useCallback((c: Connection) => {
    if (!spec || !c.source || !c.target || c.source === c.target) return;
    replaceSpec({
      ...spec,
      nodes: spec.nodes.map((n) =>
        n.id === c.target && !(n.inputs ?? []).includes(c.source)
          ? { ...n, inputs: [...(n.inputs ?? []), c.source] }
          : n,
      ),
    });
  }, [spec, replaceSpec]);

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    if (!spec) return;
    const removed = changes.filter((c) => c.type === "remove").map((c) => c.id);
    if (!removed.length) return;
    replaceSpec({
      ...spec,
      nodes: spec.nodes.map((n) => ({ ...n, inputs: (n.inputs ?? []).filter((i) => !removed.includes(`${i}->${n.id}`)) })),
    });
  }, [spec, replaceSpec]);

  // ---------------------------------------------------------------- canvas model
  const derivedNodes: StepNodeType[] = useMemo(() => {
    if (!spec) return [];
    const issuesByNode: Record<string, string[]> = {};
    for (const i of analysis?.issues ?? []) if (i.node) (issuesByNode[i.node] ??= []).push(i.message);
    return spec.nodes.map((step) => ({
      id: step.id,
      type: "step" as const,
      position: positions[step.id] ?? autoPositions[step.id] ?? { x: 0, y: 0 },
      selected: step.id === selectedId,
      data: {
        step,
        columns: analysis?.columns[step.id] ?? [],
        rowCount: counts[step.id],
        issues: issuesByNode[step.id] ?? [],
        highlight,
        selected: step.id === selectedId,
        onHoverColumn: setHovered,
        onPinColumn: (ref: string) => setPinned((p) => (p === ref ? null : ref)),
      },
    }));
  }, [spec, analysis, counts, positions, autoPositions, selectedId, highlight]);

  // React Flow hides a node until it is measured, so keep each node's measurements across
  // re-renders (hovering a column re-renders every card) instead of handing it fresh objects.
  const [nodes, setNodes] = useState<StepNodeType[]>([]);
  useEffect(() => {
    setNodes((prev) => {
      const old = new Map(prev.map((n) => [n.id, n]));
      return derivedNodes.map((n) => {
        const p = old.get(n.id);
        return p ? { ...p, ...n, measured: p.measured, width: p.width, height: p.height } : n;
      });
    });
  }, [derivedNodes]);

  const edges: Edge[] = useMemo(() => {
    if (!spec) return [];
    return spec.nodes.flatMap((n) =>
      (n.inputs ?? []).map((i) => {
        const key = `${i}->${n.id}`;
        const lit = highlight?.links.has(key);
        const className = lit ? (highlight!.mode === "impact" ? "edge-impact" : "edge-trace") : highlight ? "edge-dim" : "";
        return { id: key, source: i, target: n.id, className };
      }),
    );
  }, [spec, highlight]);

  const onNodesChange = useCallback((changes: NodeChange<StepNodeType>[]) => {
    const removed = changes.filter((c) => c.type === "remove").map((c) => c.id);
    const rest = changes.filter((c) => c.type !== "remove" && c.type !== "select");
    if (rest.length) setNodes((prev) => applyNodeChanges(rest, prev));
    const moved = changes.filter((c) => c.type === "position" && c.position);
    if (moved.length) {
      setPositions((p) => {
        const copy = { ...p };
        for (const c of moved) if (c.type === "position" && c.position) copy[c.id] = c.position;
        return copy;
      });
    }
    for (const id of removed) deleteStep(id);
  }, [deleteStep]);

  // ---------------------------------------------------------------- actions
  const doImport = async (sql: string, dialect: string) => {
    setImporting({ open: true, busy: true, error: null });
    const schema = Object.fromEntries(tables.entries());
    const r = await engine.importSql(sql, dialect, schema, "imported");
    if (!r.ok) {
      setImporting({ open: true, busy: false, error: r.error });
      return;
    }
    replaceSpec(r.spec, false);
    setNotes(r.warnings);
    setSelectedId(null);
    setExample("");
    setTab("step");
    setImporting({ open: false, busy: false, error: null });
    say(`Imported ${r.spec.nodes.length} steps`);
  };

  const loadExample = (name: string) => {
    if (!samples) return;
    if (name === "__legacy") {
      setImporting({ open: true, busy: false, error: null });
      return;
    }
    setExample(name);
    replaceSpec(samples.specs[name], false);
    setNotes([]);
    setSelectedId(null);
  };

  const addFiles = async (files: FileList | File[]) => {
    if (!spec) return;
    let next = spec;
    for (const file of Array.from(files)) {
      if (!/\.csv$/i.test(file.name)) { say(`${file.name} isn't a CSV file`); continue; }
      const { table, columns } = await addCsv(file.name, await file.text());
      const id = uniqueId(next, table);
      next = { ...next, nodes: [...next.nodes, { id, type: "source", params: { format: "csv", path: file.name, columns } }] };
      say(`Added ${file.name} as a source`);
    }
    replaceSpec(next);
  };

  const share = async () => {
    if (!spec) return;
    const url = await shareLink(spec);
    window.history.replaceState(null, "", url);
    try {
      await navigator.clipboard.writeText(url);
      say("Link copied. The pipeline is stored in the link; nothing is uploaded.");
    } catch {
      say("Link is in the address bar. Nothing is uploaded.");
    }
  };

  // ---------------------------------------------------------------- render
  const selected = spec?.nodes.find((n) => n.id === selectedId) ?? null;
  const issueCount = analysis?.issues.length ?? 0;

  return (
    <div className="app">
      <header className="bar">
        <div className="brand">
          <svg width="22" height="18" viewBox="0 0 22 18" aria-hidden="true">
            <rect x="0" y="0" width="22" height="4" rx="1" className="strata s1" />
            <rect x="3" y="7" width="19" height="4" rx="1" className="strata s2" />
            <rect x="7" y="14" width="15" height="4" rx="1" className="strata s3" />
          </svg>
          <span className="brand-name">Redcliff</span>
          <span className="brand-pipeline">{spec?.name}</span>
        </div>
        <nav className="bar-actions" aria-label="Pipeline">
          <button className="primary" onClick={() => setImporting({ open: true, busy: false, error: null })} disabled={!ready}>
            Import SQL
          </button>
          <label className="visually-hidden" htmlFor="example">Load an example</label>
          <select id="example" value={example} onChange={(e) => loadExample(e.target.value)}>
            <option value="" disabled>Examples</option>
            {samples && Object.keys(samples.specs).map((k) => <option key={k} value={k}>{k.replace(/_/g, " ")}</option>)}
            <option value="__legacy">legacy Teradata job</option>
          </select>
          <button onClick={() => fileInput.current?.click()} disabled={!runnable}>Add CSV</button>
          <input ref={fileInput} type="file" accept=".csv" multiple hidden onChange={(e) => e.target.files && addFiles(e.target.files)} />
          <button onClick={share} disabled={!spec}>Share link</button>
        </nav>
      </header>

      <main
        className="canvas"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files); }}
      >
        {eng.state === "loading" && (
          <div className="boot" role="status">
            <div className="boot-strata" aria-hidden="true"><span /><span /><span /></div>
            <p>Starting the engine in your browser</p>
            <p className="quiet">Python and DuckDB load once. After that, everything runs on this device.</p>
          </div>
        )}
        {eng.state === "failed" && <div className="boot"><p className="error">The engine didn't start: {eng.error}</p></div>}
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={(_, n) => setSelectedId(n.id)}
          onPaneClick={() => { setSelectedId(null); clearFocus(); }}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          minZoom={0.2}
          proOptions={{ hideAttribution: true }}
          deleteKeyCode={["Backspace", "Delete"]}
        >
          <Background gap={24} size={1} />
          <Controls showInteractive={false} />
        </ReactFlow>
        <button className="tidy" onClick={() => setPositions({})}>Tidy layout</button>
        {toast && <div className="toast" role="status">{toast}</div>}
      </main>

      {spec && (
        <Inspector
          tab={tab}
          onTab={setTab}
          spec={spec}
          analysis={analysis}
          selected={selected}
          impact={impact}
          notes={notes}
          ready={runnable}
          onChangeSpec={(s) => replaceSpec(s)}
          onUpdateStep={updateStep}
          onDeleteStep={deleteStep}
          onAddStep={addStep}
          onClearFocus={clearFocus}
        />
      )}

      <footer className="status">
        <span data-testid="engine-status">
          {eng.state === "ready" ? `Engine ready in ${((eng.ms ?? 0) / 1000).toFixed(1)} s` : eng.state === "failed" ? "Engine failed" : "Starting engine"}
        </span>
        <span>{spec ? `${spec.nodes.length} steps` : ""}</span>
        <span className={issueCount ? "status-issues" : ""}>
          {analysis ? (issueCount ? `${issueCount} ${issueCount === 1 ? "issue" : "issues"}` : "No issues") : ""}
        </span>
        <span className="status-note">Runs in your browser. Your data stays on this device.</span>
      </footer>

      {importing.open && samples && (
        <ImportDialog
          initialSql={samples.legacy}
          busy={importing.busy}
          error={importing.error}
          onImport={doImport}
          onClose={() => setImporting({ open: false, busy: false, error: null })}
        />
      )}
    </div>
  );
}
