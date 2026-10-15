// Excel workbooks, read in the browser with read-excel-file (MIT), loaded only when one is added.
// Each sheet becomes rows of text; DuckDB then types the columns as it does for a CSV.

export type Sheet = { name: string; rows: string[][] };

const cell = (v: unknown): string => {
  if (v === null || v === undefined) return "";
  if (v instanceof Date) return v.toISOString().replace("T00:00:00.000Z", "").replace(/\.000Z$/, "").replace("T", " ");
  return String(v);
};

/** Every sheet that has at least one row. */
export async function readWorkbook(file: File): Promise<Sheet[]> {
  const { default: readXlsxFile } = await import("read-excel-file/browser");
  const sheets = (await readXlsxFile(file)) as unknown as { sheet: string; data: unknown[][] }[];
  return sheets
    .map((s) => ({ name: s.sheet, rows: s.data.map((r) => r.map(cell)) }))
    .filter((s) => s.rows.some((r) => r.some((c) => c !== "")));
}

/** A sheet as CSV text: the first row as the header (or column_1, column_2... when it isn't one). */
export function sheetCsv(sheet: Sheet, header: boolean): string {
  const width = Math.max(...sheet.rows.map((r) => r.length));
  const rows = sheet.rows.filter((r) => r.some((c) => c !== "")).map((r) => [...r, ...Array(width - r.length).fill("")]);
  const names = header
    ? rows[0].map((n, i) => n.trim() || `column_${i + 1}`)
    : Array.from({ length: width }, (_, i) => `column_${i + 1}`);
  const seen = new Map<string, number>();
  const unique = names.map((n) => { const k = (seen.get(n) ?? 0) + 1; seen.set(n, k); return k > 1 ? `${n}_${k}` : n; });
  const quote = (v: string) => (/[",\n\r]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return [unique, ...(header ? rows.slice(1) : rows)].map((r) => r.map(quote).join(",")).join("\n") + "\n";
}
