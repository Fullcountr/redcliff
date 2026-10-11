"""Column-level lineage, blast radius and OpenLineage export.

Redcliff owns the pipeline graph and every expression in it, so lineage is computed, not
guessed from logs. The model follows OpenLineage's vocabulary:

- DIRECT: an output column's value is derived from the input column.
  Subtypes: IDENTITY (copied as is), TRANSFORMATION (computed), AGGREGATION (aggregated).
- INDIRECT: the input column affects the output without being its value.
  Column-level subtypes: CONDITIONAL (a CASE/IF condition), WINDOW (partition or order).
  Dataset-level subtypes, which affect every row of a node: FILTER, JOIN, GROUP_BY.

Lineage needs known columns, so sources should declare ``columns``; nodes whose columns
can't be known are reported as issues and left out of the graph.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from redcliff.columns import infer_columns
from redcliff.spec import Issue, Node, Pipeline

DIRECT, INDIRECT = "DIRECT", "INDIRECT"
_DIRECT_RANK = {"IDENTITY": 0, "TRANSFORMATION": 1, "AGGREGATION": 2}


@dataclass(frozen=True, order=True)
class Ref:
    """A column of a node."""

    node: str
    column: str

    def __str__(self) -> str:
        return f"{self.node}.{self.column}"


@dataclass(frozen=True)
class Edge:
    """``src`` feeds output column ``dst``. ``explicit`` means the node names ``src`` itself."""

    src: Ref
    dst: Ref
    type: str
    subtype: str
    explicit: bool


@dataclass(frozen=True)
class DatasetEdge:
    """``src`` decides which rows of ``node`` exist (a filter, join key or group-by)."""

    src: Ref
    node: str
    subtype: str


@dataclass
class ColumnGraph:
    pipeline: Pipeline
    columns: dict[str, list[str]]
    edges: list[Edge] = field(default_factory=list)
    dataset_edges: list[DatasetEdge] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def inputs_of(self, ref: Ref) -> list[Edge]:
        return [e for e in self.edges if e.dst == ref]

    def outputs_of(self, ref: Ref) -> list[Edge]:
        return [e for e in self.edges if e.src == ref]


# --------------------------------------------------------------------------- expressions


@dataclass(frozen=True)
class ColumnUse:
    column: str
    qualifier: str | None
    type: str
    subtype: str


def analyze_expression(sql: str) -> list[ColumnUse]:
    """Every column an expression reads, with how it is used.

    ``CASE WHEN status = 'x' THEN SUM(amount) END`` reads ``status`` as INDIRECT/CONDITIONAL
    and ``amount`` as DIRECT/AGGREGATION.
    """
    tree = sqlglot.parse_one(sql)
    if isinstance(tree, exp.Column):
        return [ColumnUse(tree.name, tree.table or None, DIRECT, "IDENTITY")]
    uses: list[ColumnUse] = []
    for col in tree.find_all(exp.Column):
        kind, subtype, in_agg = DIRECT, None, False
        node: exp.Expression = col
        while node.parent is not None:
            parent = node.parent
            if isinstance(parent, exp.If) and node.arg_key == "this":
                kind, subtype = INDIRECT, "CONDITIONAL"
                break
            if isinstance(parent, exp.Window) and node.arg_key in ("partition_by", "order"):
                kind, subtype = INDIRECT, "WINDOW"
                break
            if isinstance(parent, exp.AggFunc):
                in_agg = True
            node = parent
        if subtype is None:
            subtype = "AGGREGATION" if in_agg else "TRANSFORMATION"
        uses.append(ColumnUse(col.name, col.table or None, kind, subtype))
    return uses


# --------------------------------------------------------------------------- graph building


def _resolve(use: ColumnUse, node: Node, columns: dict[str, list[str]], issues: list[Issue]) -> list[Ref]:
    """Which input column a reference means. Reports unknown or ambiguous names."""
    candidates = node.inputs
    if use.qualifier:
        if use.qualifier not in node.inputs:
            issues.append(Issue(node.id, f"{use.qualifier}.{use.column}: {use.qualifier!r} is not an input"))
            return []
        candidates = [use.qualifier]
    hits = [Ref(i, use.column) for i in candidates if use.column in columns[i]]
    if not hits:
        avail = sorted({c for i in candidates for c in columns[i]})
        issues.append(Issue(node.id, f"unknown column {use.column!r}; available: {', '.join(avail)}"))
    elif len(hits) > 1:
        issues.append(Issue(node.id, f"column {use.column!r} is ambiguous; qualify it with an input id"))
    return hits


def _name_check(node: Node, name: str, inp: str, columns: dict[str, list[str]], issues: list[Issue]) -> bool:
    if name in columns[inp]:
        return True
    issues.append(Issue(node.id, f"unknown column {name!r}; available: {', '.join(columns[inp])}"))
    return False


def build_graph(pipeline: Pipeline) -> ColumnGraph:
    """The column-level lineage graph for a whole pipeline."""
    inferred = infer_columns(pipeline)
    # A target "has" its input's columns for lineage purposes.
    for node in pipeline.nodes:
        if node.type == "target":
            inferred[node.id] = inferred[node.inputs[0]]
    graph = ColumnGraph(pipeline, {k: v for k, v in inferred.items() if v is not None})
    cols = graph.columns
    issues = graph.issues
    unknown_reported: set[str] = set()

    def edge(src: Ref, dst: Ref, typ: str, sub: str, explicit: bool) -> None:
        graph.edges.append(Edge(src, dst, typ, sub, explicit))

    def expr_edges(node: Node, sql: str, dst: Ref | None) -> None:
        """Add edges for one expression; dst None means dataset-level (the subtype is set by the caller)."""
        for use in analyze_expression(sql):
            for src in _resolve(use, node, cols, issues):
                if dst is not None:
                    edge(src, dst, use.type, use.subtype, True)

    for node in pipeline.topo_order():
        nid, p, t = node.id, node.params, node.type
        if t == "source":
            if nid not in cols and nid not in unknown_reported:
                issues.append(Issue(nid, "declare 'columns' on this source to get lineage through it"))
                unknown_reported.add(nid)
            continue
        if any(i not in cols for i in node.inputs):
            continue  # upstream columns unknown; already reported at the source

        if t in ("filter", "target"):
            for c in cols[node.inputs[0]]:
                edge(Ref(node.inputs[0], c), Ref(nid, c), DIRECT, "IDENTITY", False)
            if t == "filter":
                for use in analyze_expression(p["condition"]):
                    for src in _resolve(use, node, cols, issues):
                        graph.dataset_edges.append(DatasetEdge(src, nid, "FILTER"))

        elif t == "select":
            inp = node.inputs[0]
            for c in p["columns"]:
                if _name_check(node, c, inp, cols, issues):
                    edge(Ref(inp, c), Ref(nid, c), DIRECT, "IDENTITY", True)

        elif t == "derive":
            inp = node.inputs[0]
            for c in cols[inp]:
                if c not in p["columns"]:
                    edge(Ref(inp, c), Ref(nid, c), DIRECT, "IDENTITY", False)
            for c, sql in p["columns"].items():
                expr_edges(node, sql, Ref(nid, c))

        elif t == "join":
            left, right = node.inputs
            for c in cols[left]:
                edge(Ref(left, c), Ref(nid, c), DIRECT, "IDENTITY", False)
            for c in cols[right]:
                if c not in cols[left]:
                    edge(Ref(right, c), Ref(nid, c), DIRECT, "IDENTITY", False)
            for use in analyze_expression(p["condition"]):
                for src in _resolve(use, node, cols, issues):
                    graph.dataset_edges.append(DatasetEdge(src, nid, "JOIN"))

        elif t == "aggregate":
            inp = node.inputs[0]
            for c in p.get("group_by", []):
                if _name_check(node, c, inp, cols, issues):
                    edge(Ref(inp, c), Ref(nid, c), DIRECT, "IDENTITY", True)
                    graph.dataset_edges.append(DatasetEdge(Ref(inp, c), nid, "GROUP_BY"))
            for c, sql in (p.get("measures") or {}).items():
                expr_edges(node, sql, Ref(nid, c))

        elif t == "sql":
            _sql_node_edges(graph, node, issues)

        elif t == "union":
            first = cols[node.inputs[0]]
            for other in node.inputs[1:]:
                if len(cols[other]) != len(first):
                    issues.append(Issue(nid, f"{other!r} has {len(cols[other])} columns but "
                                             f"{node.inputs[0]!r} has {len(first)}"))
            for pos, c in enumerate(first):
                for inp in node.inputs:
                    if pos < len(cols[inp]):
                        edge(Ref(inp, cols[inp][pos]), Ref(nid, c), DIRECT, "IDENTITY", False)
    return graph


def _sql_node_edges(graph: ColumnGraph, node: Node, issues: list[Issue]) -> None:
    """Best-effort lineage for a raw SQL step: its SELECT list, WHERE, JOIN ON and GROUP BY.

    Columns are resolved against the node's inputs, which the query names by node id.
    """
    cols = graph.columns
    if node.id not in cols:
        issues.append(Issue(node.id, "lineage can't see through this SQL step (it selects *)"))
        return
    tree = sqlglot.parse_one(node.params["query"])
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        return
    aliases = {}
    for table in select.find_all(exp.Table):
        if table.name in node.inputs:
            aliases[table.alias_or_name] = table.name

    def refs(expression: exp.Expression) -> list[tuple[Ref, ColumnUse]]:
        out = []
        for use in analyze_expression(expression.sql()):
            qualifier = aliases.get(use.qualifier, use.qualifier) if use.qualifier else None
            fixed = ColumnUse(use.column, qualifier, use.type, use.subtype)
            out += [(r, fixed) for r in _resolve(fixed, node, cols, issues)]
        return out

    projections = select.expressions
    if len(projections) == 1 and isinstance(projections[0], exp.Star):
        for c in cols[node.id]:
            graph.edges.append(Edge(Ref(node.inputs[0], c), Ref(node.id, c), DIRECT, "IDENTITY", False))
    else:
        for name, projection in zip(cols[node.id], projections):
            for src, use in refs(projection.unalias()):
                graph.edges.append(Edge(src, Ref(node.id, name), use.type, use.subtype, True))
    for key, subtype in (("where", "FILTER"), ("having", "FILTER"), ("group", "GROUP_BY")):
        clause = select.args.get(key)
        if clause is not None:
            for src, _ in refs(clause.this if key != "group" else exp.Tuple(expressions=clause.expressions)):
                graph.dataset_edges.append(DatasetEdge(src, node.id, subtype))
    for join in select.args.get("joins") or []:
        if join.args.get("on") is not None:
            for src, _ in refs(join.args["on"]):
                graph.dataset_edges.append(DatasetEdge(src, node.id, "JOIN"))


def column_issues(pipeline: Pipeline) -> list[Issue]:
    """Column-level problems the structural validator can't see (unknown or ambiguous names)."""
    return build_graph(pipeline).issues


