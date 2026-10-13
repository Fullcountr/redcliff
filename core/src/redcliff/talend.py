"""Import Talend Open Studio jobs (the ``.item`` XML of a Standard job) into a Redcliff pipeline.

A job is components (``node``) wired by connections: row connections carry data (``row1``,
``FILTER``, ``REJECT``, lookups), trigger connections (OnSubjobOk, OnComponentOk, Run if) set the
order things run in. Expressions are Java and go through :mod:`redcliff.talend_expr`; context
variables become pipeline variables. This importer walks the components in data order:

======================  ==========================================================
Talend                  Redcliff
======================  ==========================================================
tDBInput, tMysqlInput,  ``source`` for a table, or the query through the SQL
tOracleInput, ...       importer (columns map to the schema by position, as in
                        Talend)
tFileInputDelimited     ``source`` (CSV)
tMap                    a ``join`` per lookup (inner or left; unique-match lookups
                        keep one row per key), ``derive`` for Var and output
                        expressions, a ``filter`` per output filter, and inner
                        join rejects
tJoin                   ``join`` on the key columns, and its reject flow
tFilterRow              ``filter`` (FILTER) and its complement (REJECT)
tAggregateRow           ``aggregate``
tUniqRow                one row per key: ``aggregate``
tUnite                  ``select`` per input + ``union``
tFilterColumns          ``select``
tConvertType            ``derive`` with CAST
tReplicate, tSortRow,   nothing: rows pass through (tables have no order)
tLogRow
tDBOutput, tMysqlOutput ``select`` + ``target``: overwrite, append, merge on the
...                     schema's key columns, or insert-new
tFileOutputDelimited    ``target`` (CSV)
anything else           a placeholder step, flagged for a person to rewrite
======================  ==========================================================

Pre/post jobs, connections, tRunJob, tSendMail, tJava and the trigger order are graded as
orchestration. Only the standard library's XML parser is used, so this runs in the browser too.
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
from redcliff.talend_expr import (TalendExpressionError, java_string, talend_type, translate_talend,
                                  variable_name)

TRIGGERS = {"SUBJOB_OK", "SUBJOB_ERROR", "COMPONENT_OK", "COMPONENT_ERROR", "RUN_IF", "ITERATE",
            "SYNCHRONIZE", "PARALLELIZE", "ON_SUBJOB_OK", "ON_SUBJOB_ERROR", "ON_COMPONENT_OK",
            "ON_COMPONENT_ERROR"}
LOOKUP_STYLE = "8"
_DIALECTS = {"mysql": "mysql", "mariadb": "mysql", "oracle": "oracle", "postgresql": "postgres", "postgres": "postgres",
             "greenplum": "postgres", "mssql": "tsql", "sqlserver": "tsql", "snowflake": "snowflake",
             "teradata": "teradata", "redshift": "redshift", "hive": "hive", "bigquery": "bigquery",
             "vertica": "postgres", "sybase": "tsql", "db2": None, "jdbc": None, "db": None}


class TalendImportError(SQLImportError):
    """The XML is not a Talend job, or has nothing to import."""


class _Manual(Exception):
    """This component can't be converted faithfully; a placeholder stands in."""


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "x"
    s = re.sub(r"_+", "_", s)
    return s if re.match(r"[a-z_]", s[0]) else "_" + s


@dataclass
class _Col:
    name: str
    type: str
    length: str = ""
    precision: str = ""
    pattern: str = ""
    key: bool = False

    def sql_type(self) -> tuple[str, str | None]:
        try:
            return talend_type(self.type, self.length, self.precision, self.pattern)
        except TalendExpressionError:
            return "TEXT", None


@dataclass
class _Comp:
    name: str
    component: str
    params: dict[str, str] = field(default_factory=dict)
    tables: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    metadata: dict[str, list[_Col]] = field(default_factory=dict)  # by metadata name
    connectors: dict[str, str] = field(default_factory=dict)  # metadata name -> connector
    data: ET.Element | None = None

    def flag(self, name: str) -> bool:
        return self.params.get(name, "").strip().lower() == "true"

    @property
    def base(self) -> str:
        """A step name: the label the job gives the component, else its unique name."""
        label = (self.params.get("LABEL") or "").strip().strip('"')
        return _slug(label) if label and "__" not in label else _slug(self.name)

    def schema(self, name: str | None = None) -> list[_Col]:
        if name and name in self.metadata:
            return self.metadata[name]
        if self.name in self.metadata:
            return self.metadata[self.name]
        flows = [n for n, c in self.connectors.items() if c in ("FLOW", "UNIQUE", "FILTER")]
        return self.metadata[flows[0]] if flows else []


@dataclass
class _Conn:
    source: str
    target: str
    connector: str
    label: str
    metaname: str
    lookup: bool


@dataclass
class _Stream:
    node: str
    cols: dict[str, str]  # Talend column -> pipeline column
    kinds: dict[str, str | None] = field(default_factory=dict)  # Talend column -> kind
    types: dict[str, str] = field(default_factory=dict)  # Talend column -> SQL type
    keys: set[str] = field(default_factory=set)  # Talend columns the schema marks as key


def _parse(root: ET.Element) -> tuple[list[_Comp], list[_Conn], dict[str, tuple[str, str | None]]]:
    comps = []
    for n in root.iter("node"):
        c = _Comp(name="", component=n.get("componentName") or "")
        for p in n.findall("elementParameter"):
            name = p.get("name") or ""
            if p.get("field") == "TABLE" or p.findall("elementValue"):
                rows: list[dict[str, str]] = []
                for v in p.findall("elementValue"):
                    ref = v.get("elementRef") or ""
                    if not rows or ref in rows[-1]:
                        rows.append({})
                    rows[-1][ref] = v.get("value") or ""
                c.tables[name] = rows
            else:
                c.params[name] = p.get("value") or ""
        c.name = c.params.get("UNIQUE_NAME") or c.component
        for m in n.findall("metadata"):
            cols = [_Col(col.get("name") or "", col.get("type") or "id_String", col.get("length") or "",
                         col.get("precision") or "", (col.get("pattern") or "").strip('"'),
                         (col.get("key") or "").lower() == "true") for col in m.findall("column")]
            mname = m.get("name") or c.name
            c.metadata[mname] = cols
            c.connectors[mname] = m.get("connector") or "FLOW"
        c.data = n.find("nodeData")
        comps.append(c)
    conns = []
    for e in root.iter("connection"):
        label = e.get("label") or ""
        for p in e.findall("elementParameter"):
            if p.get("name") == "UNIQUE_NAME" and p.get("value"):
                label = p.get("value")
        conns.append(_Conn(e.get("source") or "", e.get("target") or "", (e.get("connectorName") or "FLOW").upper(),
                           label, e.get("metaname") or "", e.get("lineStyle") == LOOKUP_STYLE))
    context: dict[str, tuple[str, str | None]] = {}
    ctx = next((c for c in root.findall("context") if c.get("name") == root.get("defaultContext")), None)
    ctx = ctx if ctx is not None else root.find("context")
    for p in ctx.findall("contextParameter") if ctx is not None else []:
        t = p.get("type") or "id_String"
        kind = {"id_Integer": "int", "id_Long": "int", "id_Short": "int", "id_Double": "number", "id_Float": "number",
                "id_BigDecimal": "decimal", "id_Boolean": "bool", "id_Date": "date"}.get(t, "string")
        context[p.get("name") or ""] = (p.get("value") or "", kind)
    return comps, conns, context


