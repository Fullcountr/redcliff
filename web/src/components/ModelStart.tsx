// Model mode before there is a model: start from your tables, from the open pipeline (a legacy
// job's joins and lookups), from DDL or DBML, or from an example.
import { BookOpen, FileInput, FilePlus2, Layers, Table2, Workflow } from "lucide-react";
import { useEffect, useState } from "react";
import type { DataModel, Spec } from "../model";
import { ACCEPT } from "../files";
import type { TableInfo } from "../modelData";
import { ImportPanel, TablePicker } from "./ModelView";

type Props = {
  tables: TableInfo[];
  /** Tables to tick when the start screen opens (files just added). */
  preselect: string[];
  engineReady: boolean;
  dataReady: boolean;
  busy: boolean;
  error: string | null;
  spec: Spec | null;
  examples: Record<string, DataModel>;
  onPropose: (tables: string[], style: "kimball" | "data_vault") => void;
  onFromPipeline: (style: "kimball" | "data_vault") => void;
  onImported: (model: DataModel, note?: string) => void;
  onExample: (model: DataModel) => void;
  onAddFiles: (files: FileList) => void;
  /** Which way to start opens first (Import > Data model opens DDL). */
  initialWay?: "data" | "pipeline" | "ddl" | "example";
  /** The model you had before starting a new one, to go back to. */
  previous?: string | null;
  onBack?: () => void;
};

export function ModelStart(props: Props) {
  const { tables, preselect, engineReady, dataReady, busy, error, spec, examples } = props;
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [style, setStyle] = useState<"kimball" | "data_vault">("kimball");
  const [way, setWay] = useState<"data" | "pipeline" | "ddl" | "example">(props.initialWay ?? "data");
  useEffect(() => { if (props.initialWay) setWay(props.initialWay); }, [props.initialWay]);
  const key = preselect.join(",");
  useEffect(() => {
    // Files just added are what you want to model; otherwise yours, else the two sample tables that relate.
    const yours = tables.filter((t) => !t.sample).map((t) => t.name);
    const start = preselect.length ? preselect : yours.length ? yours : tables.filter((t) => ["orders", "customers"].includes(t.name)).map((t) => t.name);
    setPicked(new Set(start));
    if (preselect.length) setWay("data");
  }, [key, tables.length]);
  const ready = engineReady && dataReady;
  const ways = [
    { id: "data", icon: Table2, title: "From your tables", sub: "Profile them; propose entities, keys and references" },
    { id: "pipeline", icon: Workflow, title: "From the open pipeline", sub: "What a legacy job joins, looks up and sums" },
    { id: "ddl", icon: FileInput, title: "Paste DDL or DBML", sub: "A model you already have" },
    { id: "example", icon: BookOpen, title: "An example", sub: "Kimball or Data Vault, on the sample data" },
  ] as const;
  return (
    <main className="canvas model-start" data-testid="model-start">
      <div className="start-card glass">
        <div className="start-head">
          <span className="start-icon"><Layers /></span>
          <div>
            <h1>Start a data model</h1>
            <p className="quiet">A model is a file of its own (<code>model.yaml</code>): entities, keys, history and
              relationships. Redcliff proposes one with the reason for each choice; you keep editing it, and add tables
              to it as they come.</p>
          </div>
        </div>
        <div className="start-ways" role="tablist" aria-label="How to start">
          {ways.map((w) => (
            <button key={w.id} role="tab" aria-selected={way === w.id} className={`start-way${way === w.id ? " is-active" : ""}`}
              onClick={() => setWay(w.id)}>
              <w.icon aria-hidden="true" />
              <span><strong>{w.title}</strong><span className="quiet">{w.sub}</span></span>
            </button>
          ))}
        </div>
        <div className="start-body">
          {way === "data" && (
            !ready ? <p className="quiet">Available once the engine and DuckDB have started.</p> : (
              <>
                <TablePicker tables={tables} picked={picked} onPick={setPicked} />
                <div className="row between">
                  <div className="row">
                    <label className="btn sm ghost">
                      <FilePlus2 />Add file
                      <input type="file" accept={ACCEPT} multiple hidden
                        onChange={(e) => { if (e.target.files?.length) props.onAddFiles(e.target.files); e.target.value = ""; }} />
                    </label>
                    <div className="segmented small" role="group" aria-label="Style">
                      <button aria-pressed={style === "kimball"} onClick={() => setStyle("kimball")}>Kimball</button>
                      <button aria-pressed={style === "data_vault"} onClick={() => setStyle("data_vault")}>Data Vault 2.0</button>
                    </div>
                  </div>
                  <button className="btn sm primary" disabled={busy || !picked.size} data-testid="propose-model"
                    onClick={() => props.onPropose([...picked].filter((n) => tables.some((t) => t.name === n)), style)}>
                    {busy ? "Profiling…" : `Propose a model from ${picked.size} ${picked.size === 1 ? "table" : "tables"}`}
                  </button>
                </div>
                <p className="quiet small-note">Start with a few tables if you like: you can add more later, and Redcliff
                  only proposes what they add.</p>
              </>
            )
          )}
          {way === "pipeline" && (
            spec ? (
              <>
                <p>Propose a model for the tables <span className="mono">{spec.name}</span> reads, from what it does with them:
                  the columns it joins and looks up on become keys, the ones it sums become measures.</p>
                <div className="row">
                  <button className="btn sm primary" disabled={!engineReady || busy} onClick={() => props.onFromPipeline("kimball")}>
                    Kimball model</button>
                  <button className="btn sm ghost" disabled={!engineReady || busy} onClick={() => props.onFromPipeline("data_vault")}>
                    Data Vault 2.0</button>
                </div>
                <p className="quiet small-note">Works best on an imported legacy job (Import, in the top bar).</p>
              </>
            ) : <p className="quiet">Open or import a pipeline first.</p>
          )}
          {way === "ddl" && <ImportPanel engineReady={engineReady} onChange={props.onImported} />}
          {way === "example" && (
            <ul className="example-list">
              {Object.entries(examples).map(([k, m]) => (
                <li key={k}>
                  <button className="btn ghost wide" onClick={() => props.onExample(m)}>
                    <Layers /><span><strong>{k.replace(/_/g, " ")}</strong>
                      <span className="quiet">{m.style === "data_vault" ? "Data Vault" : "Kimball"}: {m.entities.map((e) => e.id).join(", ")}</span></span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {error && <p className="error">{error}</p>}
        </div>
        {props.previous && props.onBack && (
          <div className="row end"><button className="btn sm ghost" onClick={props.onBack}>Back to {props.previous}</button></div>
        )}
      </div>
    </main>
  );
}