# --------------------------------------------------------------------------- queries


def _compose(a: tuple[str, str], b: tuple[str, str]) -> tuple[str, str]:
    """Combine two hops: INDIRECT anywhere wins; otherwise the strongest DIRECT subtype."""
    if a[0] == INDIRECT:
        return a
    if b[0] == INDIRECT:
        return b
    return a if _DIRECT_RANK[a[1]] >= _DIRECT_RANK[b[1]] else b


def trace_to_sources(graph: ColumnGraph, ref: Ref) -> dict[Ref, tuple[str, str]]:
    """Source columns feeding ``ref``, each with the combined (type, subtype) along the way.

    When several paths reach the same source, DIRECT beats INDIRECT and the stronger
    DIRECT subtype wins.
    """
    out: dict[Ref, tuple[str, str]] = {}
    sources = {n.id for n in graph.pipeline.nodes if n.type == "source"}

    def walk(r: Ref, acc: tuple[str, str] | None) -> None:
        if r.node in sources:
            kind = acc or (DIRECT, "IDENTITY")
            prev = out.get(r)
            if prev is None or _better(kind, prev):
                out[r] = kind
            return
        for e in graph.inputs_of(r):
            step = (e.type, e.subtype)
            walk(e.src, step if acc is None else _compose(acc, step))

    walk(ref, None)
    return out


