// DuckDB-Wasm, loaded on demand: its JavaScript (with Apache Arrow) and its ~36 MB WebAssembly
// stay out of the first page load. The app starts it once the Python engine is up.
import type { QueryResult } from "./duck";

export type { QueryResult };

let mod: Promise<typeof import("./duck")> | null = null;
const load = () => (mod ??= import("./duck"));

export const data = {
  start: async () => (await load()).startDuck(),
  loadSamples: async (base: string, files: string[]) => (await load()).loadSamples(base, files),
  count: async (sql: string) => (await load()).count(sql),
  query: async (sql: string, limit?: number) => (await load()).query(sql, limit),
  addCsv: async (fileName: string, text: string) => (await load()).addCsv(fileName, text),
  schema: async () => Object.fromEntries((await load()).tables.entries()),
  /** Each table's file name and whether it came with the playground. */
  files: async () => Object.fromEntries((await load()).files.entries()),
};
