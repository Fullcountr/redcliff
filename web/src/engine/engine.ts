// Promise-based client for the Pyodide engine worker.
import type {
  Analysis, DataModel, Impact, ImportResult, JobMap, ModelAnalysis, ModelChange, ModelExport, ModelExtension, Spec,
} from "../model";

/** Tables by name, each a {column: type} map, as DuckDB describes them. */
type Tables = Record<string, Record<string, string>>;

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

/** Where engine calls go instead of the Pyodide worker: the VS Code extension's Python bridge. */
type Transport = (fn: string, args: unknown[]) => Promise<unknown>;
let transport: Transport | null = null;

export function useTransport(t: Transport): void {
  transport = t;
  readyResolve(0);
}

export function startEngine(): void {
  if (worker || transport) return;
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
  if (transport) return transport(fn, args) as Promise<Result<T>>;
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
  importTalend: (xml: string, name: string) => call<ImportResult>("import_talend", xml, name),
  importDatastage: (text: string) => call<ImportResult>("import_datastage", text),
  importPowercenter: (xml: string, params?: string) => call<ImportResult>("import_powercenter", xml, null, params || null),
  impact: (spec: Spec, node: string, column: string) => call<Impact>("impact", json(spec), node, column),
  diff: (base: Spec, head: Spec) => call<Record<string, unknown>>("diff", json(base), json(head)),
  modelAnalyze: (model: DataModel) => call<ModelAnalysis>("model_analyze", JSON.stringify(model)),
  modelExport: (model: DataModel, fmt: ModelExport, dialect?: string) =>
    call<{ code: string }>("model_export", JSON.stringify(model), fmt, dialect ?? null),
  modelProfileQueries: (tables: Record<string, Record<string, string>>) =>
    call<{ queries: Record<string, string> }>("model_profile_queries", JSON.stringify(tables)),
  modelReferenceQueries: (tables: Tables, rows: Record<string, unknown[]>, files: Record<string, string>, only?: string[]) =>
    call<{ checks: { ref: [string, string, string, string]; sql: string }[] }>(
      "model_reference_queries", JSON.stringify(tables), JSON.stringify(rows), JSON.stringify(files), JSON.stringify(only ?? null)),
  modelPropose: (tables: Tables, rows: Record<string, unknown[]>, held: string[][], name: string,
                 files: Record<string, string>, style: "kimball" | "data_vault") =>
    call<{ model: DataModel; reasons: Record<string, string[]> }>(
      "model_propose", JSON.stringify(tables), JSON.stringify(rows), JSON.stringify(held), name, JSON.stringify(files), style),
  modelExtend: (model: DataModel, tables: Tables, rows: Record<string, unknown[]>, held: string[][], fresh: string[],
                files: Record<string, string>) =>
    call<ModelExtension>("model_extend", JSON.stringify(model), JSON.stringify(tables), JSON.stringify(rows),
      JSON.stringify(held), JSON.stringify(fresh), JSON.stringify(files)),
  modelApply: (model: DataModel, changes: ModelChange[], accepted: string[]) =>
    call<{ model: DataModel }>("model_apply", JSON.stringify(model), JSON.stringify(changes), JSON.stringify(accepted)),
  modelLink: (model: DataModel, source: string, target: string) =>
    call<{ model: DataModel; done: string[] }>("model_link", JSON.stringify(model), source, target),
  modelLinkCheck: (model: DataModel, source: string, target: string, tables: Record<string, string>) =>
    call<{ sql: string | null; reason?: string }>("model_link_check", JSON.stringify(model), source, target, JSON.stringify(tables)),
  modelFromJob: (spec: Spec, style: "kimball" | "data_vault") =>
    call<{ model: DataModel; reasons: Record<string, string[]>; map: JobMap }>("model_from_job", JSON.stringify(spec), style),
  modelMap: (model: DataModel, spec: Spec) => call<JobMap>("model_map", JSON.stringify(model), JSON.stringify(spec)),
  modelLoadPipeline: (model: DataModel, headers?: Record<string, { columns: string[]; delimiter?: string }>) =>
    call<{ spec: Spec; targets: string[] }>("model_load_pipeline", JSON.stringify(model), JSON.stringify(headers ?? {})),
  modelImport: (text: string, fmt: "ddl" | "dbml", dialect: string | null, name: string) =>
    call<{ model: DataModel; warnings: string[] }>("model_import", text, fmt, dialect, name),
};
