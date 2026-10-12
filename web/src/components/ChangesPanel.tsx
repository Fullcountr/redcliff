// What the edits since the pipeline was loaded do: the same report the pull request bot posts
// (steps, columns, lineage of target columns, rows before and after) and the spec side by side.
import * as yaml from "js-yaml";
import { CircleAlert, GitCompare } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { data } from "../engine/data";
import { engine } from "../engine/engine";
import type { Spec } from "../model";

type StepChange = { id: string; type: string; change: string; details: string[] };
type LineageChange = { target: string; column: string; before: string[]; after: string[]; change: string };
type Report = {
  summary: string; empty: boolean; issues: string[]; steps: StepChange[];
  columns: { step: string; added: string[]; removed: string[] }[]; lineage: LineageChange[];
  affected_targets: string[]; variables: string[];
  queries: { target: string; base: string; head: string; comparable: boolean }[];
};
type Rows = { target: string; before: number | null; after: number | null; added: number | null; removed: number | null };

const dump = (s: Spec) => yaml.dump(s, { lineWidth: 100, flowLevel: 4 });

/** A line diff (longest common subsequence) laid out side by side. */
export function sideBySide(a: string[], b: string[]): { left?: string; right?: string; kind: "same" | "del" | "add" | "mod" }[] {
  const n = a.length, m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const rows: { left?: string; right?: string; kind: "same" | "del" | "add" | "mod" }[] = [];
  let i = 0, j = 0;
  const dels: string[] = [], adds: string[] = [];
  const flush = () => {
    for (let k = 0; k < Math.max(dels.length, adds.length); k++)
      rows.push({ left: dels[k], right: adds[k], kind: dels[k] !== undefined && adds[k] !== undefined ? "mod" : dels[k] !== undefined ? "del" : "add" });
    dels.length = 0; adds.length = 0;
  };
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) { flush(); rows.push({ left: a[i], right: b[j], kind: "same" }); i++; j++; }
    else if (j < m && (i >= n || lcs[i][j + 1] >= lcs[i + 1][j])) adds.push(b[j++]);
    else dels.push(a[i++]);
  }
  flush();
  return rows;
}

type Line = ReturnType<typeof sideBySide>[number] | { skipped: number };

/** Keeps a few unchanged lines around each change and folds the rest. */
export function hunks(rows: ReturnType<typeof sideBySide>, context = 3): Line[] {
  const keep = rows.map(() => false);
  rows.forEach((r, k) => {
    if (r.kind !== "same") for (let d = -context; d <= context; d++) if (rows[k + d]) keep[k + d] = true;
  });
  const out: Line[] = [];
  let skipped = 0;
  rows.forEach((r, k) => {
    if (keep[k]) { if (skipped) out.push({ skipped }); skipped = 0; out.push(r); } else skipped++;
  });
  if (skipped) out.push({ skipped });
  return out;
}

export function ChangesPanel({ baseline, spec, engineReady, ready, onRebase }: {
  baseline: Spec | null; spec: Spec; engineReady: boolean; ready: boolean; onRebase: () => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rows, setRows] = useState<Rows[]>([]);
  const same = !baseline || JSON.stringify(baseline) === JSON.stringify(spec);

  useEffect(() => {
    if (same || !engineReady || !baseline) return;
    let live = true;
    engine.diff(baseline, spec).then((r) => {
      if (!live) return;
      setReport(r.ok ? (r as unknown as Report) : null);
      setError(r.ok ? null : r.error);
    });
    return () => { live = false; };
  }, [baseline, spec, engineReady, same]);

  useEffect(() => {
    if (!report || !ready) { setRows([]); return; }
    let live = true;
    (async () => {
      const out: Rows[] = [];
      for (const q of report.queries) {
        const count = async (sql: string) => { try { return await data.count(sql); } catch { return null; } };
        const one = async (sql: string) => {
          try { return Number((await data.query(`SELECT COUNT(*) FROM (${sql})`, 1)).rows[0][0]); } catch { return null; }
        };
        out.push({
          target: q.target, before: await count(q.base), after: await count(q.head),
          added: q.comparable ? await one(`(${q.head}) EXCEPT ALL (${q.base})`) : null,
          removed: q.comparable ? await one(`(${q.base}) EXCEPT ALL (${q.head})`) : null,
        });
      }
      if (live) setRows(out);
    })();
    return () => { live = false; };
  }, [report, ready]);

  const lines = useMemo(() => (baseline && !same ? hunks(sideBySide(dump(baseline).split("\n"), dump(spec).split("\n"))) : []), [baseline, spec, same]);

  if (same) {
    return (
      <section>
        <h2>No changes</h2>
        <p className="quiet">Edit the pipeline and this tab shows what the change does, the same report the
          pull request bot posts: steps, columns, the lineage of target columns and rows before and after.</p>
      </section>
    );
  }
  return (
    <>
      <section>
        <div className="row between">
          <h2 data-testid="changes-summary">{report?.summary ?? "Comparing…"}</h2>
          <button className="btn sm ghost" onClick={onRebase} title="Compare later edits with the pipeline as it is now">
            <GitCompare />Compare from here
          </button>
        </div>
        <p className="quiet">Since the pipeline was loaded{report?.affected_targets?.length
          ? <>; targets downstream of a change: {report.affected_targets.map((t, k) => <span key={t}>{k > 0 && ", "}<code>{t}</code></span>)}</> : null}.</p>
        {error && <p className="error">{error}</p>}
        {report?.issues?.length ? (
          <ul className="issue-list">{report.issues.map((i) => <li key={i}><CircleAlert />{i}</li>)}</ul>
        ) : null}
      </section>
      {report && report.steps.length > 0 && (
        <section>
          <h3>Steps</h3>
          <table className="attr-table">
            <tbody>
              {report.steps.map((s) => (
                <tr key={s.id}>
                  <td className="mono">{s.id}</td>
                  <td><strong>{s.change}</strong>{s.details.length > 0 && <div className="quiet why">{s.details.join("; ")}</div>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {report && report.lineage.length > 0 && (
        <section>
          <h3>Lineage of target columns</h3>
          <table className="attr-table">
            <thead><tr><th>Column</th><th>Before</th><th>After</th></tr></thead>
            <tbody>
              {report.lineage.map((l) => (
                <tr key={`${l.target}.${l.column}`}>
                  <td className="mono">{l.target}.{l.column}</td>
                  <td className="mono quiet">{l.before.join(", ") || "new"}</td>
                  <td className="mono">{l.after.join(", ") || "gone"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {rows.length > 0 && (
        <section>
          <h3>Rows on the sample data</h3>
          <table className="attr-table" data-testid="changes-rows">
            <thead><tr><th>Target</th><th>Before</th><th>After</th><th>Added</th><th>Removed</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.target}>
                  <td className="mono">{r.target}</td>
                  {[r.before, r.after, r.added, r.removed].map((v, k) => <td key={k}>{v ?? "–"}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      <section>
        <h3>Spec, before and after</h3>
        <div className="side-by-side" data-testid="changes-diff">
          {lines.map((l, k) => "skipped" in l ? (
            <div key={k} className="sbs-skip">{l.skipped} unchanged line{l.skipped === 1 ? "" : "s"}</div>
          ) : (
            <div key={k} className={`sbs-row ${l.kind}`}>
              <pre className="sbs-cell left">{l.left ?? ""}</pre>
              <pre className="sbs-cell right">{l.right ?? ""}</pre>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}