class _Importer:
    def __init__(self, job: str, comps: list[_Comp], conns: list[_Conn], context: dict[str, tuple[str, str | None]]):
        self.job = job
        self.comps = {c.name: c for c in comps}
        self.conns = conns
        self.context = context
        self.b = _Builder(None, None)
        self.items: list[ScoreItem] = []
        self.functions: dict[str, int] = {}
        self.variables: dict[str, str] = {}
        self.streams: dict[str, _Stream] = {}  # connection label -> rows on it
        self.truncated: dict[str, ScoreItem] = {}
        self.outputs: list[str] = []
        self.item: ScoreItem | None = None

    def note(self, status: str, message: str) -> None:
        if self.item is not None:
            self.item.flag(status, message)

    def add(self, type_: str, inputs: list[str], params: dict[str, Any], base: str) -> str:
        nid = self.b.add(type_, inputs, params, base)
        if self.item is not None:
            self.item.nodes.append(nid)
        return nid

    def fresh(self, wanted: str, taken: set[str]) -> str:
        name = _slug(wanted)
        candidate, i = name, 2
        while candidate in taken:
            candidate, i = f"{name}_{i}", i + 1
        return candidate

    def data_in(self, comp: _Comp) -> list[_Conn]:
        return [c for c in self.conns if c.target == comp.name and c.connector not in TRIGGERS]

    def data_out(self, comp: _Comp) -> list[_Conn]:
        return [c for c in self.conns if c.source == comp.name and c.connector not in TRIGGERS]

    def string(self, comp: _Comp, param: str) -> str:
        try:
            text, used = java_string(comp.params.get(param, ""), self.context)
        except TalendExpressionError as e:
            raise _Manual(f"{param} {comp.params.get(param)!r} isn't a plain string ({e})") from e
        for var, value in used.items():
            self.variables.setdefault(var, value)
        return text

    def expr(self, label: str, text: str, rows: dict[str, _Stream], qualify: dict[str, str] | None = None
             ) -> tuple[exp.Expression, str | None]:
        """Translate a Java expression over named rows; columns are qualified by node id when asked."""

        def resolve(row: str, col: str):
            s = rows.get(row)
            if s is None or col not in s.cols:
                return None
            q = (qualify or {}).get(row)
            return exp.column(s.cols[col], table=q) if q else exp.column(s.cols[col]), s.kinds.get(col)

        try:
            t = translate_talend(text, resolve, list(rows), self.context)
        except TalendExpressionError as e:
            raise _Manual(f"{label}: could not translate {text!r} ({e})") from e
        for fn in t.functions:
            self.functions[fn] = self.functions.get(fn, 0) + 1
        for n in t.notes:
            self.note(t.status, f"{label}: {n}")
        for var, value in t.variables.items():
            self.variables.setdefault(var, value)
            self.note(CONVERTED, f"context.{_ctx_name(self.context, var)} is the pipeline variable ${{{var}}}, "
                                 f"default {value!r}")
        return t.sql, t.kind

    def stream_of(self, node: str, schema: list[_Col]) -> _Stream:
        cols = self.b.cols.get(node) or []
        by_slug = {c: c for c in cols}
        mapped = {}
        for i, col in enumerate(schema):
            name = by_slug.get(_slug(col.name)) or (cols[i] if i < len(cols) else None)
            if name:
                mapped[col.name] = name
        return _Stream(node, mapped, {c.name: c.sql_type()[1] for c in schema}, {c.name: c.sql_type()[0] for c in schema},
                       {c.name for c in schema if c.key})

    def conform(self, stream: _Stream, schema: list[_Col], label: str) -> _Stream:
        """Rows with exactly the schema's columns, in its order, named after it."""
        missing = [c.name for c in schema if c.name not in stream.cols]
        if missing:
            raise _Manual(f"its schema has columns its input doesn't: {', '.join(missing)}")
        node = stream.node
        wanted = [_slug(c.name) for c in schema]
        renames = {w: stream.cols[c.name] for c, w in zip(schema, wanted) if stream.cols[c.name] != w}
        if renames:
            node = self.add("derive", [node], {"columns": renames}, f"{label}_renamed")
        if (self.b.cols.get(node) or []) != wanted:
            node = self.add("select", [node], {"columns": wanted}, f"{label}_columns")
        return _Stream(node, {c.name: w for c, w in zip(schema, wanted)}, {c.name: stream.kinds.get(c.name) or c.sql_type()[1]
                                                                           for c in schema},
                       {c.name: c.sql_type()[0] for c in schema})

    # ------------------------------------------------------------------ the walk

    def run(self) -> None:
        data_comps = {c.target for c in self.conns if c.connector not in TRIGGERS} | \
                     {c.source for c in self.conns if c.connector not in TRIGGERS}
        order = self.order(data_comps)
        for name in order:
            comp = self.comps.get(name)
            if comp is None:
                continue
            self.item = ScoreItem(comp.name, comp.component)
            self.items.append(self.item)
            handler = getattr(self, "c_" + _handler(comp.component), None)
            try:
                if handler is None:
                    raise _Manual(f"{comp.component} has no translation")
                handler(comp)
            except _Manual as e:
                self.item.flag(MANUAL, f"{e}; a placeholder stands in")
                self.placeholder(comp)
        self.item = None
        for comp in self.comps.values():
            if comp.name not in data_comps:
                self.orchestration(comp)

    def order(self, names: set[str]) -> list[str]:
        deps: dict[str, set[str]] = {n: set() for n in names}
        for c in self.conns:
            if c.connector not in TRIGGERS and c.source in deps and c.target in deps:
                deps[c.target].add(c.source)
        out, done = [], set()
        listed = [c.name for c in self.comps.values() if c.name in names]
        while len(out) < len(listed):
            ready = [n for n in listed if n not in done and deps[n] <= done]
            if not ready:
                raise TalendImportError("the job's row connections form a cycle")
            for n in ready:
                out.append(n)
                done.add(n)
        return out

    def placeholder(self, comp: _Comp) -> None:
        """Typed NULL columns for a component that couldn't be converted, so the pipeline stays whole."""
        ins = [self.streams[c.label] for c in self.data_in(comp) if c.label in self.streams]
        for conn in self.data_out(comp):
            schema = comp.schema(conn.metaname)
            if not schema and ins:
                self.streams[conn.label] = ins[0]
                continue
            names = [_slug(c.name) for c in schema]
            if ins:
                derive = {n: f"CAST(NULL AS {c.sql_type()[0]})" for c, n in zip(schema, names)}
                node = self.add("derive", [ins[0].node], {"columns": derive}, f"{comp.base}_placeholder")
                node = self.add("select", [node], {"columns": names}, f"{comp.base}_placeholder_columns")
            else:
                select = ", ".join(f"CAST(NULL AS {c.sql_type()[0]}) AS {n}" for c, n in zip(schema, names)) or "1 AS x"
                node = self.add("sql", [], {"query": f"SELECT {select} WHERE 1 = 0"}, f"{comp.base}_placeholder")
            self.streams[conn.label] = self.stream_of(node, schema)

    def put(self, comp: _Comp, stream: _Stream, connectors: tuple[str, ...] = ("FLOW", "MAIN", "UNIQUE", "FILTER")) -> None:
        for conn in self.data_out(comp):
            if conn.connector in connectors:
                self.streams[conn.label] = stream

    def one_input(self, comp: _Comp) -> tuple[_Conn, _Stream]:
        ins = [c for c in self.data_in(comp) if c.label in self.streams]
        if len(ins) != 1:
            raise _Manual(f"expected one input row, found {len(ins)}")
        return ins[0], self.streams[ins[0].label]

    # ------------------------------------------------------------------ inputs

    def c_db_input(self, comp: _Comp) -> None:
        schema = comp.schema()
        vendor = re.sub(r"^t|Input$", "", comp.component).lower()
        dialect = _DIALECTS.get(vendor)
        query = self.string(comp, "QUERY").strip() if comp.params.get("QUERY") else ""
        table = self.string(comp, "TABLE") if comp.params.get("TABLE") else ""
        if query:
            node = self.run_sql(query, dialect, comp.base, schema)
            result = self.b.cols.get(node) or []
            if len(result) != len(schema):
                raise _Manual(f"its query returns {len(result)} columns for {len(schema)} in the schema")
            stream = _Stream(node, dict(zip([c.name for c in schema], result)),
                             {c.name: c.sql_type()[1] for c in schema}, {c.name: c.sql_type()[0] for c in schema})
            stream = self.conform(stream, schema, comp.base)
            stream.keys = {c.name for c in schema if c.key}
            self.note(CONVERTED, "the query was imported as steps; its columns map to the schema by position, as in Talend")
        elif table:
            stream = self.table_source(table, schema)
        else:
            raise _Manual("it has neither a query nor a table")
        self.put(comp, stream)

    def table_source(self, table: str, schema: list[_Col]) -> _Stream:
        key = table.split(".")[-1].strip('`"[]')
        if key in self.b.tables:
            node = self.b.tables[key]
        else:
            node = self.add("source", [], {"format": "table", "table": key,
                                           "columns": {_slug(c.name): c.sql_type()[0] for c in schema}}, key)
            self.b.tables[key] = node
        return self.stream_of(node, schema)

    def run_sql(self, sql: str, dialect: str | None, label: str, schema: list[_Col]) -> str:
        if re.search(r"(?<![\w'])\?(?![\w'])", sql) or "${" in sql:
            raise _Manual("its query takes parameters, which Redcliff can't fill; inline them or use a context variable")
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except ParseError as e:
            raise _Manual(f"its query could not be parsed: {str(e).splitlines()[0]}") from e
        for node in tree.find_all(exp.Column, exp.Table):
            for part in ("db", "catalog"):
                if node.args.get(part) is not None:
                    node.set(part, None)
        tree = normalize_identifiers(tree, dialect=dialect)
        tables = [t for t in tree.find_all(exp.Table) if t.name]
        if len(tables) == 1 and tables[0].name not in self.b.tables and isinstance(tree, exp.Select):
            outs = tree.expressions
            if len(outs) == len(schema) and all(isinstance(o, exp.Column) for o in outs):
                declared = self.b.schema.setdefault(tables[0].name, {})
                for o, c in zip(outs, schema):
                    declared[o.name] = c.sql_type()[0]
                for other in tree.find_all(exp.Column):  # filtered on but not selected: type unknown
                    declared.setdefault(other.name, "TEXT")
        node = self.b.convert_query(tree, {}, label)
        if next(n for n in self.b.nodes if n["id"] == node)["type"] == "source":
            # SELECT a, b FROM t became the table itself; the table may have more columns, so name them
            node = self.add("select", [node], {"columns": list(self.b.cols[node])}, f"{label}_columns")
        return node

    def c_file_input(self, comp: _Comp) -> None:
        schema = comp.schema()
        path = self.string(comp, "FILENAME")
        if not path:
            raise _Manual("it has no file name")
        sep = self.string(comp, "FIELDSEPARATOR") if comp.params.get("FIELDSEPARATOR") else ","
        if sep not in (",", ""):
            self.note(REVIEW, f"the file is separated by {sep!r}; Redcliff reads CSV with commas: convert the file "
                              "or read it with a SQL step")
        header = comp.params.get("HEADER", "1").strip('"')
        if header not in ("1", ""):
            self.note(REVIEW, f"the file skips {header} header lines; Redcliff reads one")
        node = self.add("source", [], {"format": "csv", "path": path,
                                       "columns": {_slug(c.name): c.sql_type()[0] for c in schema}},
                        re.sub(r"\.\w+$", "", path.replace("\\", "/").split("/")[-1]) or comp.name)
        self.put(comp, self.stream_of(node, schema))

    # ------------------------------------------------------------------ row steps

    def c_pass(self, comp: _Comp) -> None:
        _conn, stream = self.one_input(comp)
        if comp.component == "tSortRow":
            self.note(CONVERTED, "sorting is dropped: tables have no order; sort when you read them")
        elif comp.component == "tLogRow":
            self.note(CONVERTED, "prints rows to the console; dropped")
        for conn in self.data_out(comp):
            self.streams[conn.label] = stream

    def c_filter_row(self, comp: _Comp) -> None:
        _conn, stream = self.one_input(comp)
        rows = {"input_row": stream}
        parts = []
        for r in comp.tables.get("CONDITIONS", []):
            col = r.get("INPUT_COLUMN", "")
            if not col:
                continue
            source = f"input_row.{col}"
            fn = r.get("FUNCTION", "").strip()
            left = fn.replace("$source", source) if fn else source
            op = r.get("OPERATOR", "==").strip()
            value = r.get("RVALUE", "").strip() or "null"
            if op not in ("==", "!=", "<", "<=", ">", ">="):
                raise _Manual(f"operator {op!r} has no translation")
            if stream.kinds.get(col) == "string" and op in ("==", "!=") and value != "null":
                text = f"{'!' if op == '!=' else ''}{left}.equals({value})"
            else:
                text = f"{left} {op} {value}"
            parts.append(self.expr(f"condition on {col}", text, rows)[0])
        if comp.flag("USE_ADVANCED") and comp.params.get("ADVANCED_COND", "").strip():
            parts.append(self.expr("advanced condition", comp.params["ADVANCED_COND"], rows)[0])
        if not parts:
            raise _Manual("it has no conditions")
        logical = exp.Or if comp.params.get("LOGICAL_OP", "&&").strip() == "||" else exp.And
        cond = parts[0]
        for p in parts[1:]:
            cond = logical(this=_wrap(cond), expression=_wrap(p))
        base = comp.base
        for conn in self.data_out(comp):
            if conn.connector == "REJECT":
                reject = exp.Not(this=exp.Paren(this=exp.Coalesce(this=cond.copy(), expressions=[exp.false()])))
                node = self.add("filter", [stream.node], {"condition": reject.sql()}, f"{base}_reject")
                self.note(CONVERTED, "REJECT is every row the conditions don't pass, including rows where they are NULL")
                self.streams[conn.label] = _Stream(node, dict(stream.cols), dict(stream.kinds), dict(stream.types))
            else:
                node = self.add("filter", [stream.node], {"condition": cond.sql()}, base)
                self.streams[conn.label] = _Stream(node, dict(stream.cols), dict(stream.kinds), dict(stream.types))

    def c_aggregate_row(self, comp: _Comp) -> None:
        _conn, stream = self.one_input(comp)
        schema = comp.schema()
        node = stream.node
        group, renames = [], {}
        for r in comp.tables.get("GROUPBYS", []):
            out, inp = r.get("OUTPUT_COLUMN", ""), r.get("INPUT_COLUMN", "")
            if inp not in stream.cols:
                raise _Manual(f"it groups by {inp}, which its input doesn't have")
            name = _slug(out or inp)
            if name != stream.cols[inp]:
                renames[name] = stream.cols[inp]
            group.append(name)
        if renames:
            node = self.add("derive", [node], {"columns": renames}, f"{comp.base}_keys")
        measures = {}
        for r in comp.tables.get("OPERATIONS", []):
            out, fn, inp = r.get("OUTPUT_COLUMN", ""), r.get("FUNCTION", "").lower(), r.get("INPUT_COLUMN", "")
            if inp not in stream.cols:
                raise _Manual(f"it aggregates {inp}, which its input doesn't have")
            col = stream.cols[inp]
            ignore_null = r.get("IGNORE_NULL", "false").lower() == "true"
            self.functions[f"tAggregateRow {fn}"] = self.functions.get(f"tAggregateRow {fn}", 0) + 1
            if fn == "count":
                sql = f"COUNT({col})" if ignore_null else "COUNT(*)"
            elif fn in ("distinct", "count_distinct", "count(distinct)"):
                sql = f"COUNT(DISTINCT {col})"
            elif fn in ("sum", "avg", "min", "max"):
                sql = f"{fn.upper()}({col})"
            elif fn in ("first", "last"):
                sql = f"ANY_VALUE({col})"
                self.note(REVIEW, f"{out}: {fn} takes the {fn} row Talend reads; rows have no order in SQL, so this "
                                  "takes any row")
            elif fn in ("list", "list_object"):
                sql = f"STRING_AGG(CAST({col} AS TEXT), ',')"
                self.note(REVIEW, f"{out}: the list's order follows the rows Talend reads; SQL doesn't promise one")
            elif fn in ("std_dev", "stddev"):
                sql = f"STDDEV_SAMP({col})"
                self.note(REVIEW, f"{out}: Talend's std_dev is the sample standard deviation here; check yours")
            else:
                raise _Manual(f"aggregate function {fn!r} has no translation")
            measures[_slug(out)] = sql
        node = self.add("aggregate", [node], {"group_by": group, "measures": measures}, comp.base)
        self.put(comp, self.conform(self.stream_of(node, schema), schema, comp.base) if schema
                 else self.stream_of(node, schema))

    def c_uniq_row(self, comp: _Comp) -> None:
        _conn, stream = self.one_input(comp)
        rows = comp.tables.get("UNIQUE_KEY", [])
        keys = [r.get("SCHEMA_COLUMN", "") for r in rows if r.get("KEY_ATTRIBUTE", "").lower() == "true"]
        if not keys or any(k not in stream.cols for k in keys):
            raise _Manual("it has no key columns")
        for r in rows:
            if (r.get("KEY_ATTRIBUTE", "").lower() == "true" and r.get("CASE_SENSITIVE", "").lower() == "false"
                    and stream.kinds.get(r.get("SCHEMA_COLUMN", "")) == "string"):
                self.note(REVIEW, f"Talend compares {r['SCHEMA_COLUMN']} ignoring case; this compares it as it is")
        others = [c for c in stream.cols if c not in keys]
        measures = {stream.cols[c]: f"ANY_VALUE({stream.cols[c]})" for c in others}
        node = self.add("aggregate", [stream.node], {"group_by": [stream.cols[k] for k in keys], "measures": measures},
                        comp.base)
        order = [stream.cols[c] for c in stream.cols]
        if (self.b.cols.get(node) or []) != order:
            node = self.add("select", [node], {"columns": order}, f"{comp.base}_columns")
        if others:
            self.note(REVIEW, "keeps the first row Talend reads for each key; rows have no order in SQL, so this keeps "
                              "any row (the same row when duplicates are identical)")
        unique = _Stream(node, dict(stream.cols), dict(stream.kinds), dict(stream.types))
        for conn in self.data_out(comp):
            if conn.connector == "DUPLICATE":
                raise _Manual("its DUPLICATE flow (the second and later rows per key) has no translation")
            self.streams[conn.label] = unique

    def c_unite(self, comp: _Comp) -> None:
        ins = [c for c in self.data_in(comp) if c.label in self.streams]
        if len(ins) < 2:
            raise _Manual("it needs two or more inputs")
        schema = comp.schema()
        nodes = [self.conform(self.streams[c.label], schema, f"{comp.base}_{_slug(c.label)}").node for c in ins]
        node = self.add("union", nodes, {}, comp.base)
        self.put(comp, self.stream_of(node, schema))

    def c_filter_columns(self, comp: _Comp) -> None:
        _conn, stream = self.one_input(comp)
        self.put(comp, self.conform(stream, comp.schema(), comp.base))

    def c_convert_type(self, comp: _Comp) -> None:
        _conn, stream = self.one_input(comp)
        schema = comp.schema()
        casts = {}
        for c in schema:
            if c.name in stream.cols and stream.types.get(c.name) != c.sql_type()[0]:
                casts[stream.cols[c.name]] = f"CAST({stream.cols[c.name]} AS {c.sql_type()[0]})"
        node = self.add("derive", [stream.node], {"columns": casts}, comp.base) if casts else stream.node
        self.note(REVIEW if casts else CONVERTED, "a value that doesn't convert fails the query; Talend can send it "
                                                  "to a reject flow" if casts else "nothing to convert")
        self.put(comp, self.conform(_Stream(node, dict(stream.cols), dict(stream.kinds), dict(stream.types)), schema,
                                    comp.base))

    # ------------------------------------------------------------------ joins

    def lookup_rows(self, label: str, stream: _Stream, key_cols: list[str], mode: str, base: str) -> _Stream:
        """One row per key for a unique-match lookup, as Talend keeps one."""
        if mode == "ALL_MATCHES" or not key_cols:
            return stream
        if stream.keys and stream.keys == set(key_cols):
            self.note(CONVERTED, f"lookup {label}: one row per key, as its schema's key columns say")
            return stream
        others = [c for c in stream.cols if c not in key_cols]
        if not others:
            node = self.add("aggregate", [stream.node], {"group_by": [stream.cols[k] for k in key_cols]}, f"{base}_unique")
        else:
            node = self.add("aggregate", [stream.node], {"group_by": [stream.cols[k] for k in key_cols], "measures": {
                stream.cols[c]: f"ANY_VALUE({stream.cols[c]})" for c in others}}, f"{base}_unique")
            which = {"UNIQUE_MATCH": "the last", "FIRST_MATCH": "the first", "LAST_MATCH": "the last"}.get(mode, "one")
            self.note(REVIEW, f"lookup {label}: Talend keeps {which} row it loads for each key; this keeps any row "
                              "(the same when the lookup has one row per key)")
        return _Stream(node, dict(stream.cols), dict(stream.kinds), dict(stream.types))

    def no_clash(self, stream: _Stream, taken: set[str], row: str, base: str) -> _Stream:
        """Rename a lookup's columns that the main rows already have, so the join keeps both."""
        clash = {c: n for c, n in stream.cols.items() if n in taken}
        if not clash:
            return stream
        used = set(taken) | set(stream.cols.values())
        renames, cols = {}, dict(stream.cols)
        for c, n in clash.items():
            new = self.fresh(f"{row}_{n}", used)
            used.add(new)
            renames[new] = n
            cols[c] = new
        node = self.add("derive", [stream.node], {"columns": renames}, f"{base}_renamed")
        node = self.add("select", [node], {"columns": list(cols.values())}, f"{base}_columns")
        return _Stream(node, cols, dict(stream.kinds), dict(stream.types))

    def c_map(self, comp: _Comp) -> None:
        data = comp.data
        if data is None:
            raise _Manual("it has no mapping")
        ins = {c.label: c for c in self.data_in(comp) if c.label in self.streams}
        tables = data.findall("inputTables")
        main_conn = next((c for c in ins.values() if not c.lookup), None)
        if main_conn is None or not tables:
            raise _Manual("it has no main input")
        outputs = data.findall("outputTables")
        reject_inner = any((o.get("rejectInnerJoin") or "").lower() == "true" for o in outputs)
        base = comp.base
        main = self.streams[main_conn.label]
        rows: dict[str, _Stream] = {main_conn.label: main}
        node = main.node
        joined: dict[str, _Stream] = {main_conn.label: main}  # row -> its columns in the joined rows
        matched: list[exp.Expression] = []
        for t in tables:
            row = t.get("name") or ""
            if row == main_conn.label:
                continue
            conn = ins.get(row)
            if conn is None:
                raise _Manual(f"lookup {row} has no connection")
            lk = self.streams[row]
            if (t.get("activateExpressionFilter") or "").lower() == "true" and (t.get("expressionFilter") or "").strip():
                cond, _k = self.expr(f"lookup {row} filter", t.get("expressionFilter"), {row: lk})
                lk = _Stream(self.add("filter", [lk.node], {"condition": cond.sql()}, f"{base}_{_slug(row)}_filter"),
                             dict(lk.cols), dict(lk.kinds), dict(lk.types), set(lk.keys))
            keys = [(e.get("name") or "", e.get("expression") or "") for e in t.findall("mapperTableEntries")
                    if (e.get("expression") or "").strip()]
            lk = self.lookup_rows(row, lk, [k for k, _x in keys], (t.get("matchingMode") or "UNIQUE_MATCH").upper(),
                                  f"{base}_{_slug(row)}")
            lk = self.no_clash(lk, set(self.b.cols.get(node) or []), row, f"{base}_{_slug(row)}")
            parts = []
            for key, text in keys:
                if key not in lk.cols:
                    raise _Manual(f"lookup {row} joins on {key}, which it doesn't have")
                left, _k = self.expr(f"join key {row}.{key}", text, joined, {r: node for r in joined})
                parts.append(exp.EQ(this=left, expression=exp.column(lk.cols[key], table=lk.node)))
            inner = (t.get("innerJoin") or "").lower() == "true"
            if not parts:
                self.note(REVIEW, f"lookup {row} has no join keys: every main row meets every lookup row")
                cond = exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(1))
            else:
                cond = parts[0]
                for p in parts[1:]:
                    cond = exp.And(this=cond, expression=p)
            how = "inner" if inner and not reject_inner else "left"
            params = {"condition": cond.sql()} if how == "inner" else {"how": "left", "condition": cond.sql()}
            node = self.add("join", [node, lk.node], params, f"{base}_{_slug(row)}")
            joined[row] = lk
            if inner and reject_inner and parts:
                matched.append(exp.column(lk.cols[keys[0][0]]))
        rows = {r: _Stream(node, dict(s.cols), dict(s.kinds), dict(s.types)) for r, s in joined.items()}

        # Var: derived in order; a var reading an earlier one starts a new step.
        var_table = data.find("varTables")
        vars_stream = _Stream(node, {}, {}, {})
        if var_table is not None:
            batch: dict[str, str] = {}
            taken = set(self.b.cols.get(node) or [])
            for e in var_table.findall("mapperTableEntries"):
                name, text = e.get("name") or "", (e.get("expression") or "").strip()
                if not text:
                    continue
                if batch and re.search(r"\bVar\.", text):
                    node = self.add("derive", [node], {"columns": batch}, f"{base}_var")
                    batch = {}
                    rows = {r: _Stream(node, s.cols, s.kinds, s.types) for r, s in rows.items()}
                    vars_stream = _Stream(node, vars_stream.cols, vars_stream.kinds, vars_stream.types)
                sql, kind = self.expr(f"Var.{name}", text, {**rows, "Var": vars_stream})
                col = self.fresh(name, taken)
                taken.add(col)
                batch[col] = sql.sql()
                vars_stream.cols[name], vars_stream.kinds[name] = col, kind
            if batch:
                node = self.add("derive", [node], {"columns": batch}, f"{base}_var")
            vars_stream = _Stream(node, vars_stream.cols, vars_stream.kinds, vars_stream.types)
            rows = {r: _Stream(node, s.cols, s.kinds, s.types) for r, s in rows.items()}
        rows["Var"] = vars_stream

        # Outputs: a filter (if any), the expressions, the schema's columns.
        conns = {c.metaname: c for c in self.data_out(comp)}
        conds: dict[str, exp.Expression | None] = {}
        for o in outputs:
            name = o.get("name") or ""
            text = (o.get("expressionFilter") or "").strip()
            if (o.get("activateExpressionFilter") or "").lower() == "true" and text:
                conds[name] = self.expr(f"{name} filter", text, rows)[0]
            else:
                conds[name] = None
        for o in outputs:
            name = o.get("name") or ""
            conn = conns.get(name)
            if conn is None:
                continue
            out_base = f"{base}_{_slug(name)}"
            parts = []
            if (o.get("rejectInnerJoin") or "").lower() == "true":
                if not matched:
                    raise _Manual(f"{name} catches inner join rejects, but no lookup is an inner join")
                parts.append(_any([exp.Is(this=c.copy(), expression=exp.null()) for c in matched]))
                self.note(CONVERTED, f"{name}: the main rows an inner join lookup found no match for")
            elif (o.get("reject") or "").lower() == "true":
                others = [c for n, c in conds.items() if n != name and c is not None]
                if others:
                    anyof = others[0].copy()
                    for c in others[1:]:
                        anyof = exp.Or(this=anyof, expression=c.copy())
                    parts.append(exp.Not(this=exp.Paren(this=exp.Coalesce(this=anyof, expressions=[exp.false()]))))
                    self.note(CONVERTED, f"{name}: the rows no other output's filter passes")
                if matched:
                    parts.append(_found(matched))
            else:
                if matched:
                    parts.append(_found(matched))
                if conds.get(name) is not None:
                    parts.append(conds[name].copy())
            src = node
            if parts:
                src = self.add("filter", [node], {"condition": _all(parts).sql()}, f"{out_base}_filter")
            derive, names, kinds = {}, [], {}
            for e in o.findall("mapperTableEntries"):
                col, text = e.get("name") or "", (e.get("expression") or "").strip()
                target = _slug(col)
                names.append(target)
                if not text:
                    derive[target] = f"CAST(NULL AS {_Col(col, e.get('type') or 'id_String').sql_type()[0]})"
                    self.note(CONVERTED, f"{name}.{col} has no expression: NULL, as in Talend")
                    continue
                sql, kinds[col] = self.expr(f"{name}.{col}", text, rows)
                if not (isinstance(sql, exp.Column) and sql.name == target):
                    derive[target] = sql.sql()
            out = src
            if derive:
                out = self.add("derive", [out], {"columns": derive}, f"{out_base}_columns")
            if len(set(names)) != len(names):
                raise _Manual(f"{name} names a column twice")
            out = self.add("select", [out], {"columns": names}, out_base)
            schema = comp.schema(name) or [_Col(e.get("name") or "", e.get("type") or "id_String")
                                           for e in o.findall("mapperTableEntries")]
            stream = self.stream_of(out, schema)
            for c in schema:
                if kinds.get(c.name):
                    stream.kinds[c.name] = kinds[c.name] if kinds[c.name] != "decimal" or stream.kinds.get(c.name) is None \
                        else stream.kinds[c.name]
            self.streams[conn.label] = stream

    def c_join(self, comp: _Comp) -> None:
        ins = [c for c in self.data_in(comp) if c.label in self.streams]
        main_conn = next((c for c in ins if not c.lookup), None)
        lk_conn = next((c for c in ins if c.lookup), None)
        if main_conn is None or lk_conn is None:
            raise _Manual("it needs a main and a lookup input")
        main, lk = self.streams[main_conn.label], self.streams[lk_conn.label]
        keys = [(r.get("INPUT_COLUMN", ""), r.get("LOOKUP_COLUMN", "")) for r in comp.tables.get("JOIN_KEY", [])]
        if not keys or any(a not in main.cols or b not in lk.cols for a, b in keys):
            raise _Manual("its join keys aren't columns of its inputs")
        base = comp.base
        lk = self.no_clash(lk, set(self.b.cols.get(main.node) or []), lk_conn.label, base)
        cond = _all([exp.EQ(this=exp.column(main.cols[a], table=main.node), expression=exp.column(lk.cols[b], table=lk.node))
                     for a, b in keys])
        inner = comp.flag("USE_INNER_JOIN")
        reject = any(c.connector == "REJECT" for c in self.data_out(comp))
        self.note(REVIEW, "when the lookup has several rows for a key, Talend's tJoin keeps one; this join keeps all")
        how = "inner" if inner and not reject else "left"
        params = {"condition": cond.sql()} if how == "inner" else {"how": "left", "condition": cond.sql()}
        node = self.add("join", [main.node, lk.node], params, base)
        hit = exp.Not(this=exp.Is(this=exp.column(lk.cols[keys[0][1]]), expression=exp.null()))
        both = {**{c: n for c, n in lk.cols.items()}, **main.cols}
        kinds = {**lk.kinds, **main.kinds}
        for conn in self.data_out(comp):
            src = node
            if conn.connector == "REJECT":  # the main rows, unless the job declares a reject schema
                src = self.add("filter", [node], {"condition": exp.Not(this=exp.Paren(this=hit)).sql()}, f"{base}_reject")
                schema = comp.metadata.get("REJECT") or [_Col(c, "id_String") for c in main.cols]
            else:
                schema = comp.schema(conn.metaname)
                if inner and reject:
                    src = self.add("filter", [node], {"condition": hit.sql()}, f"{base}_matched")
            stream = _Stream(src, dict(both), dict(kinds), {})
            self.streams[conn.label] = self.conform(stream, schema, f"{base}_{_slug(conn.label)}") if schema else stream

    # ------------------------------------------------------------------ outputs

    def c_db_output(self, comp: _Comp) -> None:
        _conn, stream = self.one_input(comp)
        table = self.string(comp, "TABLE").split(".")[-1].strip('`"[]')
        if not table:
            raise _Manual("it has no table")
        schema = comp.schema() or [_Col(c, stream.types.get(c, "id_String")) for c in stream.cols]
        rows = self.conform(stream, schema, f"{_slug(table)}_rows")
        casts = {rows.cols[c.name]: f"CAST({rows.cols[c.name]} AS {c.sql_type()[0]})" for c in schema
                 if c.sql_type()[0] != "TEXT" and c.sql_type()[1] is not None}
        if casts:
            node = self.add("derive", [rows.node], {"columns": casts}, f"{_slug(table)}_typed")
            rows = _Stream(node, rows.cols, rows.kinds, rows.types)
            self.note(CONVERTED, "columns get the schema's types (decimals rounded to its scale), as the table "
                                 "Talend writes has them")
        table_action = comp.params.get("TABLE_ACTION", "NONE").upper()
        data_action = comp.params.get("DATA_ACTION", "INSERT").upper()
        keys = [_slug(c.name) for c in schema if c.key]
        params: dict[str, Any] = {"format": "table", "table": table}
        if data_action == "INSERT":
            if table_action in ("DROP_CREATE", "DROP_IF_EXISTS_AND_CREATE", "CLEAR", "TRUNCATE"):
                params["mode"] = "overwrite"
                self.note(CONVERTED, f"TABLE_ACTION {table_action}: the table is replaced each run")
            elif table in self.truncated:
                params["mode"] = "overwrite"
                self.truncated[table].flag(CONVERTED, f"empties {table}, which the load then replaces: overwrite covers it")
                self.note(CONVERTED, "an earlier statement empties the table: the load replaces it")
            else:
                params["mode"] = "append"
        elif data_action in ("INSERT_OR_UPDATE", "UPDATE_OR_INSERT", "UPDATE", "INSERT_IGNORE", "INSERT_IF_NOT_EXIST"):
            if not keys:
                raise _Manual(f"DATA_ACTION {data_action} needs key columns in the schema")
            params["mode"] = "insert_new" if data_action in ("INSERT_IGNORE", "INSERT_IF_NOT_EXIST") else "merge"
            params["keys"] = keys
            if data_action == "UPDATE":
                self.note(REVIEW, "UPDATE only changes rows that exist; a merge also inserts the ones that don't")
        else:
            raise _Manual(f"DATA_ACTION {data_action} has no translation")
        if table_action in ("CREATE", "CREATE_IF_NOT_EXISTS", "DROP_CREATE", "DROP_IF_EXISTS_AND_CREATE"):
            self.note(CONVERTED, "the target creates the table when it doesn't exist")
        node = self.add("target", [rows.node], params, f"{_slug(table)}_target" if _slug(table) in
                        {n["id"] for n in self.b.nodes} else _slug(table))
        self.outputs.append(node)

    def c_file_output(self, comp: _Comp) -> None:
        _conn, stream = self.one_input(comp)
        path = self.string(comp, "FILENAME")
        if not path:
            raise _Manual("it has no file name")
        sep = self.string(comp, "FIELDSEPARATOR") if comp.params.get("FIELDSEPARATOR") else ","
        if sep not in (",", ""):
            self.note(REVIEW, f"Talend separates fields with {sep!r}; Redcliff writes CSV with commas")
        if comp.params.get("INCLUDEHEADER") and not comp.flag("INCLUDEHEADER"):
            self.note(REVIEW, "Talend writes no header line; Redcliff's CSV has one")
        schema = comp.schema() or [_Col(c, "id_String") for c in stream.cols]
        name = re.sub(r"\.\w+$", "", path.replace("\\", "/").split("/")[-1]) or comp.name
        rows = self.conform(stream, schema, f"{_slug(name)}_rows")
        mode = "append" if comp.flag("APPEND") else "overwrite"
        node = self.add("target", [rows.node], {"format": "csv", "path": path, "mode": mode}, _slug(name))
        self.outputs.append(node)

    # ------------------------------------------------------------------ orchestration

    def orchestration(self, comp: _Comp) -> None:
        item = ScoreItem(comp.name, comp.component, area="orchestration")
        c = comp.component
        if re.fullmatch(r"t\w*(Connection|Commit|Close|Rollback)", c):
            item.flag(CONVERTED, "manages a database connection; a pipeline doesn't need one")
        elif re.fullmatch(r"t\w*Row", c) and c not in ("tJavaRow", "tLogRow", "tFilterRow", "tSortRow", "tUniqRow",
                                                        "tAggregateRow", "tSampleRow"):
            sql = self.string(comp, "QUERY") if comp.params.get("QUERY") else ""
            m = re.fullmatch(r"\s*(TRUNCATE\s+TABLE|DELETE\s+FROM)\s+([\w`\"\[\]\.]+)\s*;?\s*", sql, re.IGNORECASE)
            if m:
                table = m.group(2).split(".")[-1].strip('`"[]')
                self.truncated[table] = item
                item.flag(REVIEW, f"empties {table}; nothing in the job loads it")
            else:
                item.flag(REVIEW, f"runs SQL on the database, which belongs in your orchestrator: {' '.join(sql.split())}")
        else:
            status, advice = _ADVICE.get(c, (MANUAL, "has no pipeline equivalent"))
            item.flag(status, advice)
        self.items.append(item)


