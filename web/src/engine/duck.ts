// DuckDB-Wasm: runs previews in the browser. Bundles are served from this site.
import * as duckdb from "@duckdb/duckdb-wasm";
import { jsonToCsv, parquetToCsv } from "./convert";
import mvpWasm from "@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url";
import mvpWorker from "@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?url";
import ehWasm from "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url";
import ehWorker from "@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url";

export type QueryResult = { columns: string[]; rows: unknown[][] };

let db: duckdb.AsyncDuckDB | null = null;
let conn: duckdb.AsyncDuckDBConnection | null = null;
let booting: Promise<void> | null = null;
export const tables = new Map<string, Record<string, string>>(); // table -> {column: type}

export function startDuck(): Promise<void> {
  booting ??= (async () => {
    const bundle = await duckdb.selectBundle({
      mvp: { mainModule: mvpWasm, mainWorker: mvpWorker },
      eh: { mainModule: ehWasm, mainWorker: ehWorker },
    });
    const worker = new Worker(bundle.mainWorker!);
    db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    conn = await db.connect();
  })();
  return booting;
}

// Arrow type ids used below (see apache-arrow's Type enum).
const DECIMAL = 7, DATE = 8, TIMESTAMP = 10;

/** Arrow values as plain JS for display. Decimals stay exact, as text ("330.50"). */
function plain(value: unknown, typeId: number, scale = 0): unknown {
  if (value === null || value === undefined) return null;
  if (typeId === DECIMAL) {
    const raw = String(value);
    const neg = raw.startsWith("-");
    const digits = (neg ? raw.slice(1) : raw).padStart(scale + 1, "0");
    const text = scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits;
    return neg ? `-${text}` : text;
  }
  if (typeId === DATE) return new Date(Number(value)).toISOString().slice(0, 10);
  if (typeId === TIMESTAMP) return new Date(Number(value)).toISOString().slice(0, 19).replace("T", " ");
  if (typeof value === "bigint") return Number(value);
  return value;
}

export async function query(sql: string, limit?: number): Promise<QueryResult> {
  await startDuck();
  const text = limit ? `SELECT * FROM (${sql}) AS preview LIMIT ${limit}` : sql;
  const result = await conn!.query(text);
  const fields = result.schema.fields;
  const columns = fields.map((f) => f.name);
  const kinds = fields.map((f) => {
    const type = f.type as unknown as { typeId: number; scale?: number };
    return [type.typeId, type.scale ?? 0] as const;
  });
  const rows = result.toArray().map((r) =>
    columns.map((c, i) => plain((r as Record<string, unknown>)[c], kinds[i][0], kinds[i][1])),
  );
  return { columns, rows };
}

export async function count(sql: string): Promise<number> {
  const { rows } = await query(`SELECT COUNT(*) AS n FROM (${sql}) AS counted`);
  return Number(rows[0][0]);
}

/** Each table's file, and whether it came with the playground or from the person. */
export const files = new Map<string, {
  file: string; original: string; sample: boolean; format: DataFormat; delimiter?: string;
}>();

/** The table a file name becomes: lower case, letters, digits and underscores. */
export function tableName(fileName: string): string {
  return fileName.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^(\d)/, "_$1");
}

export type DataFormat = "csv" | "parquet" | "json" | "table";
export type Added = {
  table: string; file: string; format: DataFormat; delimiter?: string; columns: Record<string, string>;
  replaced: boolean; renamed: boolean;
};

/** Make a data file available both as a file (for file sources) and as a table (for table sources
 * and models). The same file name again replaces its table (a new version of your file); a file
 * that would take a sample's or another file's table gets a fresh one (orders_2), so nothing is
 * overwritten by accident. ``as`` reads text as a table only (an Excel sheet): it has no file. */
export async function addData(fileName: string, content: string | Uint8Array, format: DataFormat, sample = false,
                              as?: string): Promise<Added> {
  await startDuck();
  const base = tableName(as ?? fileName);
  const original = as ? `${fileName} [${as}]` : fileName;
  const mine = (t: string) => files.get(t)!.original === original && files.get(t)!.sample === sample;
  let table = base;
  for (let i = 2; files.has(table) && !mine(table); i++) table = `${base}_${i}`;
  const replaced = files.has(table);
  const ext = fileName.match(/\.[^.]+$/)?.[0] ?? "";
  const taken = [...files.entries()].some(([t, f]) => t !== table && f.file === fileName);
  // A sample keeps its file name; yours is read as <table>.<ext> when the name is taken. A sheet is
  // read from a hidden CSV into its table, and pipelines read the table.
  const file = format === "table" ? table : replaced ? files.get(table)!.file : taken ? `${table}${ext}` : fileName;
  const registered = format === "table" ? `__sheet_${table}.csv` : file;
  // JSON and Parquet are read in JavaScript and handed over as CSV (see convert.ts); the browser's
  // previews read them that way, the generated code reads the real format.
  const text = format === "json" ? jsonToCsv(content as string)
    : format === "parquet" ? await parquetToCsv((content as Uint8Array).slice().buffer as ArrayBuffer) : content;
  if (typeof text === "string") await db!.registerFileText(registered, text);
  else await db!.registerFileBuffer(registered, text);
  const lit = `'${registered.replace(/'/g, "''")}'`;
  let delimiter: string | undefined;
  if (format === "csv") {
    const sniffed = await query(`SELECT Delimiter FROM sniff_csv(${lit})`).catch(() => null);
    const d = String(sniffed?.rows[0]?.[0] ?? ",");
    if (d && d !== ",") delimiter = d;
  }
  const read = `read_csv(${lit}, header = true${delimiter ? `, delim = '${delimiter.replace(/'/g, "''")}'` : ""})`;
  await conn!.query(`CREATE OR REPLACE TABLE ${table} AS SELECT * FROM ${read}`);
  const described = await query(`DESCRIBE ${table}`);
  const columns: Record<string, string> = {};
  for (const row of described.rows) columns[String(row[0]).toLowerCase()] = String(row[1]);
  tables.set(table, columns);
  files.set(table, { file, original, sample, format, delimiter });
  return { table, file, format, delimiter, columns, replaced, renamed: table !== base };
}

export const addCsv = (fileName: string, text: string, sample = false) => addData(fileName, text, "csv", sample);

export async function loadSamples(base: string, files: string[]): Promise<void> {
  await Promise.all(
    files.map(async (f) => {
      const text = await (await fetch(`${base}samples/${f}`)).text();
      await addCsv(f, text, true);
    }),
  );
}

