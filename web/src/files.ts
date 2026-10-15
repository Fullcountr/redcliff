// What a file added with "Add file" is: data (and how to read it), a workbook, a legacy job, or
// something Redcliff doesn't read.
import type { DataFormat } from "./engine/data";
import type { ImportMode } from "./components/ImportDialog";

export const DATA_FORMATS: Record<string, DataFormat> = {
  csv: "csv", tsv: "csv", txt: "csv", tab: "csv", psv: "csv",
  parquet: "parquet", json: "json", jsonl: "json", ndjson: "json",
};
export const LEGACY: Record<string, ImportMode> = {
  sql: "sql", bteq: "sql", xml: "powercenter", dtsx: "ssis", item: "talend", dsx: "datastage",
};
export const WORKBOOKS = ["xlsx", "xlsm"];

/** The file picker's filter: everything Redcliff reads. */
export const ACCEPT = [...Object.keys(DATA_FORMATS), ...WORKBOOKS, ...Object.keys(LEGACY)].map((e) => `.${e}`).join(",");
export const READS = "CSV, TSV, Parquet, JSON or Excel files, or a legacy job (SQL, PowerCenter, SSIS, Talend, DataStage)";

export type Kind =
  | { kind: "data"; format: DataFormat }
  | { kind: "workbook" }
  | { kind: "legacy"; mode: ImportMode }
  | { kind: "unknown"; why: string };

export function kindOf(name: string): Kind {
  const ext = name.toLowerCase().match(/\.([a-z0-9]+)$/)?.[1] ?? "";
  if (ext in DATA_FORMATS) return { kind: "data", format: DATA_FORMATS[ext] };
  if (WORKBOOKS.includes(ext)) return { kind: "workbook" };
  if (ext in LEGACY) return { kind: "legacy", mode: LEGACY[ext] };
  if (ext === "xls") return { kind: "unknown", why: "is an old-style Excel file; save it as .xlsx and add it again" };
  return { kind: "unknown", why: `isn't a file Redcliff reads (it reads ${READS})` };
}