_ADVICE = {
    "tPrejob": (REVIEW, "runs its subjob before the job; recreate that order in your orchestrator"),
    "tPostjob": (REVIEW, "runs its subjob after the job; recreate that order in your orchestrator"),
    "tRunJob": (REVIEW, "runs another job; import that job too and chain them in your orchestrator"),
    "tSendMail": (MANUAL, "sends an email; use your orchestrator's notifications"),
    "tJava": (MANUAL, "runs Java code; rewrite it as a task in your orchestrator"),
    "tJavaFlex": (MANUAL, "runs Java code; rewrite it as a task in your orchestrator"),
    "tGroovy": (MANUAL, "runs Groovy code; rewrite it as a task in your orchestrator"),
    "tWarn": (REVIEW, "logs a warning; use your orchestrator's alerts"),
    "tDie": (REVIEW, "stops the job; use your orchestrator's failure handling"),
    "tSleep": (REVIEW, "waits; recreate it in your orchestrator if it matters"),
    "tChronometerStart": (CONVERTED, "times the job; dropped"),
    "tChronometerStop": (CONVERTED, "times the job; dropped"),
    "tLoop": (MANUAL, "repeats its subjob in a loop; recreate the loop in your orchestrator"),
    "tForeach": (MANUAL, "repeats its subjob for each value; recreate the loop in your orchestrator"),
    "tFileList": (MANUAL, "loops over files; recreate the loop in your orchestrator"),
    "tFileDelete": (MANUAL, "deletes files; use your orchestrator's file tasks"),
    "tFileCopy": (MANUAL, "copies files; use your orchestrator's file tasks"),
    "tFileExist": (MANUAL, "checks for a file; use your orchestrator's sensors"),
    "tFileArchive": (MANUAL, "zips files; use your orchestrator's file tasks"),
    "tLogCatcher": (REVIEW, "collects log messages; use your orchestrator's logging"),
    "tStatCatcher": (REVIEW, "collects run statistics; your orchestrator and OpenLineage run events cover this"),
    "tContextLoad": (REVIEW, "loads context values at run time; pass them as pipeline variables (--var)"),
}

