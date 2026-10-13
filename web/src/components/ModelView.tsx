// The data model view: entities as cards, relationships as edges, and a panel to check, export
// and import the model. Read-only on the canvas (M1); edits go through the YAML tab or an import.
import dagre from "@dagrejs/dagre";
import {
  Background, BackgroundVariant, Handle, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import * as yaml from "js-yaml";
import {
  Check, CircleAlert, CircleCheck, CircleDot, Copy, Download, FileCode2, FileInput, Layers, Link2, Maximize, Minus,
  Orbit, Plus, ScrollText, Sigma, Table2, Workflow, type LucideIcon,
} from "lucide-react";
import { memo, useEffect, useMemo, useState } from "react";
import { data } from "../engine/data";
import { engine } from "../engine/engine";
import type { DataModel, EntityKind, JobMap, ModelAnalysis, ModelEntity, ModelExport, Spec } from "../model";
import { fitPadding } from "../layout";
import { highlight } from "../ui";

export type ModelTab = "overview" | "load" | "export" | "yaml" | "import";

const KIND: Record<EntityKind, { icon: LucideIcon; label: string; color: string }> = {
  dimension: { icon: Layers, label: "Dimension", color: "var(--t-source)" },
  fact: { icon: Sigma, label: "Fact", color: "var(--t-target)" },
  hub: { icon: CircleDot, label: "Hub", color: "var(--t-join)" },
  link: { icon: Link2, label: "Link", color: "var(--t-union)" },
  satellite: { icon: Orbit, label: "Satellite", color: "var(--t-derive)" },
  table: { icon: Table2, label: "Table", color: "var(--t-select)" },
};

const ROLE_MARK: Record<string, [string, string]> = {
  surrogate_key: ["SK", "Surrogate key"],
  durable_key: ["DK", "Durable key"],
  business_key: ["BK", "Business key"],
  measure: ["Σ", "Measure"],
};

const WIDTH = 248;
const HEAD = 62;
const ROW = 23;

/** The columns that identify a row: the surrogate key, else the business key (as in model.py). */
export function entityKey(e: ModelEntity): string[] {
  for (const role of ["surrogate_key", "business_key"]) {
    const names = e.attributes.filter((a) => a.role === role).map((a) => a.name);
    if (names.length) return names;
  }
  return [];
}

const isScd2 = (e: ModelEntity) => e.kind === "dimension" && (e.history ?? "scd2") === "scd2";

type EntityData = { entity: ModelEntity; fks: Set<string>; selected: boolean; dimmed: boolean };
type EntityNodeType = Node<EntityData, "entity">;

const EntityNode = memo(function EntityNode({ data }: NodeProps<EntityNodeType>) {
  const { entity: e, fks, selected, dimmed } = data;
  const kind = KIND[e.kind] ?? KIND.table;
  const key = new Set(entityKey(e));
  return (
    <div className={`entity${selected ? " is-selected" : ""}${dimmed ? " is-dimmed" : ""}`}
      style={{ "--t": kind.color } as React.CSSProperties} data-testid={`entity-${e.id}`}>
      <div className="step-head">
        <span className="step-icon"><kind.icon /></span>
        <span className="step-titles">
          <span className="step-id">{e.id}</span>
          <span className="step-type">
            {kind.label}
            {e.kind === "dimension" && <> · {isScd2(e) ? "SCD2" : "SCD1"}</>}
            {e.grain && <> · {e.grain}</>}
          </span>
        </span>
      </div>
      <ul className="entity-attrs">
        {e.attributes.map((a) => {
          const marks = [
            key.has(a.name) && ["PK", "Primary key"],
            fks.has(a.name) && ["FK", "Foreign key"],
            a.role && ROLE_MARK[a.role],
            a.history === "scd1" && ["SCD1", "Corrections overwrite every version"],
            a.track === false && ["skip", "Not tracked: a change doesn't make a new version"],
          ].filter(Boolean) as [string, string][];
          return (
            <li key={a.name} className={`attr${key.has(a.name) ? " is-key" : ""}`} title={a.description || undefined}>
              <Handle type="source" position={Position.Right} id={`r-${a.name}`} className="attr-handle" />
              <Handle type="target" position={Position.Left} id={`l-${a.name}`} className="attr-handle" />
              <span className="attr-name">{a.name}</span>
              {marks.map(([m, t]) => <span key={m} className={`attr-mark mark-${m.toLowerCase()}`} title={t}>{m}</span>)}
              <span className="attr-type">{a.type}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
});

const nodeTypes = { entity: EntityNode };

/** Referenced entities (dimensions, hubs) on the left, the ones referencing them on the right. */
function layoutModel(model: DataModel): Record<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 90, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const e of model.entities) g.setNode(e.id, { width: WIDTH, height: HEAD + e.attributes.length * ROW + 10 });
  for (const r of model.relationships ?? []) {
    const [from, to] = [r.from.split(".")[0], r.to.split(".")[0]];
    if (g.hasNode(from) && g.hasNode(to) && from !== to) g.setEdge(to, from);
  }
  dagre.layout(g);
  return Object.fromEntries(model.entities.map((e) => {
    const p = g.node(e.id);
    return [e.id, { x: p.x - p.width / 2, y: p.y - p.height / 2 }];
  }));
}

type Props = {
  model: DataModel;
  engineReady: boolean;
  dataReady: boolean;
  tab: ModelTab;
  onTab: (t: ModelTab) => void;
  onChange: (m: DataModel, note?: string) => void;
  onOpenPipeline: (spec: Spec) => void;
  /** The legacy job the model was proposed from: its reasons, and the job to map onto the model. */
  job?: { spec: Spec; reasons: Record<string, string[]> } | null;
  onClearJob?: () => void;
};

export function ModelView(props: Props) {
  return (
    <ReactFlowProvider>
      <ModelCanvas {...props} />
    </ReactFlowProvider>
  );
}

function ModelCanvas({ model, engineReady, dataReady, tab, onTab, onChange, onOpenPipeline, job, onClearJob }: Props) {
  const flow = useReactFlow();
  const [reasons, setReasons] = useState<Record<string, string[]>>(job?.reasons ?? {});
  const [jobMap, setJobMap] = useState<JobMap | null>(null);
  useEffect(() => { setReasons(job?.reasons ?? {}); }, [job]);
  useEffect(() => {
    if (!job || !engineReady) { setJobMap(null); return; }
    let live = true;
    engine.modelMap(model, job.spec).then((r) => { if (live) setJobMap(r.ok ? r : null); });
    return () => { live = false; };
  }, [model, job, engineReady]);
  const [picked, setSelected] = useState<string | null>(null);
  const selected = picked && model.entities.some((e) => e.id === picked) ? picked : null; // gone after an import
  const [analysis, setAnalysis] = useState<ModelAnalysis | null>(null);
  const positions = useMemo(() => layoutModel(model), [model]);

  useEffect(() => {
    if (!engineReady) return;
    let live = true;
    engine.modelAnalyze(model).then((r) => { if (live) setAnalysis(r.ok ? r : null); });
    return () => { live = false; };
  }, [model, engineReady]);

  useEffect(() => {
    const id = window.setTimeout(() => flow.fitView({ padding: fitPadding(), maxZoom: 1, duration: 250 }), 60);
    return () => window.clearTimeout(id);
  }, [positions, flow]);

  const related = useMemo(() => {
    if (!selected) return null;
    const out = new Set([selected]);
    for (const r of model.relationships ?? []) {
      const [a, b] = [r.from.split(".")[0], r.to.split(".")[0]];
      if (a === selected) out.add(b);
      if (b === selected) out.add(a);
    }
    return out;
  }, [selected, model]);

  const nodes: EntityNodeType[] = model.entities.map((e) => ({
    id: e.id,
    type: "entity",
    position: positions[e.id] ?? { x: 0, y: 0 },
    // Known sizes, so a card shows before React Flow has measured it (it hides unmeasured nodes)
    initialWidth: WIDTH,
    initialHeight: HEAD + e.attributes.length * ROW + 10,
    draggable: true,
    data: {
      entity: e,
      fks: new Set((model.relationships ?? []).filter((r) => r.from.startsWith(`${e.id}.`)).map((r) => r.from.split(".")[1])),
      selected: e.id === selected,
      dimmed: !!related && !related.has(e.id),
    },
  }));
  const edges: Edge[] = (model.relationships ?? []).map((r) => {
    const [src, srcCol] = r.to.split(".");
    const [tgt, tgtCol] = r.from.split(".");
    const lit = !!selected && (src === selected || tgt === selected);
    return {
      id: `${r.from}->${r.to}`, source: src, sourceHandle: `r-${srcCol}`, target: tgt, targetHandle: `l-${tgtCol}`,
      type: "smoothstep", className: lit ? "rel is-lit" : "rel", animated: lit,
    };
  });

  return (
    <>
      <main className="canvas" data-testid="model-canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={(_, n) => { setSelected(n.id); onTab("overview"); }}
          onPaneClick={() => setSelected(null)}
          nodesConnectable={false}
          fitView
          fitViewOptions={{ padding: fitPadding(), maxZoom: 1 }}
          minZoom={0.1}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
        </ReactFlow>
      </main>
      <div className="zoom glass" role="group" aria-label="Zoom">
        <button className="btn ghost icon" onClick={() => flow.zoomIn({ duration: 150 })} aria-label="Zoom in"><Plus /></button>
        <button className="btn ghost icon" onClick={() => flow.zoomOut({ duration: 150 })} aria-label="Zoom out"><Minus /></button>
        <div className="zoom-sep" />
        <button className="btn ghost icon" onClick={() => flow.fitView({ padding: fitPadding(), maxZoom: 1, duration: 250 })}
          aria-label="Fit to screen" title="Fit to screen"><Maximize /></button>
      </div>
      <aside className="inspector" aria-label="Model">
        <div className="tabs" role="tablist">
          {([["overview", "Model", Layers], ["load", "Load", Workflow], ["export", "Export", ScrollText], ["yaml", "YAML", FileCode2],
            ["import", "Import", FileInput]] as const).map(([id, label, Icon]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={`tab${tab === id ? " is-active" : ""}`}
              onClick={() => onTab(id)}>
              <Icon />{label}
            </button>
          ))}
        </div>
        <div className="panel" role="tabpanel">
          {tab === "overview" && <Overview model={model} analysis={analysis} engineReady={engineReady} reasons={reasons}
            selected={model.entities.find((e) => e.id === selected) ?? null} onSelect={setSelected} />}
          {tab === "overview" && !selected && job && jobMap && <JobMapSection map={jobMap} name={job.spec.name} />}
          {tab === "load" && <LoadPanel model={model} valid={analysis?.valid ?? false} engineReady={engineReady}
            onOpen={onOpenPipeline} />}
          {tab === "export" && <ExportPanel model={model} valid={analysis?.valid ?? false} engineReady={engineReady} />}
          {tab === "yaml" && <YamlPanel model={model} onChange={onChange} />}
          {tab === "import" && (
            <>
              <ProposePanel ready={engineReady && dataReady} onPropose={(m, why) => {
                onClearJob?.(); setReasons(why); setSelected(null); onChange(m, `Proposed ${m.entities.length} entities; click one to see why`);
                onTab("overview");
              }} />
              <ImportPanel engineReady={engineReady} onChange={(m, note) => { onClearJob?.(); setReasons({}); onChange(m, note); }} />
            </>
          )}
        </div>
      </aside>
    </>
  );
}

function Overview({ model, analysis, engineReady, reasons, selected, onSelect }: {
  model: DataModel; analysis: ModelAnalysis | null; engineReady: boolean; reasons: Record<string, string[]>;
  selected: ModelEntity | null;
  onSelect: (id: string | null) => void;
}) {
  const counts = model.entities.reduce<Record<string, number>>((acc, e) => ({ ...acc, [e.kind]: (acc[e.kind] ?? 0) + 1 }), {});
  return (
    <>
      {selected ? (
        <section>
          <h2>{selected.id}</h2>
          <p className="quiet">
            {KIND[selected.kind]?.label ?? selected.kind}
            {selected.kind === "dimension" && ` · ${isScd2(selected) ? "SCD2: keeps every version" : "SCD1: current values only"}`}
          </p>
          {selected.grain && <p><strong>Grain:</strong> {selected.grain}</p>}
          {selected.description && <p>{selected.description}</p>}
          {reasons[selected.id] && (
            <ul className="why-list" data-testid="model-why">
              {reasons[selected.id].map((r) => <li key={r}>{r}</li>)}
            </ul>
          )}
          <table className="attr-table">
            <thead><tr><th>Attribute</th><th>Type</th><th>Role</th></tr></thead>
            <tbody>
              {selected.attributes.map((a) => (
                <tr key={a.name} title={a.description || undefined}>
                  <td className="mono">{a.name}</td>
                  <td className="mono quiet">{a.type}</td>
                  <td>{[a.role?.replace("_", " "), a.history === "scd1" && "SCD1",
                    a.track === false && "not tracked"].filter(Boolean).join(", ")}
                    {reasons[`${selected.id}.${a.name}`] && <div className="quiet why">{reasons[`${selected.id}.${a.name}`].join("; ")}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="btn sm ghost" onClick={() => onSelect(null)}>Show the whole model</button>
        </section>
      ) : (
        <section>
          <h2>{model.name}</h2>
          <p className="quiet">
            {model.style === "data_vault" ? "Data Vault 2.0" : "Kimball"} ·{" "}
            {Object.entries(counts).map(([k, n]) => `${n} ${KIND[k as EntityKind]?.label.toLowerCase() ?? k}${n === 1 ? "" : "s"}`).join(", ")}
            {" "}· {(model.relationships ?? []).length} relationships
          </p>
          {model.description && <p>{model.description}</p>}
          <p className="quiet">Click an entity for its attributes. Edit the model in the YAML tab, or import DDL or DBML.</p>
        </section>
      )}
      <section>
        <h3>Checks</h3>
        {!engineReady || !analysis ? <p className="quiet">Checking once the engine has started…</p>
          : analysis.valid ? <p className="ok-line" data-testid="model-valid"><CircleCheck />The model is valid</p>
            : (
              <ul className="issue-list" data-testid="model-issues">
                {analysis.issues.map((i, n) => (
                  <li key={n}><CircleAlert />{i.node ? <><strong>{i.node}</strong>: </> : null}{i.message}</li>
                ))}
              </ul>
            )}
      </section>
      {(model.relationships ?? []).length > 0 && (
        <section>
          <h3>Relationships</h3>
          <ul className="rel-list">
            {(model.relationships ?? []).map((r) => (
              <li key={`${r.from}->${r.to}`} className="mono">{r.from} <span className="quiet">→</span> {r.to}</li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

/** How much of what a legacy job writes the model covers, column by column; gaps first. */
function JobMapSection({ map, name }: { map: JobMap; name: string }) {
  const cols = [...map.columns].sort((a, b) => Number(a.covered) - Number(b.covered));
  return (
    <section data-testid="job-map">
      <h3>Legacy job coverage</h3>
      <p data-testid="job-map-summary"><strong>{map.covered} of {map.total}</strong> columns <span className="mono">{name}</span> writes
        ({Math.round(map.coverage * 100)}%) come from model attributes.</p>
      <div className="bar-track"><div className="bar-fill" style={{ width: `${map.coverage * 100}%` }} /></div>
      <ul className="job-map-list">
        {cols.map((c) => {
          const direct = c.inputs.filter((i) => i.kind === "direct");
          return (
            <li key={`${c.target}.${c.column}`} className={c.covered ? "" : "is-gap"}>
              <div className="mono">{c.table ?? c.target}.{c.column}</div>
              {direct.length === 0 ? <div className="quiet">computed by the job</div> : direct.map((i) => (
                <div key={`${i.table}.${i.column}`} className="mono from">
                  <span className="quiet">{i.table}.{i.column} → </span>
                  {i.attributes.length ? i.attributes.join(", ") : <span className="bad-text">not in the model</span>}
                </div>
              ))}
            </li>
          );
        })}
      </ul>
      {Object.keys(map.unmapped).length > 0 && (
        <p className="quiet">Read by the job but not in the model: {Object.entries(map.unmapped).map(([t, cs]) =>
          <span key={t}><span className="mono">{t}</span> ({cs.join(", ")}) </span>)}</p>
      )}
    </section>
  );
}

const FORMATS: { id: ModelExport; label: string; file: (name: string) => string }[] = [
  { id: "ddl", label: "DDL", file: (n) => `${n}.sql` },
  { id: "dbml", label: "DBML", file: (n) => `${n}.dbml` },
  { id: "mermaid", label: "Mermaid", file: (n) => `${n}.mmd` },
  { id: "dbt", label: "dbt", file: () => "schema.yml" },
];
const DIALECTS = ["duckdb", "snowflake", "databricks", "bigquery", "postgres", "redshift", "tsql", "oracle"];

function ExportPanel({ model, valid, engineReady }: { model: DataModel; valid: boolean; engineReady: boolean }) {
  const [fmt, setFmt] = useState<ModelExport>("ddl");
  const [dialect, setDialect] = useState("duckdb");
  const [code, setCode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!engineReady) return;
    let live = true;
    engine.modelExport(model, fmt, dialect).then((r) => {
      if (!live) return;
      setCode(r.ok ? r.code : null);
      setError(r.ok ? null : r.issues?.length ? "Fix the model's problems first (see the Model tab)." : r.error);
    });
    return () => { live = false; };
  }, [model, fmt, dialect, engineReady]);
  const file = FORMATS.find((f) => f.id === fmt)!.file(model.name.replace(/\W+/g, "_") || "model");
  return (
    <section>
      <div className="segmented small" role="group" aria-label="Format">
        {FORMATS.map((f) => <button key={f.id} aria-pressed={fmt === f.id} onClick={() => setFmt(f.id)}>{f.label}</button>)}
      </div>
      {fmt === "ddl" && (
        <div className="row between project-bar">
          <select className="file-select" value={dialect} onChange={(e) => setDialect(e.target.value)} aria-label="Dialect">
            {DIALECTS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
      )}
      {!engineReady ? <p className="quiet">Available once the engine has started.</p>
        : error ? <p className="error">{error}</p> : code !== null && (
          <>
            <div className="code-shell">
              <div className="code-head">
                <span className="code-file">{file}</span>
                <span className="row">
                  <button className="btn sm copy" onClick={() => {
                    const a = document.createElement("a");
                    a.href = URL.createObjectURL(new Blob([code], { type: "text/plain" }));
                    a.download = file;
                    a.click();
                    URL.revokeObjectURL(a.href);
                  }}><Download />Download</button>
                  <button className="btn sm copy" onClick={async () => {
                    try { await navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* blocked */ }
                  }}>{copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy"}</button>
                </span>
              </div>
              <pre className="code-block" data-testid="model-export"><code>{fmt === "ddl" ? highlight(code) : code}</code></pre>
            </div>
            {fmt === "mermaid" && <p className="quiet code-note">Paste into any Markdown that renders Mermaid, such as a GitHub README.</p>}
            {fmt === "dbml" && <p className="quiet code-note">Opens in dbdiagram.io. Roles and history travel in column notes.</p>}
          </>
        )}
      {!valid && engineReady && !error && <p className="quiet">Checking the model…</p>}
    </section>
  );
}

function YamlPanel({ model, onChange }: { model: DataModel; onChange: (m: DataModel, note?: string) => void }) {
  const [text, setText] = useState(() => yaml.dump(model, { lineWidth: 100, flowLevel: 3 }));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { setText(yaml.dump(model, { lineWidth: 100, flowLevel: 3 })); }, [model]);
  return (
    <section>
      <p className="quiet">The model is a file of its own (<code>model.yaml</code>); pipelines will reference it.</p>
      <textarea className="code-input tall" spellCheck={false} value={text} aria-label="Model YAML"
        onChange={(e) => setText(e.target.value)} />
      {error && <p className="error">{error}</p>}
      <div className="row between">
        <button className="btn sm primary" onClick={() => {
          try {
            const next = yaml.load(text) as DataModel;
            if (!next || typeof next !== "object" || !Array.isArray(next.entities)) throw new Error("a model needs a list of entities");
            setError(null);
            onChange(next, "Model updated");
          } catch (e) { setError(String((e as Error).message ?? e)); }
        }}>Apply</button>
        <button className="btn sm ghost" onClick={() => {
          const a = document.createElement("a");
          a.href = URL.createObjectURL(new Blob([text], { type: "text/yaml" }));
          a.download = "model.yaml";
          a.click();
          URL.revokeObjectURL(a.href);
        }}><Download />model.yaml</button>
      </div>
    </section>
  );
}

const SAMPLE_DDL = `CREATE TABLE dim_product (
  product_key BIGINT PRIMARY KEY,
  sku VARCHAR(20) NOT NULL,
  name VARCHAR(100),
  category VARCHAR(40)
);
CREATE TABLE fct_sales (
  sale_id BIGINT PRIMARY KEY,
  product_key BIGINT REFERENCES dim_product (product_key),
  quantity INT,
  revenue DECIMAL(12, 2)
);`;

function ImportPanel({ engineReady, onChange }: { engineReady: boolean; onChange: (m: DataModel, note?: string) => void }) {
  const [text, setText] = useState(SAMPLE_DDL);
  const [dialect, setDialect] = useState("");
  const [name, setName] = useState("imported");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const fmt: "ddl" | "dbml" = /^\s*Table\s+[\w".]+[^\n]*\{/im.test(text) ? "dbml" : "ddl";
  return (
    <section>
      <p className="quiet">Paste <code>CREATE TABLE</code> statements or DBML. Table names give the kind
        (<code>dim_</code>, <code>fct_</code>, <code>hub_</code>, <code>lnk_</code>, <code>sat_</code>); anything Redcliff
        can't place comes back as a warning.</p>
      <textarea className="code-input tall" spellCheck={false} value={text} aria-label="DDL or DBML"
        onChange={(e) => setText(e.target.value)} />
      <div className="row between project-bar">
        <span className="chip">{fmt === "dbml" ? "DBML" : "SQL DDL"}</span>
        {fmt === "ddl" && (
          <select className="file-select" value={dialect} onChange={(e) => setDialect(e.target.value)} aria-label="Dialect">
            <option value="">any dialect</option>
            {DIALECTS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        )}
        <input className="file-select" value={name} onChange={(e) => setName(e.target.value)} aria-label="Model name" />
      </div>
      {error && <p className="error">{error}</p>}
      {warnings.length > 0 && (
        <ul className="issue-list warn" data-testid="model-import-warnings">
          {warnings.map((w) => <li key={w}><CircleAlert />{w}</li>)}
        </ul>
      )}
      <button className="btn sm primary" disabled={!engineReady || busy || !text.trim()} onClick={async () => {
        setBusy(true);
        const r = await engine.modelImport(text, fmt, dialect || null, name || "imported");
        setBusy(false);
        if (!r.ok) { setError(r.issues?.length ? r.issues.join("; ") : r.error); return; }
        setError(null);
        setWarnings(r.warnings);
        onChange(r.model, `Imported ${r.model.entities.length} tables`);
      }}>{busy ? "Importing…" : "Import"}</button>
    </section>
  );
}

function ProposePanel({ ready, onPropose }: { ready: boolean; onPropose: (m: DataModel, reasons: Record<string, string[]>) => void }) {
  const [tables, setTables] = useState<Record<string, Record<string, string>>>({});
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refresh = async () => {
    const t = await data.schema();
    setTables(t);
    setPicked((old) => (old.size ? new Set([...old].filter((n) => n in t)) : new Set(Object.keys(t).filter((n) => ["orders", "customers"].includes(n)))));
  };
  useEffect(() => { if (ready) void refresh(); }, [ready]);
  const propose = async () => {
    setBusy(true);
    setError(null);
    try {
      const chosen = Object.fromEntries(Object.entries(tables).filter(([n]) => picked.has(n)));
      const q = await engine.modelProfileQueries(chosen);
      if (!q.ok) throw new Error(q.error);
      const rows: Record<string, unknown[]> = {};
      for (const [t, sql] of Object.entries(q.queries)) rows[t] = (await data.query(sql, 1)).rows[0].map((v) => Number(v));
      const refs = await engine.modelReferenceQueries(chosen, rows);
      if (!refs.ok) throw new Error(refs.error);
      const held: string[][] = [];
      for (const c of refs.checks) if ((await data.query(c.sql, 1)).rows[0][0] == 0) held.push(c.ref);
      const r = await engine.modelPropose(chosen, rows, held, "proposed");
      if (!r.ok) throw new Error(r.error);
      onPropose(r.model, r.reasons);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section>
      <h3>From your data</h3>
      <p className="quiet">Redcliff profiles the tables (keys, nulls, references between them) and proposes a
        Kimball model, with the reason for each choice. Add your own CSVs to the list.</p>
      {!ready ? <p className="quiet">Available once the engine and DuckDB have started.</p> : (
        <>
          <ul className="pick-list">
            {Object.entries(tables).map(([t, cols]) => (
              <li key={t}>
                <label>
                  <input type="checkbox" checked={picked.has(t)} onChange={(e) => {
                    const next = new Set(picked);
                    if (e.target.checked) next.add(t); else next.delete(t);
                    setPicked(next);
                  }} />
                  <span className="mono">{t}</span> <span className="quiet">{Object.keys(cols).length} columns</span>
                </label>
              </li>
            ))}
          </ul>
          <div className="row between">
            <label className="btn sm ghost">
              Add CSV
              <input type="file" accept=".csv" multiple hidden onChange={async (e) => {
                for (const f of Array.from(e.target.files ?? [])) {
                  const { table } = await data.addCsv(f.name, await f.text());
                  setPicked((old) => new Set([...old, table]));
                }
                await refresh();
              }} />
            </label>
            <button className="btn sm primary" disabled={busy || !picked.size} onClick={propose}>
              {busy ? "Profiling…" : `Propose a model from ${picked.size} ${picked.size === 1 ? "table" : "tables"}`}
            </button>
          </div>
          {error && <p className="error">{error}</p>}
        </>
      )}
      <h3 style={{ marginTop: 22 }}>From DDL or DBML</h3>
    </section>
  );
}

function LoadPanel({ model, valid, engineReady, onOpen }: {
  model: DataModel; valid: boolean; engineReady: boolean; onOpen: (spec: Spec) => void;
}) {
  const [result, setResult] = useState<{ spec: Spec; targets: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!engineReady) return;
    let live = true;
    engine.modelLoadPipeline(model).then((r) => {
      if (!live) return;
      setResult(r.ok ? { spec: r.spec, targets: r.targets } : null);
      setError(r.ok ? null : r.issues?.length ? r.issues.join("; ") : r.error);
    });
    return () => { live = false; };
  }, [model, engineReady]);
  const vault = model.style === "data_vault";
  return (
    <section>
      <h3>The pipeline that loads this model</h3>
      <p className="quiet">
        {vault
          ? "Hubs and links insert new keys, satellites insert a row when their hashdiff changes (MD5 hash keys), and each hub gets a current-state mart."
          : "Dimensions keep history (SCD2: type-1 columns overwrite, ignored columns don't make versions, an unknown member for missing keys); facts look up the version valid on their date."}
        {" "}It's an ordinary pipeline, so it compiles to DuckDB, Spark, PySpark, dbt and Lakeflow.
      </p>
      {!engineReady ? <p className="quiet">Available once the engine has started.</p>
        : error ? <p className="error">{valid ? error : "Fix the model's problems first (see the Model tab)."}</p>
          : result && (
            <>
              <ul className="rel-list" data-testid="model-load-targets">
                {result.targets.map((t) => <li key={t} className="mono">{t}</li>)}
              </ul>
              <div className="row between" style={{ marginTop: 12 }}>
                <span className="quiet">{result.spec.nodes.length} steps, {result.targets.length} targets</span>
                <button className="btn sm primary" onClick={() => onOpen(result.spec)}><Workflow />Open as a pipeline</button>
              </div>
              <p className="quiet code-note">From the command line: <code>redcliff model pipeline model.yaml -o load.yaml</code>,
                then <code>redcliff run load.yaml --db warehouse.duckdb</code> and <code>redcliff model test model.yaml --db warehouse.duckdb</code>.</p>
            </>
          )}
    </section>
  );
}
