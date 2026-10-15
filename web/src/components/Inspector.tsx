import * as yaml from "js-yaml";
import {
  ArrowRight, Boxes, Check, CircleAlert, Code2, Copy, Download, FileText, Gauge, GitCompare, Keyboard, Layers, MousePointerClick,
  Rows3, SlidersHorizontal, Table2, Trash2, TriangleAlert, Unlink, Waypoints, X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { data, type QueryResult } from "../engine/data";
import { engine } from "../engine/engine";
import type { Analysis, CheckRun, Impact, Scorecard, ScoreItem, ScoreStatus, Spec, Step, StepType } from "../model";
import { STEP_LABELS, summarize } from "../model";
import { STEP_ICONS, StepIcon, highlight } from "../ui";
import { zip } from "../zip";
import { ChangesPanel } from "./ChangesPanel";

export type Tab = "step" | "data" | "code" | "spec" | "changes" | "migration";

type Props = {
  tab: Tab;
  onTab: (t: Tab) => void;
  spec: Spec;
  analysis: Analysis | null;
  selected: Step | null;
  impact: Impact | null;
  notes: string[];
  ready: boolean; // engine and DuckDB both loaded
  engineReady: boolean;
  checkRuns: CheckRun[];
  scorecard: Scorecard | null;
  activeItem: string | null;
  onPickItem: (name: string | null) => void;
  onSelectStep: (id: string) => void;
  onChangeSpec: (spec: Spec) => void;
  onUpdateStep: (id: string, params: Record<string, unknown>) => void;
  onDeleteStep: (id: string) => void;
  onAddStep: (type: StepType) => void;
  onClearFocus: () => void;
  baseline: Spec | null;
  onRebase: () => void;
  onModelFromJob: (style: "kimball" | "data_vault") => void;
  /** The table a CSV source reads, and modelling it (Model mode). */
  tableOf: (step: Step | null) => string | null;
  onModelTable: (table: string) => void;
};

const ADDABLE: StepType[] = ["filter", "derive", "select", "aggregate", "join", "union", "sql", "target"];

export function Inspector(props: Props) {
  const { tab, onTab, scorecard } = props;
  const tabs: { id: Tab; label: string; icon: typeof Table2 }[] = [
    { id: "step", label: "Step", icon: SlidersHorizontal },
    { id: "data", label: "Data", icon: Table2 },
    { id: "code", label: "Code", icon: Code2 },
    { id: "spec", label: "Spec", icon: FileText },
    { id: "changes", label: "Changes", icon: GitCompare },
  ];
  if (scorecard) tabs.push({ id: "migration", label: "Migration", icon: Gauge });
  return (
    <aside className="inspector" aria-label="Inspector">
      <div className={tabs.length > 5 ? "tabs compact" : "tabs"} role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            aria-label={t.label}
            title={t.label}
            className={tab === t.id ? "tab is-active" : "tab"}
            onClick={() => onTab(t.id)}
          >
            <t.icon aria-hidden="true" />
            <span className="tab-label">{t.label}</span>
            {t.id === "migration" && scorecard && (
              <span className="tab-badge">{Math.round(scorecard.automated * 100)}%</span>
            )}
          </button>
        ))}
      </div>
      <div className="panel" role="tabpanel">
        {tab === "step" && <StepPanel {...props} />}
        {tab === "data" && <DataPanel {...props} />}
        {tab === "code" && <CodePanel {...props} />}
        {tab === "spec" && <SpecPanel {...props} />}
        {tab === "changes" && <ChangesPanel baseline={props.baseline} spec={props.spec} engineReady={props.engineReady}
          ready={props.ready} onRebase={props.onRebase} />}
        {tab === "migration" && scorecard && <MigrationPanel {...props} scorecard={scorecard} />}
      </div>
    </aside>
  );
}

// ------------------------------------------------------------------ Step

