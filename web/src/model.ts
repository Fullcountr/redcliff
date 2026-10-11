export type StepType =
  | "source" | "filter" | "select" | "derive" | "join" | "aggregate" | "union" | "sql" | "target";

export type Step = {
  id: string;
  type: StepType;
  inputs?: string[];
  params?: Record<string, unknown>;
};

export type Spec = {
  version: 0;
  name: string;
  description?: string;
  nodes: Step[];
};

export type LineageEdge = { from: string; to: string; type: string; subtype: string; explicit: boolean };
export type DatasetEdge = { from: string; node: string; subtype: string };
export type Issue = { node: string | null; message: string };

export type Analysis = {
  valid: boolean;
  issues: Issue[];
  columns: Record<string, string[]>;
  edges: LineageEdge[];
  dataset_edges: DatasetEdge[];
  order?: string[];
};

export type Impact = { column: string; values: string[]; rows: string[]; breaks: string[] };

export const STEP_LABELS: Record<StepType, string> = {
  source: "Source",
  filter: "Filter",
  select: "Select",
  derive: "Derive",
  join: "Join",
  aggregate: "Aggregate",
  union: "Union",
  sql: "SQL",
  target: "Target",
};

/** One-line summary of what a step does, for its card. */
export function summarize(step: Step): string {
  const p = step.params ?? {};
  switch (step.type) {
    case "source":
      return String(p.format === "table" ? p.table : p.path ?? "");
    case "target":
      return `write ${String(p.format === "table" ? p.table : p.path ?? "")}`;
    case "filter":
      return String(p.condition ?? "");
    case "join":
      return `${p.how ?? "inner"} on ${String(p.condition ?? "")}`;
    case "aggregate": {
      const by = (p.group_by as string[] | undefined) ?? [];
      return by.length ? `by ${by.join(", ")}` : "totals";
    }
    case "select":
      return `${((p.columns as string[] | undefined) ?? []).length} columns`;
    case "derive":
      return Object.keys((p.columns as object) ?? {}).join(", ");
    case "union":
      return p.distinct ? "distinct rows" : "all rows";
    case "sql":
      return String(p.query ?? "").replace(/\s+/g, " ").slice(0, 60);
  }
}

/** Default params for a new step, using the input's columns where helpful. */
export function defaultParams(type: StepType, inputColumns: string[]): Record<string, unknown> {
  const first = inputColumns[0] ?? "id";
  switch (type) {
    case "filter":
      return { condition: `${first} IS NOT NULL` };
    case "select":
      return { columns: inputColumns.slice(0, 3).length ? inputColumns.slice(0, 3) : [first] };
    case "derive":
      return { columns: { new_column: first } };
    case "aggregate":
      return { group_by: [first], measures: { row_count: "COUNT(*)" } };
    case "join":
      return { how: "inner", condition: "1 = 1" };
    case "union":
      return {};
    case "sql":
      return { query: "SELECT * FROM input" };
    case "target":
      return { format: "csv", path: "output.csv" };
    case "source":
      return { format: "table", table: "orders" };
  }
}

export function refParts(ref: string): [string, string] {
  const i = ref.indexOf(".");
  return [ref.slice(0, i), ref.slice(i + 1)];
}
