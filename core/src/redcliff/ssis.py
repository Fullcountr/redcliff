"""Import SQL Server Integration Services packages (.dtsx) into a Redcliff pipeline.

A package's control flow runs tasks in an order set by precedence constraints; its Data Flow
tasks hold components wired by paths. Columns travel by lineage id: synchronous components
(Derived Column, Conditional Split, Lookup, ...) keep the ids of the columns they pass on, and
asynchronous ones (Sort, Merge Join, Union All, Aggregate) mint new ones. This importer walks
the components in dependency order and turns each into pipeline steps:

=====================  ==========================================================
SSIS                   Redcliff
=====================  ==========================================================
OLE DB / ADO.NET       ``source`` for a table, or the SQL command through the
source                 SQL importer (T-SQL)
Flat File source       ``source`` (CSV)
Derived Column         ``derive``; replacing a column keeps its name
Data Conversion        ``derive`` with CAST
Conditional Split      one ``filter`` per output in use; a row takes the first
                       output whose condition is true, as in SSIS
Multicast, Row Count   nothing: rows pass through
Sort                   nothing (keys are remembered for Merge Join), or a
                       distinct ``aggregate`` when it removes duplicates
Merge Join             ``join`` on the sort keys (inner, left, full)
Lookup                 ``source`` + ``join``, one reference row per key
Union All              ``select`` per input + ``union``
Aggregate              ``aggregate`` (group by, sum, avg, min, max, counts)
OLE DB destination     ``select`` + ``target`` (overwrite when a TRUNCATE TABLE
                       task runs first)
Flat File destination  ``target`` (CSV)
anything else          a placeholder step, flagged for a person to rewrite
=====================  ==========================================================

Expressions go through :mod:`redcliff.ssis_expr`; package variables become pipeline variables.
Execute SQL tasks, loops and other control-flow tasks are graded as orchestration.
Only the standard library's XML parser is used, so this runs in the browser too.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from redcliff.importer import ImportResult, SQLImportError, _Builder
from redcliff.scorecard import CONVERTED, MANUAL, REVIEW, ScoreItem, Scorecard, lineage_coverage
from redcliff.spec import ID_PATTERN, Pipeline, SpecError, validate
from redcliff.ssis_expr import SSISExpressionError, ssis_type, translate_ssis, variable_name

DTS = "www.microsoft.com/SqlServer/Dts"
SQLTASK = "www.microsoft.com/sqlserver/dts/tasks/sqltask"


class SSISImportError(SQLImportError):
    """The XML is not an SSIS package, or has nothing to import."""


class _Manual(Exception):
    """This component can't be converted faithfully; a placeholder stands in."""


def _d(name: str) -> str:
    return f"{{{DTS}}}{name}"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "x"
    s = re.sub(r"_+", "_", s)
    return s if re.match(r"[a-z_]", s[0]) else "_" + s


def _props(el: ET.Element) -> dict[str, str]:
    holder = el.find("properties")
    if holder is None:
        return {}
    return {(p.get("name") or ""): (p.text or "") for p in holder.findall("property")}


def _ref(value: str) -> str:
    """#{Package\\DFT\\Comp.Outputs[X].Columns[y]} -> the reference inside."""
    m = re.fullmatch(r"\s*#\{(.*)\}\s*", value or "")
    return m.group(1) if m else (value or "").strip()


def _column_name(ref: str) -> str:
    m = re.search(r"\.(?:Columns|ExternalColumns)\[([^\]]*)\]$", ref)
    return m.group(1) if m else ref


def _table_name(text: str) -> str:
    """[dbo].[Customers] -> customers (the schema is dropped, as in the SQL importer)."""
    parts = [p.strip("[]\" ") for p in re.split(r"\.(?![^\[]*\])", text.strip()) if p.strip()]
    return _slug(parts[-1]) if parts else "table"


_DTYPE_KIND = {"2": "int", "3": "int", "4": "number", "5": "number", "6": "number", "7": "date", "8": "string",
               "11": "bool", "14": "number", "16": "int", "17": "int", "18": "int", "19": "int", "20": "int",
               "21": "int"}


def _column_type(el: ET.Element) -> tuple[str, str | None]:
    """(SQL type, kind) of an SSIS column element from its dataType attributes."""
    t = (el.get("dataType") or "wstr").lower()
    args = {"numeric": [el.get("precision") or "18", el.get("scale") or "0"],
            "decimal": [el.get("scale") or "0"]}.get(t, [])
    try:
        sql_type, kind = ssis_type(t, args)
    except SSISExpressionError:
        return "TEXT", "string"
    return sql_type.lower().replace(" ", ""), kind


# --------------------------------------------------------------------------- streams


@dataclass
class _Stream:
    """The rows on one output: the node holding them and the column for each lineage id."""
    node: str
    cols: dict[str, str]                       # lineage id -> column name in node
    kinds: dict[str, str | None] = field(default_factory=dict)
    sort_keys: list[str] = field(default_factory=list)

    def named(self) -> dict[str, tuple[str, str | None]]:
        """SSIS column name -> (column, kind); later columns win, as a replaced column does."""
        out: dict[str, tuple[str, str | None]] = {}
        for lineage, col in self.cols.items():
            out[_column_name(lineage)] = (col, self.kinds.get(lineage))
            out[_column_name(lineage).lower()] = (col, self.kinds.get(lineage))
        return out


# --------------------------------------------------------------------------- the importer