function ImpactCard({ impact, onClear }: { impact: Impact; onClear: () => void }) {
  const group = (icon: typeof Unlink, title: string, items: string[], empty: string) => {
    const Icon = icon;
    return (
      <div className="impact-group">
        <h3><Icon aria-hidden="true" />{title}<span className="count">{items.length}</span></h3>
        {items.length ? <ul className="refs">{items.map((i) => <li key={i}>{i}</li>)}</ul> : <p className="empty">{empty}</p>}
      </div>
    );
  };
  return (
    <section className="impact" aria-live="polite">
      <div className="impact-head">
        <h2>If <code>{impact.column}</code> changes</h2>
        <button className="btn ghost sm icon" onClick={onClear} aria-label="Clear"><X /></button>
      </div>
      <div className="impact-groups">
        {group(Unlink, "A rename breaks", impact.breaks, "Nothing names this column directly.")}
        {group(Rows3, "Rows change in", impact.rows, "No step filters, joins or groups on it.")}
        {group(Waypoints, "Values change in", impact.values, "No column is computed from it.")}
      </div>
    </section>
  );
}

function Overview({ spec, analysis, notes }: Props) {
  const issues = analysis?.issues ?? [];
  const count = (t: StepType) => spec.nodes.filter((n) => n.type === t).length;
  return (
    <>
      <section>
        <h3>Pipeline</h3>
        <div className="stats">
          <div className="stat"><b>{spec.nodes.length}</b><span>steps</span></div>
          <div className="stat"><b>{count("source")}</b><span>sources</span></div>
          <div className="stat"><b>{count("target")}</b><span>targets</span></div>
          <div className={`stat ${issues.length ? "bad" : "good"}`}><b>{issues.length}</b><span>issues</span></div>
        </div>
      </section>
      {issues.length > 0 && (
        <section>
          <h3>Issues</h3>
          <ul className="callouts bad issues">
            {issues.map((i, k) => <li key={k}><CircleAlert /><span><strong>{i.node ?? "pipeline"}</strong>{i.message}</span></li>)}
          </ul>
        </section>
      )}
      {notes.length > 0 && (
        <section>
          <h3>Import notes</h3>
          <ul className="callouts notes">{notes.map((n) => <li key={n}><TriangleAlert /><span>{n}</span></li>)}</ul>
        </section>
      )}
      <section>
        <h3>Explore</h3>
        <ul className="tips">
          <li><span className="mi-icon"><MousePointerClick /></span><span><b>Hover a column</b>Trace it back to the source columns it comes from.</span></li>
          <li><span className="mi-icon"><Waypoints /></span><span><b>Click a column</b>See its blast radius: what a change would break, re-filter or recompute.</span></li>
          <li><span className="mi-icon"><Keyboard /></span><span><b>Select a step</b>Edit it, preview its rows or read the code it compiles to. <kbd>Esc</kbd> clears.</span></li>
        </ul>
      </section>
    </>
  );
}