def _better(a: tuple[str, str], b: tuple[str, str]) -> bool:
    if a[0] != b[0]:
        return a[0] == DIRECT
    if a[0] == DIRECT:
        return _DIRECT_RANK[a[1]] > _DIRECT_RANK[b[1]]
    return False


def dataset_dependencies(graph: ColumnGraph, node_id: str) -> dict[Ref, set[str]]:
    """Source columns that decide which rows ``node_id`` has, with the reasons (FILTER, JOIN, GROUP_BY)."""
    upstream = _upstream_nodes(graph.pipeline, node_id)
    out: dict[Ref, set[str]] = defaultdict(set)
    for de in graph.dataset_edges:
        if de.node in upstream:
            for src in trace_to_sources(graph, de.src):
                out[src].add(de.subtype)
    return dict(out)


def _upstream_nodes(pipeline: Pipeline, node_id: str) -> set[str]:
    seen, stack = set(), [node_id]
    while stack:
        nid = stack.pop()
        if nid not in seen:
            seen.add(nid)
            stack.extend(pipeline.node(nid).inputs)
    return seen


def _downstream_nodes(pipeline: Pipeline, node_id: str) -> set[str]:
    seen, stack = set(), [node_id]
    while stack:
        nid = stack.pop()
        if nid not in seen:
            seen.add(nid)
            stack.extend(pipeline.downstream(nid))
    return seen