class _Importer:
    def __init__(self, package: str, variables: dict[str, tuple[str, str | None]]):
        self.package = package
        self.b = _Builder("tsql", None)
        self.items: list[ScoreItem] = []
        self.functions: dict[str, int] = {}
        self.vars = variables                  # User::Name -> (value, kind)
        self.variables: dict[str, str] = {}    # pipeline variables used -> default value
        self.written: dict[str, str] = {}      # table -> node holding the rows written to it
        self.truncated: dict[str, ScoreItem] = {}  # table -> the Execute SQL task truncating it
        self.outputs: list[str] = []
        self.item: ScoreItem | None = None

    def note(self, status: str, message: str) -> None:
        if self.item is not None:
            self.item.flag(status, message)

    def add(self, type_: str, inputs: list[str], params: dict[str, Any], base: str) -> str:
        return self.b.add(type_, inputs, params, base)

    def fresh(self, wanted: str, node: str, taken: set[str] | None = None) -> str:
        existing = set(self.b.cols.get(node) or []) | (taken or set())
        name = _slug(wanted)
        candidate, i = name, 2
        while candidate in existing:
            candidate, i = f"{name}_{i}", i + 1
        return candidate

    def expr(self, label: str, text: str, stream: _Stream) -> tuple[exp.Expression, str | None]:
        named = stream.named()

        def resolve(name: str):
            hit = named.get(name) or named.get(name.lower())
            return (exp.column(hit[0]), hit[1]) if hit else None

        try:
            t = translate_ssis(text, resolve, self.vars, self.package)
        except SSISExpressionError as e:
            self.note(MANUAL, f"{label}: could not translate {text!r} ({e}); a NULL placeholder stands in")
            return exp.null(), None
        for fn in t.functions:
            self.functions[fn] = self.functions.get(fn, 0) + 1
        for n in t.notes:
            self.note(t.status, f"{label}: {n}")
        for var, value in t.variables.items():
            self.variables.setdefault(var, value)
            self.note(CONVERTED, f"@[User::{_var_label(self.vars, var)}] is the pipeline variable ${{{var}}}, "
                                 f"default {value!r}")
        return t.sql, t.kind

    def run_sql(self, sql: str, label: str, hints: dict[str, tuple[str, str | None]] | None = None) -> str:
        if re.search(r"(?<![\w'])\?(?![\w'])", sql):
            raise _Manual("its SQL takes ? parameters, which Redcliff can't fill; inline them or use a variable")
        try:
            tree = sqlglot.parse_one(sql, read="tsql")
        except ParseError as e:
            raise _Manual(f"its SQL could not be parsed: {str(e).splitlines()[0]}") from e
        for node in tree.find_all(exp.Column, exp.Table):  # dbo.orders -> orders
            for part in ("db", "catalog"):
                if node.args.get(part) is not None:
                    node.set(part, None)
        tree = normalize_identifiers(tree, dialect="tsql")
        if hints:
            self._schema_hints(tree, hints)
        nid = self.b.convert_query(tree, {}, label)
        return nid

    def _schema_hints(self, tree: exp.Expression, hints: dict[str, tuple[str, str | None]]) -> None:
        """Declare source columns from the SSIS output metadata, so lineage runs through them."""
        tables = [t for t in tree.find_all(exp.Table) if t.name]
        alias_of = {t.alias_or_name: t.name for t in tables}
        for c in tree.find_all(exp.Column):
            table = alias_of.get(c.table) if c.table else (tables[0].name if len(tables) == 1 else None)
            if table and c.name in hints and table not in self.b.tables:
                self.b.schema.setdefault(table, {})[c.name] = hints[c.name][0]


def _var_label(variables: dict[str, tuple[str, str | None]], var: str) -> str:
    for q in variables:
        if variable_name(q) == var:
            return q.split("::")[-1]
    return var


# --------------------------------------------------------------------------- one data flow


