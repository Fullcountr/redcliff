"""Import IBM DataStage parallel jobs (a ``.dsx`` export, or the same export as XML) into a Redcliff pipeline.

An export holds records: the job (``ROOT``, with its parameters), stages, and the pins where links
meet a stage. Each link joins an output pin to an input pin (its ``Partner``) and carries columns,
which a Transformer or Lookup derives with expressions. This importer reads both export formats
into the same records, then walks the stages in link order:

=======================  ==========================================================
DataStage                Redcliff
=======================  ==========================================================
Oracle, DB2, ODBC,       ``source`` for a table, or the SELECT through the SQL
Teradata, Snowflake...   importer; as a target, ``target`` with overwrite, append or
connectors               merge from the write mode and table action
Sequential File          ``source`` / ``target`` (CSV)
Transformer              stage variables and derivations as ``derive``, a
                         ``filter`` per output constraint, the otherwise/reject
                         link, ``select`` of each output's columns
Filter                   a ``filter`` per where clause, and the reject link
Lookup                   one row per key, a ``join`` per reference link; if-not-
                         found continue, drop, reject or fail
Join                     ``join`` on the key columns (inner, left, right, full)
Aggregator               ``aggregate``
Remove Duplicates        one row per key: ``aggregate``
Funnel                   ``union``
Copy                     ``select`` (and renames)
Sort, Peek               nothing: rows pass through
anything else            a placeholder step, flagged for a person to rewrite
=======================  ==========================================================

Expressions go through :mod:`redcliff.datastage_expr`; job parameters become pipeline variables.
Before- and after-job subroutines and sequence jobs are graded as orchestration.

This importer follows IBM's published description of the export and is tested on invented
exports; anything it doesn't recognise becomes a placeholder with the reason, never a silent guess.
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

from redcliff.datastage_expr import (DataStageExpressionError, param_name, sql_type, substitute_params,
                                     translate_datastage)
from redcliff.importer import ImportResult, SQLImportError, _Builder
from redcliff.scorecard import CONVERTED, MANUAL, REVIEW, ScoreItem, Scorecard, lineage_coverage
from redcliff.spec import ID_PATTERN, Pipeline, SpecError, validate

_DIALECTS = {"oracle": "oracle", "db2": None, "odbc": None, "teradata": "teradata", "snowflake": "snowflake",
             "netezza": "postgres", "sqlserver": "tsql", "mssql": "tsql", "greenplum": "postgres",
             "redshift": "redshift", "hive": "hive", "jdbc": None, "bigquery": "bigquery", "informix": None,
             "sybase": "tsql", "postgres": "postgres", "mysql": "mysql"}
_DB = re.compile(r"(?i)(connector|oracle|db2|teradata|netezza|odbc|snowflake|sybase|informix|sqlserver|greenplum|"
                 r"redshift|hive|jdbc|bigquery|postgres|mysql)")
PARAM_KINDS = {"2": "int", "3": "number", "6": "date", "7": "date"}
WRITE_MODES = {"0": "insert", "1": "update", "2": "delete", "3": "insert_then_update", "4": "update_then_insert",
               "5": "delete_then_insert", "6": "insert_new"}
TABLE_ACTIONS = {"0": "append", "1": "create", "2": "replace", "3": "truncate"}


class DataStageImportError(SQLImportError):
    """The file is not a DataStage export, or has nothing to import."""


class _Manual(Exception):
    """This stage can't be converted faithfully; a placeholder stands in."""


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "x"
    s = re.sub(r"_+", "_", s)
    return s if re.match(r"[a-z_]", s[0]) else "_" + s


# --------------------------------------------------------------------------- the records


@dataclass
class _Record:
    identifier: str
    type: str  # the OLE type, e.g. CCustomStage, CTransformerStage, CTrxOutput
    props: dict[str, str] = field(default_factory=dict)
    collections: dict[str, list[dict[str, str]]] = field(default_factory=dict)


@dataclass
class _Job:
    name: str
    records: list[_Record] = field(default_factory=list)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return re.sub(r"\\(.)", r"\1", text[1:-1])
    return text


def parse_dsx(text: str) -> list[_Job]:
    """The jobs in a .dsx (text) export, as records."""
    lines = text.splitlines()
    jobs: list[_Job] = []
    job: _Job | None = None
    record: _Record | None = None
    sub: dict[str, str] | None = None
    last_prop: str | None = None
    skip_depth = 0
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if skip_depth:
            if line.startswith("BEGIN "):
                skip_depth += 1
            elif line.startswith("END "):
                skip_depth -= 1
            continue
        if line == "BEGIN DSJOB":
            job = _Job("")
            continue
        if line == "END DSJOB":
            if job is not None:
                jobs.append(job)
            job = None
            continue
        if job is None:
            if line.startswith("BEGIN ") and line != "BEGIN HEADER":
                skip_depth = 1
            continue
        if line == "BEGIN DSRECORD":
            record = _Record("", "")
            last_prop = None
            continue
        if line == "END DSRECORD":
            if record is not None:
                job.records.append(record)
            record = None
            continue
        if line == "BEGIN DSSUBRECORD":
            sub = {}
            continue
        if line == "END DSSUBRECORD":
            if record is not None and sub is not None:
                record.collections.setdefault(last_prop or "Collection", []).append(sub)
            sub = None
            continue
        if line.startswith("BEGIN "):
            skip_depth = 1
            continue
        m = re.match(r"(\w+)\s+(.*)$", line)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)
        if rest.strip() == "=+=+=+=":
            block = []
            while i < len(lines) and lines[i].strip() != "=+=+=+=":
                block.append(lines[i])
                i += 1
            i += 1
            value = "\n".join(block)
        else:
            value = _unquote(rest)
        target = sub if sub is not None else (record.props if record is not None else None)
        if target is None:
            if key == "Identifier":
                job.name = value
            continue
        if sub is None and record is not None:
            if key == "Identifier":
                record.identifier = value
            elif key == "OLEType":
                record.type = value
            last_prop = key
        target[key] = value
    return jobs


