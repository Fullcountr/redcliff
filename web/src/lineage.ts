// Instant, client-side lineage queries for hovering. Pinned blast radius comes from the engine.
import type { Analysis, Impact } from "./model";
import { refParts } from "./model";

export type Highlight = {
  mode: "trace" | "impact";
  focus: string;
  columns: Set<string>; // highlighted column refs, focus included
  rowNodes: Set<string>; // nodes whose rows change (impact only)
  breaks: Set<string>; // nodes a rename would break (impact only)
  nodes: Set<string>; // nodes touched at all
  links: Set<string>; // "from->to" node links carrying the highlight
};

/** Every column feeding ``focus``, back to the sources. */
export function trace(analysis: Analysis, focus: string): Highlight {
  const byTarget = new Map<string, string[]>();
  for (const e of analysis.edges) {
    const list = byTarget.get(e.to) ?? [];
    list.push(e.from);
    byTarget.set(e.to, list);
  }
  const columns = new Set([focus]);
  const links = new Set<string>();
  const stack = [focus];
  while (stack.length) {
    const ref = stack.pop()!;
    for (const src of byTarget.get(ref) ?? []) {
      links.add(`${refParts(src)[0]}->${refParts(ref)[0]}`);
      if (!columns.has(src)) {
        columns.add(src);
        stack.push(src);
      }
    }
  }
  const nodes = new Set([...columns].map((r) => refParts(r)[0]));
  return { mode: "trace", focus, columns, rowNodes: new Set(), breaks: new Set(), nodes, links };
}

/** The engine's blast radius, shaped for the canvas. */
export function fromImpact(analysis: Analysis, impact: Impact): Highlight {
  const columns = new Set([impact.column, ...impact.values]);
  const rowNodes = new Set(impact.rows);
  const links = new Set<string>();
  for (const e of analysis.edges) {
    if (columns.has(e.from) && columns.has(e.to)) links.add(`${refParts(e.from)[0]}->${refParts(e.to)[0]}`);
  }
  const nodes = new Set([...[...columns].map((r) => refParts(r)[0]), ...rowNodes, ...impact.breaks]);
  return { mode: "impact", focus: impact.column, columns, rowNodes, breaks: new Set(impact.breaks), nodes, links };
}
