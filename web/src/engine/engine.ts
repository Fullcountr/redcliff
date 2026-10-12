// Promise-based client for the Pyodide engine worker.
import type { Analysis, DataModel, Impact, ImportResult, ModelAnalysis, ModelExport, Spec } from "../model";

type Result<T> = ({ ok: true } & T) | { ok: false; error: string; issues?: string[] };

let worker: Worker | null = null;
let nextId = 1;
const pending = new Map<number, (value: unknown) => void>();
let readyResolve: (ms: number) => void;
let readyReject: (err: Error) => void;
export const engineReady = new Promise<number>((resolve, reject) => {
  readyResolve = resolve;
  readyReject = reject;
});

export function startEngine(): void {
  if (worker) return;
  worker = new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
  worker.onmessage = (e: MessageEvent) => {
    const msg = e.data;
    if (msg.type === "ready") return readyResolve(msg.ms);
    if (msg.type === "failed") return readyReject(new Error(msg.error));
    pending.get(msg.id)?.(msg.result);
    pending.delete(msg.id);
  };
}

function call<T>(fn: string, ...args: unknown[]): Promise<Result<T>> {
  startEngine();
  const id = nextId++;
  return new Promise((resolve) => {
    pending.set(id, resolve as (v: unknown) => void);
    worker!.postMessage({ id, fn, args });
  });
}

const json = (spec: Spec) => JSON.stringify(spec);

export const engine = {
  analyze: (spec: Spec) => call<Analysis>("analyze", json(spec)),
  previewQuery: (spec: Spec, node: string) => call<{ sql: string; node: string }>("preview_query", json(spec), node),
  code: (spec: Spec, target: string, node?: string | null) =>
    call<{ code: string; files?: Record<string, string> }>("code", json(spec), target, node ?? null),
  checks: (spec: Spec, node?: string | null) =>
    call<{ checks: { node: string; name: string; kind: string; severity: "error" | "warn"; describe: string; sql: string }[] }>(
      "checks", json(spec), node ?? null),
  traceQuery: (spec: Spec, node: string) =>
    call<{ sql: string; node: string; columns: string[] | null; provenance: Record<string, string>; opaque: string[] }>(
      "trace_query", json(spec), node),
  sourceRows: (spec: Spec, source: string, rows: number[]) =>
    call<{ sql: string }>("source_rows", json(spec), source, JSON.stringify(rows)),
  importSql: (sql: string, dialect: string, schema: Record<string, Record<string, string>>, name: string) =>
    call<ImportResult>("import_sql", sql, dialect, JSON.stringify(schema), name),
  importSsis: (xml: string) => call<ImportResult>("import_ssis", xml),
  importPowercenter: (xml: string, params?: string) => call<ImportResult>("import_powercenter", xml, null, params || null),
  impact: (spec: Spec, node: string, column: string) => call<Impact>("impact", json(spec), node, column),
  diff: (base: Spec, head: Spec) => call<Record<string, unknown>>("diff", json(base), json(head)),
  modelAnalyze: (model: DataModel) => call<ModelAnalysis>("model_analyze", JSON.stringify(model)),
  modelExport: (model: DataModel, fmt: ModelExport, dialect?: string) =>
    call<{ code: string }>("model_export", JSON.stringify(model), fmt, dialect ?? null),
  modelProfileQueries: (tables: Record<string, Record<string, string>>) =>
    call<{ queries: Record<string, string> }>("model_profile_queries", JSON.stringify(tables)),
  modelReferenceQueries: (tables: Record<string, Record<string, string>>, rows: Record<string, unknown[]>) =>
    call<{ checks: { ref: [string, string, string, string]; sql: string }[] }>(
      "model_reference_queries", JSON.stringify(tables), JSON.stringify(rows)),
  modelPropose: (tables: Record<string, Record<string, string>>, rows: Record<string, unknown[]>, held: string[][], name: string) =>
    call<{ model: DataModel; reasons: Record<string, string[]> }>(
      "model_propose", JSON.stringify(tables), JSON.stringify(rows), JSON.stringify(held), name),
  modelLoadPipeline: (model: DataModel) => call<{ spec: Spec; targets: string[] }>("model_load_pipeline", JSON.stringify(model)),
  modelImport: (text: string, fmt: "ddl" | "dbml", dialect: string | null, name: string) =>
    call<{ model: DataModel; warnings: string[] }>("model_import", text, fmt, dialect, name),
};
