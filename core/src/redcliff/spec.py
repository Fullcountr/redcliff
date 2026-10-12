"""Pipeline spec: the single source of truth for a Redcliff pipeline.

A spec is a small document (YAML or JSON) with a name and a list of nodes. Each node has an
``id``, a ``type``, the ids of its ``inputs`` and type-specific ``params``. Expressions inside
params are SQL strings, checked with sqlglot.

A spec may declare ``variables`` with default values. ``${name}`` anywhere in a node's params is
replaced by the variable's value (as text, like a template) before the spec is checked, analyzed
or compiled; :func:`bind` does that, and takes overrides for a run.

Everything here is pure Python so it also runs in the browser through Pyodide.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

SPEC_VERSION = 0
ID_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
# A table a source reads or a target writes: name, schema.name or catalog.schema.name. The compilers
# paste it into SQL, Python and Jinja, so it must never be able to carry anything but a name.
TABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$")
VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

NODE_TYPES = ("source", "filter", "select", "derive", "join", "aggregate", "union", "sql", "target")
FORMATS = ("csv", "parquet", "table")
JOIN_TYPES = ("inner", "left", "right", "full")
WRITE_MODES = ("overwrite", "append", "merge", "scd1", "scd2", "insert_new", "insert_changed")
KEYED_MODES = ("merge", "scd1", "scd2", "insert_new", "insert_changed")

# (minimum inputs, maximum inputs or None for unlimited)
INPUT_COUNTS: dict[str, tuple[int, int | None]] = {
    "source": (0, 0),
    "filter": (1, 1),
    "select": (1, 1),
    "derive": (1, 1),
    "join": (2, 2),
    "aggregate": (1, 1),
    "union": (2, None),
    "sql": (0, None),
    "target": (1, 1),
}


@dataclass
class Node:
    id: str
    type: str
    inputs: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    expect: Any = None  # data quality checks on this step's rows; see redcliff.expect

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=data.get("id", ""),
            type=data.get("type", ""),
            inputs=list(data.get("inputs") or []),
            params=dict(data.get("params") or {}),
            expect=data.get("expect"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.inputs:
            out["inputs"] = list(self.inputs)
        if self.params:
            out["params"] = dict(self.params)
        if self.expect is not None:
            out["expect"] = self.expect
        return out


@dataclass
class Pipeline:
    name: str
    nodes: list[Node]
    version: int = SPEC_VERSION
    description: str = ""
    variables: dict[str, Any] = field(default_factory=dict)

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
            variables=dict(data.get("variables") or {}) if isinstance(data.get("variables") or {}, dict)
            else data.get("variables"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"version": self.version, "name": self.name}
        if self.description:
            out["description"] = self.description
        if self.variables:
            out["variables"] = dict(self.variables)
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


def file_reads(tree: exp.Expression) -> list[str]:
    """Tables in ``tree`` that would read something other than a pipeline step: table functions
    such as read_text('...') or read_csv('...'), and quoted names such as 'data.csv', which DuckDB
    reads as files. Spec SQL reaches files only through source steps, whose paths are checked."""
    found = []
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            found.append(table.this.sql())
        elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", table.name):
            found.append(table.name)
    return found


def _check_expr(issues: list[Issue], nid: str, label: str, sql: Any) -> None:
    if not isinstance(sql, str) or not sql.strip():
        issues.append(Issue(nid, f"{label} must be a non-empty SQL expression"))
        return
    try:
        tree = sqlglot.parse_one(sql)
    except ParseError as e:
        first = str(e).splitlines()[0]
        issues.append(Issue(nid, f"{label} is not valid SQL ({sql!r}): {first}"))
        return
    for what in file_reads(tree):
        issues.append(Issue(nid, f"{label} reads {what!r}; read files with a source step"))


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
    elif key == "table" and not TABLE_PATTERN.match(params[key]):
        issues.append(Issue(nid, f"'table' {params[key]!r} must be a table name: name, schema.name "
                                 "or catalog.schema.name"))
    elif any(c in params[key] for c in "\r\n\0"):
        issues.append(Issue(nid, f"'{key}' must be a single line"))
    if writing:
        mode = params.get("mode", "overwrite")
        if mode not in WRITE_MODES:
            issues.append(Issue(nid, f"'mode' must be one of {list(WRITE_MODES)}"))
        if mode in KEYED_MODES:
            if fmt != "table":
                issues.append(Issue(nid, f"'mode: {mode}' needs a table target"))
            keys = params.get("keys")
            if not isinstance(keys, list) or not keys:
                issues.append(Issue(nid, f"'mode: {mode}' needs 'keys': the columns that identify a row"))
            else:
                for k in keys:
                    _check_name(issues, nid, "key", k)
        else:
            if "keys" in params:
                issues.append(Issue(nid, "'keys' is only used with a keyed mode (merge, scd1, scd2, insert_new, insert_changed)"))
        if params.get("delete_when") is not None:
            if mode != "merge":
                issues.append(Issue(nid, "'delete_when' is only used with 'mode: merge'"))
            else:
                _check_expr(issues, nid, "'delete_when'", params["delete_when"])
        _check_history(issues, nid, params, mode)
        for hook in ("pre_sql", "post_sql"):
            statements = params.get(hook)
            if statements is None:
                continue
            if not isinstance(statements, list) or not all(isinstance(s, str) and s.strip() for s in statements):
                issues.append(Issue(nid, f"'{hook}' must be a list of SQL statements"))
                continue
            for sql in statements:
                try:
                    sqlglot.parse_one(sql)
                except ParseError as e:
                    issues.append(Issue(nid, f"'{hook}' is not valid SQL ({sql!r}): {str(e).splitlines()[0]}"))
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


HISTORY_PARAMS = {
    "surrogate_key": ("scd1", "scd2"), "unknown_member": ("scd1", "scd2"), "scd1": ("scd2",), "ignore": ("scd2",),
    "as_of": ("scd2",), "compare": ("insert_changed",), "order_by": ("insert_changed",),
}


def _check_history(issues: list[Issue], nid: str, params: dict, mode: str) -> None:
    """Parameters of the history modes: scd1, scd2, insert_new and insert_changed."""
    for key, modes in HISTORY_PARAMS.items():
        if key in params and mode not in modes:
            issues.append(Issue(nid, f"'{key}' is only used with 'mode: {' or '.join(modes)}'"))
    for key in ("surrogate_key", "compare", "order_by"):
        if key in params:
            _check_name(issues, nid, f"'{key}'", params[key])
    for key in ("scd1", "ignore"):
        if key in params:
            names = params[key]
            if not isinstance(names, list):
                issues.append(Issue(nid, f"'{key}' must be a list of column names"))
            else:
                for n in names:
                    _check_name(issues, nid, f"column in '{key}'", n)
    if "unknown_member" in params and not isinstance(params["unknown_member"], bool):
        issues.append(Issue(nid, "'unknown_member' is true or false"))
    if params.get("as_of") is not None:
        _check_expr(issues, nid, "'as_of'", params["as_of"])
    if mode == "insert_changed" and not ("compare" in params and "order_by" in params):
        issues.append(Issue(nid, "'mode: insert_changed' needs 'compare' (e.g. a hashdiff) and 'order_by' (e.g. a load time)"))
    if params.get("unknown_member") and not params.get("surrogate_key"):
        issues.append(Issue(nid, "'unknown_member' needs a 'surrogate_key' (it gets -1)"))


def _check_sql_node(issues: list[Issue], node: Node) -> None:
    """A raw SQL step: one SELECT that may read only its inputs, by node id."""
    query = node.params.get("query")
    if not isinstance(query, str) or not query.strip():
        issues.append(Issue(node.id, "'query' must be a non-empty SELECT"))
        return
    try:
        tree = sqlglot.parse_one(query)
    except ParseError as e:
        issues.append(Issue(node.id, f"'query' is not valid SQL: {str(e).splitlines()[0]}"))
        return
    if not isinstance(tree, exp.Query):
        issues.append(Issue(node.id, "'query' must be a SELECT (or a UNION of SELECTs)"))
        return
    for what in file_reads(tree):
        issues.append(Issue(node.id, f"'query' reads {what!r}; a SQL step may read only its inputs"))
    local = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.name and table.name not in local and table.name not in node.inputs:
            issues.append(Issue(node.id, f"'query' reads {table.name!r}, which is not one of its inputs"))


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
        # Measures may be empty when grouping alone: that is SELECT DISTINCT.
        if p.get("measures") or not group_by:
            _check_mapping(issues, nid, p, "measures")
    elif t == "sql":
        _check_sql_node(issues, node)
    elif t == "union" and not isinstance(p.get("distinct", False), bool):
        issues.append(Issue(nid, "'distinct' must be true or false"))


def _var_text(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _variable_issues(pipeline: Pipeline) -> list[Issue]:
    v = pipeline.variables
    if not isinstance(v, dict):
        return [Issue(None, "'variables' must map names to values")]
    issues = []
    for name, value in v.items():
        if not isinstance(name, str) or not ID_PATTERN.match(name):
            issues.append(Issue(None, f"variable name {name!r} must match {ID_PATTERN.pattern}"))
        if not isinstance(value, (str, int, float, bool)):
            issues.append(Issue(None, f"variable {name!r} needs a text, number or true/false value"))
    return issues


def variables_used(pipeline: Pipeline) -> dict[str, list[str]]:
    """Each ``${name}`` in the nodes' params -> the node ids using it, in spec order."""
    used: dict[str, list[str]] = {}

    def walk(value: Any, nid: str) -> None:
        if isinstance(value, str):
            for name in VAR_PATTERN.findall(value):
                if nid not in used.setdefault(name, []):
                    used[name].append(nid)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v, nid)
        elif isinstance(value, list):
            for v in value:
                walk(v, nid)

    for n in pipeline.nodes:
        walk(n.params, n.id)
        walk(n.expect, n.id)
    return used


