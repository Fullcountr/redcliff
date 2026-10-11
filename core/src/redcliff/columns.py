"""Work out each node's output columns from the columns declared on its sources.

Knowing columns lets the compiler emit explicit, portable SQL (a derive that replaces a column,
a join that drops duplicate names) and is the base the lineage engine builds on. When a source
does not declare its columns, everything downstream of it is ``None`` (unknown).
"""

from __future__ import annotations

from redcliff.spec import Node, Pipeline

Columns = list[str] | None


def output_columns(node: Node, inputs: list[Columns]) -> Columns:
    """Output columns of one node, given its inputs' columns (in input order)."""
    t, p = node.type, node.params
    if t == "source":
        declared = p.get("columns")
        return list(declared) if declared else None
    if t == "target":
        return []
    if t == "select":
        return list(p["columns"])
    if t == "aggregate":
        return list(p.get("group_by", [])) + list(p["measures"])

    first = inputs[0] if inputs else None
    if t in ("filter", "union"):
        return list(first) if first is not None else None
    if t == "derive":
        if first is None:
            return None
        new = [c for c in p["columns"] if c not in first]
        return list(first) + new
    if t == "join":
        left, right = inputs
        if left is None or right is None:
            return None
        return list(left) + [c for c in right if c not in left]
    raise ValueError(f"unknown node type {t!r}")


def infer_columns(pipeline: Pipeline) -> dict[str, Columns]:
    """Map every node id to its output column names, or None where they can't be known."""
    cols: dict[str, Columns] = {}
    for node in pipeline.topo_order():
        cols[node.id] = output_columns(node, [cols[i] for i in node.inputs])
    return cols
