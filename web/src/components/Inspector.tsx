import * as yaml from "js-yaml";
import { useEffect, useState } from "react";
import { query, type QueryResult } from "../engine/duck";
import { engine } from "../engine/engine";
import type { Analysis, Impact, Spec, Step, StepType } from "../model";
import { STEP_LABELS, summarize } from "../model";

export type Tab = "step" | "data" | "code" | "spec";

type Props = {
  tab: Tab;
  onTab: (t: Tab) => void;
  spec: Spec;
  analysis: Analysis | null;
  selected: Step | null;
  impact: Impact | null;
  notes: string[];
  ready: boolean;
  onChangeSpec: (spec: Spec) => void;
  onUpdateStep: (id: string, params: Record<string, unknown>) => void;
  onDeleteStep: (id: string) => void;
  onAddStep: (type: StepType) => void;
  onClearFocus: () => void;
};

const TABS: { id: Tab; label: string }[] = [
  { id: "step", label: "Step" },
  { id: "data", label: "Data" },
  { id: "code", label: "Code" },
  { id: "spec", label: "Spec" },
];

const ADDABLE: StepType[] = ["filter", "derive", "select", "aggregate", "join", "union", "sql", "target"];

export function Inspector(props: Props) {
  const { tab, onTab } = props;
  return (
    <aside className="inspector" aria-label="Inspector">
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={tab === t.id ? "tab is-active" : "tab"}
            onClick={() => onTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="panel" role="tabpanel">
        {tab === "step" && <StepPanel {...props} />}
        {tab === "data" && <DataPanel {...props} />}
        {tab === "code" && <CodePanel {...props} />}
        {tab === "spec" && <SpecPanel {...props} />}
      </div>
    </aside>
  );
}

// ------------------------------------------------------------------ Step

function ImpactCard({ impact, onClear }: { impact: Impact; onClear: () => void }) {
  const list = (items: string[], empty: string) =>
    items.length ? <ul className="refs">{items.map((i) => <li key={i}>{i}</li>)}</ul> : <p className="quiet">{empty}</p>;
  return (
    <section className="impact" aria-live="polite">
      <div className="impact-head">
        <h2>If <code>{impact.column}</code> changes</h2>
        <button className="link" onClick={onClear}>Clear</button>
      </div>
      <h3>A rename breaks</h3>
      {list(impact.breaks, "Nothing names this column directly.")}
      <h3>Rows change in</h3>
      {list(impact.rows, "No step filters, joins or groups on it.")}
      <h3>Values change in</h3>
      {list(impact.values, "No column is computed from it.")}
    </section>
  );
}

function StepPanel({ spec, analysis, selected, impact, notes, onUpdateStep, onDeleteStep, onAddStep, onChangeSpec, onClearFocus }: Props) {
  const issues = analysis?.issues ?? [];
  if (!selected) {
    return (
      <>
        {impact && <ImpactCard impact={impact} onClear={onClearFocus} />}
        <section>
          <label className="field">
            <span>Pipeline name</span>
            <input value={spec.name} onChange={(e) => onChangeSpec({ ...spec, name: e.target.value })} />
          </label>
          <p className="quiet">
            Hover a column on a step to trace where it comes from. Click it to see what a change would touch.
          </p>
        </section>
        {notes.length > 0 && (
          <section>
            <h2>Import notes</h2>
            <ul className="notes">{notes.map((n) => <li key={n}>{n}</li>)}</ul>
          </section>
        )}
        <section>
          <h2>{issues.length ? `${issues.length} ${issues.length === 1 ? "issue" : "issues"}` : "No issues"}</h2>
          {issues.length > 0 && (
            <ul className="issues">
              {issues.map((i, k) => <li key={k}><strong>{i.node ?? "pipeline"}</strong> {i.message}</li>)}
            </ul>
          )}
        </section>
      </>
    );
  }
  return (
    <>
      {impact && <ImpactCard impact={impact} onClear={onClearFocus} />}
      <StepEditor
        key={selected.id + JSON.stringify(selected.params)}
        step={selected}
        issues={issues.filter((i) => i.node === selected.id).map((i) => i.message)}
        columns={analysis?.columns[selected.id] ?? []}
        onApply={(params) => onUpdateStep(selected.id, params)}
      />
      <section className="actions">
        <label className="field">
          <span>Add a step after this one</span>
          <select
            value=""
            onChange={(e) => e.target.value && onAddStep(e.target.value as StepType)}
            disabled={selected.type === "target"}
          >
            <option value="">Choose a step type</option>
            {ADDABLE.map((t) => <option key={t} value={t}>{STEP_LABELS[t]}</option>)}
          </select>
        </label>
        <button className="danger" onClick={() => onDeleteStep(selected.id)}>Delete step</button>
      </section>
    </>
  );
}

function StepEditor({ step, issues, columns, onApply }: {
  step: Step; issues: string[]; columns: string[]; onApply: (p: Record<string, unknown>) => void;
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
      <p className="step-kicker">{STEP_LABELS[step.type]}</p>
      <h2 className="step-title">{step.id}</h2>
      <p className="quiet mono">{summarize(step)}</p>
      {(step.inputs ?? []).length > 0 && <p className="quiet">Reads from {step.inputs!.join(", ")}</p>}
      {issues.length > 0 && <ul className="issues">{issues.map((i) => <li key={i}>{i}</li>)}</ul>}
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
      <button className="primary" onClick={apply}>Apply changes</button>
      {columns.length > 0 && <p className="quiet">Outputs {columns.length} columns: {columns.join(", ")}</p>}
    </section>
  );
}

// ------------------------------------------------------------------ Data

function DataPanel({ spec, selected, analysis, ready }: Props) {
  const nodeId = selected?.id ?? analysis?.order?.at(-1) ?? null;
  const [result, setResult] = useState<QueryResult | null>(null);
  const [state, setState] = useState<{ loading: boolean; error: string | null; shown: string | null }>({
    loading: false, error: null, shown: null,
  });
  useEffect(() => {
    if (!ready || !nodeId || !analysis?.valid) return;
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    (async () => {
      const q = await engine.previewQuery(spec, nodeId);
      if (!q.ok) throw new Error(q.error);
      const r = await query(q.sql, 200);
      if (live) {
        setResult(r);
        setState({ loading: false, error: null, shown: q.node });
      }
    })().catch((e) => live && setState({ loading: false, error: String(e).replace(/^Error: /, ""), shown: null }));
    return () => { live = false; };
  }, [spec, nodeId, ready, analysis]);

  if (!nodeId) return <p className="quiet">Add a step to preview its data.</p>;
  if (analysis && !analysis.valid) return <p className="error">Fix the pipeline's issues to preview data.</p>;
  return (
    <section className="data">
      <h2>{state.shown ?? nodeId}</h2>
      <p className="quiet">
        {state.loading ? "Running in your browser…" : result ? `First ${Math.min(result.rows.length, 200)} rows` : ""}
      </p>
      {state.error && <p className="error">{state.error}</p>}
      {result && !state.error && (
        <div className="table-wrap">
          <table>
            <thead><tr>{result.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
            <tbody>
              {result.rows.map((r, i) => (
                <tr key={i}>{r.map((v, j) => <td key={j} className={typeof v === "number" || /^-?\d+(\.\d+)?$/.test(String(v)) ? "num" : ""}>{v === null ? <span className="null">null</span> : String(v)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ Code

const TARGETS = [
  { id: "spark", label: "Spark SQL" },
  { id: "pyspark", label: "PySpark" },
  { id: "duckdb", label: "DuckDB SQL" },
];

function CodePanel({ spec, selected, ready }: Props) {
  const [target, setTarget] = useState("spark");
  const [scope, setScope] = useState<"pipeline" | "step">("pipeline");
  const [code, setCode] = useState("");
  const [copied, setCopied] = useState(false);
  const stepScope = scope === "step" && selected && selected.type !== "target" && target !== "pyspark";
  useEffect(() => {
    if (!ready) return;
    let live = true;
    engine.code(spec, target, stepScope ? selected!.id : null).then((r) => {
      if (live) setCode(r.ok ? r.code : `-- ${r.error}`);
    });
    return () => { live = false; };
  }, [spec, target, stepScope, selected, ready]);
  return (
    <section className="code">
      <div className="segmented" role="group" aria-label="Language">
        {TARGETS.map((t) => (
          <button key={t.id} aria-pressed={target === t.id} onClick={() => setTarget(t.id)}>{t.label}</button>
        ))}
      </div>
      {selected && selected.type !== "target" && target !== "pyspark" && (
        <div className="segmented small" role="group" aria-label="Scope">
          <button aria-pressed={scope === "pipeline"} onClick={() => setScope("pipeline")}>Whole pipeline</button>
          <button aria-pressed={scope === "step"} onClick={() => setScope("step")}>Up to {selected.id}</button>
        </div>
      )}
      <pre className="code-block"><code>{code}</code></pre>
      <button
        onClick={async () => {
          try { await navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* clipboard blocked */ }
        }}
      >
        {copied ? "Copied" : "Copy code"}
      </button>
    </section>
  );
}

// ------------------------------------------------------------------ Spec

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
  const download = () => {
    const blob = new Blob([text], { type: "text/yaml" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${spec.name || "pipeline"}.yaml`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return (
    <section className="spec">
      <p className="quiet">The pipeline spec is the source of truth. Edit it here or on the canvas.</p>
      <textarea className="code-input tall" value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} aria-label="Pipeline spec (YAML)" />
      {error && <p className="error">{error}</p>}
      <div className="row">
        <button className="primary" onClick={apply}>Apply spec</button>
        <button onClick={download}>Download YAML</button>
      </div>
    </section>
  );
}