def parse_xml(text: str) -> list[_Job]:
    """The jobs in a DataStage XML export (DSExport), as records."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise DataStageImportError(f"could not read the XML: {e}") from e
    jobs = []
    for j in root.iter("Job"):
        job = _Job(j.get("Identifier") or "")
        for r in j.findall("Record"):
            t = r.get("Type") or ""
            rec = _Record(r.get("Identifier") or "", t if t.startswith("C") else f"C{t}")
            for p in r.findall("Property"):
                rec.props[p.get("Name") or ""] = (p.text or "").strip("\n") if p.text else ""
            for c in r.findall("Collection"):
                rows = []
                for s in c.findall("SubRecord"):
                    rows.append({p.get("Name") or "": (p.text or "") for p in s.findall("Property")})
                rec.collections[c.get("Name") or ""] = rows
            job.records.append(rec)
        jobs.append(job)
    return jobs


def is_datastage(text: str, filename: str = "") -> bool:
    head = text[:4000]
    return filename.lower().endswith(".dsx") or "BEGIN DSJOB" in head or "<DSExport" in head \
        or "IBM InfoSphere DataStage" in head or "DataStage Export" in head


# --------------------------------------------------------------------------- stages and links


@dataclass
class _Col:
    name: str
    type: str
    kind: str | None
    key: bool = False
    derivation: str = ""


def _columns(rec: _Record | None) -> list[_Col]:
    out = []
    for c in (rec.collections.get("Columns", []) if rec else []):
        try:
            t, k = sql_type(c.get("SqlType"), c.get("Precision"), c.get("Scale"))
        except DataStageExpressionError:
            t, k = "TEXT", None
        key = (c.get("KeyPosition") or "0").strip() not in ("", "0")
        out.append(_Col(c.get("Name") or "", t, k, key, (c.get("Derivation") or "").strip()))
    return out


@dataclass
class _Stage:
    rec: _Record
    name: str
    type: str
    inputs: list[_Record]
    outputs: list[_Record]

    def props(self) -> list[tuple[str, str]]:
        return [(p.get("Name") or "", p.get("Value") or "") for p in self.rec.collections.get("Properties", [])]

    def prop(self, *names: str) -> str | None:
        wanted = {n.lower() for n in names}
        return next((v for n, v in self.props() if n.lower() in wanted), None)

    def all(self, name: str) -> list[str]:
        return [v for n, v in self.props() if n.lower() == name.lower()]

    def connector(self) -> dict[str, str]:
        """The connector's XMLProperties, flattened to {tag: text}."""
        blob = self.prop("XMLProperties")
        if not blob:
            return {}
        blob = re.sub(r"^<\?xml[^>]*\?>", "", blob.strip())
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            return {}
        return {el.tag: (el.text or "").strip() for el in root.iter()}


@dataclass
class _Stream:
    node: str
    cols: dict[str, str]  # DataStage column -> pipeline column
    kinds: dict[str, str | None] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    keys: set[str] = field(default_factory=set)


def _link_type(pin: _Record) -> str:
    return (pin.props.get("LinkType") or "1").strip()