function StepPanel(props: Props) {
  const { analysis, selected, impact, onUpdateStep, onDeleteStep, onAddStep, onClearFocus, onSelectStep } = props;
  const issues = analysis?.issues ?? [];
  if (!selected) {
    return (
      <>
        {impact && <ImpactCard impact={impact} onClear={onClearFocus} />}
        <Overview {...props} />
      </>
    );
  }
  const columns = analysis?.columns[selected.id] ?? [];
  return (
    <>
      {impact && <ImpactCard impact={impact} onClear={onClearFocus} />}
      <section>
        <div className={`step-hero step-${selected.type}`}>
          <StepIcon type={selected.type} />
          <div>
            <h2>{selected.id}</h2>
            <span className="step-type">{STEP_LABELS[selected.type]} · {summarize(selected) || "no settings"}</span>
          </div>
        </div>
        {(selected.inputs ?? []).length > 0 && (
          <div className="field">
            <span>Reads from</span>
            <div className="ref-chips">
              {selected.inputs!.map((i) => (
                <button key={i} className="ref-chip" onClick={() => onSelectStep(i)}>{i}<ArrowRight aria-hidden="true" /></button>
              ))}
            </div>
          </div>
        )}
      </section>
      {props.tableOf(selected) && (
        <section className="model-this" data-testid="model-this">
          <div className="row between">
            <span className="quiet">Reads the table <span className="mono">{props.tableOf(selected)}</span>.</span>
            <button className="btn sm" onClick={() => props.onModelTable(props.tableOf(selected)!)}>
              <Layers />Model this data</button>
          </div>
        </section>
      )}
      <StepEditor
        key={selected.id + JSON.stringify(selected.params)}
        step={selected}
        issues={issues.filter((i) => i.node === selected.id).map((i) => i.message)}
        onApply={(params) => onUpdateStep(selected.id, params)}
      />
      {columns.length > 0 && (
        <section>
          <h3>Outputs {columns.length} columns</h3>
          <div className="ref-chips">{columns.map((c) => <span key={c} className="ref-chip static">{c}</span>)}</div>
        </section>
      )}
      <section>
        <h3>Add a step after this one</h3>
        <div className="add-grid">
          {ADDABLE.map((t) => {
            const Icon = STEP_ICONS[t];
            return (
              <button key={t} className={`step-${t}`} style={{ ["--t" as string]: `var(--t-${t})` }}
                onClick={() => onAddStep(t)} disabled={selected.type === "target"}>
                <Icon aria-hidden="true" />{STEP_LABELS[t]}
              </button>
            );
          })}
        </div>
      </section>
      <section className="row end">
        <button className="btn ghost danger sm" onClick={() => onDeleteStep(selected.id)}><Trash2 />Delete step</button>
      </section>
    </>
  );
}

