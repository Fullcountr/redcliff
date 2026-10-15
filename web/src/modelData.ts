// Data models from the tables in the browser: profile them in DuckDB, test the references their
// names suggest, and let the engine propose a model or extend one (core/src/redcliff/extend.py).
import { data } from "./engine/data";
import { engine } from "./engine/engine";
import type { DataModel, ModelEntity, ModelExtension } from "./model";

export type TableInfo = {
  name: string; file: string; sample: boolean; columns: Record<string, string>;
  format: "csv" | "parquet" | "json" | "table"; delimiter?: string; original: string;
};

/** Every table the playground has: yours first, then the samples. */
export async function listTables(): Promise<TableInfo[]> {
  const [schema, files] = await Promise.all([data.schema(), data.files()]);
  return Object.entries(schema)
    .map(([name, columns]) => ({
      name, columns, file: files[name]?.file ?? `${name}.csv`, sample: files[name]?.sample ?? true,
      format: files[name]?.format ?? "csv", delimiter: files[name]?.delimiter, original: files[name]?.original ?? name,
    }))
    .sort((a, b) => Number(a.sample) - Number(b.sample) || a.name.localeCompare(b.name));
}

/** Each file's column names (and a CSV's separator), so a model's load declares them as the CLI's does. */
export async function csvHeaders(): Promise<Record<string, { columns: string[]; delimiter?: string }>> {
  return Object.fromEntries((await listTables())
    .map((t) => [t.file, { columns: Object.keys(t.columns), ...(t.delimiter ? { delimiter: t.delimiter } : {}) }]));
}

/** Two sources name the same file: equal, or the same file name (as extend.py's _same_source). */
export function sameSource(a?: string | null, b?: string | null): boolean {
  if (!a || !b) return false;
  if (a === b) return true;
  const base = (p: string) => p.replace(/\\/g, "/").split("/").pop()!;
  const stem = (p: string) => base(p).replace(/\.[^.]+$/, "");
  const [x, y] = [base(a), base(b)];
  return x === y || (!x.includes(".") && x === stem(b)) || (!y.includes(".") && y === stem(a));
}

const sources = (e: ModelEntity) => [e.source, ...(e.append ?? [])].filter(Boolean) as string[];

/** The tables a model already loads (by its entities' sources). */
export function tablesInModel(model: DataModel, tables: TableInfo[]): Set<string> {
  return new Set(tables.filter((t) => model.entities.some((e) => sources(e).some((s) => sameSource(s, t.file)))).map((t) => t.name));
}

async function profile(names: string[]) {
  const all = await data.schema();
  const files = await data.files();
  const tables = Object.fromEntries(names.filter((n) => n in all).map((n) => [n, all[n]]));
  const q = await engine.modelProfileQueries(tables);
  if (!q.ok) throw new Error(q.error);
  const rows: Record<string, unknown[]> = {};
  for (const [t, sql] of Object.entries(q.queries)) rows[t] = (await data.query(sql, 1)).rows[0].map((v) => Number(v));
  const fileOf = Object.fromEntries(Object.keys(tables).map((t) => [t, files[t]?.file ?? `${t}.csv`]));
  return { tables, rows, files: fileOf };
}

async function heldReferences(p: Awaited<ReturnType<typeof profile>>, only?: string[]): Promise<string[][]> {
  const refs = await engine.modelReferenceQueries(p.tables, p.rows, p.files, only);
  if (!refs.ok) throw new Error(refs.error);
  const held: string[][] = [];
  for (const c of refs.checks) if (Number((await data.query(c.sql, 1)).rows[0][0]) === 0) held.push(c.ref);
  return held;
}

/** A new model from tables, with the reason for each choice. */
export async function proposeFrom(names: string[], style: "kimball" | "data_vault", name: string) {
  const p = await profile(names);
  const r = await engine.modelPropose(p.tables, p.rows, await heldReferences(p), name, p.files, style);
  if (!r.ok) throw new Error(r.error);
  return r;
}

/** What ``fresh`` tables add to a model: profiles them with the tables the model loads, so
 * references are tested both ways. */
export async function extendWith(model: DataModel, fresh: string[]): Promise<ModelExtension> {
  const loaded = [...tablesInModel(model, await listTables())];
  const p = await profile([...new Set([...loaded, ...fresh])]);
  const r = await engine.modelExtend(model, p.tables, p.rows, await heldReferences(p, fresh), fresh, p.files);
  if (!r.ok) throw new Error(r.error);
  return { changes: r.changes, notes: r.notes };
}

/** How many of a column's values a dimension's key has, before linking them by hand; or why
 * they can't be linked (``ok`` false), or why the data can't tell (``ok`` true). */
export type LinkCheck = { total: number; found: number } | { note: string; ok: boolean };
export async function checkLink(model: DataModel, source: string, target: string): Promise<LinkCheck> {
  const files = await data.files();
  const byFile = Object.fromEntries(Object.entries(files).map(([t, f]) => [f.file, t]));
  const r = await engine.modelLinkCheck(model, source, target, byFile);
  if (!r.ok) return { note: r.error, ok: false };
  if (!r.sql) return { note: r.reason ?? "can't check it against the data", ok: true };
  const row = (await data.query(r.sql, 1)).rows[0];
  return { total: Number(row[0]), found: Number(row[1]) };
}