class _Importer:
    def __init__(self, job: _Job, b: _Builder, params: dict[str, tuple[str, str | None]], prefix: str):
        self.job = job
        self.b = b
        self.params = params
        self.prefix = prefix
        self.by_id = {r.identifier: r for r in job.records}
        self.items: list[ScoreItem] = []
        self.functions: dict[str, int] = {}
        self.variables: dict[str, str] = {}
        self.streams: dict[str, _Stream] = {}  # output pin id -> rows on its link
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

    def stages(self) -> list[_Stage]:
        out = []
        for r in self.job.records:
            if not r.type.endswith("Stage") or r.type in ("CContainerView",):
                continue
            ins, outs = ([self.by_id[p] for p in (r.props.get(key) or "").split("|") if p in self.by_id]
                         for key in ("InputPins", "OutputPins"))
            stage_type = r.props.get("StageType") or r.type
            out.append(_Stage(r, r.props.get("Name") or r.identifier, stage_type, ins, outs))
        return out

    def source_of(self, pin: _Record) -> str | None:
        """The output pin feeding an input pin."""
        partner = (pin.props.get("Partner") or "").split("|")
        return partner[1] if len(partner) == 2 else None

    def sub(self, text: str) -> str:
        try:
            out, used = substitute_params(text, self.params)
        except DataStageExpressionError as e:
            raise _Manual(str(e)) from e
        for var, value in used.items():
            self.variables.setdefault(var, value)
        return out

    def expr(self, label: str, text: str, links: dict[str, _Stream], bare: dict[str, tuple[str, str | None]] | None = None
             ) -> tuple[exp.Expression, str | None]:
        def resolve(link, col):
            if link is None:
                hit = (bare or {}).get(col)
                return (exp.column(hit[0]), hit[1]) if hit else None
            s = links.get(link)
            if s is None:
                return None
            name = s.cols.get(col) or next((v for k, v in s.cols.items() if k.upper() == col.upper()), None)
            if name is None:
                return None
            kind = s.kinds.get(col) or next((v for k, v in s.kinds.items() if k.upper() == col.upper()), None)
            return exp.column(name), kind

        try:
            t = translate_datastage(text, resolve, self.params)
        except DataStageExpressionError as e:
            raise _Manual(f"{label}: could not translate {text!r} ({e})") from e
        for fn in t.functions:
            self.functions[fn] = self.functions.get(fn, 0) + 1
        for n in t.notes:
            self.note(t.status, f"{label}: {n}")
        for var, value in t.variables.items():
            self.variables.setdefault(var, value)
        return t.sql, t.kind

    def stream_of(self, node: str, cols: list[_Col]) -> _Stream:
        names = self.b.cols.get(node) or []
        by = {n: n for n in names}
        mapped = {}
        for i, c in enumerate(cols):
            name = by.get(_slug(c.name)) or (names[i] if i < len(names) else None)
            if name:
                mapped[c.name] = name
        return _Stream(node, mapped, {c.name: c.kind for c in cols}, {c.name: c.type for c in cols},
                       {c.name for c in cols if c.key})

    def conform(self, stream: _Stream, cols: list[_Col], label: str, cast: bool = False) -> _Stream:
        """Rows with exactly these columns, in this order, named after them (cast to their types if asked)."""
        missing = [c.name for c in cols if c.name not in stream.cols]
        if missing:
            raise _Manual(f"its columns aren't all on its input: {', '.join(missing)}")
        node = stream.node
        wanted = [_slug(c.name) for c in cols]
        derive = {w: stream.cols[c.name] for c, w in zip(cols, wanted) if stream.cols[c.name] != w}
        if cast:
            for c, w in zip(cols, wanted):
                if c.kind is not None and c.type != "TEXT":
                    derive[w] = f"CAST({stream.cols[c.name]} AS {c.type})"
        if derive:
            node = self.add("derive", [node], {"columns": derive}, f"{label}_typed" if cast else f"{label}_renamed")
        if (self.b.cols.get(node) or []) != wanted:
            node = self.add("select", [node], {"columns": wanted}, f"{label}_columns")
        return _Stream(node, {c.name: w for c, w in zip(cols, wanted)}, {c.name: c.kind for c in cols},
                       {c.name: c.type for c in cols}, {c.name for c in cols if c.key})

    def link_cols(self, pin: _Record, fallback: _Stream | None) -> list[_Col]:
        cols = _columns(pin)
        if cols or fallback is None:
            return cols
        return [_Col(c, fallback.types.get(c, "TEXT"), fallback.kinds.get(c), c in fallback.keys) for c in fallback.cols]

    def put(self, pin: _Record, stream: _Stream) -> None:
        self.streams[pin.identifier] = stream

    def ins(self, stage: _Stage) -> list[tuple[_Record, _Stream]]:
        out = []
        for pin in stage.inputs:
            src = self.source_of(pin)
            if src in self.streams:
                out.append((pin, self.streams[src]))
        return out

    def one_input(self, stage: _Stage) -> tuple[_Record, _Stream]:
        ins = self.ins(stage)
        if len(ins) != 1:
            raise _Manual(f"expected one input link, found {len(ins)}")
        return ins[0]

    # ------------------------------------------------------------------ the walk

    def run(self) -> None:
        stages = self.stages()
        pin_stage = {p.identifier: s.name for s in stages for p in s.outputs}
        deps = {s.name: {pin_stage[self.source_of(p)] for p in s.inputs if self.source_of(p) in pin_stage} for s in stages}
        done: set[str] = set()
        order: list[_Stage] = []
        while len(order) < len(stages):
            ready = [s for s in stages if s.name not in done and deps[s.name] <= done]
            if not ready:
                raise DataStageImportError("the job's links form a cycle")
            for s in ready:
                order.append(s)
                done.add(s.name)
        for stage in order:
            self.item = ScoreItem(self.prefix + stage.name, _kind(stage.type))
            self.items.append(self.item)
            try:
                self.dispatch(stage)
            except _Manual as e:
                self.item.flag(MANUAL, f"{e}; a placeholder stands in")
                self.placeholder(stage)
        self.item = None

    def dispatch(self, stage: _Stage) -> None:
        t = stage.type
        if t == "CTransformerStage" or t.lower() in ("pxtransformer", "ctransformerstage"):
            return self.transformer(stage)
        handler = {
            "pxsequentialfile": self.sequential_file, "pxfilter": self.filter, "pxlookup": self.lookup,
            "pxjoin": self.join, "pxaggregator": self.aggregator, "pxremdup": self.remove_duplicates,
            "pxfunnel": self.funnel, "pxcopy": self.copy, "pxsort": self.passthrough, "pxpeek": self.passthrough,
        }.get(t.lower())
        if handler is not None:
            return handler(stage)
        if _DB.search(t):
            return self.database(stage)
        raise _Manual(f"the {t} stage has no translation")

    def placeholder(self, stage: _Stage) -> None:
        ins = self.ins(stage)
        for pin in stage.outputs:
            cols = self.link_cols(pin, ins[0][1] if ins else None)
            names = [_slug(c.name) for c in cols]
            if ins:
                derive = {n: f"CAST(NULL AS {c.type})" for c, n in zip(cols, names)}
                node = self.add("derive", [ins[0][1].node], {"columns": derive}, f"{_slug(stage.name)}_placeholder")
                node = self.add("select", [node], {"columns": names}, f"{_slug(stage.name)}_placeholder_columns")
            else:
                select = ", ".join(f"CAST(NULL AS {c.type}) AS {n}" for c, n in zip(cols, names)) or "1 AS x"
                node = self.add("sql", [], {"query": f"SELECT {select} WHERE 1 = 0"}, f"{_slug(stage.name)}_placeholder")
            self.put(pin, self.stream_of(node, cols))

    # ------------------------------------------------------------------ sources and targets

    def database(self, stage: _Stage) -> None:
        if stage.outputs and stage.inputs:
            raise _Manual("a database stage with both input and output links (a sparse lookup or a write with "
                          "rejects) has no translation")
        props = stage.connector()
        vendor = _DB.search(stage.type).group(1).lower()
        dialect = _DIALECTS.get(vendor.replace("connector", "")) if vendor != "connector" else \
            next((d for k, d in _DIALECTS.items() if k in stage.type.lower()), None)
        table = self.sub(props.get("TableName") or stage.prop("table", "tablename") or "").strip()
        if stage.outputs:
            pin = stage.outputs[0]
            cols = _columns(pin)
            query = self.sub(props.get("SelectStatement") or stage.prop("query", "userquery", "selectstatement") or "")
            generate = (props.get("GenerateSQL") or "").strip().lower() in ("1", "true", "yes")
            if query.strip() and not generate:
                node = self.run_sql(query, dialect, _slug(stage.name), cols)
                result = self.b.cols.get(node) or []
                if len(result) != len(cols):
                    raise _Manual(f"its SELECT returns {len(result)} columns for {len(cols)} on its link")
                stream = _Stream(node, dict(zip([c.name for c in cols], result)), {c.name: c.kind for c in cols},
                                 {c.name: c.type for c in cols})
                stream = self.conform(stream, cols, _slug(stage.name))
                self.note(CONVERTED, "the SELECT was imported as steps; its columns map to the link's by position")
            elif table:
                stream = self.table_source(table, cols)
            else:
                raise _Manual("it reads neither a table nor a SELECT")
            stream.keys = {c.name for c in cols if c.key}
            for out in stage.outputs:
                self.put(out, stream)
            return
        pin, stream = self.one_input(stage)
        if not table:
            raise _Manual("it writes no table")
        cols = self.link_cols(pin, stream)
        rows = self.conform(stream, cols, f"{_slug(table.split('.')[-1])}", cast=True)
        name = table.split(".")[-1].strip('"[]`').lower()
        write = WRITE_MODES.get((props.get("WriteMode") or stage.prop("writemode", "write_mode") or "0").strip(), "?")
        action = TABLE_ACTIONS.get((props.get("TableAction") or stage.prop("tableaction", "table_action") or "0").strip(),
                                   "append")
        keys = [_slug(c.name) for c in cols if c.key]
        params: dict[str, Any] = {"format": "table", "table": name}
        if write == "insert":
            params["mode"] = "overwrite" if action in ("replace", "truncate") else "append"
            if action in ("replace", "truncate"):
                self.note(CONVERTED, f"table action {action}: the table's rows are replaced each run")
        elif write in ("insert_then_update", "update_then_insert", "update", "insert_new"):
            if not keys:
                raise _Manual(f"write mode {write.replace('_', ' ')} needs key columns on its link")
            params["mode"] = "insert_new" if write == "insert_new" else "merge"
            params["keys"] = keys
            if write == "update":
                self.note(REVIEW, "update only changes rows that exist; a merge also inserts the ones that don't")
        else:
            raise _Manual(f"write mode {write.replace('_', ' ')} has no translation")
        self.note(CONVERTED, "columns are cast to the link's types, as the table has them")
        node = self.add("target", [rows.node], params, name if name not in {n["id"] for n in self.b.nodes} else f"{name}_target")
        self.outputs.append(node)

    def table_source(self, table: str, cols: list[_Col]) -> _Stream:
        key = table.split(".")[-1].strip('"[]`').lower()
        if key in self.b.tables:
            node = self.b.tables[key]
        else:
            node = self.add("source", [], {"format": "table", "table": key,
                                           "columns": {_slug(c.name): c.type for c in cols}}, key)
            self.b.tables[key] = node
        return self.stream_of(node, cols)

    def run_sql(self, sql: str, dialect: str | None, label: str, cols: list[_Col]) -> str:
        if re.search(r"(?<![\w'])\?(?![\w'])", sql) or "ORCHESTRATE." in sql.upper():
            raise _Manual("its SQL takes parameters, which Redcliff can't fill")
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except ParseError as e:
            raise _Manual(f"its SQL could not be parsed: {str(e).splitlines()[0]}") from e
        for node in tree.find_all(exp.Column, exp.Table):
            for part in ("db", "catalog"):
                if node.args.get(part) is not None:
                    node.set(part, None)
        tree = normalize_identifiers(tree, dialect="duckdb")  # lower case, as pipeline names are (Oracle would upper-case)
        tables = [t for t in tree.find_all(exp.Table) if t.name]
        if len(tables) == 1 and isinstance(tree, exp.Select) and tables[0].name not in self.b.tables:
            outs = tree.expressions
            if len(outs) == len(cols) and all(isinstance(o, exp.Column) for o in outs):
                schema = self.b.schema.setdefault(tables[0].name, {})
                for o, c in zip(outs, cols):
                    schema[o.name] = c.type
                for other in tree.find_all(exp.Column):  # filtered or joined on but not selected: type unknown
                    schema.setdefault(other.name, "TEXT")
        node = self.b.convert_query(tree, {}, label)
        if next(n for n in self.b.nodes if n["id"] == node)["type"] == "source":
            # SELECT a, b FROM t became the table itself; the table may have more columns, so name them,
            # in a step named for the stage
            node = self.add("select", [node], {"columns": list(self.b.cols[node])}, label)
        return node

    def sequential_file(self, stage: _Stage) -> None:
        path = self.sub(stage.prop("file", "filename", "file_name") or "").strip()
        if not path:
            raise _Manual("it names no file")
        delimiter = stage.prop("delimiter", "delim")
        if delimiter not in (None, ",", "comma"):
            self.note(REVIEW, f"the file is delimited by {delimiter!r}; Redcliff reads and writes CSV with commas")
        header = (stage.prop("firstLineColumnNames", "first_line_is_column_names") or "true").lower()
        if header not in ("true", "1", "yes"):
            self.note(REVIEW, "the file has no header line; Redcliff's CSV has one")
        base = re.sub(r"\.\w+$", "", path.replace("\\", "/").split("/")[-1]) or stage.name
        if stage.outputs:
            cols = _columns(stage.outputs[0])
            node = self.add("source", [], {"format": "csv", "path": path,
                                           "columns": {_slug(c.name): c.type for c in cols}}, _slug(base))
            for out in stage.outputs:
                self.put(out, self.stream_of(node, cols))
            return
        pin, stream = self.one_input(stage)
        cols = self.link_cols(pin, stream)
        rows = self.conform(stream, cols, f"{_slug(base)}_rows")
        mode = (stage.prop("appendOrOverwrite", "file_update_mode", "fileUpdateMode") or "overwrite").lower()
        node = self.add("target", [rows.node], {"format": "csv", "path": path,
                                                "mode": "append" if mode.startswith("append") else "overwrite"},
                        _slug(base))
        self.outputs.append(node)

    # ------------------------------------------------------------------ processing stages

    def passthrough(self, stage: _Stage) -> None:
        _pin, stream = self.one_input(stage)
        if stage.type.lower() == "pxsort":
            unique = (stage.prop("unique", "allowDups") or "").lower()
            if unique in ("true", "1") and stage.prop("unique"):
                return self.remove_duplicates(stage)
            self.note(CONVERTED, "sorting is dropped: tables have no order; sort when you read them")
        else:
            self.note(CONVERTED, "shows rows in the job log; dropped")
        for out in stage.outputs:
            self.put(out, self.narrow(stream, out, stage))

    def narrow(self, stream: _Stream, pin: _Record, stage: _Stage) -> _Stream:
        """An output link with fewer (or renamed, by derivation) columns than its input."""
        cols = _columns(pin)
        if not cols:
            return stream
        renamed = _Stream(stream.node, dict(stream.cols), dict(stream.kinds), dict(stream.types), set(stream.keys))
        for c in cols:
            m = re.fullmatch(r"\s*(?:\w+\.)?(\w+)\s*", c.derivation) if c.derivation else None
            src = m.group(1) if m else c.name
            if c.name not in renamed.cols:
                hit = next((k for k in stream.cols if k.upper() == src.upper()), None)
                if hit is None:
                    raise _Manual(f"{pin.props.get('Name')}.{c.name} isn't a column of its input")
                renamed.cols[c.name] = stream.cols[hit]
                renamed.kinds[c.name] = stream.kinds.get(hit)
        return self.conform(renamed, cols, f"{_slug(stage.name)}_{_slug(pin.props.get('Name') or 'out')}")

    def copy(self, stage: _Stage) -> None:
        _pin, stream = self.one_input(stage)
        for out in stage.outputs:
            self.put(out, self.narrow(stream, out, stage))

    def filter(self, stage: _Stage) -> None:
        _pin, stream = self.one_input(stage)
        wheres: list[tuple[str, int | None]] = []
        props = stage.props()
        for idx, (name, value) in enumerate(props):
            if name.lower() == "where":
                nxt = props[idx + 1] if idx + 1 < len(props) else ("", "")
                target = int(nxt[1]) if nxt[0].lower() == "target" and nxt[1].strip().isdigit() else None
                wheres.append((value, target))
        if not wheres:
            raise _Manual("it has no where clause")
        once = (stage.prop("first", "outputRowsOnlyOnce") or "").lower() in ("true", "1")
        rejects = [p for p in stage.outputs if _link_type(p) == "3"]
        regular = [p for p in stage.outputs if _link_type(p) != "3"]
        conds = [self.where(value, stream) for value, _t in wheres]
        base = _slug(stage.name)
        for i, (value, target) in enumerate(wheres):
            pin_index = target if target is not None else i
            if pin_index >= len(regular):
                raise _Manual(f"where clause {value!r} sends rows to output {pin_index}, which isn't linked")
            cond = conds[i]
            if once and i:
                earlier = _any([c.copy() for c in conds[:i]])
                cond = exp.And(this=exp.Not(this=exp.Paren(this=exp.Coalesce(this=earlier, expressions=[exp.false()]))),
                               expression=_wrap(cond))
                self.note(CONVERTED, "output rows only once: a row takes the first where clause it passes")
            node = self.add("filter", [stream.node], {"condition": cond.sql()},
                            base if len(wheres) == 1 else f"{base}_{_slug(regular[pin_index].props.get('Name') or str(i))}")
            out = _Stream(node, dict(stream.cols), dict(stream.kinds), dict(stream.types), set(stream.keys))
            self.put(regular[pin_index], self.narrow(out, regular[pin_index], stage))
        for pin in rejects:
            none = exp.Not(this=exp.Paren(this=exp.Coalesce(this=_any([c.copy() for c in conds]), expressions=[exp.false()])))
            node = self.add("filter", [stream.node], {"condition": none.sql()}, f"{base}_reject")
            self.note(CONVERTED, "the reject link gets the rows no where clause passes, including NULL results")
            self.put(pin, self.narrow(_Stream(node, dict(stream.cols), dict(stream.kinds), dict(stream.types)), pin, stage))

    def where(self, text: str, stream: _Stream) -> exp.Expression:
        """A Filter where clause (SQL-like) over the input's columns; #Param# becomes ${param}."""
        marked = re.sub(r"#([A-Za-z_][A-Za-z0-9_.$]*)#", lambda m: f"__rcparam_{m.group(1).replace('.', '__')}", text)
        try:
            tree = sqlglot.parse_one(marked)
        except ParseError as e:
            raise _Manual(f"the where clause {text!r} could not be parsed") from e
        for col in list(tree.find_all(exp.Column)):
            if col.name.startswith("__rcparam_"):
                param = col.name[len("__rcparam_"):].replace("__", ".")
                col.replace(exp.Var(this=self.sub(f"#{param}#")))
                continue
            hit = next((k for k in stream.cols if k.upper() == col.name.upper()), None)
            if hit is None:
                raise _Manual(f"the where clause names {col.name}, which its input doesn't have")
            col.replace(exp.column(stream.cols[hit]))
        return tree

    def remove_duplicates(self, stage: _Stage) -> None:
        _pin, stream = self.one_input(stage)
        keys = stage.all("key")
        names = []
        for k in keys:
            hit = next((c for c in stream.cols if c.upper() == k.upper()), None)
            if hit is None:
                raise _Manual(f"its key {k} isn't a column of its input")
            names.append(hit)
        if not names:
            raise _Manual("it has no key")
        others = [c for c in stream.cols if c not in names]
        measures = {stream.cols[c]: f"ANY_VALUE({stream.cols[c]})" for c in others}
        params: dict[str, Any] = {"group_by": [stream.cols[k] for k in names]}
        if measures:
            params["measures"] = measures
        node = self.add("aggregate", [stream.node], params, _slug(stage.name))
        order = [stream.cols[c] for c in stream.cols]
        if (self.b.cols.get(node) or []) != order:
            node = self.add("select", [node], {"columns": order}, f"{_slug(stage.name)}_columns")
        keep = (stage.prop("keep", "duplicateToRetain") or "first").lower()
        if others:
            self.note(REVIEW, f"keeps the {keep} row of each key in sort order; rows have no order in SQL, so this "
                              "keeps any row (the same row when duplicates are identical)")
        out = _Stream(node, dict(stream.cols), dict(stream.kinds), dict(stream.types), set(names))
        for pin in stage.outputs:
            self.put(pin, self.narrow(out, pin, stage))

    def funnel(self, stage: _Stage) -> None:
        ins = self.ins(stage)
        if len(ins) < 2:
            raise _Manual("it needs two or more input links")
        cols = _columns(stage.outputs[0]) if stage.outputs else []
        cols = cols or self.link_cols(ins[0][0], ins[0][1])
        nodes = [self.conform(s, cols, f"{_slug(stage.name)}_{_slug(p.props.get('Name') or str(i))}").node
                 for i, (p, s) in enumerate(ins)]
        node = self.add("union", nodes, {}, _slug(stage.name))
        if (stage.prop("funnelType", "funnel_type") or "").lower() in ("sequence", "sort"):
            self.note(CONVERTED, "the funnel's row order is dropped: tables have no order")
        for pin in stage.outputs:
            self.put(pin, self.stream_of(node, cols))

    def join(self, stage: _Stage) -> None:
        ins = self.ins(stage)
        if len(ins) != 2:
            raise _Manual(f"a join of {len(ins)} links has no translation; split it into joins of two")
        (lp, left), (rp, right) = ins
        keys = stage.all("key")
        if not keys:
            raise _Manual("it has no key")
        operator = (stage.prop("operator", "joinType") or "innerjoin").lower().replace(" ", "")
        how = {"innerjoin": "inner", "inner": "inner", "leftouterjoin": "left", "leftouter": "left",
               "rightouterjoin": "right", "rightouter": "right", "fullouterjoin": "full", "fullouter": "full"}.get(operator)
        if how is None:
            raise _Manual(f"join operator {operator} has no translation")
        base = _slug(stage.name)
        right = self.no_clash(right, set(self.b.cols.get(left.node) or []), keys, base)
        parts = []
        for k in keys:
            lk = next((c for c in left.cols if c.upper() == k.upper()), None)
            rk = next((c for c in right.cols if c.upper() == k.upper()), None)
            if lk is None or rk is None:
                raise _Manual(f"key {k} isn't on both input links")
            parts.append(exp.EQ(this=exp.column(left.cols[lk], table=left.node),
                                expression=exp.column(right.cols[rk], table=right.node)))
        cond = _all(parts)
        if how == "full":
            self.note(REVIEW, "a full outer join keeps the left link's key columns; DataStage fills both sides' keys")
        params = {"condition": cond.sql()} if how == "inner" else {"how": how, "condition": cond.sql()}
        node = self.add("join", [left.node, right.node], params, base)
        both = {**right.cols, **left.cols}
        kinds = {**right.kinds, **left.kinds}
        out = _Stream(node, both, kinds, {**right.types, **left.types})
        for pin in stage.outputs:
            self.put(pin, self.narrow(out, pin, stage))

    def no_clash(self, stream: _Stream, taken: set[str], keys: list[str], base: str) -> _Stream:
        """Rename an input's non-key columns the other side already has (the left side wins, as in DataStage)."""
        keyset = {k.upper() for k in keys}
        clash = {c: n for c, n in stream.cols.items() if n in taken and c.upper() not in keyset}
        if not clash:
            return stream
        drop = set(clash)
        cols = {c: n for c, n in stream.cols.items() if c not in drop}
        node = self.add("select", [stream.node], {"columns": list(cols.values())}, f"{base}_right_columns")
        self.note(CONVERTED, f"{', '.join(sorted(clash))} come from the left link, as in DataStage")
        return _Stream(node, cols, {c: stream.kinds.get(c) for c in cols}, {c: stream.types.get(c, "TEXT") for c in cols},
                       set(stream.keys) - drop)

    def lookup(self, stage: _Stage) -> None:
        ins = self.ins(stage)
        primary = next(((p, s) for p, s in ins if _link_type(p) == "1"), ins[0] if ins else None)
        refs = [(p, s) for p, s in ins if primary is None or p is not primary[0]]
        if primary is None or not refs:
            raise _Manual("it needs a stream link and a reference link")
        ppin, stream = primary
        pname = ppin.props.get("Name") or ""
        base = _slug(stage.name)
        fail = (stage.prop("ifNotFound", "lookupFailure", "LookupFail") or "fail").lower()
        if fail not in ("continue", "drop", "fail", "reject"):
            raise _Manual(f"if-not-found action {fail!r} has no translation")
        rejects = [p for p in stage.outputs if _link_type(p) == "3"]
        links = {pname: stream}
        node = stream.node
        found: list[str] = []
        for rpin, ref in refs:
            rname = rpin.props.get("Name") or ""
            keys = [c for c in _columns(rpin) if c.derivation]
            if not keys:
                raise _Manual(f"reference link {rname} has no key derivations")
            key_names = [next((k for k in ref.cols if k.upper() == c.name.upper()), c.name) for c in keys]
            if any(k not in ref.cols for k in key_names):
                raise _Manual(f"reference link {rname}'s keys aren't its columns")
            if not (ref.keys and {k.upper() for k in ref.keys} == {k.upper() for k in key_names}):
                others = [c for c in ref.cols if c not in key_names]
                measures = {ref.cols[c]: f"ANY_VALUE({ref.cols[c]})" for c in others}
                params: dict[str, Any] = {"group_by": [ref.cols[k] for k in key_names]}
                if measures:
                    params["measures"] = measures
                uniq = self.add("aggregate", [ref.node], params, f"{base}_{_slug(rname)}_unique")
                ref = _Stream(uniq, dict(ref.cols), dict(ref.kinds), dict(ref.types), set(key_names))
                self.note(REVIEW, f"reference {rname}: DataStage returns the first row it finds for a key; this keeps "
                                  "any row (the same when the reference has one row per key)")
            ref = self.no_clash(ref, set(self.b.cols.get(node) or []), [], f"{base}_{_slug(rname)}")
            parts = []
            qualified = {n: _Stream(node, s.cols, s.kinds, s.types) for n, s in links.items()}
            for c, k in zip(keys, key_names):
                left, _kind = self.expr(f"key {rname}.{c.name}", c.derivation, qualified)
                left = left.transform(lambda e, at=node: exp.column(e.name, table=at) if isinstance(e, exp.Column) else e)
                parts.append(exp.EQ(this=left, expression=exp.column(ref.cols[k], table=ref.node)))
            how = "inner" if fail == "drop" else "left"
            params = {"condition": _all(parts).sql()} if how == "inner" else {"how": "left", "condition": _all(parts).sql()}
            node = self.add("join", [node, ref.node], params, f"{base}_{_slug(rname)}")
            links = {n: _Stream(node, s.cols, s.kinds, s.types) for n, s in links.items()}
            links[rname] = _Stream(node, dict(ref.cols), dict(ref.kinds), dict(ref.types))
            found.append(ref.cols[key_names[0]])
        if fail == "fail":
            self.note(REVIEW, "if not found: fail stops the job when a row has no match; this keeps the row with NULLs")
        elif fail == "continue":
            self.note(CONVERTED, "if not found: continue keeps the row with NULLs (a left join)")
        hit = _all([exp.Not(this=exp.Is(this=exp.column(c), expression=exp.null())) for c in found])
        for pin in stage.outputs:
            if pin in rejects:
                if fail != "reject":
                    raise _Manual("its reject link only gets rows when if-not-found is reject")
                missing = _any([exp.Is(this=exp.column(c), expression=exp.null()) for c in found])
                src = self.add("filter", [node], {"condition": missing.sql()}, f"{base}_reject")
                cols = self.link_cols(pin, stream)
                out = _Stream(src, dict(stream.cols), dict(stream.kinds), dict(stream.types))
                self.put(pin, self.conform(out, cols, f"{base}_{_slug(pin.props.get('Name') or 'reject')}"))
                self.note(CONVERTED, "the reject link gets the stream rows with no match")
                continue
            src = self.add("filter", [node], {"condition": hit.sql()}, f"{base}_found") if fail == "reject" else node
            self.put(pin, self.derived(pin, {n: _Stream(src, s.cols, s.kinds, s.types) for n, s in links.items()}, {},
                                       f"{base}_{_slug(pin.props.get('Name') or 'out')}", src))

    def derived(self, pin: _Record, links: dict[str, _Stream], bare: dict[str, tuple[str, str | None]],
                label: str, node: str) -> _Stream:
        """An output link whose columns are derivations over the input links."""
        cols = _columns(pin)
        if not cols:
            raise _Manual(f"output link {pin.props.get('Name')} has no columns")
        derive, names = {}, []
        for c in cols:
            target = _slug(c.name)
            names.append(target)
            text = c.derivation or ""
            if not text:
                only = next(iter(links.values()))
                hit = next((k for k in only.cols if k.upper() == c.name.upper()), None)
                if hit is None:
                    derive[target] = f"CAST(NULL AS {c.type})"
                    self.note(CONVERTED, f"{c.name} has no derivation: NULL")
                    continue
                text = f"{next(iter(links))}.{hit}"
            sql, kind = self.expr(f"{pin.props.get('Name')}.{c.name}", text, links, bare)
            if c.kind is None and kind:
                c.kind = kind
            if not (isinstance(sql, exp.Column) and sql.name == target):
                derive[target] = sql.sql()
        out = node
        if derive:
            out = self.add("derive", [out], {"columns": derive}, f"{label}_columns")
        out = self.add("select", [out], {"columns": names}, label)
        return self.stream_of(out, cols)

    def transformer(self, stage: _Stage) -> None:
        pin, stream = self.one_input(stage)
        name = pin.props.get("Name") or ""
        base = _slug(stage.name)
        node = stream.node
        links = {name: stream}
        bare: dict[str, tuple[str, str | None]] = {}
        stage_vars = stage.rec.collections.get("StageVars", [])
        batch: dict[str, str] = {}
        taken = set(self.b.cols.get(node) or [])
        names = [sv.get("Name") or "" for sv in stage_vars]
        for pos, sv in enumerate(stage_vars):
            sv_name, text = sv.get("Name") or "", (sv.get("Expression") or sv.get("Derivation") or "").strip()
            if not text:
                continue
            later = [n for n in names[pos:] if n and re.search(rf"(?<![\w.]){re.escape(n)}\b", text)]
            if later:
                raise _Manual(f"stage variable {sv_name} reads {', '.join(later)} before it is set this row, so it "
                              "carries a value from the previous row; rewrite it with a window function (LAG)")
            if batch and any(re.search(rf"\b{re.escape(v)}\b", text) for v in bare):
                node = self.add("derive", [node], {"columns": batch}, f"{base}_vars")
                batch = {}
                links = {n: _Stream(node, s.cols, s.kinds, s.types) for n, s in links.items()}
            sql, kind = self.expr(f"stage variable {sv_name}", text, links, bare)
            col = self.fresh(param_name(sv_name), taken)
            taken.add(col)
            batch[col] = sql.sql()
            bare[sv_name] = (col, kind)
        if batch:
            node = self.add("derive", [node], {"columns": batch}, f"{base}_vars")
            links = {n: _Stream(node, s.cols, s.kinds, s.types) for n, s in links.items()}
        conds: dict[str, exp.Expression | None] = {}
        for out in stage.outputs:
            text = (out.props.get("Constraint") or out.props.get("ParsedConstraint") or "").strip()
            conds[out.identifier] = self.expr(f"{out.props.get('Name')} constraint", text, links, bare)[0] if text else None
        for out in stage.outputs:
            label = f"{base}_{_slug(out.props.get('Name') or 'out')}"
            otherwise = (out.props.get("Reject") or out.props.get("Otherwise") or "0").strip() in ("1", "true", "True")
            src = node
            if otherwise:
                others = [c for k, c in conds.items() if k != out.identifier and c is not None]
                if others:
                    none = exp.Not(this=exp.Paren(this=exp.Coalesce(this=_any([c.copy() for c in others]),
                                                                    expressions=[exp.false()])))
                    src = self.add("filter", [node], {"condition": none.sql()}, f"{label}_filter")
                self.note(CONVERTED, f"{out.props.get('Name')} gets the rows no other output's constraint passes")
            elif conds.get(out.identifier) is not None:
                src = self.add("filter", [node], {"condition": conds[out.identifier].sql()}, f"{label}_filter")
            self.put(out, self.derived(out, {n: _Stream(src, s.cols, s.kinds, s.types) for n, s in links.items()},
                                       bare, label, src))

    def aggregator(self, stage: _Stage) -> None:
        _pin, stream = self.one_input(stage)
        keys, measures, column = [], {}, None
        for name, value in stage.props():
            low = name.lower()
            if low == "key":
                hit = next((c for c in stream.cols if c.upper() == value.upper()), None)
                if hit is None:
                    raise _Manual(f"its key {value} isn't a column of its input")
                keys.append(stream.cols[hit])
            elif low in ("reduce", "column", "preserveType"):
                if low == "preservetype":
                    continue
                column = next((c for c in stream.cols if c.upper() == value.upper()), None)
                if column is None:
                    raise _Manual(f"it aggregates {value}, which isn't a column of its input")
            elif low in ("countfield", "count"):
                measures[_slug(value)] = "COUNT(*)"
            elif low in ("sum", "mean", "max", "min", "missingvaluescount", "nonmissingvaluescount", "range",
                         "stddev", "sumofsquares", "variance", "uncorrectedsumofsquares", "css", "coefficientofvariation"):
                if column is None:
                    raise _Manual(f"{name} comes before the column it aggregates")
                col = stream.cols[column]
                fn = {"sum": f"SUM({col})", "mean": f"AVG({col})", "max": f"MAX({col})", "min": f"MIN({col})",
                      "nonmissingvaluescount": f"COUNT({col})", "missingvaluescount": f"COUNT(*) - COUNT({col})",
                      "range": f"MAX({col}) - MIN({col})", "stddev": f"STDDEV_SAMP({col})",
                      "variance": f"VAR_SAMP({col})"}.get(low)
                if fn is None:
                    raise _Manual(f"aggregation {name} has no translation")
                if low in ("stddev", "variance"):
                    self.note(REVIEW, f"{value}: the sample {low}; DataStage can use the population one")
                measures[_slug(value)] = fn
                self.functions[f"Aggregator {name}"] = self.functions.get(f"Aggregator {name}", 0) + 1
        if not measures:
            raise _Manual("it has no aggregations")
        node = self.add("aggregate", [stream.node], {"group_by": keys, "measures": measures}, _slug(stage.name))
        for pin in stage.outputs:
            cols = _columns(pin)
            out = self.stream_of(node, cols) if cols else _Stream(node, {c: c for c in self.b.cols[node]})
            self.put(pin, self.conform(out, cols, f"{_slug(stage.name)}_out") if cols else out)


