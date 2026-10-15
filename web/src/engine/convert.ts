// JSON and Parquet files as CSV text for DuckDB-Wasm, read in JavaScript: DuckDB-Wasm would fetch
// its JSON and Parquet readers from extensions.duckdb.org, which work networks often block.
// Nested values (objects, lists) become JSON text. The pipeline still reads the original format
// (read_json / read_parquet in DuckDB, spark.read.json / .parquet in Spark).

function csvOf(columns: string[], rows: Record<string, unknown>[]): string {
  const text = (v: unknown): string => {
    if (v === null || v === undefined) return "";
    if (v instanceof Date) return v.toISOString().replace("T00:00:00.000Z", "").replace(/\.000Z$/, "").replace("T", " ").replace(/Z$/, "");
    if (typeof v === "bigint") return v.toString();
    if (typeof v === "object") return JSON.stringify(v, (_k, x) => (typeof x === "bigint" ? x.toString() : x));
    return String(v);
  };
  const quote = (s: string) => (/[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  return [columns.map(quote).join(","), ...rows.map((r) => columns.map((c) => quote(text(r[c]))).join(","))].join("\n") + "\n";
}

const keysOf = (rows: Record<string, unknown>[]) => {
  const seen = new Set<string>();
  for (const r of rows) for (const k of Object.keys(r)) seen.add(k);
  return [...seen];
};

/** An array of objects, or one object per line (JSON Lines). */
export function jsonToCsv(text: string): string {
  const trimmed = text.trim();
  let rows: unknown[];
  if (trimmed.startsWith("[")) rows = JSON.parse(trimmed);
  else rows = trimmed.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.parse(l));
  if (!rows.length) throw new Error("it has no records");
  if (!rows.every((r) => r && typeof r === "object" && !Array.isArray(r))) throw new Error("its records aren't objects");
  const objects = rows as Record<string, unknown>[];
  return csvOf(keysOf(objects), objects);
}

export async function parquetToCsv(buffer: ArrayBuffer): Promise<string> {
  const [{ parquetMetadata, parquetReadObjects, parquetSchema }, { compressors }] =
    await Promise.all([import("hyparquet"), import("hyparquet-compressors")]);
  const metadata = parquetMetadata(buffer);
  const columns = parquetSchema(metadata).children.map((c) => c.element.name);
  const rows = await parquetReadObjects({ file: buffer, metadata, compressors });
  return csvOf(columns, rows);
}
