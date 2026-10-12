import { FileCode2, FileUp, Lock, Sparkles, Workflow, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

const DIALECTS = [
  ["teradata", "Teradata"], ["oracle", "Oracle"], ["tsql", "SQL Server"], ["snowflake", "Snowflake"],
  ["redshift", "Redshift"], ["bigquery", "BigQuery"], ["postgres", "PostgreSQL"], ["mysql", "MySQL"],
  ["hive", "Hive"], ["spark", "Spark SQL"], ["duckdb", "DuckDB"],
] as const;

export type ImportMode = "sql" | "powercenter";

type Props = {
  mode: ImportMode;
  samples: { sql: string; powercenter: string };
  busy: boolean;
  error: string | null;
  onImport: (mode: ImportMode, text: string, dialect: string) => void;
  onClose: () => void;
};

export function ImportDialog({ mode: initialMode, samples, busy, error, onImport, onClose }: Props) {
  const [mode, setMode] = useState<ImportMode>(initialMode);
  const [sql, setSql] = useState(samples.sql);
  const [xml, setXml] = useState(samples.powercenter);
  const [fileName, setFileName] = useState<string | null>(null);
  const [dialect, setDialect] = useState("teradata");
  const [over, setOver] = useState(false);
  const ref = useRef<HTMLDialogElement>(null);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);

  const text = mode === "sql" ? sql : xml;
  const readFile = async (f: File) => {
    const content = await f.text();
    const isXml = /\.xml$/i.test(f.name) || content.trimStart().startsWith("<");
    setFileName(f.name);
    if (isXml) { setMode("powercenter"); setXml(content); } else { setMode("sql"); setSql(content); }
  };

  return (
    <dialog ref={ref} className="dialog" onClose={onClose} aria-labelledby="import-title">
      <form method="dialog" onSubmit={(e) => { e.preventDefault(); onImport(mode, text, dialect); }}>
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
              <span><b>Informatica PowerCenter</b><span>A mapping or folder XML export, with a migration scorecard</span></span>
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
              <b>{fileName ?? (mode === "sql" ? "Drop a .sql file" : "Drop a PowerCenter .xml export")}</b>
              <p>{fileName ? "Loaded below. Review it, then import." : "Or edit the sample below, an invented job built for this demo."}</p>
            </div>
            <button type="button" className="btn sm" onClick={() => file.current?.click()}>Choose file</button>
            <input ref={file} type="file" accept=".sql,.txt,.bteq,.xml" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) readFile(f); }} />
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
          ) : (
            <label className="field">
              <span>Mapping XML</span>
              <textarea className="code-input tall" value={xml} spellCheck={false} onChange={(e) => setXml(e.target.value)} />
            </label>
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