_HANDLERS = {
    "tMap": "map", "tJoin": "join", "tFilterRow": "filter_row", "tAggregateRow": "aggregate_row",
    "tUniqRow": "uniq_row", "tUnite": "unite", "tFilterColumns": "filter_columns", "tConvertType": "convert_type",
    "tReplicate": "pass", "tSortRow": "pass", "tLogRow": "pass", "tFileInputDelimited": "file_input",
    "tFileInputCSV": "file_input", "tFileOutputDelimited": "file_output", "tFileOutputCSV": "file_output",
}


def _handler(component: str) -> str:
    if component in _HANDLERS:
        return _HANDLERS[component]
    if component.endswith("Input") and not component.startswith(("tFile", "tFixedFlow", "tRowGenerator")):
        return "db_input"
    if component.endswith("Output") and not component.startswith(("tFile", "tLog", "tJSON", "tXML")):
        return "db_output"
    return "unknown"


def _wrap(e: exp.Expression) -> exp.Expression:
    return exp.Paren(this=e) if isinstance(e, (exp.Or, exp.And)) else e


def _found(cols: list[exp.Expression]) -> exp.Expression:
    """Every inner-join lookup found its row: its key column isn't NULL."""
    return _all([exp.Not(this=exp.Is(this=c.copy(), expression=exp.null())) for c in cols])


