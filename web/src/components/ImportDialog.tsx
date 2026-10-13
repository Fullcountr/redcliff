import { Boxes, Cable, FileCode2, FileUp, Lock, Network, Sparkles, Workflow, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

const DIALECTS = [
  ["teradata", "Teradata"], ["oracle", "Oracle"], ["tsql", "SQL Server"], ["snowflake", "Snowflake"],
  ["redshift", "Redshift"], ["bigquery", "BigQuery"], ["postgres", "PostgreSQL"], ["mysql", "MySQL"],
  ["hive", "Hive"], ["spark", "Spark SQL"], ["duckdb", "DuckDB"],
] as const;

export type ImportMode = "sql" | "powercenter" | "ssis" | "talend" | "datastage";
export type ImportSamples = {
  sql: string; powercenter: string; params?: string; ssis: string; talend: string; datastage: string;
};

type Props = {
  mode: ImportMode;
  samples: ImportSamples;
  busy: boolean;
  error: string | null;
  onImport: (mode: ImportMode, text: string, dialect: string, params: string) => void;
  onClose: () => void;
};

export function ImportDialog({ mode: initialMode, samples, busy, error, onImport, onClose }: Props) {
  const [mode, setMode] = useState<ImportMode>(initialMode);
  const [sql, setSql] = useState(samples.sql);
  const [xml, setXml] = useState(samples.powercenter);
  const [params, setParams] = useState(samples.params ?? "");
  const [dtsx, setDtsx] = useState(samples.ssis);
  const [item, setItem] = useState(samples.talend);
  const [dsx, setDsx] = useState(samples.datastage);
  const [fileName, setFileName] = useState<string | null>(null);
  const [dialect, setDialect] = useState("teradata");
  const [over, setOver] = useState(false);
  const ref = useRef<HTMLDialogElement>(null);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);

  const text = mode === "sql" ? sql : mode === "ssis" ? dtsx : mode === "talend" ? item : mode === "datastage" ? dsx : xml;
  const readFile = async (f: File) => {
    const content = await f.text();
    const isSsis = /\.dtsx$/i.test(f.name) || content.slice(0, 4000).includes("www.microsoft.com/SqlServer/Dts");
    const isTalend = /\.item$/i.test(f.name) || /talendfile:ProcessType|TalendFile\.xsd/.test(content.slice(0, 4000));
    const isDatastage = /\.dsx$/i.test(f.name) || /BEGIN DSJOB|<DSExport/.test(content.slice(0, 4000));
    const isXml = /\.xml$/i.test(f.name) || content.trimStart().startsWith("<");
    setFileName(f.name);
    if (isSsis) { setMode("ssis"); setDtsx(content); }
    else if (isTalend) { setMode("talend"); setItem(content); }
    else if (isDatastage) { setMode("datastage"); setDsx(content); }
    else if (isXml) { setMode("powercenter"); setXml(content); }
    else { setMode("sql"); setSql(content); }
  };

  return (
    <dialog ref={ref} className="dialog" onClose={onClose} aria-labelledby="import-title">
      <form method="dialog" onSubmit={(e) => {
        e.preventDefault();
        onImport(mode, text, dialect, mode === "talend" ? (fileName ?? "j_order_revenue.item").replace(/\.item$/i, "") : params);
      }}>
        <div className="dialog-head">
          <div>
            <h2 id="import-title">Import a legacy job</h2>
            <p>Each step becomes a node you can inspect, with column lineage and Spark code. Anything without a direct equivalent is kept and flagged, never dropped.</p>
          </div>
          <button type="button" className="btn ghost icon" onClick={onClose} aria-label="Close"><X /></button>
        </div>
        <div className="dialog-body">
          <div className="mode-switch" role="group" aria-label="Format">
            <button type="button" className="mode-card" aria-pressed={mode === "sql"} onClick={() => setMode("sql")}>
              <span className="mi-icon"><FileCode2 /></span>
              <span><b>SQL script</b><span>Teradata BTEQ, Oracle, SQL Server and 8 more dialects</span></span>
            </button>
            <button type="button" className="mode-card" aria-pressed={mode === "powercenter"} onClick={() => setMode("powercenter")}>
              <span className="mi-icon"><Workflow /></span>
              <span><b>Informatica PowerCenter</b><span>Mappings, mapplets, sessions and workflows, with a migration scorecard</span></span>
            </button>
            <button type="button" className="mode-card" aria-pressed={mode === "ssis"} onClick={() => setMode("ssis")}>
              <span className="mi-icon"><Boxes /></span>
              <span><b>SSIS package</b><span>Data flows and control flow from a .dtsx, with a migration scorecard</span></span>
            </button>
            <button type="button" className="mode-card" aria-pressed={mode === "talend"} onClick={() => setMode("talend")}>
              <span className="mi-icon"><Network /></span>
              <span><b>Talend job</b><span>tMap, lookups, filters and outputs from a .item, with a migration scorecard</span></span>
            </button>
            <button type="button" className="mode-card" aria-pressed={mode === "datastage"} onClick={() => setMode("datastage")}>
              <span className="mi-icon"><Cable /></span>
              <span><b>IBM DataStage</b><span>Parallel jobs from a .dsx or XML export, with a migration scorecard</span></span>
            </button>
          </div>
          <div
            className={`drop${over ? " is-over" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setOver(true); }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files[0]; if (f) readFile(f); }}
          >
            <span className="mi-icon"><FileUp /></span>
            <div>
              <b>{fileName ?? (mode === "sql" ? "Drop a .sql file" : mode === "ssis" ? "Drop an SSIS .dtsx package"
                : mode === "talend" ? "Drop a Talend job (.item)"
                  : mode === "datastage" ? "Drop a DataStage export (.dsx or .xml)" : "Drop a PowerCenter .xml export")}</b>
              <p>{fileName ? "Loaded below. Review it, then import." : "Or edit the sample below, an invented job built for this demo."}</p>
            </div>
            <button type="button" className="btn sm" onClick={() => file.current?.click()}>Choose file</button>
            <input ref={file} type="file" accept=".sql,.txt,.bteq,.xml,.dtsx,.item,.dsx" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) readFile(f); }} />
          </div>
          {mode === "sql" ? (
            <>
              <label className="field">
                <span>Dialect</span>
                <select className="select" value={dialect} onChange={(e) => setDialect(e.target.value)}>
                  {DIALECTS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
                </select>
              </label>
              <label className="field">
                <span>SQL</span>
                <textarea className="code-input tall" value={sql} spellCheck={false} onChange={(e) => setSql(e.target.value)} />
              </label>
            </>
          ) : mode === "datastage" ? (
            <label className="field">
              <span>Export (.dsx, or the XML export) with one or more parallel jobs</span>
              <textarea className="code-input tall" value={dsx} spellCheck={false} onChange={(e) => setDsx(e.target.value)} aria-label="DataStage export" />
            </label>
          ) : mode === "talend" ? (
            <label className="field">
              <span>Job (.item, a Standard job exported from Talend Studio)</span>
              <textarea className="code-input tall" value={item} spellCheck={false} onChange={(e) => setItem(e.target.value)} aria-label="Talend job" />
            </label>
          ) : mode === "ssis" ? (
            <label className="field">
              <span>Package (.dtsx, SSIS 2012 or later)</span>
              <textarea className="code-input tall" value={dtsx} spellCheck={false} onChange={(e) => setDtsx(e.target.value)} aria-label="SSIS package" />
            </label>
          ) : (
            <>
              <label className="field">
                <span>Export XML (mappings, mapplets, sessions, workflows)</span>
                <textarea className="code-input tall" value={xml} spellCheck={false} onChange={(e) => setXml(e.target.value)} />
              </label>
              <details className="field params" open={!!params}>
                <summary>Parameter file <span className="faint">(optional, sets $$PARAMS)</span></summary>
                <textarea className="code-input" rows={6} value={params} spellCheck={false}
                  placeholder={"[Global]\n$$RUN_DATE=2026-09-01"} onChange={(e) => setParams(e.target.value)}
                  aria-label="Parameter file" />
              </details>
            </>
          )}
          {error && <p className="error">{error}</p>}
        </div>
        <div className="dialog-foot">
          <span className="faint"><Lock />Parsed in this tab. Nothing is uploaded.</span>
          <div className="row">
            <button type="button" className="btn" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn primary" disabled={busy || !text.trim()}>
              <Sparkles />{busy ? "Importing…" : "Import"}
            </button>
          </div>
        </div>
      </form>
    </dialog>
  );
}
