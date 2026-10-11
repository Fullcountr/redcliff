import dagre from "@dagrejs/dagre";
import type { Spec } from "./model";

export const NODE_WIDTH = 204;
export const MAX_COLUMNS = 8;
const HEADER = 84;
const ROW = 23;

export function nodeHeight(columnCount: number): number {
  const shown = Math.min(columnCount, MAX_COLUMNS);
  const more = columnCount > MAX_COLUMNS ? 1 : 0;
  return HEADER + (shown + more) * ROW + (columnCount ? 10 : 0);
}

/** Left-to-right layout: sources on the left, targets on the right. */
export function layout(spec: Spec, columns: Record<string, string[]>): Record<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 22, ranksep: 46, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of spec.nodes) {
    g.setNode(n.id, { width: NODE_WIDTH, height: nodeHeight(columns[n.id]?.length ?? 0) });
  }
  for (const n of spec.nodes) for (const i of n.inputs ?? []) if (g.hasNode(i)) g.setEdge(i, n.id);
  dagre.layout(g);
  const out: Record<string, { x: number; y: number }> = {};
  for (const n of spec.nodes) {
    const p = g.node(n.id);
    out[n.id] = { x: p.x - p.width / 2, y: p.y - p.height / 2 };
  }
  return out;
}