def substitute_variables(value: Any, values: dict[str, str]) -> Any:
    """``value`` (text, or lists and mappings of it) with every ``${name}`` replaced."""
    if isinstance(value, str):
        return VAR_PATTERN.sub(lambda m: values.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: substitute_variables(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_variables(v, values) for v in value]
    return value


def bind(pipeline: Pipeline, overrides: dict[str, Any] | None = None) -> Pipeline:
    """The pipeline with every ``${name}`` replaced by its value; ``overrides`` win over defaults.

    The result declares no variables, so binding it again changes nothing. Raises SpecError when
    a variable is used but has no value.
    """
    if not pipeline.variables and not overrides and not variables_used(pipeline):
        return pipeline
    issues = _variable_issues(pipeline)
    if issues:
        raise SpecError(issues)
    values = {k: _var_text(v) for k, v in {**pipeline.variables, **(overrides or {})}.items()}
    missing = {name: nids for name, nids in variables_used(pipeline).items() if name not in values}
    if missing:
        raise SpecError([Issue(nids[0], f"uses ${{{name}}}, which has no value; declare it under 'variables'")
                         for name, nids in missing.items()])
    nodes = [Node(n.id, n.type, list(n.inputs), substitute_variables(n.params, values),
                  substitute_variables(n.expect, values)) for n in pipeline.nodes]
    return Pipeline(pipeline.name, nodes, pipeline.version, pipeline.description)


def validate(pipeline: Pipeline) -> list[Issue]:
    """Return every problem in the spec. An empty list means the spec is valid."""
    try:
        pipeline = bind(pipeline)
    except SpecError as e:
        return list(e.issues)
    issues: list[Issue] = []

    if pipeline.version != SPEC_VERSION:
        issues.append(Issue(None, f"unsupported spec version {pipeline.version!r}; expected {SPEC_VERSION}"))
    if not isinstance(pipeline.name, str) or not pipeline.name.strip():
        issues.append(Issue(None, "'name' is required"))
    elif any(ord(c) < 32 for c in pipeline.name):
        issues.append(Issue(None, "'name' must be a single line of text"))
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
        if node.expect is not None:
            from redcliff.expect import parse_expectations

            issues += [Issue(node.id, m) for m in parse_expectations(node.id, node.expect)[1]]

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


def dump_spec(spec: Pipeline | dict[str, Any], fmt: str = "yaml") -> str:
    """Spec text in the house style: YAML with short lists inline (JSON without PyYAML)."""
    data = spec.to_dict() if isinstance(spec, Pipeline) else spec
    if fmt == "yaml":
        try:
            import yaml
        except ImportError:
            pass
        else:
            class _Dumper(yaml.SafeDumper):
                pass

            def short_lists_inline(dumper, items):  # inputs: [orders], not one line per item
                flow = all(isinstance(i, (str, int, float, bool)) for i in items)
                return dumper.represent_sequence("tag:yaml.org,2002:seq", items, flow_style=flow)

            _Dumper.add_representer(list, short_lists_inline)
            return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    return json.dumps(data, indent=2) + "\n"


def edit_spec_text(text: str, after: dict[str, Any]) -> str:
    """``text`` changed to say ``after``, keeping the layout of everything that didn't change.

    Unchanged steps keep their exact text (comments, flow style, blank lines); changed and new
    steps are written in the same indentation. Falls back to a full rewrite when the file's
    layout isn't one step per block.
    """
    fmt = "json" if text.lstrip().startswith("{") else "yaml"
    if fmt == "json" or not text.strip():
        return dump_spec(after, fmt)
    import re

    import yaml

    lines = text.splitlines(keepends=True)
    try:
        start = next(i for i, line in enumerate(lines) if re.match(r"nodes:\s*(#.*)?$", line))
        before = yaml.safe_load(text)
        first = next(i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].lstrip().startswith("#"))
        indent = re.match(r"(\s*)- ", lines[first]).group(1)
    except (StopIteration, AttributeError, yaml.YAMLError):
        return dump_spec(after)
    blocks: list[list[str]] = []
    for line in lines[first:]:
        if line.startswith(f"{indent}- "):
            blocks.append([line])
        elif line.strip() == "" or line.startswith(indent + "  ") or line.lstrip().startswith("#"):
            if not blocks:
                return dump_spec(after)
            blocks[-1].append(line)
        else:
            return dump_spec(after)  # something after the nodes: rewrite
    old_nodes = before.get("nodes") or []
    if len(blocks) != len(old_nodes):
        return dump_spec(after)
    for block, node in zip(blocks, old_nodes):
        if yaml.safe_load("".join(block)) != [node]:
            return dump_spec(after)
    text_of = {n.get("id"): "".join(b) for n, b in zip(old_nodes, blocks)}
    old_of = {n.get("id"): n for n in old_nodes}
    spaced = len(blocks) > 1 and blocks[0][-1].strip() == ""

    header = {k: v for k, v in after.items() if k != "nodes"}
    old_header = {k: v for k, v in before.items() if k != "nodes"}
    head = "".join(lines[:start + 1]) if header == old_header else \
        dump_spec({**header, "nodes": []}).replace("nodes: []", "nodes:")
    out = [head + "".join(lines[start + 1:first])]
    nodes = after.get("nodes") or []
    for i, node in enumerate(nodes):
        if old_of.get(node.get("id")) == node:
            block = text_of[node["id"]]
        else:
            body = dump_spec(node).splitlines()
            block = "".join([f"{indent}- {body[0]}\n"] + [f"{indent}  {line}\n" for line in body[1:]])
        block = block.rstrip("\n") + "\n"
        if spaced and i < len(nodes) - 1:
            block += "\n"
        out.append(block)
    return "".join(out)


def load_spec(path: str | Path) -> Pipeline:
    """Read, parse and validate a spec file (.yaml, .yml or .json)."""
    return parse_spec(Path(path).read_text(encoding="utf-8"))


def json_schema() -> dict[str, Any]:
    """The JSON Schema for spec files, for editors and the web app."""
    return json.loads((Path(__file__).parent / "pipeline.schema.json").read_text(encoding="utf-8"))
