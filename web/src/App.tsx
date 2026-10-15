import {
  Background, BackgroundVariant, ReactFlow, type Connection, type Edge, type EdgeChange, type NodeChange,
  applyNodeChanges, useReactFlow,
} from "@xyflow/react";
import {
  ArrowLeft, BookOpen, Boxes, Cable, ChevronDown, CircleCheck, FileCode2, FileSpreadsheet, Layers, Lock, Maximize, Minus,
  Monitor, Moon, MousePointerClick, Network, Plus, Share2, Sparkles, Sun, Upload, WandSparkles, Waypoints, Workflow,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { data } from "./engine/data";
import { engine, engineReady, startEngine } from "./engine/engine";
import { ImportDialog, type ImportMode, type ImportSamples } from "./components/ImportDialog";
import { Inspector, type Tab } from "./components/Inspector";
import { ModelStart } from "./components/ModelStart";
import { ModelView, type ModelTab } from "./components/ModelView";
import { StepNode, type StepNodeType } from "./components/StepNode";
import { Variables } from "./components/Variables";
import { fitPadding, layout } from "./layout";
import { fromImpact, trace, type Highlight } from "./lineage";
import {
  defaultParams, refParts, type Analysis, type DataModel, type Impact, type ImportResult, type ModelChange, type ModelExtension,
  type Scorecard, type ScoreStatus, type Spec, type Step, type StepType, tally, type CheckRun,
} from "./model";
import { extendWith, listTables, proposeFrom, sameSource, tablesInModel, type TableInfo } from "./modelData";
import { modelFromHash, shareLink, shareModelLink, specFromHash } from "./share";
import { BrandMark, Menu, MenuItem, useTheme, type Theme } from "./ui";

type Precomputed = { analysis: Analysis; counts: Record<string, number>; impacts: Record<string, Impact> };
type Samples = {
  tables: string[]; specs: Record<string, Spec>; models: Record<string, DataModel>; legacy: string; powercenter: string;
  workflow: { xml: string; params: string };
  storeMart: string;
  ssis: string;
  talend: string;
  datastage: string;
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
  const [toast, setToast] = useState<{ text: string; action?: { label: string; run: () => void } } | null>(null);
  const [example, setExample] = useState("orders_revenue");
  // Two modes over the same tables: the pipeline canvas, and the data model.
  const [view, setView] = useState<"pipeline" | "model">("pipeline");
  const [dataModel, setDataModel] = useState<DataModel | null>(null);
  const [modelTab, setModelTab] = useState<ModelTab>("overview");
  const [reasons, setReasons] = useState<Record<string, string[]>>({});
  // The legacy job a model was proposed from, so the model view can show how much of it the model covers.
  const [job, setJob] = useState<{ spec: Spec } | null>(null);
  // What new tables would add to the model, until the person accepts or discards it.
  const [review, setReview] = useState<{ ext: ModelExtension; tables: string[] } | null>(null);
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [start, setStart] = useState<{ preselect: string[]; way?: "data" | "pipeline" | "ddl" | "example";
    previous: DataModel | null }>({ preselect: [], previous: null });
  const [modelBusy, setModelBusy] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);
  // The model a pipeline was generated from (Load > Open as a pipeline), to go back to it.
  const [loadOf, setLoadOf] = useState<string | null>(null);
  useEffect(() => { if (importing.mode) setView("pipeline"); }, [importing.mode]);
  const [theme, setTheme, dark] = useTheme();
  const fileInput = useRef<HTMLInputElement>(null);
  const dataLoaded = useRef<Promise<void> | null>(null);
  const flow = useReactFlow();
  const ready = eng.state === "ready";
  const runnable = ready && dataReady;

  const say = useCallback((message: string, action?: { label: string; run: () => void }) => {
    const next = { text: message, action };
    setToast(next);
    window.setTimeout(() => setToast((t) => (t === next ? null : t)), action ? 7000 : 3200);
  }, []);
  const refreshTables = useCallback(async () => setTables(await listTables()), []);

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
      const sharedModel = await modelFromHash(window.location.hash);
      if (sharedModel) { setDataModel(sharedModel); setView("model"); }
      // Python first, with the whole connection to itself; DuckDB (previews) after.
      await engineReady.catch(() => undefined);
      await data.start();
      await data.loadSamples(BASE, s.tables);
      setTables(await listTables());
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
    setLoadOf(null);
    replaceSpec(r.spec, false);
    setNotes(r.warnings);
    setScorecard(r.scorecard);
    setActiveItem(null);
    setSelectedId(null);
    setExample("");
    setImporting({ mode: null, busy: false, error: null });
    const c = r.scorecard.counts;
    if (["powercenter", "talend", "datastage"].includes(r.scorecard.origin)) {
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
          : mode === "datastage"
            ? await engine.importDatastage(text)
            : await engine.importSql(text, dialect, await dataLoaded.current!.then(data.schema, () => ({})), "imported");
    if (!r.ok) {
      setImporting((s) => ({ ...s, mode, busy: false, error: r.error }));
      return;
    }
    loaded(r, mode === "powercenter" ? "Imported the mapping" : mode === "ssis" ? "Imported the package"
      : mode === "talend" || mode === "datastage" ? "Imported the job" : "Imported the SQL");
  };

  const loadExample = (name: string) => {
    if (!samples) return;
    setLoadOf(null);
    setExample(name);
    replaceSpec(samples.specs[name], false);
    setNotes([]);
    setScorecard(null);
    setActiveItem(null);
    setSelectedId(null);
    if (tab === "migration") setTab("step");
  };

  // ---------------------------------------------------------------- data models
  /** Grow the model with tables: what they add comes back as changes to review. */
  const grow = async (names: string[], model: DataModel | null = dataModel) => {
    if (!model || !names.length) return;
    setView("model");
    setModelTab("overview");
    setModelBusy(true);
    try {
      const ext = await extendWith(model, names);
      setReview({ ext, tables: names });
      if (!ext.changes.length) say(`Nothing to add from ${names.join(", ")}`);
    } catch (e) {
      say(`Couldn't read the tables: ${String((e as Error).message ?? e)}`);
    } finally {
      setModelBusy(false);
    }
  };

  /** Model these tables: grow the model you have, or start one with them ticked. */
  const modelTables = (names: string[]) => {
    setView("model");
    if (!dataModel) { setStart((st) => ({ ...st, preselect: names, way: "data" })); return; }
    const inModel = tablesInModel(dataModel, tables);
    const fresh = names.filter((n) => !inModel.has(n));
    if (fresh.length) void grow(fresh);
    else say(`${names.join(", ")} ${names.length === 1 ? "is" : "are"} in the model already`);
  };

  const propose = async (names: string[], style: "kimball" | "data_vault") => {
    setModelBusy(true);
    setModelError(null);
    try {
      const r = await proposeFrom(names, style, "proposed");
      setDataModel(r.model);
      setReasons(r.reasons);
      setJob(null);
      setReview(null);
      setModelTab("overview");
      say(`Proposed ${r.model.entities.length} entities from ${names.length} ${names.length === 1 ? "table" : "tables"}; click one to see why`);
    } catch (e) {
      setModelError(String((e as Error).message ?? e));
    } finally {
      setModelBusy(false);
    }
  };

  const onReviewed = (model: DataModel | null, accepted: ModelChange[]) => {
    setReview(null);
    if (!model) return;
    setDataModel(model);
    setReasons((old) => {
      const next = { ...old };
      for (const c of accepted) {
        Object.assign(next, c.reasons ?? {});
        if (c.relationship && c.why.length) next[c.relationship.from] = c.why;
      }
      return next;
    });
    say(`Added ${accepted.length} ${accepted.length === 1 ? "change" : "changes"} to ${model.name}`);
  };

  const addFiles = async (files: FileList | File[]) => {
    const added: string[] = [];
    let next = spec;
    for (const file of Array.from(files)) {
      if (!/\.csv$/i.test(file.name)) { say(`${file.name} isn't a CSV file`); continue; }
      const r = await data.addCsv(file.name, await file.text());
      added.push(r.table);
      if (view === "pipeline" && next && !next.nodes.some((n) => n.type === "source" && n.params?.path === r.file)) {
        const id = uniqueId(next, r.table);
        next = { ...next, nodes: [...next.nodes, { id, type: "source", params: { format: "csv", path: r.file, columns: r.columns } }] };
      }
      if (r.renamed) say(`${file.name} is the table ${r.table}: the name ${r.table.replace(/_\d+$/, "")} was taken`);
    }
    await refreshTables();
    if (!added.length) return;
    const what = added.length === 1 ? added[0] : `${added.length} tables`;
    if (view === "pipeline") {
      if (next && next !== spec) replaceSpec(next);
      say(`Added ${what} as ${added.length === 1 ? "a source" : "sources"}`, { label: "Model it", run: () => modelTables(added) });
    } else if (dataModel && !review) {
      void grow(added);
    } else if (!dataModel) {
      setStart((st) => ({ ...st, preselect: [...new Set([...st.preselect, ...added])], way: "data" }));
      say(`Added ${what}; tick what to model`);
    } else {
      say(`Added ${what}; add it to the model once you've finished this review`);
    }
  };

  const openModel = (m: DataModel) => {
    setJob(null);
    setReview(null);
    setReasons({});
    setDataModel(m);
    setModelTab("overview");
    setView("model");
  };

  const newModel = (way?: "data" | "pipeline" | "ddl" | "example") => {
    setStart({ preselect: [], way, previous: dataModel });
    setDataModel(null);
    setReview(null);
    setJob(null);
    setModelError(null);
    setView("model");
  };

  const modelFromJob = async (style: "kimball" | "data_vault") => {
    if (!spec) return;
    const r = await engine.modelFromJob(spec, style);
    if (!r.ok) { say(`Couldn't propose a model: ${r.error}`); return; }
    setJob({ spec });
    setReasons(r.reasons);
    setReview(null);
    setDataModel(r.model);
    setModelTab("overview");
    setView("model");
    say(`Proposed ${r.model.entities.length} entities from ${spec.name}; ${Math.round(r.map.coverage * 100)}% of what it writes comes from them`);
  };

  /** The table a pipeline's CSV source reads, if the playground has it. */
  const tableOf = (step: Step | null) =>
    step?.type === "source" && step.params?.format === "csv"
      ? tables.find((t) => sameSource(t.file, String(step.params?.path ?? "")))?.name ?? null : null;

  const share = async () => {
    const modelMode = view === "model";
    if (modelMode ? !dataModel : !spec) return;
    const url = modelMode ? await shareModelLink(dataModel!) : await shareLink(spec!);
    window.history.replaceState(null, "", url);
    const what = modelMode ? "The model lives in the link (your data doesn't)" : "The pipeline lives in the link";
    try {
      await navigator.clipboard.writeText(url);
      say(`Link copied. ${what}; nothing is uploaded.`);
    } catch {
      say(`The link is in the address bar. ${what}; nothing is uploaded.`);
    }
  };

  // ---------------------------------------------------------------- render
  const selected = spec?.nodes.find((n) => n.id === selectedId) ?? null;
  const issueCount = analysis?.issues.length ?? 0;
  const ThemeIcon = theme === "system" ? Monitor : dark ? Moon : Sun;
  const nextTheme: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };

  return (
    <div className="app" onDragOver={(e) => { if (view === "model") e.preventDefault(); }}
      onDrop={(e) => { if (view === "model" && e.dataTransfer.files.length) { e.preventDefault(); void addFiles(e.dataTransfer.files); } }}>
      {view === "model" && dataModel && (
        <ModelView model={dataModel} engineReady={ready} dataReady={dataReady} tab={modelTab} onTab={setModelTab} job={job}
          reasons={reasons} review={review} onReviewed={onReviewed} tables={tables} onGrow={(names) => void grow(names)}
          onAddFiles={(f) => void addFiles(f)} onNewModel={() => newModel()}
          onChange={(m, note) => { setDataModel(m); if (note) say(note); }}
          onOpenPipeline={(s) => { setView("pipeline"); setExample(""); replaceSpec(s, false); setNotes([]); setScorecard(null);
            setSelectedId(null); setLoadOf(dataModel.name); say(`Opened ${s.name}: the load generated from the model`); }} />
      )}
      {view === "model" && !dataModel && (
        <ModelStart tables={tables} preselect={start.preselect} initialWay={start.way} engineReady={ready} dataReady={dataReady}
          busy={modelBusy} error={modelError} spec={spec} examples={samples?.models ?? {}}
          onPropose={(names, style) => void propose(names, style)} onFromPipeline={(style) => void modelFromJob(style)}
          onImported={(m, note) => { openModel(m); if (note) say(note); }} onExample={openModel}
          onAddFiles={(f) => void addFiles(f)}
          previous={start.previous?.name} onBack={() => start.previous && setDataModel(start.previous)} />
      )}
      {view === "model" && modelBusy && dataModel && <div className="busy-chip glass" role="status">Reading the tables…</div>}
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
          {view === "pipeline" && loadOf && !scorecard && (
            <button className="chip" onClick={() => setView("model")} title="Back to the model this pipeline loads"
              data-testid="loads-model">
              <ArrowLeft />Loads {loadOf}
            </button>
          )}
          {view === "pipeline" && scorecard && (
            <button className="chip ok" onClick={() => setTab("migration")} title="Open the migration scorecard">
              <CircleCheck />{Math.round(scorecard.automated * 100)}% automatic
            </button>
          )}
        </div>
        <div className="mode-switch-bar segmented" role="tablist" aria-label="Mode">
          <button role="tab" aria-selected={view === "pipeline"} aria-pressed={view === "pipeline"} onClick={() => setView("pipeline")}
            title="The pipeline canvas: steps, lineage, code" aria-label="Pipeline">
            <Waypoints /><span className="label">Pipeline</span>
          </button>
          <button role="tab" aria-selected={view === "model"} aria-pressed={view === "model"} onClick={() => setView("model")}
            title="The data model: entities, keys, history, relationships" aria-label="Model">
            <Layers /><span className="label">Model</span>
            {dataModel && <span className="mode-count">{dataModel.entities.length}</span>}
          </button>
        </div>
        <nav className="bar-actions" aria-label="Actions">
          <Menu
            label="Examples"
            button={({ open, toggle }) => (
              <button className="btn ghost" onClick={toggle} aria-expanded={open} disabled={!samples} aria-label="Examples">
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
                    checked={view === "model" && dataModel?.name === m.name && !review} onClick={() => { close(); openModel(m); }} />
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
                <MenuItem icon={Cable} title="DataStage parallel job" sub="Transformer, lookup reject, filter, join"
                  onClick={() => { close(); setImporting({ mode: "datastage", busy: false, error: null }); }} />
              </>
            )}
          </Menu>
          <button className="btn ghost" onClick={() => fileInput.current?.click()} disabled={!runnable} aria-label="Add CSV"
            title={view === "pipeline" ? "Add CSV files as sources" : "Add CSV files to the model"}>
            <FileSpreadsheet /><span className="label">Add CSV</span>
          </button>
          <input ref={fileInput} type="file" accept=".csv" multiple hidden
            onChange={(e) => { if (e.target.files?.length) void addFiles(e.target.files); e.target.value = ""; }} />
          <button className="btn ghost icon" onClick={() => setTheme(nextTheme[theme])}
            aria-label={`Theme: ${theme}`} title={`Theme: ${theme}`}>
            <ThemeIcon />
          </button>
          <span className="bar-divider" />
          <button className="btn" onClick={share} disabled={view === "pipeline" ? !spec : !dataModel} aria-label="Share link"
            title={view === "pipeline" ? "Copy a link that holds this pipeline" : "Copy a link that holds this model"}>
            <Share2 /><span className="label">Share</span>
          </button>
          <Menu
            label="Import"
            button={({ open, toggle }) => (
              <button className="btn primary" onClick={toggle} aria-expanded={open} disabled={!ready} aria-label="Import"
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
                <MenuItem icon={Cable} title="IBM DataStage" sub=".dsx parallel jobs, with a migration scorecard"
                  onClick={() => { close(); setView("pipeline"); setImporting({ mode: "datastage", busy: false, error: null }); }} />
                <div className="menu-sep" />
                <MenuItem icon={Layers} title="Data model" sub="CREATE TABLE statements or DBML"
                  onClick={() => { close(); newModel("ddl"); }} />
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
          tableOf={tableOf}
          onModelTable={(t) => modelTables([t])}
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

      {toast && (
        <div className="toast" role="status"><Sparkles />{toast.text}
          {toast.action && <button className="toast-action" onClick={() => { toast.action!.run(); setToast(null); }}>{toast.action.label}</button>}
        </div>
      )}

      {eng.state === "failed" && (
        <div className="boot" role="alert">
          <div className="boot-card">
            <div className="boot-mark" aria-hidden="true"><span /><span /><span /></div>
            <h1>The engine didn't start</h1>
            <p className="error" data-testid="engine-error">{(eng.error ?? "").replace(/^(Error: )+/, "").replace(/^blocked: /, "").replace(/^./, (c) => c.toUpperCase())}</p>
            {eng.error?.includes("blocked: ") ? (
              <p>A firewall or proxy on this network stopped the download; work networks often do. Try
                another network (a phone hotspot works), or ask your IT team to allow this site.</p>
            ) : (
              <p>Redcliff needs a recent browser with WebAssembly. Reloading the page usually helps.</p>
            )}
          </div>
        </div>
      )}

      {importing.mode && samples && (
        <ImportDialog
          mode={importing.mode}
          samples={(importing.sample === "workflow"
            ? { sql: samples.legacy, powercenter: samples.workflow.xml, params: samples.workflow.params, ssis: samples.ssis, talend: samples.talend, datastage: samples.datastage }
            : importing.sample === "store"
              ? { sql: samples.legacy, powercenter: samples.storeMart, ssis: samples.ssis, talend: samples.talend, datastage: samples.datastage }
              : { sql: samples.legacy, powercenter: samples.powercenter, ssis: samples.ssis, talend: samples.talend, datastage: samples.datastage }) satisfies ImportSamples}
          busy={importing.busy}
          error={importing.error}
          onImport={doImport}
          onClose={() => setImporting({ mode: null, busy: false, error: null })}
        />
      )}
    </div>
  );
}