class _DataFlow:
    def __init__(self, imp: _Importer, flow: ET.Element, name: str, connections: dict[str, str]):
        self.imp = imp
        self.name = name
        self.connections = connections   # connection manager refId -> connection string
        pipeline = flow.find(f"{_d('ObjectData')}/pipeline")
        if pipeline is None:
            raise SSISImportError(f"data flow {name} has no pipeline")
        self.components = {c.get("refId") or c.get("name"): c for c in pipeline.findall("components/component")}
        self.paths: dict[str, str] = {}  # input refId -> output refId feeding it
        for p in pipeline.findall("paths/path"):
            self.paths[p.get("endId") or ""] = p.get("startId") or ""
        self.used_outputs = set(self.paths.values())
        self.owner: dict[str, str] = {}  # output refId -> component refId
        for ref, c in self.components.items():
            for o in c.findall("outputs/output"):
                self.owner[o.get("refId") or ""] = ref
        self.streams: dict[str, _Stream] = {}

    def order(self) -> list[str]:
        deps = {ref: set() for ref in self.components}
        for ref, c in self.components.items():
            for i in c.findall("inputs/input"):
                start = self.paths.get(i.get("refId") or "")
                if start and self.owner.get(start) in deps:
                    deps[ref].add(self.owner[start])
        done: list[str] = []
        seen: set[str] = set()

        def visit(n: str, stack: tuple[str, ...]) -> None:
            if n in seen or n in stack:
                return
            for d in sorted(deps[n]):
                visit(d, stack + (n,))
            seen.add(n)
            done.append(n)

        for n in self.components:
            visit(n, ())
        return done

    def input_stream(self, inp: ET.Element) -> _Stream:
        start = self.paths.get(inp.get("refId") or "")
        if not start:
            raise _Manual(f"nothing is connected to its input {inp.get('name')}")
        stream = self.streams.get(start)
        if stream is None:
            raise _Manual(f"its input {inp.get('name')} comes from a component that could not be converted")
        return stream

    def run(self) -> None:
        imp = self.imp
        for ref in self.order():
            c = self.components[ref]
            cls = (c.get("componentClassID") or "").split(".")[-1].lower()
            label = c.get("name") or ref
            item = ScoreItem(label, _KIND_LABELS.get(cls, c.get("componentClassID") or "component"))
            imp.item = item
            before = len(imp.b.nodes)
            warnings_before = len(imp.b.warnings)
            try:
                handler = getattr(self, f"c_{cls}", None)
                if handler is None:
                    raise _Manual(f"{item.kind} components have no Redcliff equivalent yet")
                handler(c, label)
            except _Manual as reason:
                item.flag(MANUAL, f"{reason}; a placeholder stands in")
                self.placeholder(c, label)
            for w in imp.b.warnings[warnings_before:]:
                item.flag(REVIEW, w)
            del imp.b.warnings[warnings_before:]
            item.nodes = [n["id"] for n in imp.b.nodes[before:]]
            imp.items.append(item)
        imp.item = None

    # ------------------------------------------------------------------ helpers

    def outputs(self, c: ET.Element, include_errors: bool = False) -> list[ET.Element]:
        return [o for o in c.findall("outputs/output") if include_errors or (o.get("isErrorOut") or "").lower() != "true"]

    def pass_through(self, c: ET.Element, stream: _Stream, extra: dict[str, str] | None = None,
                     kinds: dict[str, str | None] | None = None, node: str | None = None) -> None:
        """Every output of a synchronous component carries the input's columns (and new ones)."""
        for o in self.outputs(c):
            cols = {**stream.cols, **(extra or {})}
            self.streams[o.get("refId") or ""] = _Stream(node or stream.node, cols, {**stream.kinds, **(kinds or {})},
                                                         list(stream.sort_keys))

    def placeholder(self, c: ET.Element, label: str) -> None:
        """Typed NULL columns for a component that couldn't be converted, so the pipeline stays whole."""
        imp = self.imp
        inputs = c.findall("inputs/input")
        stream = None
        for i in inputs:
            start = self.paths.get(i.get("refId") or "")
            if start in self.streams:
                stream = self.streams[start]
                break
        for o in self.outputs(c):
            cols = o.findall("outputColumns/outputColumn")
            if stream is not None:
                taken: set[str] = set()
                new = {}
                for oc in cols:
                    name = imp.fresh(oc.get("name") or "column", stream.node, taken)
                    taken.add(name)
                    new[oc.get("lineageId") or name] = name
                if new:
                    derive = {name: f"CAST(NULL AS {_column_type(oc)[0]})" for oc, name in zip(cols, new.values())}
                    node = imp.add("derive", [stream.node], {"columns": derive}, f"{_slug(label)}_placeholder")
                else:
                    node = stream.node
                self.streams[o.get("refId") or ""] = _Stream(node, {**stream.cols, **new}, dict(stream.kinds))
            elif cols:
                names = [_slug(oc.get("name") or "column") for oc in cols]
                select = ", ".join(f"CAST(NULL AS {_column_type(oc)[0]}) AS {n}" for oc, n in zip(cols, names))
                node = imp.add("sql", [], {"query": f"SELECT {select} WHERE 1 = 0"}, f"{_slug(label)}_placeholder")
                self.streams[o.get("refId") or ""] = _Stream(
                    node, {(oc.get("lineageId") or n): n for oc, n in zip(cols, names)})

    def connection(self, c: ET.Element) -> str:
        for conn in c.findall("connections/connection"):
            cm = conn.get("connectionManagerRefId") or conn.get("connectionManagerID") or ""
            if cm in self.connections:
                return self.connections[cm]
        return ""

    def variable_value(self, name: str) -> str:
        qualified = name if "::" in name else f"User::{name}"
        found = self.imp.vars.get(qualified) or next((v for k, v in self.imp.vars.items()
                                                      if k.upper() == qualified.upper()), None)
        if found is None:
            raise _Manual(f"it reads variable {qualified}, which isn't in the package")
        self.imp.note(REVIEW, f"its table or query comes from variable {qualified}, read once at import "
                              f"({found[0]!r}); make it a pipeline variable if it changes")
        return found[0]

    # ------------------------------------------------------------------ sources

    def source_columns(self, c: ET.Element) -> list[ET.Element]:
        out = self.outputs(c)
        return out[0].findall("outputColumns/outputColumn") if out else []

    def c_oledbsource(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        p = _props(c)
        mode = (p.get("AccessMode") or "0").strip()
        cols = self.source_columns(c)
        if mode in ("0", "1"):
            table = p.get("OpenRowset", "") if mode == "0" else self.variable_value(p.get("OpenRowsetVariable", ""))
            self.table_source(c, label, _table_name(table), cols)
            return
        sql = p.get("SqlCommand", "") if mode == "2" else self.variable_value(p.get("SqlCommandVariable", ""))
        if not sql.strip():
            raise _Manual("it has no SQL command")
        hints = {_slug(oc.get("name") or ""): _column_type(oc) for oc in cols}
        node = imp.run_sql(sql, _slug(label), hints)
        result = imp.b.cols.get(node) or []
        if len(result) != len(cols):
            by_name = {r: r for r in result}
            mapped = {oc.get("lineageId") or "": by_name.get(_slug(oc.get("name") or "")) for oc in cols}
            if any(v is None for v in mapped.values()):
                raise _Manual(f"its SQL returns {len(result)} columns for {len(cols)} output columns")
        else:
            mapped = {(oc.get("lineageId") or ""): r for oc, r in zip(cols, result)}
        imp.note(CONVERTED, "the SQL command was imported as steps")
        stream = _Stream(node, mapped, {oc.get("lineageId") or "": _column_type(oc)[1] for oc in cols})
        for o in self.outputs(c):
            self.streams[o.get("refId") or ""] = stream

    c_adonetsource = c_oledbsource

    def table_source(self, c: ET.Element, label: str, table: str, cols: list[ET.Element]) -> None:
        imp = self.imp
        declared = {_slug(oc.get("name") or ""): _column_type(oc)[0] for oc in cols}
        if table in imp.written:
            node = imp.written[table]
            imp.note(CONVERTED, f"reads {table}, which an earlier data flow writes: the pipelines connect")
        elif table in imp.b.tables:
            node = imp.b.tables[table]
        else:
            node = imp.add("source", [], {"format": "table", "table": table, "columns": declared}, table)
            imp.b.tables[table] = node
        stream = _Stream(node, {(oc.get("lineageId") or ""): _slug(oc.get("name") or "") for oc in cols},
                         {(oc.get("lineageId") or ""): _column_type(oc)[1] for oc in cols})
        for o in self.outputs(c):
            self.streams[o.get("refId") or ""] = stream

    def c_flatfilesource(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        path = self.connection(c) or f"{_slug(label)}.csv"
        cols = self.source_columns(c)
        declared = {_slug(oc.get("name") or ""): _column_type(oc)[0] for oc in cols}
        node = imp.add("source", [], {"format": "csv", "path": re.split(r"[\\/]", path)[-1], "columns": declared},
                       _slug(label))
        imp.note(REVIEW, f"reads the flat file {path}; the spec reads it by name next to the spec file, with a "
                         "header row; check the delimiter and code page")
        stream = _Stream(node, {(oc.get("lineageId") or ""): _slug(oc.get("name") or "") for oc in cols},
                         {(oc.get("lineageId") or ""): _column_type(oc)[1] for oc in cols})
        for o in self.outputs(c):
            self.streams[o.get("refId") or ""] = stream

    # ------------------------------------------------------------------ synchronous transforms

    def first_input(self, c: ET.Element) -> tuple[ET.Element, _Stream]:
        inputs = c.findall("inputs/input")
        if not inputs:
            raise _Manual("it has no input")
        return inputs[0], self.input_stream(inputs[0])

    def c_derivedcolumn(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        _inp, stream = self.first_input(c)
        derive: dict[str, str] = {}
        new: dict[str, str] = {}
        kinds: dict[str, str | None] = {}
        for ic in c.findall("inputs/input/inputColumns/inputColumn"):
            p = _props(ic)
            text = p.get("FriendlyExpression") or p.get("Expression")
            if (ic.get("usageType") or "").lower() == "readwrite" and text:
                col = stream.cols.get(ic.get("lineageId") or "")
                if col is None:
                    raise _Manual(f"it replaces {ic.get('cachedName')}, which isn't in its input")
                e, _k = imp.expr(ic.get("cachedName") or col, text, stream)
                derive[col] = e.sql()
        taken = set(derive)
        for o in self.outputs(c):
            for oc in o.findall("outputColumns/outputColumn"):
                p = _props(oc)
                text = p.get("FriendlyExpression") or p.get("Expression") or "NULL"
                col = imp.fresh(oc.get("name") or "column", stream.node, taken)
                taken.add(col)
                e, k = imp.expr(oc.get("name") or col, text, stream)
                derive[col] = e.sql()
                new[oc.get("lineageId") or col] = col
                kinds[oc.get("lineageId") or col] = _column_type(oc)[1] or k
        node = imp.add("derive", [stream.node], {"columns": derive}, _slug(label)) if derive else stream.node
        self.pass_through(c, stream, new, kinds, node)

    def c_dataconvert(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        _inp, stream = self.first_input(c)
        derive, new, kinds = {}, {}, {}
        taken: set[str] = set()
        for o in self.outputs(c):
            for oc in o.findall("outputColumns/outputColumn"):
                src = stream.cols.get(_ref(_props(oc).get("SourceInputColumnLineageID", "")))
                if src is None:
                    raise _Manual(f"{oc.get('name')} converts a column that isn't in its input")
                col = imp.fresh(oc.get("name") or "column", stream.node, taken)
                taken.add(col)
                sql_type, kind = _column_type(oc)
                derive[col] = f"CAST({src} AS {sql_type})"
                new[oc.get("lineageId") or col] = col
                kinds[oc.get("lineageId") or col] = kind
        node = imp.add("derive", [stream.node], {"columns": derive}, _slug(label)) if derive else stream.node
        self.pass_through(c, stream, new, kinds, node)

    def c_multicast(self, c: ET.Element, label: str) -> None:
        _inp, stream = self.first_input(c)
        self.imp.note(CONVERTED, "every output gets every row")
        self.pass_through(c, stream)

    def c_rowcount(self, c: ET.Element, label: str) -> None:
        _inp, stream = self.first_input(c)
        var = _props(c).get("VariableName", "")
        self.imp.note(CONVERTED, "rows pass through" + (f"; the count it stores in {var} is for the control flow"
                                                        if var else ""))
        self.pass_through(c, stream)

    def c_conditionalsplit(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        _inp, stream = self.first_input(c)
        cases: list[tuple[int, ET.Element, exp.Expression]] = []
        default = None
        for o in self.outputs(c):
            p = _props(o)
            if (p.get("IsDefaultOut") or "").lower() == "true":
                default = o
                continue
            text = p.get("FriendlyExpression") or p.get("Expression") or "FALSE"
            e, _k = imp.expr(o.get("name") or "condition", text, stream)
            cases.append((int(p.get("EvaluationOrder") or len(cases)), o, e))
        cases.sort(key=lambda x: x[0])
        earlier: list[exp.Expression] = []
        for _order, o, cond in [*cases, (None, default, None)]:
            if o is None:
                continue
            ref = o.get("refId") or ""
            parts = [exp.Not(this=exp.Coalesce(this=e, expressions=[exp.false()])) for e in earlier]
            if cond is not None:
                parts = [exp.Coalesce(this=cond, expressions=[exp.false()]), *parts]
                earlier.append(cond)
            if ref not in self.used_outputs:
                continue
            node = stream.node
            if parts:
                full = parts[0]
                for part in parts[1:]:
                    full = exp.And(this=full, expression=part)
                base = f"{_slug(label)}_{_slug(o.get('name') or 'out')}"
                node = imp.add("filter", [stream.node], {"condition": full.sql()}, base)
            self.streams[ref] = _Stream(node, dict(stream.cols), dict(stream.kinds), list(stream.sort_keys))
        imp.note(CONVERTED, "each row takes the first output whose condition is true, as in SSIS; a NULL "
                            "condition counts as false")

    def c_sort(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        _inp, stream = self.first_input(c)
        keys: list[tuple[int, str]] = []
        for ic in c.findall("inputs/input/inputColumns/inputColumn"):
            pos = int(_props(ic).get("NewSortKeyPosition") or 0)
            col = stream.cols.get(ic.get("lineageId") or "")
            if pos and col:
                keys.append((abs(pos), col))
        sort_keys = [col for _p, col in sorted(keys)]
        out = self.outputs(c)[0] if self.outputs(c) else None
        if out is None:
            return
        cols: dict[str, str] = {}
        kinds: dict[str, str | None] = {}
        for oc in out.findall("outputColumns/outputColumn"):
            src = _ref(_props(oc).get("SortColumnId", ""))
            col = stream.cols.get(src)
            if col is not None:
                cols[oc.get("lineageId") or ""] = col
                kinds[oc.get("lineageId") or ""] = stream.kinds.get(src)
        node = stream.node
        if (_props(c).get("EliminateDuplicates") or "").lower() == "true":
            node = imp.add("aggregate", [stream.node], {"group_by": list(dict.fromkeys(cols.values()))}, _slug(label))
            imp.note(CONVERTED, "duplicate rows are removed; the sort itself isn't needed")
        else:
            imp.note(CONVERTED, "row order doesn't matter to set-based steps; the sort keys are kept for a Merge Join")
        self.streams[out.get("refId") or ""] = _Stream(node, cols, kinds, sort_keys)

    # ------------------------------------------------------------------ joins and lookups

    def c_mergejoin(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        inputs = c.findall("inputs/input")
        if len(inputs) != 2:
            raise _Manual("a merge join needs two inputs")
        left_in, right_in = sorted(inputs, key=lambda i: "right" in (i.get("name") or "").lower())
        left, right = self.input_stream(left_in), self.input_stream(right_in)
        p = _props(c)
        n = int(p.get("NumKeyColumns") or 1)
        if len(left.sort_keys) < n or len(right.sort_keys) < n:
            raise _Manual("its inputs' sort keys aren't known (sort them with a Sort, or mark the source sorted)")
        lkeys, rkeys = left.sort_keys[:n], right.sort_keys[:n]
        # Input columns: their refId -> (side, column)
        by_input: dict[str, tuple[str, str]] = {}
        for side, inp, stream in (("left", left_in, left), ("right", right_in, right)):
            for ic in inp.findall("inputColumns/inputColumn"):
                col = stream.cols.get(ic.get("lineageId") or "")
                if col is not None:
                    by_input[ic.get("refId") or ""] = (side, col)
        l_node, r_node = left.node, right.node
        if l_node == r_node:  # a self-join: read one side through its own step
            r_node = imp.add("select", [right.node], {"columns": list(imp.b.cols[right.node] or [])},
                             f"{_slug(label)}_right")
        # Right columns that clash with left ones would be hidden by the join: rename them.
        left_cols = set(imp.b.cols[l_node] or [])
        rename = {}
        for col in imp.b.cols[r_node] or []:
            if col in left_cols:
                rename[col] = imp.fresh(f"{col}_right", r_node, left_cols | set(rename.values()))
        if rename:
            r_node = imp.add("derive", [r_node], {"columns": {v: k for k, v in rename.items()}},
                             f"{_slug(label)}_right_renamed")
        nulls_equal = (p.get("TreatNullsAsEqual") or "true").lower() == "true"
        conds = []
        for lk, rk in zip(lkeys, rkeys):
            a, b = exp.column(lk, table=l_node), exp.column(rename.get(rk, rk), table=r_node)
            conds.append(exp.NullSafeEQ(this=a, expression=b) if nulls_equal else exp.EQ(this=a, expression=b))
        cond = conds[0]
        for more in conds[1:]:
            cond = exp.And(this=cond, expression=more)
        how = {"0": "full", "1": "left", "2": "inner"}.get((p.get("JoinType") or "2").strip(), "inner")
        params = {"condition": cond.sql()} if how == "inner" else {"how": how, "condition": cond.sql()}
        node = imp.add("join", [l_node, r_node], params, _slug(label))
        if nulls_equal:
            imp.note(CONVERTED, "NULL keys match each other, as SSIS's 'treat nulls as equal' does")
        out = self.outputs(c)[0]
        cols, kinds = {}, {}
        for oc in out.findall("outputColumns/outputColumn"):
            hit = by_input.get(_ref(_props(oc).get("InputColumnID", "")))
            if hit is None:
                continue
            side, col = hit
            cols[oc.get("lineageId") or ""] = rename.get(col, col) if side == "right" else col
            kinds[oc.get("lineageId") or ""] = _column_type(oc)[1]
        self.streams[out.get("refId") or ""] = _Stream(node, cols, kinds)

    def c_lookup(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        inp, stream = self.first_input(c)
        p = _props(c)
        sql = p.get("SqlCommand", "").strip()
        if not sql:
            raise _Manual("its reference has no SQL command")
        keys: list[tuple[str, str]] = []   # (stream column, reference column)
        for ic in inp.findall("inputColumns/inputColumn"):
            ref_col = _props(ic).get("JoinToReferenceColumn")
            col = stream.cols.get(ic.get("lineageId") or "")
            if ref_col and col:
                keys.append((col, _slug(ref_col)))
        if not keys:
            raise _Manual("it has no join columns")
        match = next((o for o in self.outputs(c) if "no match" not in (o.get("name") or "").lower()), None)
        copies = [(oc, _slug(_props(oc).get("CopyFromReferenceColumn") or oc.get("name") or ""))
                  for oc in (match.findall("outputColumns/outputColumn") if match is not None else [])]
        hints = {rc: _column_type(oc) for oc, rc in copies}
        for col, rc in keys:
            hints.setdefault(rc, ("text", stream.kinds.get(col)))
        ref_node = imp.run_sql(sql, f"{_slug(label)}_reference", hints)
        ref_cols = imp.b.cols.get(ref_node) or []
        missing = [rc for rc in [k for _c, k in keys] + [rc for _o, rc in copies] if rc not in ref_cols]
        if ref_cols and missing:
            raise _Manual(f"its reference query has no column(s) {missing}")
        wanted = [rc for _o, rc in copies]
        ref_keys = list(dict.fromkeys(k for _c, k in keys))
        measures = {rc: f"ANY_VALUE({rc})" for rc in wanted if rc not in ref_keys}
        params: dict[str, Any] = {"group_by": ref_keys}
        if measures:
            params["measures"] = measures
        ref_node = imp.add("aggregate", [ref_node], params, f"{_slug(label)}_unique")
        imp.note(CONVERTED, "one reference row per key: SSIS uses the first match it caches")
        # Copied columns that clash with the stream's columns would be hidden: rename them.
        left_cols = set(imp.b.cols[stream.node] or [])
        out_names = {}
        rename = {}
        for oc, rc in copies:
            want = _slug(oc.get("name") or rc)
            name = want if want not in left_cols and want not in out_names.values() else \
                imp.fresh(want, stream.node, set(out_names.values()))
            out_names[oc.get("lineageId") or ""] = name
            if name != rc:
                rename[name] = rc
        key_alias = {}
        for _c, rk in keys:
            if rk in left_cols or rk in rename.values():
                key_alias[rk] = imp.fresh(f"{rk}_lookup", stream.node, left_cols | set(out_names.values()))
        if rename or key_alias:
            cols = {**rename, **{v: k for k, v in key_alias.items()}}
            ref_node = imp.add("derive", [ref_node], {"columns": cols}, f"{_slug(label)}_renamed")
            ref_node = imp.add("select", [ref_node], {"columns": [key_alias.get(k, k) for k in ref_keys] + list(rename)
                                                      + [rc for _o, rc in copies if rc not in rename.values()
                                                         and rc not in rename and rc not in ref_keys]},
                               f"{_slug(label)}_columns")
        cond = None
        for col, rk in keys:
            eq = exp.EQ(this=exp.column(col, table=stream.node), expression=exp.column(key_alias.get(rk, rk), table=ref_node))
            cond = eq if cond is None else exp.And(this=cond, expression=eq)
        node = imp.add("join", [stream.node, ref_node], {"how": "left", "condition": cond.sql()}, _slug(label))
        no_match = (p.get("NoMatchBehavior") or "0").strip()
        disposition = (inp.get("errorRowDisposition") or "FailComponent").lower()
        marker = key_alias.get(ref_keys[0], ref_keys[0])
        if no_match == "1":
            imp.note(CONVERTED, "rows with no match go to the no-match output")
        elif disposition == "ignorefailure":
            imp.note(CONVERTED, "a row with no match keeps NULLs, as 'Ignore failure' does")
        elif disposition == "redirectrow":
            imp.note(CONVERTED, "rows with no match go to the error output")
        else:
            imp.note(REVIEW, "SSIS fails the package when a row finds no match; here the row keeps NULLs")
        new = {lineage: name for lineage, name in out_names.items()}
        for o in self.outputs(c, include_errors=True):
            ref = o.get("refId") or ""
            if ref not in self.used_outputs:
                continue
            name = (o.get("name") or "").lower()
            is_error = (o.get("isErrorOut") or "").lower() == "true"
            if "no match" in name or (is_error and disposition == "redirectrow"):
                n2 = imp.add("filter", [node], {"condition": f"{marker} IS NULL"}, f"{_slug(label)}_no_match")
                self.streams[ref] = _Stream(n2, dict(stream.cols), dict(stream.kinds))
            elif no_match == "1" or disposition == "redirectrow":
                n2 = imp.add("filter", [node], {"condition": f"{marker} IS NOT NULL"}, f"{_slug(label)}_match")
                self.streams[ref] = _Stream(n2, {**stream.cols, **new}, {**stream.kinds})
            else:
                self.streams[ref] = _Stream(node, {**stream.cols, **new}, {**stream.kinds})

    # ------------------------------------------------------------------ asynchronous transforms

    def c_unionall(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        out = self.outputs(c)[0]
        out_cols = out.findall("outputColumns/outputColumn")
        names = [_slug(oc.get("name") or "column") for oc in out_cols]
        index = {oc.get("lineageId") or "": i for i, oc in enumerate(out_cols)}
        branches = []
        for inp in c.findall("inputs/input"):
            start = self.paths.get(inp.get("refId") or "")
            if not start:
                continue
            stream = self.input_stream(inp)
            picks: list[str | None] = [None] * len(names)
            for ic in inp.findall("inputColumns/inputColumn"):
                pos = index.get(_ref(_props(ic).get("OutputColumnLineageID", "")))
                col = stream.cols.get(ic.get("lineageId") or "")
                if pos is not None and col is not None:
                    picks[pos] = col
            derive = {n: (src if src is not None else f"CAST(NULL AS {_column_type(oc)[0]})")
                      for n, src, oc in zip(names, picks, out_cols) if src != n}
            node = stream.node
            if derive:
                node = imp.add("derive", [node], {"columns": derive}, f"{_slug(label)}_{_slug(inp.get('name') or 'in')}")
            if imp.b.cols[node] != names:
                node = imp.add("select", [node], {"columns": names}, f"{_slug(label)}_{_slug(inp.get('name') or 'in')}")
            branches.append(node)
        if len(branches) < 2:
            raise _Manual("fewer than two inputs are connected")
        node = imp.add("union", branches, {}, _slug(label))
        self.streams[out.get("refId") or ""] = _Stream(
            node, {(oc.get("lineageId") or ""): n for oc, n in zip(out_cols, names)},
            {(oc.get("lineageId") or ""): _column_type(oc)[1] for oc in out_cols})

    def c_aggregate(self, c: ET.Element, label: str) -> None:
        imp = self.imp
        _inp, stream = self.first_input(c)
        outs = self.outputs(c)
        if len(outs) != 1:
            raise _Manual("an aggregate with several outputs isn't supported yet")
        out = outs[0]
        group_by: list[str] = []
        pre: dict[str, str] = {}
        measures: dict[str, str] = {}
        cols: dict[str, str] = {}
        kinds: dict[str, str | None] = {}
        taken: set[str] = set()
        for oc in out.findall("outputColumns/outputColumn"):
            p = _props(oc)
            how = (p.get("AggregationType") or "0").strip()
            src = stream.cols.get(_ref(p.get("AggregationColumnId", "")))
            name = _slug(oc.get("name") or "column")
            if name in taken:
                name = imp.fresh(name, stream.node, taken)
            taken.add(name)
            if how != "2" and src is None:
                raise _Manual(f"{oc.get('name')} aggregates a column that isn't in its input")
            if how == "0":
                if src != name:
                    pre[name] = src
                group_by.append(name)
            else:
                fn = {"1": f"COUNT({src})", "2": "COUNT(*)", "3": f"COUNT(DISTINCT {src})", "4": f"SUM({src})",
                      "5": f"AVG({src})", "6": f"MIN({src})", "7": f"MAX({src})"}.get(how)
                if fn is None:
                    raise _Manual(f"aggregation type {how} isn't supported")
                measures[name] = fn
            cols[oc.get("lineageId") or ""] = name
            kinds[oc.get("lineageId") or ""] = _column_type(oc)[1]
        base = stream.node
        if pre:
            base = imp.add("derive", [base], {"columns": pre}, f"{_slug(label)}_keys")
        params: dict[str, Any] = {}
        if group_by:
            params["group_by"] = group_by
        if measures:
            params["measures"] = measures
        node = imp.add("aggregate", [base], params, _slug(label))
        self.streams[out.get("refId") or ""] = _Stream(node, cols, kinds)

    # ------------------------------------------------------------------ destinations

    def c_oledbdestination(self, c: ET.Element, label: str) -> None:
        inp, stream = self.first_input(c)
        p = _props(c)
        mode = (p.get("AccessMode") or "3").strip()
        if mode in ("0", "3"):
            table = _table_name(p.get("OpenRowset", ""))
        elif mode in ("1", "4"):
            table = _table_name(self.variable_value(p.get("OpenRowsetVariable", "")))
        else:
            raise _Manual("it writes through a SQL command, which has no equivalent yet")
        self.write(inp, stream, label, {"format": "table", "table": table}, table)

    c_adonetdestination = c_oledbdestination
    c_sqlserverdestination = c_oledbdestination

    def c_flatfiledestination(self, c: ET.Element, label: str) -> None:
        inp, stream = self.first_input(c)
        path = self.connection(c) or f"{_slug(label)}.csv"
        name = re.split(r"[\\/]", path)[-1]
        self.imp.note(REVIEW, f"writes the flat file {path}; the spec writes {name} with a header row; check the "
                              "delimiter and code page")
        self.write(inp, stream, label, {"format": "csv", "path": name}, None)

    def write(self, inp: ET.Element, stream: _Stream, label: str, params: dict[str, Any], table: str | None) -> None:
        imp = self.imp
        ext = {e.get("refId") or "": e.get("name") or "" for e in inp.findall("externalMetadataColumns/externalMetadataColumn")}
        picks: dict[str, str] = {}
        for ic in inp.findall("inputColumns/inputColumn"):
            col = stream.cols.get(ic.get("lineageId") or "")
            target = ext.get(ic.get("externalMetadataColumnId") or "") or ic.get("cachedName") or col
            if col is not None and target:
                picks[_slug(target)] = col
        if not picks:
            raise _Manual("no input column is mapped to a destination column")
        names = list(picks)
        derive = {n: src for n, src in picks.items() if n != src}
        node = stream.node
        if derive:
            node = imp.add("derive", [node], {"columns": derive}, f"{_slug(label)}_renamed")
        if imp.b.cols[node] != names:
            node = imp.add("select", [node], {"columns": names}, f"{_slug(label)}_rows")
        params = dict(params)
        task = imp.truncated.get(table) if table else None
        params["mode"] = "overwrite" if task is not None else "append"
        imp.add("target", [node], params, _slug(label))
        if table:
            imp.written[table] = node
        imp.outputs.append(node)
        if task is not None:
            imp.note(CONVERTED, f"{task.name} truncates {table} first, so rows overwrite it")
            task.flag(CONVERTED, f"truncates {table} before {self.name} loads it: the target overwrites")
        else:
            imp.note(CONVERTED, "rows are appended, as SSIS inserts")
        unmapped = [n for n in ext.values() if _slug(n) not in picks]
        if unmapped:
            imp.note(CONVERTED, f"{len(unmapped)} destination column(s) aren't mapped and get the table's default: "
                                f"{', '.join(unmapped)}")


_KIND_LABELS = {
    "oledbsource": "OLE DB Source", "adonetsource": "ADO.NET Source", "flatfilesource": "Flat File Source",
    "derivedcolumn": "Derived Column", "dataconvert": "Data Conversion", "conditionalsplit": "Conditional Split",
    "multicast": "Multicast", "rowcount": "Row Count", "sort": "Sort", "mergejoin": "Merge Join",
    "lookup": "Lookup", "unionall": "Union All", "aggregate": "Aggregate",
    "oledbdestination": "OLE DB Destination", "adonetdestination": "ADO.NET Destination",
    "sqlserverdestination": "SQL Server Destination", "flatfiledestination": "Flat File Destination",
    "scriptcomponenthost": "Script Component", "scriptcomponent": "Script Component",
}


# --------------------------------------------------------------------------- the control flow


@dataclass
class _Task:
    key: str
    label: str
    kind: str
    el: ET.Element


def _executables(container: ET.Element, prefix: str = "") -> list[_Task]:
    """A container's tasks in run order (precedence constraints), with sequence containers inlined."""
    holder = container.find(_d("Executables"))
    if holder is None:
        return []
    children = holder.findall(_d("Executable"))
    refs = [e.get(_d("refId")) or e.get(_d("ObjectName")) or "" for e in children]
    links = [(pc.get(_d("From")) or "", pc.get(_d("To")) or "")
             for pc in container.findall(f"{_d('PrecedenceConstraints')}/{_d('PrecedenceConstraint')}")]
    preds = {r: {a for a, b in links if b == r and a in refs} for r in refs}
    order: list[str] = []
    pending = list(refs)
    while pending:
        ready = [r for r in pending if preds[r] <= set(order)] or pending[:1]
        order.append(ready[0])
        pending.remove(ready[0])
    by_ref = dict(zip(refs, children))
    out: list[_Task] = []
    for ref in order:
        e = by_ref[ref]
        kind = e.get(_d("ExecutableType")) or e.get(_d("CreationName")) or "Task"
        label = f"{prefix}{e.get(_d('ObjectName')) or ref}"
        if kind.upper().startswith("STOCK:SEQUENCE"):
            out += _executables(e, f"{label}/")
            continue
        out.append(_Task(ref, label, kind, e))
        if kind.upper().startswith(("STOCK:FORLOOP", "STOCK:FOREACHLOOP")):
            out += _executables(e, f"{label}/")
    return out


def _variables(root: ET.Element) -> dict[str, tuple[str, str | None]]:
    out = {}
    for v in root.iter(_d("Variable")):
        ns = v.get(_d("Namespace")) or "User"
        name = v.get(_d("ObjectName")) or ""
        value_el = v.find(_d("VariableValue"))
        value = (value_el.text or "") if value_el is not None else ""
        kind = _DTYPE_KIND.get((value_el.get(_d("DataType")) if value_el is not None else "8") or "8", "string")
        out.setdefault(f"{ns}::{name}", (value, kind))
    return out


def _connections(root: ET.Element) -> dict[str, str]:
    out = {}
    for cm in root.iter(_d("ConnectionManager")):
        ref = cm.get(_d("refId"))
        inner = cm.find(f"{_d('ObjectData')}/{_d('ConnectionManager')}")
        if ref and inner is not None:
            out[ref] = inner.get(_d("ConnectionString")) or ""
    return out


_TASK_ADVICE = {
    "microsoft.sendmailtask": (MANUAL, "sends an email; use your orchestrator's notifications"),
    "microsoft.scripttask": (MANUAL, "runs a script; rewrite it as a task in your orchestrator"),
    "microsoft.executeprocess": (MANUAL, "runs a program; recreate it as a task in your orchestrator"),
    "microsoft.filesystemtask": (MANUAL, "moves or copies files; use your orchestrator's file tasks"),
    "microsoft.executepackagetask": (REVIEW, "runs another package; import that package too and chain them"),
    "stock:forloop": (MANUAL, "repeats its tasks in a loop; recreate the loop in your orchestrator"),
    "stock:foreachloop": (MANUAL, "repeats its tasks for each item; recreate the loop in your orchestrator"),
}


def is_ssis(text: str, filename: str = "") -> bool:
    """Does this look like an SSIS package rather than a PowerCenter export or SQL?"""
    return filename.lower().endswith(".dtsx") or DTS in text[:4000]


def import_ssis(xml: str, name: str | None = None) -> ImportResult:
    """Import an SSIS package (the .dtsx XML, SSIS 2012 or later) into a pipeline with a scorecard."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise SSISImportError(f"could not read the XML: {e}") from e
    if root.tag != _d("Executable"):
        raise SSISImportError("this XML isn't an SSIS package (no DTS:Executable root)")
    fmt = next((p.text for p in root.findall(_d("Property")) if p.get(_d("Name")) == "PackageFormatVersion"), None)
    if fmt is not None and fmt.strip() in ("2", "3"):
        raise SSISImportError("this package uses the SSIS 2005/2008 format; open and save it in SQL Server Data "
                              "Tools (2012 or later) first")
    package = root.get(_d("ObjectName")) or "package"
    imp = _Importer(package, _variables(root))
    connections = _connections(root)
    tasks = _executables(root)
    flows = [t for t in tasks if t.kind.lower() == "microsoft.pipeline"]
    if not flows:
        raise SSISImportError("the package has no data flow tasks")

    # Execute SQL tasks first: a TRUNCATE TABLE makes a later load overwrite.
    pending_sql: list[tuple[_Task, ScoreItem, str]] = []
    for t in tasks:
        if t.kind.lower() != "microsoft.executesqltask":
            continue
        data = t.el.find(f"{_d('ObjectData')}/{{{SQLTASK}}}SqlTaskData")
        sql = (data.get(f"{{{SQLTASK}}}SqlStatementSource") if data is not None else "") or ""
        item = ScoreItem(f"{package}/{t.label}", "Execute SQL Task", area="orchestration")
        m = re.fullmatch(r"\s*TRUNCATE\s+TABLE\s+([\w\[\]\.\"]+)\s*;?\s*", sql, re.IGNORECASE)
        if m:
            imp.truncated[_table_name(m.group(1))] = item
        pending_sql.append((t, item, sql))

    for t in flows:
        _DataFlow(imp, t.el, t.label, connections).run()

    steps = [t.label for t in tasks]
    flow_item = ScoreItem(package, "Package", area="orchestration")
    flow_item.flag(REVIEW, "Redcliff builds pipelines, not schedules: recreate this run order in your orchestrator: "
                   + " → ".join(steps))
    imp.items.append(flow_item)
    for _t, item, sql in pending_sql:
        if item.notes and item.status == CONVERTED:
            imp.items.append(item)
            continue
        item.flag(REVIEW, f"runs SQL on the database, which belongs in your orchestrator: {' '.join(sql.split())}")
        imp.items.append(item)
    for t in tasks:
        kind = t.kind.lower()
        if kind in ("microsoft.pipeline", "microsoft.executesqltask"):
            continue
        status, advice = _TASK_ADVICE.get(kind, (MANUAL, "has no pipeline equivalent"))
        item = ScoreItem(f"{package}/{t.label}", t.kind.split(".")[-1], area="orchestration")
        item.flag(status, advice)
        imp.items.append(item)

    if not imp.b.nodes:
        raise SSISImportError("nothing in the data flows could be imported")
    title = name or package
    data = {"version": 0, "name": _slug(title)}
    if imp.variables:
        data["variables"] = imp.variables
    data["nodes"] = imp.b.nodes
    pipeline = Pipeline.from_dict(data)
    issues = validate(pipeline)
    if issues:  # a bug in the importer, not in the package
        raise SpecError(issues)
    card = Scorecard("ssis", title, imp.items, dict(imp.functions), lineage_coverage(pipeline))
    warnings = [f"{i.name}: {n}" for i in imp.items if i.status != CONVERTED for n in i.notes]
    bad = sorted({c for cols in imp.b.cols.values() if cols for c in cols if not ID_PATTERN.match(c)})
    if bad:
        warnings.append(f"column names that need quoting: {bad}")
    return ImportResult(pipeline, warnings, imp.outputs, card)
