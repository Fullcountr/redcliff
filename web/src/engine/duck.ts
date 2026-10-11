// DuckDB-Wasm: runs previews in the browser. Bundles are served from this site.
import * as duckdb from "@duckdb/duckdb-wasm";
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

/** Make a CSV available both as a file (for csv sources) and as a table (for table sources). */
export async function addCsv(fileName: string, text: string): Promise<{ table: string; columns: Record<string, string> }> {
  await startDuck();
  await db!.registerFileText(fileName, text);
  const table = fileName.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^(\d)/, "_$1");
  await conn!.query(`CREATE OR REPLACE TABLE ${table} AS SELECT * FROM read_csv('${fileName.replace(/'/g, "''")}', header = true)`);
  const described = await query(`DESCRIBE ${table}`);
  const columns: Record<string, string> = {};
  for (const row of described.rows) columns[String(row[0]).toLowerCase()] = String(row[1]);
  tables.set(table, columns);
  return { table, columns };
}

export async function loadSamples(base: string, files: string[]): Promise<void> {
  await Promise.all(
    files.map(async (f) => {
      const text = await (await fetch(`${base}samples/${f}`)).text();
      await addCsv(f, text);
    }),
  );
}