function StepEditor({ step, issues, onApply }: {
  step: Step; issues: string[]; onApply: (p: Record<string, unknown>) => void;
}) {
  const [text, setText] = useState(() => yaml.dump(step.params ?? {}, { lineWidth: 80 }));
  const [error, setError] = useState<string | null>(null);
  const apply = () => {
    try {
      const parsed = yaml.load(text);
      if (parsed !== null && typeof parsed !== "object") throw new Error("Settings must be key: value pairs.");
      setError(null);
      onApply((parsed ?? {}) as Record<string, unknown>);
    } catch (e) {
      setError(e instanceof Error ? e.message.split("\n")[0] : String(e));
    }
  };
  return (
    <section>
      {issues.length > 0 && (
        <ul className="callouts bad" style={{ marginBottom: 12 }}>
          {issues.map((i) => <li key={i}><CircleAlert /><span>{i}</span></li>)}
        </ul>
      )}
      <label className="field">
        <span>Settings</span>
        <textarea
          className="code-input"
          value={text}
          spellCheck={false}
          rows={Math.min(14, Math.max(4, text.split("\n").length + 1))}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") apply(); }}
        />
      </label>
      {error && <p className="error">{error}</p>}
      <div className="row between">
        <span className="faint" style={{ fontSize: 12 }}>YAML, the same as in the spec</span>
        <button className="btn primary sm" onClick={apply}>Apply changes<span className="shortcut">⌘↵</span></button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ Data

type Behind = { source: string; result: QueryResult }[];

function isNumeric(result: QueryResult): boolean[] {
  return result.columns.map((_, j) => result.rows.length > 0 && result.rows.every((r) => r[j] === null || typeof r[j] === "number" || /^-?\d+(\.\d+)?$/.test(String(r[j]))));
}

function Cell({ v }: { v: unknown }) {
  return v === null ? <span className="null">null</span> : <>{String(v)}</>;
}

function CheckList({ runs }: { runs: CheckRun[] }) {
  return (
    <section className="checks" aria-label="Data quality checks">
      <div className="section-head"><h3>Checks</h3><span className="chip">{runs.length}</span></div>
      <ul className="check-list">
        {runs.map((r) => {
          const state = r.failures === 0 ? "passed" : r.failures === null ? "error" : r.severity === "warn" ? "warned" : "failed";
          const result = r.failures === null ? "couldn't run" : r.failures === 0 ? "passed"
            : `${r.failures} ${r.failures === 1 ? "row fails" : "rows fail"}${r.severity === "warn" ? " (warn)" : ""}`;
          return (
            <li key={r.name} className="check">
              <div className="check-head">
                <span className={`check-dot ${state}`} aria-label={state} />
                <span className="name">{r.name}</span>
                <span className="result">{result}</span>
              </div>
              <div className="what">{r.error ?? r.describe}</div>
              {r.sample.length > 0 && (
                <div className="table-wrap">
                  <table>
                    <thead><tr>{r.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
                    <tbody>
                      {r.sample.map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j}><Cell v={v} /></td>)}</tr>)}
                    </tbody>
                  </table>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function DataPanel({ spec, selected, analysis, ready, checkRuns }: Props) {
  const nodeId = selected?.id ?? analysis?.order?.at(-1) ?? null;
  const [result, setResult] = useState<QueryResult | null>(null);
  const [trace, setTrace] = useState<{ provenance: Record<string, string>; opaque: string[] }>({ provenance: {}, opaque: [] });
  const [raw, setRaw] = useState<string[][]>([]); // each row's source row numbers, per source
  const [picked, setPicked] = useState<number | null>(null);
  const [behind, setBehind] = useState<Behind | null>(null);
  const [state, setState] = useState<{ loading: boolean; error: string | null; shown: string | null }>({
    loading: false, error: null, shown: null,
  });
  useEffect(() => {
    if (!ready || !nodeId || !analysis?.valid) return;
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    setPicked(null);
    setBehind(null);
    (async () => {
      // The rows come with the source row numbers behind each one, so a click can trace it.
      const q = await engine.traceQuery(spec, nodeId);
      if (!q.ok) throw new Error(q.error);
      const prov = Object.keys(q.provenance);
      const sql = prov.length
        ? `SELECT * REPLACE (${prov.map((c) => `array_to_string(${c}, ',') AS ${c}`).join(", ")}) FROM (${q.sql}) AS traced`
        : q.sql;
      const r = await data.query(sql, 200);
      if (!live) return;
      const at = prov.map((c) => r.columns.indexOf(c));
      const keep = r.columns.map((_, j) => !at.includes(j));
      setResult({ columns: r.columns.filter((_, j) => keep[j]), rows: r.rows.map((row) => row.filter((_, j) => keep[j])) });
      setTrace({ provenance: q.provenance, opaque: q.opaque });
      setRaw(r.rows.map((row) => at.map((j) => String(row[j] ?? ""))));
      setState({ loading: false, error: null, shown: q.node });
    })().catch((e) => live && setState({ loading: false, error: String(e).replace(/^Error: /, ""), shown: null }));
    return () => { live = false; };
  }, [spec, nodeId, ready, analysis]);

  const pick = async (i: number) => {
    if (picked === i) { setPicked(null); setBehind(null); return; }
    setPicked(i);
    setBehind(null);
    const sources = Object.values(trace.provenance);
    const out: Behind = [];
    for (const [k, source] of sources.entries()) {
      const ids = (raw[i]?.[k] ?? "").split(",").filter(Boolean).map(Number);
      const q = await engine.sourceRows(spec, source, ids);
      if (!q.ok) continue;
      out.push({ source, result: await data.query(q.sql) });
    }
    setBehind(out);
  };

  if (!nodeId) return <p className="quiet">Add a step to preview its data.</p>;
  if (!ready) return <Starting what="DuckDB, which computes previews in this tab" />;
  if (analysis && !analysis.valid) return <p className="error">Fix the pipeline's issues to preview data.</p>;
  const numeric = result ? isNumeric(result) : [];
  const traceable = Object.keys(trace.provenance).length > 0;
  return (
    <section className={picked !== null ? "data has-pick" : "data"}>
      <div className="section-head">
        <h2 className="mono" style={{ fontSize: 14 }}>{state.shown ?? nodeId}</h2>
        <span className="chip">
          {state.loading ? "Running…" : result ? `${result.rows.length < 200 ? result.rows.length : "first 200"} rows` : ""}
        </span>
      </div>
      <p className="quiet" style={{ fontSize: 12 }}>
        Computed by DuckDB in this tab.{" "}
        {traceable ? "Click a row to see the source rows behind it." : "Select another step to preview it."}
      </p>
      {state.error && <p className="error">{state.error}</p>}
      {result && !state.error && (
        <div className="table-wrap">
          <table className={traceable ? "traceable" : ""}>
            <thead><tr>{result.columns.map((c, j) => <th key={c} scope="col" className={numeric[j] ? "num" : ""}>{c}</th>)}</tr></thead>
            <tbody>
              {result.rows.map((r, i) => (
                <tr
                  key={i}
                  className={picked === i ? "is-picked" : ""}
                  onClick={traceable ? () => pick(i) : undefined}
                  tabIndex={traceable ? 0 : undefined}
                  onKeyDown={traceable ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(i); } } : undefined}
                  aria-selected={traceable ? picked === i : undefined}
                >
                  {r.map((v, j) => <td key={j} className={numeric[j] ? "num" : ""}><Cell v={v} /></td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {picked !== null && (
        <div className="behind" aria-live="polite">
          <div className="section-head">
            <h3>Behind this row</h3>
            <button className="btn sm ghost" onClick={() => { setPicked(null); setBehind(null); }}>Clear</button>
          </div>
          {!behind && <p className="quiet">Tracing…</p>}
          {behind?.map(({ source, result: rows }) => {
            const num = isNumeric(rows);
            return (
              <div className="behind-source" key={source}>
                <div className="behind-title">
                  <span className="mono">{source}</span>
                  <span className="chip">{rows.rows.length} {rows.rows.length === 1 ? "row" : "rows"}</span>
                </div>
                {rows.rows.length === 0 ? (
                  <p className="quiet" style={{ fontSize: 12 }}>No row from {source}: an outer join found no match.</p>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead><tr>{rows.columns.map((c, j) => <th key={c} scope="col" className={num[j] ? "num" : ""}>{c === "row" ? "#" : c}</th>)}</tr></thead>
                      <tbody>
                        {rows.rows.map((r, i) => (
                          <tr key={i}>{r.map((v, j) => <td key={j} className={j === 0 ? "num rowno" : num[j] ? "num" : ""}><Cell v={v} /></td>)}</tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
          {behind && trace.opaque.length > 0 && (
            <p className="quiet" style={{ fontSize: 12 }}>
              Rows also pass through the SQL step {trace.opaque.join(", ")}, which a trace can't see inside.
            </p>
          )}
        </div>
      )}
      {checkRuns.some((r) => r.node === nodeId) && <CheckList runs={checkRuns.filter((r) => r.node === nodeId)} />}
    </section>
  );
}

// ------------------------------------------------------------------ Code

const TARGETS = [
  { id: "spark", label: "Spark SQL" },
  { id: "pyspark", label: "PySpark" },
  { id: "duckdb", label: "DuckDB" },
  { id: "dbt", label: "dbt" },
  { id: "lakeflow", label: "Lakeflow" },
];

const PROJECT_NOTE: Record<string, string> = {
  dbt: "A dbt project for Databricks: steps are ephemeral models, targets are tables, and every column is documented with its lineage. On the CLI, --adapter picks another warehouse.",
  lakeflow: "A Lakeflow Declarative Pipeline (pyspark.pipelines): steps are temporary views, targets materialized views. It runs on Databricks and with open-source spark-pipelines.",
};

/** The file a project opens on: the one a reader wants first. */
function mainFile(target: string, files: Record<string, string>): string {
  const names = Object.keys(files);
  if (target === "lakeflow") return names.find((n) => n.endsWith(".py")) ?? names[0];
  const models = names.filter((n) => n.startsWith("models/") && n.endsWith(".sql") && !/\/(int|src)_/.test(n));
  return models.find((n) => /config\(/.test(files[n])) ?? models[0] ?? names[0];
}

function Starting({ what }: { what: string }) {
  return (
    <div className="starting" role="status">
      <span className="status-dot loading" />
      <span>Starting {what}. It loads once, then stays cached.</span>
    </div>
  );
}

function CodePanel({ spec, selected, engineReady: ready }: Props) {
  const [target, setTarget] = useState("spark");
  const [scope, setScope] = useState<"pipeline" | "step">("pipeline");
  const [code, setCode] = useState("");
  const [files, setFiles] = useState<Record<string, string> | null>(null);
  const [file, setFile] = useState("");
  const [copied, setCopied] = useState(false);
  const project = target === "dbt" || target === "lakeflow";
  const stepScope = scope === "step" && selected && selected.type !== "target" && target !== "pyspark" && !project;
  useEffect(() => {
    if (!ready) return;
    let live = true;
    engine.code(spec, target, stepScope ? selected!.id : null).then((r) => {
      if (!live) return;
      if (r.ok && r.files) {
        setFiles(r.files);
        setFile((f) => (f in r.files! ? f : mainFile(target, r.files!)));
      } else {
        setFiles(null);
        setCode(r.ok ? r.code : `-- ${r.error}`);
      }
    });
    return () => { live = false; };
  }, [spec, target, stepScope, selected, ready]);
  const shown = project ? (files?.[file] ?? "") : code;
  const python = project ? !file.endsWith(".sql") : target === "pyspark";
  const coloured = useMemo(() => highlight(shown, python), [shown, python]);
  const base = stepScope ? selected!.id : spec.name || "pipeline";
  if (!ready) return <Starting what="the code generator (Python in your browser)" />;
  const fileName = project ? file : target === "pyspark" ? `${base}.py` : `${base}.${target === "spark" ? "spark" : "duckdb"}.sql`;
  const root = `${spec.name || "pipeline"}_${target}`;
  return (
    <section className="code">
      <div className="segmented wrap" role="group" aria-label="Language">
        {TARGETS.map((t) => (
          <button key={t.id} aria-pressed={target === t.id} onClick={() => setTarget(t.id)}>{t.label}</button>
        ))}
      </div>
      {selected && selected.type !== "target" && target !== "pyspark" && !project && (
        <div className="segmented small" role="group" aria-label="Scope" style={{ marginTop: 8 }}>
          <button aria-pressed={scope === "pipeline"} onClick={() => setScope("pipeline")}>Whole pipeline</button>
          <button aria-pressed={scope === "step"} onClick={() => setScope("step")}>Up to {selected.id}</button>
        </div>
      )}
      {project && files && (
        <>
          <p className="quiet code-note">{PROJECT_NOTE[target]}</p>
          <div className="row between project-bar">
            <select className="file-select" value={file} onChange={(e) => setFile(e.target.value)} aria-label="File">
              {Object.keys(files).filter((n) => !n.endsWith(".gitkeep")).map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
            <button className="btn sm" onClick={() => downloadBlob(`${root}.zip`, zip(files, root))}>
              <Download />Download .zip
            </button>
          </div>
        </>
      )}
      <div className="code-shell">
        <div className="code-head">
          <span className="code-file">{fileName}</span>
          <button
            className="btn sm copy"
            onClick={async () => {
              try { await navigator.clipboard.writeText(shown); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* clipboard blocked */ }
            }}
          >
            {copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy code"}
          </button>
        </div>
        <pre className="code-block"><code>{coloured}</code></pre>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ Spec

function download(name: string, text: string, type: string) {
  downloadBlob(name, new Blob([text], { type }));
}

function downloadBlob(name: string, blob: Blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function SpecPanel({ spec, onChangeSpec }: Props) {
  const [text, setText] = useState(() => yaml.dump(spec, { lineWidth: 100, flowLevel: 4 }));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { setText(yaml.dump(spec, { lineWidth: 100, flowLevel: 4 })); }, [spec]);
  const apply = () => {
    try {
      const parsed = yaml.load(text) as Spec;
      if (!parsed || !Array.isArray(parsed.nodes)) throw new Error("A spec needs a name and a list of nodes.");
      setError(null);
      onChangeSpec(parsed);
    } catch (e) {
      setError(e instanceof Error ? e.message.split("\n")[0] : String(e));
    }
  };
  return (
    <section className="spec">
      <p className="quiet" style={{ fontSize: 12.5 }}>
        The spec is the source of truth. The canvas edits it; code and lineage are generated from it.
      </p>
      <textarea className="code-input tall" value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} aria-label="Pipeline spec (YAML)" />
      {error && <p className="error">{error}</p>}
      <div className="row between" style={{ marginTop: 10 }}>
        <button className="btn sm" onClick={() => download(`${spec.name || "pipeline"}.yaml`, text, "text/yaml")}><Download />Download YAML</button>
        <button className="btn primary sm" onClick={apply}>Apply spec</button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ Migration

const STATUS_LABEL: Record<ScoreStatus, string> = { converted: "Converted", review: "Review", manual: "Rewrite" };

function Ring({ card }: { card: Scorecard }) {
  const total = card.items.length || 1;
  const r = 38;
  const c = 2 * Math.PI * r;
  let offset = 0;
  const segs = (["converted", "review", "manual"] as ScoreStatus[]).map((s) => {
    const len = (card.counts[s] / total) * c;
    const seg = { s, len, offset };
    offset += len;
    return seg;
  });
  const color = { converted: "var(--ok)", review: "var(--warn)", manual: "var(--bad)" };
  return (
    <div className="ring" role="img" aria-label={`${Math.round(card.automated * 100)}% converted automatically`}>
      <svg viewBox="0 0 88 88">
        <circle className="track" cx="44" cy="44" r={r} />
        {segs.filter((g) => g.len > 0).map((g) => (
          <circle key={g.s} className="seg" cx="44" cy="44" r={r} stroke={color[g.s]}
            strokeDasharray={`${Math.max(g.len - 2, 0.5)} ${c}`} strokeDashoffset={-g.offset} />
        ))}
      </svg>
      <div className="ring-label"><b>{Math.round(card.automated * 100)}%</b><span>automatic</span></div>
    </div>
  );
}

function describe(card: Scorecard, source: string): string {
  const tasks = card.items.filter((i) => i.area === "orchestration").length;
  const unit = card.origin === "powercenter" ? "instances" : card.origin === "ssis" || card.origin === "talend" ? "components"
    : card.origin === "datastage" ? "stages" : "statements";
  const steps = card.items.length - tasks;
  return tasks
    ? `${steps} ${unit} and ${tasks} workflow ${tasks === 1 ? "task" : "tasks"} from ${source}`
    : `${steps} ${unit} from ${source}`;
}

function MigrationPanel({ scorecard: card, activeItem, onPickItem, onTab, onSelectStep, engineReady, onModelFromJob }: Props & { scorecard: Scorecard }) {
  const [filter, setFilter] = useState<"all" | ScoreStatus>("all");
  const order: Record<ScoreStatus, number> = { manual: 0, review: 1, converted: 2 };
  const items = [...card.items]
    .filter((i) => filter === "all" || i.status === filter)
    .sort((a, b) => order[a.status] - order[b.status]);
  const lin = card.lineage;
  const source = card.origin === "powercenter" ? "Informatica PowerCenter" : card.origin === "ssis" ? "SSIS"
    : card.origin === "talend" ? "Talend" : card.origin === "datastage" ? "IBM DataStage" : "SQL";
  const markdown = () => {
    const lines = [`# Migration scorecard: ${card.title}`, "",
      `${card.counts.converted} of ${card.items.length} converted automatically (${Math.round(card.automated * 100)}%), ` +
      `${card.counts.review} to review, ${card.counts.manual} to rewrite by hand.`,
      ...(card.items.some((i) => i.area === "orchestration")
        ? [`${card.items.filter((i) => i.area === "orchestration").length} of the ${card.items.length} are workflow tasks, which belong in an orchestrator rather than a pipeline.`]
        : []),
      `Lineage: ${lin.traced} of ${lin.columns} target columns trace back to a source.`, "",
      "| Status | Name | Type | Became | Notes |", "| --- | --- | --- | --- | --- |",
      ...[...card.items].sort((a, b) => order[a.status] - order[b.status]).map((i) =>
        `| ${i.status} | ${i.name} | ${i.kind} | ${i.nodes.map((n) => `\`${n}\``).join(", ") || "–"} | ${i.notes.join("<br>").replace(/\|/g, "\\|")} |`)];
    return lines.join("\n") + "\n";
  };
  const pick = (item: ScoreItem) => {
    const next = activeItem === item.name ? null : item.name;
    onPickItem(next);
    if (next && item.nodes.length) onSelectStep(item.nodes[item.nodes.length - 1]);
  };
  return (
    <>
      <section>
        <div className="score-hero">
          <Ring card={card} />
          <div className="score-text">
            <h2>{card.title}</h2>
            <p>{describe(card, source)}</p>
            <div className="score-legend">
              <span className="chip ok">{card.counts.converted} converted</span>
              <span className="chip warn">{card.counts.review} review</span>
              <span className={`chip ${card.counts.manual ? "bad" : ""}`}>{card.counts.manual} rewrite</span>
            </div>
          </div>
        </div>
        {lin.columns > 0 && (
          <div className="lineage-bar">
            <div className="row"><span>Column lineage to a source</span><b className="num">{lin.traced} / {lin.columns}</b></div>
            <div className="bar-track"><div className="bar-fill" style={{ width: `${(lin.traced / lin.columns) * 100}%` }} /></div>
            {lin.untraced.length > 0 && (
              <p className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
                No source column (constants or placeholders): <span className="mono">{lin.untraced.join(", ")}</span>
              </p>
            )}
          </div>
        )}
      </section>
      <section data-testid="model-from-job">
        <h3>Model it</h3>
        <p className="quiet">Propose a data model for the tables this job reads, from what it does with them: the
          columns it joins and looks up on become keys, the ones it sums become measures. Every choice names its step.</p>
        <div className="row">
          <button className="btn sm" disabled={!engineReady} onClick={() => onModelFromJob("kimball")}><Boxes />Kimball model</button>
          <button className="btn sm ghost" disabled={!engineReady} onClick={() => onModelFromJob("data_vault")}>Data Vault 2.0</button>
        </div>
      </section>
      <section>
        <div className="row between filters">
          <div className="segmented small" role="group" aria-label="Filter">
            {(["all", "review", "manual", "converted"] as const).map((f) => (
              <button key={f} aria-pressed={filter === f} onClick={() => setFilter(f)}>
                {f === "all" ? `All ${card.items.length}` : `${STATUS_LABEL[f]} ${card.counts[f]}`}
              </button>
            ))}
          </div>
          <button className="btn sm ghost" onClick={() => download(`${card.title}-scorecard.md`, markdown(), "text/markdown")}>
            <Download />Report
          </button>
        </div>
        <ul className="score-items">
          {items.map((item) => (
            <li key={item.name}>
              <button className={`score-item${activeItem === item.name ? " is-active" : ""}`} onClick={() => pick(item)}>
                <div className="score-item-head">
                  <span className={`score-dot ${item.status}`} aria-label={item.status} />
                  <span className="name">{item.name}</span>
                  <span className="kind">{item.kind}</span>
                </div>
                {item.status !== "converted" && item.notes.length > 0 && (
                  <ul>{item.notes.map((n) => <li key={n}>{n}</li>)}</ul>
                )}
                {item.nodes.length > 0 && (
                  <div className="became"><ArrowRight aria-hidden="true" />{item.nodes.map((n) => <span key={n}>{n}</span>)}</div>
                )}
              </button>
            </li>
          ))}
        </ul>
        {items.length === 0 && <p className="faint">Nothing here.</p>}
        <p className="faint" style={{ fontSize: 11.5, marginTop: 12 }}>
          Pick an item to find the steps it became. <button className="link-btn" onClick={() => onTab("code")}>See the generated code</button>
        </p>
      </section>
    </>
  );
}
