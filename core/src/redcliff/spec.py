"""Pipeline spec: the single source of truth for a Redcliff pipeline.

A spec is a small document (YAML or JSON) with a name and a list of nodes. Each node has an
``id``, a ``type``, the ids of its ``inputs`` and type-specific ``params``. Expressions inside
params are SQL strings, checked with sqlglot.

Everything here is pure Python so it also runs in the browser through Pyodide.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot.errors import ParseError

SPEC_VERSION = 0
ID_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")

NODE_TYPES = ("source", "filter", "select", "derive", "join", "aggregate", "union", "target")
FORMATS = ("csv", "parquet", "table")
JOIN_TYPES = ("inner", "left", "right", "full")
WRITE_MODES = ("overwrite", "append")

# (minimum inputs, maximum inputs or None for unlimited)
INPUT_COUNTS: dict[str, tuple[int, int | None]] = {
    "source": (0, 0),
    "filter": (1, 1),
    "select": (1, 1),
    "derive": (1, 1),
    "join": (2, 2),
    "aggregate": (1, 1),
    "union": (2, None),
    "target": (1, 1),
}


@dataclass
class Node:
    id: str
    type: str
    inputs: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=data.get("id", ""),
            type=data.get("type", ""),
            inputs=list(data.get("inputs") or []),
            params=dict(data.get("params") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.inputs:
            out["inputs"] = list(self.inputs)
        if self.params:
            out["params"] = dict(self.params)
        return out


@dataclass
class Pipeline:
    name: str
    nodes: list[Node]
    version: int = SPEC_VERSION
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pipeline:
        if not isinstance(data, dict):
            raise SpecError([Issue(None, "spec must be a mapping with 'name' and 'nodes'")])
        nodes_raw = data.get("nodes")
        if not isinstance(nodes_raw, list):
            raise SpecError([Issue(None, "'nodes' must be a list")])
        bad = [i for i, n in enumerate(nodes_raw) if not isinstance(n, dict)]
        if bad:
            raise SpecError([Issue(None, f"node #{i} must be a mapping") for i in bad])
        return cls(
            name=data.get("name", ""),
            nodes=[Node.from_dict(n) for n in nodes_raw],
            version=data.get("version", SPEC_VERSION),
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"version": self.version, "name": self.name}
        if self.description:
            out["description"] = self.description
        out["nodes"] = [n.to_dict() for n in self.nodes]
        return out

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def downstream(self, node_id: str) -> list[str]:
        """Ids of nodes that read directly from ``node_id``."""
        return [n.id for n in self.nodes if node_id in n.inputs]

    def topo_order(self) -> list[Node]:
        """Nodes ordered so every node comes after its inputs. Raises SpecError on a cycle."""
        by_id = {n.id: n for n in self.nodes}
        order: list[Node] = []
        state: dict[str, int] = {}  # 1 = visiting, 2 = done

        def visit(nid: str, path: list[str]) -> None:
            if state.get(nid) == 2:
                return
            if state.get(nid) == 1:
                cycle = " -> ".join(path[path.index(nid):] + [nid])
                raise SpecError([Issue(nid, f"cycle detected: {cycle}")])
            state[nid] = 1
            for inp in by_id[nid].inputs:
                if inp in by_id:
                    visit(inp, path + [nid])
            state[nid] = 2
            order.append(by_id[nid])

        for n in self.nodes:  # keep the author's order where dependencies allow
            visit(n.id, [])
        return order


@dataclass(frozen=True)
class Issue:
    node_id: str | None
    message: str

    def __str__(self) -> str:
        return f"{self.node_id}: {self.message}" if self.node_id else self.message


class SpecError(ValueError):
    """Raised when a spec is invalid. ``issues`` lists every problem found, not just the first."""

    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("invalid pipeline spec:\n" + "\n".join(f"  - {i}" for i in issues))


# --------------------------------------------------------------------------- validation


def _check_expr(issues: list[Issue], nid: str, label: str, sql: Any) -> None:
    if not isinstance(sql, str) or not sql.strip():
        issues.append(Issue(nid, f"{label} must be a non-empty SQL expression"))
        return
    try:
        sqlglot.parse_one(sql)
    except ParseError as e:
        first = str(e).splitlines()[0]
        issues.append(Issue(nid, f"{label} is not valid SQL ({sql!r}): {first}"))


def _check_name(issues: list[Issue], nid: str, label: str, name: Any) -> None:
    if not isinstance(name, str) or not ID_PATTERN.match(name):
        issues.append(Issue(nid, f"{label} {name!r} must match {ID_PATTERN.pattern}"))


def _check_mapping(issues: list[Issue], nid: str, params: dict, key: str) -> None:
    value = params.get(key)
    if not isinstance(value, dict) or not value:
        issues.append(Issue(nid, f"'{key}' must be a non-empty mapping of column name to SQL"))
        return
    for col, sql in value.items():
        _check_name(issues, nid, f"column name in '{key}'", col)
        _check_expr(issues, nid, f"{key}.{col}", sql)


def _check_io(issues: list[Issue], nid: str, params: dict, writing: bool) -> None:
    fmt = params.get("format")
    if fmt not in FORMATS:
        issues.append(Issue(nid, f"'format' must be one of {list(FORMATS)}"))
        return
    key = "table" if fmt == "table" else "path"
    if not isinstance(params.get(key), str) or not params[key].strip():
        issues.append(Issue(nid, f"format '{fmt}' needs a '{key}'"))
    if writing:
        if params.get("mode", "overwrite") not in WRITE_MODES:
            issues.append(Issue(nid, f"'mode' must be one of {list(WRITE_MODES)}"))
    else:
        cols = params.get("columns")
        if cols is not None:
            if not isinstance(cols, dict) or not cols:
                issues.append(Issue(nid, "'columns' must map column names to types"))
            else:
                for col, typ in cols.items():
                    _check_name(issues, nid, "column name", col)
                    if not isinstance(typ, str) or not typ.strip():
                        issues.append(Issue(nid, f"column {col!r} needs a type"))


def _check_params(issues: list[Issue], node: Node) -> None:
    nid, p = node.id, node.params
    t = node.type
    if any(isinstance(k, bool) for k in p):
        issues.append(Issue(nid, "a params key was read as true/false; YAML turns bare `on:`, `yes:` "
                                 "or `no:` into booleans, so use 'condition' or quote the key"))
        return
    if t == "source":
        _check_io(issues, nid, p, writing=False)
    elif t == "target":
        _check_io(issues, nid, p, writing=True)
    elif t == "filter":
        _check_expr(issues, nid, "'condition'", p.get("condition"))
    elif t == "select":
        cols = p.get("columns")
        if not isinstance(cols, list) or not cols:
            issues.append(Issue(nid, "'columns' must be a non-empty list of column names"))
        else:
            for c in cols:
                _check_name(issues, nid, "column", c)
            dups = sorted({c for c in cols if cols.count(c) > 1})
            if dups:
                issues.append(Issue(nid, f"duplicate columns: {dups}"))
    elif t == "derive":
        _check_mapping(issues, nid, p, "columns")
    elif t == "join":
        if p.get("how", "inner") not in JOIN_TYPES:
            issues.append(Issue(nid, f"'how' must be one of {list(JOIN_TYPES)}"))
        # Not "on": YAML 1.1 reads a bare `on:` key as the boolean true.
        _check_expr(issues, nid, "'condition'", p.get("condition"))
    elif t == "aggregate":
        group_by = p.get("group_by", [])
        if not isinstance(group_by, list):
            issues.append(Issue(nid, "'group_by' must be a list of column names"))
        else:
            for c in group_by:
                _check_name(issues, nid, "group_by column", c)
        _check_mapping(issues, nid, p, "measures")
    elif t == "union":
        if not isinstance(p.get("distinct", False), bool):
            issues.append(Issue(nid, "'distinct' must be true or false"))


def validate(pipeline: Pipeline) -> list[Issue]:
    """Return every problem in the spec. An empty list means the spec is valid."""
    issues: list[Issue] = []

    if pipeline.version != SPEC_VERSION:
        issues.append(Issue(None, f"unsupported spec version {pipeline.version!r}; expected {SPEC_VERSION}"))
    if not isinstance(pipeline.name, str) or not pipeline.name.strip():
        issues.append(Issue(None, "'name' is required"))
    if not pipeline.nodes:
        issues.append(Issue(None, "a pipeline needs at least one node"))

    seen: set[str] = set()
    for node in pipeline.nodes:
        if not isinstance(node.id, str) or not ID_PATTERN.match(node.id):
            issues.append(Issue(None, f"node id {node.id!r} must match {ID_PATTERN.pattern}"))
        elif node.id in seen:
            issues.append(Issue(node.id, "duplicate node id"))
        seen.add(node.id)

    ids = {n.id for n in pipeline.nodes}
    for node in pipeline.nodes:
        if node.type not in NODE_TYPES:
            issues.append(Issue(node.id, f"unknown node type {node.type!r}; expected one of {list(NODE_TYPES)}"))
            continue
        lo, hi = INPUT_COUNTS[node.type]
        n_in = len(node.inputs)
        if n_in < lo or (hi is not None and n_in > hi):
            want = f"exactly {lo}" if lo == hi else f"at least {lo}" if hi is None else f"{lo} to {hi}"
            issues.append(Issue(node.id, f"{node.type} needs {want} input(s), got {n_in}"))
        for inp in node.inputs:
            if inp not in ids:
                issues.append(Issue(node.id, f"input {inp!r} does not exist"))
            elif inp == node.id:
                issues.append(Issue(node.id, "a node cannot read from itself"))
            elif pipeline.node(inp).type == "target":
                issues.append(Issue(node.id, f"cannot read from target {inp!r}; read its source instead"))
        if len(set(node.inputs)) != len(node.inputs):
            issues.append(Issue(node.id, "the same input is listed twice"))
        _check_params(issues, node)

    if not issues:
        try:
            pipeline.topo_order()
        except SpecError as e:
            issues.extend(e.issues)
    return issues


# --------------------------------------------------------------------------- loading


def parse_spec(text: str) -> Pipeline:
    """Parse and validate a spec from YAML or JSON text. Raises SpecError if invalid.

    JSON needs nothing extra. YAML needs PyYAML (``pip install redcliff[yaml]``); Pyodide
    ships PyYAML in its standard package set.
    """
    stripped = text.lstrip()
    if stripped.startswith("{"):
        data = json.loads(text)
    else:
        try:
            import yaml  # optional dependency
        except ImportError as e:  # pragma: no cover - exercised only without PyYAML
            raise SpecError([Issue(None, "YAML specs need PyYAML: pip install 'redcliff[yaml]'")]) from e
        data = yaml.safe_load(text)
    pipeline = Pipeline.from_dict(data)
    issues = validate(pipeline)
    if issues:
        raise SpecError(issues)
    return pipeline


def load_spec(path: str | Path) -> Pipeline:
    """Read, parse and validate a spec file (.yaml, .yml or .json)."""
    return parse_spec(Path(path).read_text(encoding="utf-8"))


def json_schema() -> dict[str, Any]:
    """The JSON Schema for spec files, for editors and the web app."""
    return json.loads((Path(__file__).parent / "pipeline.schema.json").read_text(encoding="utf-8"))