def _kind(stage_type: str) -> str:
    names = {"CTransformerStage": "Transformer", "PxSequentialFile": "Sequential File", "PxFilter": "Filter",
             "PxLookup": "Lookup", "PxJoin": "Join", "PxAggregator": "Aggregator", "PxRemDup": "Remove Duplicates",
             "PxFunnel": "Funnel", "PxCopy": "Copy", "PxSort": "Sort", "PxPeek": "Peek"}
    return names.get(stage_type, stage_type)


def _wrap(e: exp.Expression) -> exp.Expression:
    return exp.Paren(this=e) if isinstance(e, (exp.Or, exp.And)) else e


def _all(parts: list[exp.Expression]) -> exp.Expression:
    out = _wrap(parts[0].copy())
    for p in parts[1:]:
        out = exp.And(this=out, expression=_wrap(p.copy()))
    return out


def _any(parts: list[exp.Expression]) -> exp.Expression:
    out = parts[0]
    for p in parts[1:]:
        out = exp.Or(this=out, expression=p)
    return out


_SUBROUTINES = {
    "execsh": (MANUAL, "runs a shell command {when} the job: {arg}; move it to your orchestrator"),
    "execdos": (MANUAL, "runs a command {when} the job: {arg}; move it to your orchestrator"),
    "dssendmail": (MANUAL, "sends an email {when} the job; use your orchestrator's notifications"),
    "dswaitforfile": (MANUAL, "waits for a file {when} the job; use your orchestrator's sensors"),
}


