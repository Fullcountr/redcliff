import { useEffect, useRef, useState } from "react";

const DIALECTS = [
  ["teradata", "Teradata"], ["oracle", "Oracle"], ["tsql", "SQL Server"], ["snowflake", "Snowflake"],
  ["redshift", "Redshift"], ["bigquery", "BigQuery"], ["postgres", "PostgreSQL"], ["mysql", "MySQL"],
  ["hive", "Hive"], ["spark", "Spark SQL"], ["duckdb", "DuckDB"],
] as const;

type Props = {
  initialSql: string;
  busy: boolean;
  error: string | null;
  onImport: (sql: string, dialect: string) => void;
  onClose: () => void;
};

export function ImportDialog({ initialSql, busy, error, onImport, onClose }: Props) {
  const [sql, setSql] = useState(initialSql);
  const [dialect, setDialect] = useState("teradata");
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);
  return (
    <dialog ref={ref} className="dialog" onClose={onClose} aria-labelledby="import-title">
      <form method="dialog" onSubmit={(e) => { e.preventDefault(); onImport(sql, dialect); }}>
        <h2 id="import-title">Import SQL</h2>
        <p className="quiet">
          Paste a query or a whole script. Each step becomes a node you can inspect and edit.
          Anything without a node type yet is kept as a SQL step, with a note.
        </p>
        <label className="field">
          <span>Dialect</span>
          <select value={dialect} onChange={(e) => setDialect(e.target.value)}>
            {DIALECTS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>
        <label className="field">
          <span>SQL</span>
          <textarea className="code-input tall" value={sql} spellCheck={false} onChange={(e) => setSql(e.target.value)} />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="row end">
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || !sql.trim()}>
            {busy ? "Importing…" : "Import"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
