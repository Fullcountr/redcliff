import {
  Background, BackgroundVariant, ReactFlow, type Connection, type Edge, type EdgeChange, type NodeChange,
  applyNodeChanges, useReactFlow,
} from "@xyflow/react";
import {
  BookOpen, Boxes, ChevronDown, CircleCheck, FileCode2, FileSpreadsheet, Layers, Lock, Maximize, Minus,
  Monitor, Moon, MousePointerClick, Network, Plus, Share2, Sparkles, Sun, Upload, WandSparkles, Waypoints, Workflow,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { data } from "./engine/data";
import { engine, engineReady, startEngine } from "./engine/engine";
import { ImportDialog, type ImportMode, type ImportSamples } from "./components/ImportDialog";
import { Inspector, type Tab } from "./components/Inspector";
import { ModelView, type ModelTab } from "./components/ModelView";
import { StepNode, type StepNodeType } from "./components/StepNode";
import { Variables } from "./components/Variables";
import { layout } from "./layout";
import { fromImpact, trace, type Highlight } from "./lineage";
import {
  defaultParams, refParts, type Analysis, type DataModel, type Impact, type ImportResult, type Scorecard, type ScoreStatus,
  type Spec, type Step, type StepType, tally, type CheckRun,
} from "./model";
import { shareLink, specFromHash } from "./share";
import { BrandMark, Menu, MenuItem, useTheme, type Theme } from "./ui";

type Precomputed = { analysis: Analysis; counts: Record<string, number>; impacts: Record<string, Impact> };
type Samples = {
  tables: string[]; specs: Record<string, Spec>; models: Record<string, DataModel>; legacy: string; powercenter: string;
  workflow: { xml: string; params: string };
  storeMart: string;
  ssis: string;
  talend: string;
  precomputed: Record<string, Precomputed>;
};
type EngineState = { state: "loading" | "ready" | "failed"; ms?: number; error?: string };

const nodeTypes = { step: StepNode };
const BASE = import.meta.env.BASE_URL;
const RANK: Record<ScoreStatus, number> = { converted: 0, review: 1, manual: 2 };

const EXAMPLES: Record<string, string> = {
  orders_revenue: "Join, filter and aggregate two CSVs",
  customer_cleanup: "Tidy names and emails, derive columns",
  daily_events: "Union two event streams by day",
};

/** Fit the pipeline into the part of the canvas the floating bar and inspector leave free. */
function fitPadding() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  if (w <= 900) return { top: "72px", right: "24px", bottom: `${Math.round(h * 0.46) + 40}px`, left: "24px" } as const;
  const inspector = w <= 1100 ? 350 : 400;
  return { top: "112px", right: `${inspector + 48}px`, bottom: "72px", left: "72px" } as const;
}

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
  // The pipeline as loaded: the Changes tab compares edits with it.
  const [baseline, setBaseline] = useState<Spec | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [eng, setEng] = useState<EngineState>({ state: "loading" });
  const [dataReady, setDataReady] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const [impact, setImpact] = useState<Impact | null>(null);
  const [counts, setCounts] = useState<Record<string, number | null>>({});
  const [checkRuns, setCheckRuns] = useState<CheckRun[]>([]);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [notes, setNotes] = useState<string[]>([]);
  const [scorecard, setScorecard] = useState<Scorecard | null>(null);
  const [activeItem, setActiveItem] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("step");
  const [importing, setImporting] = useState<{
    mode: ImportMode | null; busy: boolean; error: string | null; sample?: "workflow" | "store";
  }>({ mode: null, busy: false, error: null });
  const [toast, setToast] = useState<string | null>(null);
  const [example, setExample] = useState("orders_revenue");
  const [view, setView] = useState<"pipeline" | "model">("pipeline");
  const [dataModel, setDataModel] = useState<DataModel | null>(null);
  const [modelTab, setModelTab] = useState<ModelTab>("overview");
  // The legacy job a model was proposed from, so the model view can show the reasons and the coverage.
  const [job, setJob] = useState<{ spec: Spec; reasons: Record<string, string[]> } | null>(null);
  useEffect(() => { if (importing.mode) setView("pipeline"); }, [importing.mode]);
  const [theme, setTheme, dark] = useTheme();
  const fileInput = useRef<HTMLInputElement>(null);
  const dataLoaded = useRef<Promise<void> | null>(null);
  const flow = useReactFlow();
  const ready = eng.state === "ready";
  const runnable = ready && dataReady;

  const say = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast((t) => (t === message ? null : t)), 3200);
  }, []);

  // ---------------------------------------------------------------- boot
  useEffect(() => {
    startEngine();
    engineReady.then(
      (ms) => setEng({ state: "ready", ms }),
      (err) => setEng({ state: "failed", error: String(err) }),
    );
    const boot = (async () => {
      const [s, precomputed] = await Promise.all([
        fetch(`${BASE}samples/samples.json`).then((r) => r.json()),
        fetch(`${BASE}samples/precomputed.json`).then((r) => r.json()).catch(() => ({})),
      ]);
      setSamples({ ...s, precomputed });
      const shared = await specFromHash(window.location.hash);
      setSpec(shared ?? s.specs.orders_revenue);
      setBaseline(shared ?? s.specs.orders_revenue);
      if (shared) setExample("");
      // Python first, with the whole connection to itself; DuckDB (previews) after.
      await engineReady.catch(() => undefined);
      await data.start();
      await data.loadSamples(BASE, s.tables);
      setDataReady(true);
    })();
    dataLoaded.current = boot;
    boot.catch((e) => setEng({ state: "failed", error: String(e) }));
  }, []);

  // An unchanged example has its lineage, row counts and blast radius precomputed at build time,
  // so it's interactive before the engine has loaded.
  const fresh = samples && spec && example && spec === samples.specs[example] ? samples.precomputed[example] ?? null : null;

  // ---------------------------------------------------------------- analysis and row counts
  useEffect(() => {
    if (fresh) { setAnalysis(fresh.analysis); return; }
    if (!ready || !spec) return;
    let live = true;
    engine.analyze(spec).then((a) => {
      if (!live) return;
      if (a.ok) setAnalysis(a);
      else setAnalysis({ valid: false, issues: [{ node: null, message: a.error }], columns: {}, edges: [], dataset_edges: [] });
    });
    return () => { live = false; };
  }, [spec, ready, fresh]);

  useEffect(() => {
    if (fresh) { setCounts(fresh.counts); return; }
    if (!runnable || !spec || !analysis?.valid) return;
    let live = true;
    (async () => {
      const next: Record<string, number | null> = {};
      for (const id of analysis.order ?? []) {
        const step = spec.nodes.find((n) => n.id === id);
        if (!step || step.type === "target") continue;
        try {
          const q = await engine.previewQuery(spec, id);
          next[id] = q.ok ? await data.count(q.sql) : null;
        } catch {
          next[id] = null;
        }
        if (!live) return;
        setCounts({ ...next });
      }
    })();
    return () => { live = false; };
  }, [analysis, spec, runnable, fresh]);

  // Data quality expectations, run in DuckDB once the data is there.
  useEffect(() => {
    if (!runnable || !spec || !analysis?.valid || !spec.nodes.some((n) => n.expect?.length)) { setCheckRuns([]); return; }
    let live = true;
    (async () => {
      const q = await engine.checks(spec);
      if (!q.ok) return;
      const runs: CheckRun[] = [];
      for (const c of q.checks) {
        try {
          const failures = await data.count(c.sql);
          const sample = failures ? await data.query(c.sql, 5) : { columns: [], rows: [] };
          runs.push({ ...c, failures, columns: sample.columns, sample: sample.rows });
        } catch (e) {
          runs.push({ ...c, failures: null, columns: [], sample: [], error: String(e).replace(/^Error: /, "") });
        }
        if (!live) return;
      }
      setCheckRuns(runs);
    })();
    return () => { live = false; };
  }, [analysis, spec, runnable]);

  // ---------------------------------------------------------------- layout
  const autoPositions = useMemo(() => (spec ? layout(spec, analysis?.columns ?? {}) : {}), [spec, analysis]);

  // Refit when the automatic layout changes (new pipeline, new steps, columns arriving).
  const layoutKey = Object.keys(positions).length ? "manual" : JSON.stringify(autoPositions);
  const fit = useCallback(() => flow.fitView({ padding: fitPadding(), maxZoom: 1.05, duration: 250 }), [flow]);
  useEffect(() => {
    if (layoutKey === "manual") return;
    const id = window.requestAnimationFrame(() => flow.fitView({ padding: fitPadding(), maxZoom: 1.05 }));
    return () => window.cancelAnimationFrame(id);
  }, [layoutKey, flow]);

  // ---------------------------------------------------------------- lineage focus
  useEffect(() => {
    if (!pinned || !spec) { setImpact(null); return; }
    if (fresh?.impacts[pinned]) { setImpact(fresh.impacts[pinned]); return; }
    const [node, column] = refParts(pinned);
    let live = true;
    engine.impact(spec, node, column).then((r) => { if (live && r.ok) setImpact(r); });
    return () => { live = false; };
  }, [pinned, spec, fresh]);

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

  const clearFocus = useCallback(() => { setPinned(null); setHovered(null); setActiveItem(null); }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector("dialog[open]")) clearFocus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [clearFocus]);

  // ---------------------------------------------------------------- scorecard on the canvas
  const scoreByNode = useMemo(() => {
    const out: Record<string, ScoreStatus> = {};
    for (const item of scorecard?.items ?? []) {
      for (const n of item.nodes) if (!out[n] || RANK[item.status] > RANK[out[n]]) out[n] = item.status;
    }
    return out;
  }, [scorecard]);
  const activeNodes = useMemo(() => {
    const item = scorecard?.items.find((i) => i.name === activeItem);
    return item ? { nodes: new Set(item.nodes), status: item.status } : null;
  }, [scorecard, activeItem]);

  // ---------------------------------------------------------------- spec edits
  const replaceSpec = useCallback((next: Spec, keepPositions = true) => {
    setSpec(next);
    if (!keepPositions) { setPositions({}); setBaseline(next); }
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
        checks: step.expect?.length ? tally(checkRuns.filter((r) => r.node === step.id)) : null,
        issues: issuesByNode[step.id] ?? [],
        highlight,
        selected: step.id === selectedId,
        score: scoreByNode[step.id] ?? null,
        scored: activeNodes?.nodes.has(step.id) ? activeNodes.status : null,
        onHoverColumn: setHovered,
        onPinColumn: (ref: string) => setPinned((p) => (p === ref ? null : ref)),
      },
    }));
  }, [spec, analysis, counts, checkRuns, positions, autoPositions, selectedId, highlight, scoreByNode, activeNodes]);

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
  const loaded = (r: ImportResult, label: string) => {
    replaceSpec(r.spec, false);
    setNotes(r.warnings);
    setScorecard(r.scorecard);
    setActiveItem(null);
    setSelectedId(null);
    setExample("");
    setImporting({ mode: null, busy: false, error: null });
    const c = r.scorecard.counts;
    if (r.scorecard.origin === "powercenter" || r.scorecard.origin === "talend") {
      setTab("migration");
      say(`${label}: ${r.spec.nodes.length} steps, ${Math.round(r.scorecard.automated * 100)}% converted automatically`);
    } else {
      setTab("step");
      say(`${label}: ${r.spec.nodes.length} steps${c.review ? `, ${c.review} to review` : ""}`);
    }
  };

  const doImport = async (mode: ImportMode, text: string, dialect: string, params: string) => {
    setImporting((s) => ({ ...s, mode, busy: true, error: null }));
    const r = mode === "powercenter"
      ? await engine.importPowercenter(text, params)
      : mode === "ssis"
        ? await engine.importSsis(text)
        : mode === "talend"
          ? await engine.importTalend(text, params)
          : await engine.importSql(text, dialect, await dataLoaded.current!.then(data.schema, () => ({})), "imported");
    if (!r.ok) {
      setImporting((s) => ({ ...s, mode, busy: false, error: r.error }));
      return;
    }
    loaded(r, mode === "powercenter" ? "Imported the mapping" : mode === "ssis" ? "Imported the package"
      : mode === "talend" ? "Imported the job" : "Imported the SQL");
  };

  const loadExample = (name: string) => {
    if (!samples) return;
    setExample(name);
    replaceSpec(samples.specs[name], false);
    setNotes([]);
    setScorecard(null);
    setActiveItem(null);
    setSelectedId(null);
    if (tab === "migration") setTab("step");
  };

  const addFiles = async (files: FileList | File[]) => {
    if (!spec) return;
    let next = spec;
    for (const file of Array.from(files)) {
      if (!/\.csv$/i.test(file.name)) { say(`${file.name} isn't a CSV file`); continue; }
      const { table, columns } = await data.addCsv(file.name, await file.text());
      const id = uniqueId(next, table);
      next = { ...next, nodes: [...next.nodes, { id, type: "source", params: { format: "csv", path: file.name, columns } }] };
      say(`Added ${file.name} as a source`);
    }
    replaceSpec(next);
  };

  const openModel = (m: DataModel | null, t: ModelTab = "overview") => {
    const next = m ?? dataModel ?? (samples ? Object.values(samples.models)[0] : null);
    if (!next) return;
    if (m) setJob(null);
    setDataModel(next);
    setModelTab(t);
    setView("model");
  };

  const modelFromJob = async (style: "kimball" | "data_vault") => {
    if (!spec) return;
    const r = await engine.modelFromJob(spec, style);
    if (!r.ok) { say(`Couldn't propose a model: ${r.error}`); return; }
    setJob({ spec, reasons: r.reasons });
    setDataModel(r.model);
    setModelTab("overview");
    setView("model");
    say(`Proposed ${r.model.entities.length} entities from ${spec.name}; ${Math.round(r.map.coverage * 100)}% of what it writes comes from them`);
  };

  const share = async () => {
    if (!spec) return;
    const url = await shareLink(spec);
    window.history.replaceState(null, "", url);
    try {
      await navigator.clipboard.writeText(url);
      say("Link copied. The pipeline lives in the link; nothing is uploaded.");
    } catch {
      say("The link is in the address bar. Nothing is uploaded.");
    }
  };

  // ---------------------------------------------------------------- render
  const selected = spec?.nodes.find((n) => n.id === selectedId) ?? null;
  const issueCount = analysis?.issues.length ?? 0;
  const ThemeIcon = theme === "system" ? Monitor : dark ? Moon : Sun;
  const nextTheme: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };

  return (
    <div className="app">
      {view === "model" && dataModel && (
        <ModelView model={dataModel} engineReady={ready} dataReady={dataReady} tab={modelTab} onTab={setModelTab} job={job} onClearJob={() => setJob(null)}
          onChange={(m, note) => { setDataModel(m); if (note) say(note); }}
          onOpenPipeline={(s) => { setView("pipeline"); setExample(""); replaceSpec(s, false); setNotes([]); setScorecard(null);
            setSelectedId(null); say(`Opened ${s.name}: the load generated from the model`); }} />
      )}
      {view === "pipeline" && <main
        className="canvas"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files); }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={(_, n) => setSelectedId(n.id)}
          onNodeDoubleClick={(_, n) => flow.fitView({ nodes: [{ id: n.id }], padding: fitPadding(), maxZoom: 1, duration: 300 })}
          zoomOnDoubleClick={false}
          onPaneClick={() => { setSelectedId(null); clearFocus(); }}
          fitView
          fitViewOptions={{ padding: fitPadding(), maxZoom: 1.05 }}
          minZoom={0.1}
          proOptions={{ hideAttribution: true }}
          deleteKeyCode={["Backspace", "Delete"]}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
        </ReactFlow>
      </main>}

      <header className="topbar glass">
        <div className="brand">
          <BrandMark />
          <span className="brand-name">Redcliff</span>
          <span className="crumb-sep" aria-hidden="true">/</span>
          {view === "model" && dataModel && (
            <input
              className="pipeline-name"
              aria-label="Model name"
              value={dataModel.name}
              spellCheck={false}
              onChange={(e) => setDataModel({ ...dataModel, name: e.target.value })}
            />
          )}
          {view === "pipeline" && spec && (
            <input
              className="pipeline-name"
              aria-label="Pipeline name"
              value={spec.name}
              spellCheck={false}
              onChange={(e) => setSpec({ ...spec, name: e.target.value })}
            />
          )}
          {view === "pipeline" && scorecard && (
            <button className="chip ok" onClick={() => setTab("migration")} title="Open the migration scorecard">
              <CircleCheck />{Math.round(scorecard.automated * 100)}% automatic
            </button>
          )}
        </div>
        <nav className="bar-actions" aria-label="Pipeline">
          {view === "model" && (
            <button className="btn ghost" onClick={() => setView("pipeline")} title="Back to the pipeline canvas">
              <Waypoints /><span className="label">Pipelines</span>
            </button>
          )}
          <Menu
            label="Examples"
            button={({ open, toggle }) => (
              <button className="btn ghost" onClick={toggle} aria-expanded={open} disabled={!samples}>
                <BookOpen /><span className="label">Examples</span><ChevronDown className="caret" />
              </button>
            )}
          >
            {(close) => (
              <>
                <div className="menu-label">Pipelines</div>
                {samples && Object.keys(samples.specs).map((k) => (
                  <MenuItem key={k} icon={Waypoints} title={k.replace(/_/g, " ")} sub={EXAMPLES[k]}
                    checked={view === "pipeline" && example === k} onClick={() => { close(); setView("pipeline"); loadExample(k); }} />
                ))}
                <div className="menu-sep" />
                <div className="menu-label">Data models</div>
                {samples && Object.entries(samples.models).map(([k, m]) => (
                  <MenuItem key={k} icon={Layers} title={k.replace(/_/g, " ")}
                    sub={`${m.style === "data_vault" ? "Data Vault" : "Kimball"}: ${m.entities.map((e) => e.id).join(", ")}`}
                    checked={view === "model" && dataModel?.name === m.name} onClick={() => { close(); openModel(m); }} />
                ))}
                <div className="menu-sep" />
                <div className="menu-label">Legacy jobs to import</div>
                <MenuItem icon={FileCode2} title="Teradata BTEQ job" sub="CTEs, QUALIFY, CREATE TABLE AS"
                  onClick={() => { close(); setImporting({ mode: "sql", busy: false, error: null }); }} />
                <MenuItem icon={Workflow} title="PowerCenter mapping" sub="11 transformations, 2 targets"
                  onClick={() => { close(); setImporting({ mode: "powercenter", busy: false, error: null }); }} />
                <MenuItem icon={Workflow} title="PowerCenter workflow" sub="Mapplet, session, parameter file, tasks"
                  onClick={() => { close(); setImporting({ mode: "powercenter", busy: false, error: null, sample: "workflow" }); }} />
                <MenuItem icon={Workflow} title="PowerCenter store mart" sub="Normalizer, Rank, worklet"
                  onClick={() => { close(); setImporting({ mode: "powercenter", busy: false, error: null, sample: "store" }); }} />
                <MenuItem icon={Boxes} title="SSIS package" sub="Split, merge join, lookup, aggregate"
                  onClick={() => { close(); setImporting({ mode: "ssis", busy: false, error: null }); }} />
                <MenuItem icon={Network} title="Talend job" sub="tMap lookups and rejects, aggregates, upsert"
                  onClick={() => { close(); setImporting({ mode: "talend", busy: false, error: null }); }} />
              </>
            )}
          </Menu>
          <button className="btn ghost" onClick={() => fileInput.current?.click()} disabled={!runnable || view !== "pipeline"} title="Add CSV files as sources">
            <FileSpreadsheet /><span className="label">Add CSV</span>
          </button>
          <input ref={fileInput} type="file" accept=".csv" multiple hidden onChange={(e) => e.target.files && addFiles(e.target.files)} />
          <button className="btn ghost icon" onClick={() => setTheme(nextTheme[theme])}
            aria-label={`Theme: ${theme}`} title={`Theme: ${theme}`}>
            <ThemeIcon />
          </button>
          <span className="bar-divider" />
          <button className="btn" onClick={share} disabled={!spec || view !== "pipeline"} aria-label="Share link" title="Copy a link that holds this pipeline">
            <Share2 /><span className="label">Share</span>
          </button>
          <Menu
            label="Import"
            button={({ open, toggle }) => (
              <button className="btn primary" onClick={toggle} aria-expanded={open} disabled={!ready}
                title={ready ? "Import a legacy job" : "Available once the engine has started"}>
                <Upload /><span className="label">Import</span><ChevronDown className="caret" />
              </button>
            )}
          >
            {(close) => (
              <>
                <MenuItem icon={FileCode2} title="SQL script" sub="Teradata, Oracle, SQL Server and more"
                  onClick={() => { close(); setImporting({ mode: "sql", busy: false, error: null }); }} />
                <MenuItem icon={Workflow} title="Informatica PowerCenter" sub="Mapping XML, with a migration scorecard"
                  onClick={() => { close(); setImporting({ mode: "powercenter", busy: false, error: null }); }} />
                <MenuItem icon={Boxes} title="SSIS package" sub=".dtsx data flows, with a migration scorecard"
                  onClick={() => { close(); setView("pipeline"); setImporting({ mode: "ssis", busy: false, error: null }); }} />
                <MenuItem icon={Network} title="Talend job" sub=".item jobs, with a migration scorecard"
                  onClick={() => { close(); setView("pipeline"); setImporting({ mode: "talend", busy: false, error: null }); }} />
                <div className="menu-sep" />
                <MenuItem icon={Layers} title="Data model" sub="Propose one from your data, or paste DDL or DBML"
                  onClick={() => { close(); openModel(null, "import"); }} />
              </>
            )}
          </Menu>
        </nav>
      </header>

      {view === "pipeline" && spec && spec.variables && Object.keys(spec.variables).length > 0 && !importing.mode && (
        <Variables values={spec.variables} belowHint={!selectedId && !highlight}
          onChange={(variables) => replaceSpec({ ...spec, variables })} />
      )}

      {view === "pipeline" && spec && !selectedId && !highlight && !importing.mode && (
        <div className="hint glass">
          <span><MousePointerClick />Hover a column to trace it</span>
          <span className="dot-sep" />
          <span>Click it for its blast radius</span>
          <span className="dot-sep" />
          <span>Double-click a step to zoom in</span>
          <span className="dot-sep" />
          <span><kbd>Esc</kbd> clears</span>
        </div>
      )}

      {view === "pipeline" && <div className="zoom glass" role="group" aria-label="Zoom">
        <button className="btn ghost icon" onClick={() => flow.zoomIn({ duration: 150 })} aria-label="Zoom in"><Plus /></button>
        <button className="btn ghost icon" onClick={() => flow.zoomOut({ duration: 150 })} aria-label="Zoom out"><Minus /></button>
        <div className="zoom-sep" />
        <button className="btn ghost icon" onClick={fit} aria-label="Fit to screen" title="Fit to screen"><Maximize /></button>
        <button className="btn ghost icon" onClick={() => setPositions({})} aria-label="Tidy layout" title="Tidy layout"><WandSparkles /></button>
      </div>}

      {view === "pipeline" && spec && (
        <Inspector
          tab={tab}
          onTab={setTab}
          spec={spec}
          baseline={baseline}
          onRebase={() => setBaseline(spec)}
          onModelFromJob={(style) => void modelFromJob(style)}
          analysis={analysis}
          selected={selected}
          impact={impact}
          notes={notes}
          ready={runnable}
          engineReady={ready}
          checkRuns={checkRuns}
          scorecard={scorecard}
          activeItem={activeItem}
          onPickItem={setActiveItem}
          onSelectStep={(id) => setSelectedId(id)}
          onChangeSpec={(s) => replaceSpec(s)}
          onUpdateStep={updateStep}
          onDeleteStep={deleteStep}
          onAddStep={addStep}
          onClearFocus={clearFocus}
        />
      )}

      <footer className="statusbar glass">
        <span data-testid="engine-status">
          <span className={`status-dot ${eng.state}`} />
          {eng.state === "ready" ? `Engine ready in ${((eng.ms ?? 0) / 1000).toFixed(1)} s` : eng.state === "failed" ? "Engine failed" : "Starting the engine"}
        </span>
        {ready && !dataReady && <><span className="sep" /><span className="faint">Loading DuckDB for previews</span></>}
        {view === "model" && dataModel && <><span className="sep" /><span>{dataModel.entities.length} entities</span></>}
        {view === "pipeline" && spec && <><span className="sep" /><span>{spec.nodes.length} steps</span></>}
        {view === "pipeline" && analysis && (
          <><span className="sep" /><span className={issueCount ? "issues" : ""}>
            {issueCount ? `${issueCount} ${issueCount === 1 ? "issue" : "issues"}` : "No issues"}
          </span></>
        )}
        <span className="sep" />
        <span className="local"><Lock />Runs in your browser</span>
      </footer>

      {toast && <div className="toast" role="status"><Sparkles />{toast}</div>}

      {eng.state === "failed" && (
        <div className="boot" role="alert">
          <div className="boot-card">
            <div className="boot-mark" aria-hidden="true"><span /><span /><span /></div>
            <h1>The engine didn't start</h1>
            <p className="error">{eng.error}</p>
            <p>Redcliff needs a recent browser with WebAssembly. Reloading the page usually helps.</p>
          </div>
        </div>
      )}

      {importing.mode && samples && (
        <ImportDialog
          mode={importing.mode}
          samples={(importing.sample === "workflow"
            ? { sql: samples.legacy, powercenter: samples.workflow.xml, params: samples.workflow.params, ssis: samples.ssis, talend: samples.talend }
            : importing.sample === "store"
              ? { sql: samples.legacy, powercenter: samples.storeMart, ssis: samples.ssis, talend: samples.talend }
              : { sql: samples.legacy, powercenter: samples.powercenter, ssis: samples.ssis, talend: samples.talend }) satisfies ImportSamples}
          busy={importing.busy}
          error={importing.error}
          onImport={doImport}
          onClose={() => setImporting({ mode: null, busy: false, error: null })}
        />
      )}
    </div>
  );
}