def _parameters(root: _Record | None) -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    for p in (root.collections.get("Parameters", []) if root else []):
        name = p.get("Name") or ""
        if name:
            out[name] = (p.get("Default") or "", PARAM_KINDS.get((p.get("ParamType") or "0").strip(), "string"))
    return out


def import_datastage(text: str, name: str | None = None) -> ImportResult:
    """Import the parallel jobs in a DataStage export (.dsx text or XML) into a pipeline with a scorecard."""
    jobs = parse_xml(text) if text.lstrip().startswith("<") else parse_dsx(text)
    if not jobs:
        raise DataStageImportError("this file isn't a DataStage export (no jobs)")
    b = _Builder(None, None)
    items: list[ScoreItem] = []
    functions: dict[str, int] = {}
    variables: dict[str, str] = {}
    outputs: list[str] = []
    parallel = 0
    for job in jobs:
        root = next((r for r in job.records if r.identifier == "ROOT"), None)
        job_name = (root.props.get("Name") if root else None) or job.name or "job"
        job_type = (root.props.get("JobType") if root else "3") or "3"
        prefix = f"{job_name}/" if len(jobs) > 1 else ""
        if job_type == "2":
            item = ScoreItem(job_name, "Sequence job", area="orchestration")
            activities = [r.props.get("Name") for r in job.records if r.type.endswith("JobActivity")]
            item.flag(REVIEW, "runs other jobs in order: recreate it in your orchestrator"
                      + (f" ({', '.join(a for a in activities if a)})" if activities else ""))
            items.append(item)
            continue
        if job_type != "3":
            item = ScoreItem(job_name, "Server job" if job_type == "0" else "Job", area="pipeline")
            item.flag(MANUAL, "only parallel jobs are imported; this is a server or mainframe job")
            items.append(item)
            continue
        parallel += 1
        imp = _Importer(job, b, _parameters(root), prefix)
        imp.run()
        items += imp.items
        for fn, n in imp.functions.items():
            functions[fn] = functions.get(fn, 0) + n
        for var, value in imp.variables.items():
            variables.setdefault(var, value)
        outputs += imp.outputs
        for key, when in (("BeforeSubr", "before"), ("AfterSubr", "after")):
            value = (root.props.get(key) or "").strip() if root else ""
            if not value:
                continue
            routine, _, arg = value.replace("\\\\", "\\").partition("\\")
            item = ScoreItem(f"{prefix}{when}-job subroutine", routine, area="orchestration")
            status, advice = _SUBROUTINES.get(routine.lower(), (MANUAL, "runs routine {routine} {when} the job"))
            item.flag(status, advice.format(when=when, arg=arg.strip() or "(no input)", routine=routine))
            items.append(item)
    if not parallel:
        raise DataStageImportError("the export has no parallel jobs to import")
    if not b.nodes:
        raise DataStageImportError("nothing in the jobs could be imported")
    title = name or (jobs[0].name if len(jobs) == 1 else "datastage_export")
    data = {"version": 0, "name": _slug(title)}
    if variables:
        data["variables"] = variables
    data["nodes"] = b.nodes
    pipeline = Pipeline.from_dict(data)
    issues = validate(pipeline)
    if issues:  # a bug in the importer, not in the job
        raise SpecError(issues)
    card = Scorecard("datastage", title, items, functions, lineage_coverage(pipeline))
    warnings = [f"{i.name}: {n}" for i in items if i.status != CONVERTED for n in i.notes]
    bad = sorted({c for cols in b.cols.values() if cols for c in cols if not ID_PATTERN.match(c)})
    if bad:
        warnings.append(f"column names that need quoting: {bad}")
    return ImportResult(pipeline, warnings, outputs, card)


__all__ = ["DataStageImportError", "import_datastage", "is_datastage", "param_name", "parse_dsx", "parse_xml"]