def _any(parts: list[exp.Expression]) -> exp.Expression:
    out = parts[0].copy()
    for p in parts[1:]:
        out = exp.Or(this=out, expression=p.copy())
    return out


def _all(parts: list[exp.Expression]) -> exp.Expression:
    out = _wrap(parts[0].copy())
    for p in parts[1:]:
        out = exp.And(this=out, expression=_wrap(p.copy()))
    return out


def _ctx_name(context: dict[str, tuple[str, str | None]], var: str) -> str:
    return next((name for name in context if variable_name(name) == var), var)


def is_talend(text: str, filename: str = "") -> bool:
    """Does this look like a Talend job (.item) rather than another XML export?"""
    head = text[:4000]
    return filename.lower().endswith(".item") or "talendfile:ProcessType" in head or "TalendFile.xsd" in head


def import_talend(xml: str, name: str | None = None) -> ImportResult:
    """Import a Talend Standard job (the .item XML) into a pipeline with a migration scorecard."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise TalendImportError(f"could not read the XML: {e}") from e
    if not root.tag.endswith("ProcessType"):
        raise TalendImportError("this XML isn't a Talend job (no talendfile:ProcessType root)")
    comps, conns, context = _parse(root)
    if not comps:
        raise TalendImportError("the job has no components")
    job = name or root.get("label") or "talend_job"
    imp = _Importer(job, comps, conns, context)
    imp.run()
    triggers = [f"{c.source} → {c.target} ({_TRIGGER_LABEL.get(c.connector, c.connector)})"
                for c in conns if c.connector in TRIGGERS]
    if triggers:
        item = ScoreItem(job, "Job", area="orchestration")
        item.flag(REVIEW, "Redcliff builds pipelines, not schedules: recreate this run order in your orchestrator: "
                  + "; ".join(triggers))
        imp.items.insert(0, item)
    if not imp.b.nodes:
        raise TalendImportError("nothing in the job could be imported")
    data = {"version": 0, "name": _slug(job)}
    if imp.variables:
        data["variables"] = imp.variables
    data["nodes"] = imp.b.nodes
    pipeline = Pipeline.from_dict(data)
    issues = validate(pipeline)
    if issues:  # a bug in the importer, not in the job
        raise SpecError(issues)
    card = Scorecard("talend", job, imp.items, dict(imp.functions), lineage_coverage(pipeline))
    warnings = [f"{i.name}: {n}" for i in imp.items if i.status != CONVERTED for n in i.notes]
    bad = sorted({c for cols in imp.b.cols.values() if cols for c in cols if not ID_PATTERN.match(c)})
    if bad:
        warnings.append(f"column names that need quoting: {bad}")
    return ImportResult(pipeline, warnings, imp.outputs, card)


_TRIGGER_LABEL = {"SUBJOB_OK": "OnSubjobOk", "SUBJOB_ERROR": "OnSubjobError", "COMPONENT_OK": "OnComponentOk",
                  "COMPONENT_ERROR": "OnComponentError", "RUN_IF": "Run if", "ITERATE": "Iterate"}