@dataclass
class Impact:
    """What a change to one column touches."""

    column: Ref
    values: set[Ref]   # columns whose values come from it, directly or through CASE/WINDOW
    rows: set[str]     # nodes whose rows change because it filters, joins or groups them
    breaks: set[str]   # nodes that name it, which a rename or drop would break

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": str(self.column),
            "values": sorted(str(r) for r in self.values),
            "rows": sorted(self.rows),
            "breaks": sorted(self.breaks),
        }


def blast_radius(graph: ColumnGraph, ref: Ref) -> Impact:
    """Everything downstream of ``ref``: changed values, changed rows, and what a rename breaks."""
    values: set[Ref] = set()
    rows: set[str] = set()
    frontier = [ref]
    reached = {ref}
    while frontier:
        r = frontier.pop()
        for de in graph.dataset_edges:
            if de.src == r:
                rows |= _downstream_nodes(graph.pipeline, de.node)
        for e in graph.outputs_of(r):
            if e.dst not in reached:
                reached.add(e.dst)
                values.add(e.dst)
                frontier.append(e.dst)

    # A rename travels along same-named IDENTITY copies; any node naming one of them breaks.
    same_name = {ref}
    frontier = [ref]
    while frontier:
        r = frontier.pop()
        for e in graph.outputs_of(r):
            if e.subtype == "IDENTITY" and e.dst.column == ref.column and e.dst not in same_name:
                same_name.add(e.dst)
                frontier.append(e.dst)
    breaks = {e.dst.node for e in graph.edges if e.src in same_name and e.explicit}
    breaks |= {de.node for de in graph.dataset_edges if de.src in same_name}
    return Impact(ref, values, rows, breaks)


# --------------------------------------------------------------------------- export


def _dataset_name(node: Node) -> str:
    p = node.params
    return p["table"] if p["format"] == "table" else p["path"]


def openlineage_facet(graph: ColumnGraph, node_id: str, namespace: str = "redcliff") -> dict[str, Any]:
    """The OpenLineage ColumnLineageDatasetFacet (1-2-0) for one node, usually a target.

    Inputs are the pipeline's source datasets; ``dataset`` lists columns that affect every row.
    """
    pipeline = graph.pipeline

    def field_entry(src: Ref, typ: str, sub: str) -> dict[str, Any]:
        return {
            "namespace": namespace,
            "name": _dataset_name(pipeline.node(src.node)),
            "field": src.column,
            "transformations": [{"type": typ, "subtype": sub, "description": "", "masking": False}],
        }

    fields = {}
    for col in graph.columns.get(node_id, []):
        traced = trace_to_sources(graph, Ref(node_id, col))
        fields[col] = {"inputFields": [field_entry(s, *k) for s, k in sorted(traced.items())]}
    dataset = []
    for src, reasons in sorted(dataset_dependencies(graph, node_id).items()):
        for sub in sorted(reasons):
            dataset.append(field_entry(src, INDIRECT, sub))
    return {
        "_producer": "https://github.com/Fullcountr/redcliff",
        "_schemaURL": "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json",
        "fields": fields,
        "dataset": dataset,
    }


def lineage_report(pipeline: Pipeline) -> dict[str, Any]:
    """A JSON-ready view of the whole graph, for the web app's lineage panel."""
    g = build_graph(pipeline)
    return {
        "columns": g.columns,
        "edges": [
            {"from": str(e.src), "to": str(e.dst), "type": e.type, "subtype": e.subtype}
            for e in g.edges
        ],
        "dataset_edges": [{"from": str(d.src), "node": d.node, "subtype": d.subtype} for d in g.dataset_edges],
        "issues": [str(i) for i in g.issues],
    }
