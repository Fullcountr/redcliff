"""Import Informatica PowerCenter mappings (repository XML exports) into a Redcliff pipeline.

A PowerCenter export describes a mapping as instances (sources, transformations, targets) wired
port to port by connectors. This importer walks the instances in dependency order and turns
each one into pipeline steps:

=====================  ==========================================================
PowerCenter            Redcliff
=====================  ==========================================================
Source Definition      ``source`` (a table, or a CSV for flat files)
Source Qualifier       the SQL it would run, through the SQL importer: joins,
                       source filter, DISTINCT, or the SQL override itself
Expression             ``derive`` (variable ports are inlined)
Filter                 ``filter``
Aggregator             ``aggregate`` (+ ``derive`` for non-aggregate outputs)
Joiner                 ``join`` (normal, master outer, detail outer, full outer)
Lookup (connected)     ``source`` + ``join``, de-duplicated by the lookup keys
Router                 one ``filter`` per group that is used, DEFAULT included
Union                  ``select`` per input group + ``union``
Sorter                 nothing (order doesn't matter), or ``aggregate`` if distinct
Update Strategy        nothing, or a ``filter`` that drops DD_REJECT rows
Sequence Generator     a ``ROW_NUMBER()`` column, flagged for review
Rank                   ``derive`` with a ``RANK()`` window + ``filter`` on it
Normalizer             ``derive`` + ``select`` per occurrence, then ``union``
Target Definition      ``select`` of the connected columns + ``target``
anything else          a placeholder step, flagged for a person to rewrite
=====================  ==========================================================

Expressions go through :mod:`redcliff.informatica`. Every instance lands in a
:class:`~redcliff.scorecard.Scorecard` with an honest status. A folder export with several
mappings becomes one pipeline: when one mapping reads a table another writes, they connect,
so lineage runs across the whole folder.

Only the standard library's XML parser is used, so this runs in the browser too.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from redcliff.importer import ImportResult, SQLImportError, _Builder
from redcliff.informatica import (
    CONVERTED, MANUAL, REVIEW, ExpressionError, _p, kind_of_type, param_variable, translate,
)
from redcliff.powercenter_workflow import (
    Session, Workflow, flatten_mapplets, parse_folder, parse_param_file, resolve_params, substitute_as_variables,
)
from redcliff.scorecard import ScoreItem, Scorecard, lineage_coverage
from redcliff.spec import ID_PATTERN, Pipeline, SpecError, validate


class PowerCenterImportError(SQLImportError):
    """The XML is not a PowerCenter export, or has nothing to import."""


_DIALECTS = {
    "oracle": "oracle", "teradata": "teradata", "microsoft sql server": "tsql", "sybase": "tsql",
    "netezza": "postgres", "postgresql": "postgres", "mysql": "mysql", "snowflake": "snowflake",
    "redshift": "redshift", "hive": "hive", "bigquery": "bigquery",
}


# --------------------------------------------------------------------------- the XML model


@dataclass
class _Port:
    name: str              # upper case, as PowerCenter matches names case-insensitively
    porttype: str
    datatype: str = ""
    precision: str = ""
    scale: str = ""
    expression: str = ""
    exprtype: str = ""
    group: str = ""
    ref_field: str = ""
    ref_table: str = ""    # source fields: foreign key target table
    ref_column: str = ""
    keytype: str = ""      # target and source fields: PRIMARY KEY, FOREIGN KEY, ...

    @property
    def kind(self) -> str | None:
        return kind_of_type(self.datatype)

    def has(self, word: str) -> bool:
        return word in self.porttype.split("/")


def _ports(el: ET.Element, tag: str) -> list[_Port]:
    out = []
    for f in el.findall(tag):
        out.append(_Port(
            name=(f.get("NAME") or "").upper(),
            porttype=(f.get("PORTTYPE") or "INPUT/OUTPUT").upper().replace(" ", "/") if tag == "TRANSFORMFIELD"
            else "OUTPUT",
            datatype=f.get("DATATYPE") or "",
            precision=f.get("PRECISION") or "",
            scale=f.get("SCALE") or "",
            expression=f.get("EXPRESSION") or "",
            exprtype=(f.get("EXPRESSIONTYPE") or "").upper(),
            group=(f.get("GROUP") or "").upper(),
            ref_field=(f.get("REF_FIELD") or "").upper(),
            ref_table=(f.get("REFERENCEDTABLE") or "").upper(),
            ref_column=(f.get("REFERENCEDFIELD") or "").upper(),
            keytype=(f.get("KEYTYPE") or "").upper(),
        ))
    return out


def _attrs(el: ET.Element) -> dict[str, str]:
    return {(a.get("NAME") or "").upper(): a.get("VALUE") or "" for a in el.findall("TABLEATTRIBUTE")}


def _sql_type(p: _Port) -> str:
    kind = p.kind
    t = p.datatype.lower()
    if kind == "string":
        return "text"
    if kind == "date":
        return "date" if t == "date" else "timestamp"
    scale = int(p.scale) if p.scale.isdigit() else 0
    prec = int(p.precision) if p.precision.isdigit() else 0
    if t in ("double", "float", "real", "binary_double", "binary_float"):
        return "double"
    if scale > 0:
        return f"decimal({prec or 38},{scale})"
    if t in ("integer", "int", "small integer", "smallint", "tinyint"):
        return "int"
    if t == "bigint":
        return "bigint"
    if prec:
        return "int" if prec <= 9 else "bigint" if prec <= 18 else f"decimal({prec},0)"
    return "double"


def _output_name(port: str, existing: list[str]) -> str:
    """An output port's column name: o_FULL_NAME becomes full_name unless that name is taken."""
    slug = _slug(port)
    bare = re.sub(r"^(o|out)_(?=[a-z])", "", slug)
    return bare if bare != slug and bare not in existing else slug


def _label(attribute: str) -> str:
    """A PowerCenter attribute name for people: PRE SQL -> Pre SQL."""
    return re.sub(r"\bSql\b", "SQL", attribute.title())


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "x"
    return s if re.match(r"[a-z_]", s[0]) else "_" + s


# --------------------------------------------------------------------------- column references


@dataclass(frozen=True)
class _Col:
    """A port's value: column ``col`` of pipeline node ``node``."""
    node: str
    col: str


@dataclass(frozen=True)
class _Seq:
    """A Sequence Generator port; becomes a column wherever it is consumed."""
    inst: str
    port: str
    start: int
    step: int


class _Manual(Exception):
    """This instance can't be converted faithfully; a placeholder stands in."""


# --------------------------------------------------------------------------- one mapping


class _MappingConverter:
    def __init__(self, imp: _Importer, mapping: ET.Element, session: Session | None = None,
                 file_params: dict[str, str] | None = None):
        self.imp = imp
        self.session = session
        self.b = imp.b
        self.el = mapping
        self.name = mapping.get("NAME") or "mapping"
        self.instances: dict[str, ET.Element] = {}
        for inst in mapping.findall("INSTANCE"):
            self.instances[(inst.get("NAME") or "").upper()] = inst
        self.local = {(t.get("NAME") or "").upper(): t for t in mapping.findall("TRANSFORMATION")}
        self.incoming: dict[str, dict[str, tuple[str, str]]] = {k: {} for k in self.instances}
        self.used: dict[str, set[str]] = {k: set() for k in self.instances}
        for c in mapping.findall("CONNECTOR"):
            frm, to = (c.get("FROMINSTANCE") or "").upper(), (c.get("TOINSTANCE") or "").upper()
            ff, tf = (c.get("FROMFIELD") or "").upper(), (c.get("TOFIELD") or "").upper()
            if to in self.incoming:
                self.incoming[to][tf] = (frm, ff)
            if frm in self.used:
                self.used[frm].add(ff)
        self.params: dict[str, tuple[str, str | None]] = {}
        kinds: dict[str, str | None] = {}
        for v in mapping.findall("MAPPINGVARIABLE"):
            pname = (v.get("NAME") or "").upper()
            kinds[pname] = kind_of_type(v.get("DATATYPE"))
            default = v.get("DEFAULTVALUE")
            if default is not None and default != "":
                self.params[pname] = (default, kinds[pname])
        self.from_file: set[str] = set()
        for pname, value in (file_params or {}).items():
            if pname.startswith("$$"):
                self.params[pname] = (value, kinds.get(pname))
                self.from_file.add(pname)
        self.env: dict[str, dict[str, Any]] = {}
        self.item: ScoreItem | None = None

    # ------------------------------------------------------------------ ordering

    def order(self) -> list[str]:
        deps: dict[str, set[str]] = {k: set() for k in self.instances}
        for to, ports in self.incoming.items():
            deps[to] |= {frm for frm, _ in ports.values() if frm in self.instances}
        for name, inst in self.instances.items():
            for a in inst.findall("ASSOCIATED_SOURCE_INSTANCE"):
                src = (a.get("NAME") or "").upper()
                if src in self.instances:
                    deps[name].add(src)
        done: list[str] = []
        seen: set[str] = set()

        def visit(n: str, stack: tuple[str, ...]) -> None:
            if n in seen or n in stack:
                return
            for d in sorted(deps[n]):
                visit(d, stack + (n,))
            seen.add(n)
            done.append(n)

        for n in self.instances:
            visit(n, ())
        return done

    # ------------------------------------------------------------------ helpers

    def definition(self, inst: ET.Element) -> ET.Element | None:
        tname = (inst.get("TRANSFORMATION_NAME") or inst.get("NAME") or "").upper()
        kind = inst.get("TYPE", "").upper()
        if kind == "SOURCE":
            return self.imp.sources.get(tname)
        if kind == "TARGET":
            return self.imp.targets.get(tname)
        if inst.get("REUSABLE", "").upper() == "YES" or tname not in self.local:
            found = self.imp.transforms.get(tname)  # elements with no children are falsy: test for None
            return found if found is not None else self.local.get(tname)
        return self.local.get(tname)

    def note(self, status: str, message: str) -> None:
        if self.item is not None:
            self.item.flag(status, message)

    def attrs(self, name: str, tdef: ET.Element | None) -> dict[str, str]:
        """A transformation's attributes: its definition, then the instance, then the session."""
        merged = _attrs(tdef) if tdef is not None else {}
        inst = self.instances.get(name)
        if inst is not None:
            merged.update({k: v for k, v in _attrs(inst).items() if v.strip()})
        overrides = (self.session.attrs.get(name) or {}) if self.session else {}
        if overrides:
            merged.update(overrides)
            shown = ", ".join(_label(k) for k in overrides)
            self.note(CONVERTED, f"session {self.session.label} sets: {shown}")
        return merged

    def subst(self, text: str) -> str:
        """Expand $$PARAMS in SQL text, as PowerCenter does: quoted uses become pipeline variables."""
        values = {k: v for k, (v, _kind) in self.params.items()}
        out, variables, baked, missing = substitute_as_variables(text, values, param_variable)
        self.use_variables(variables)
        for pname in dict.fromkeys(baked):
            where = "the parameter file" if pname in self.from_file else "its default"
            self.note(REVIEW, f"{pname} is fixed at {values[pname]!r} (from {where}): it isn't in quotes, so "
                              "it can't be a variable here; make it one if it changes between runs")
        if missing:
            raise _Manual(f"its SQL uses {', '.join(dict.fromkeys(missing))}, which has no value; "
                          "pass the parameter file")
        return out

    def refs(self, name: str, ports: list[_Port] | None = None) -> dict[str, Any]:
        """Each connected input port of an instance -> the value feeding it."""
        out: dict[str, Any] = {}
        wanted = {p.name for p in ports} if ports is not None else None
        for port, (frm, ff) in self.incoming[name].items():
            if wanted is not None and port not in wanted:
                continue
            upstream = self.env.get(frm)
            if upstream is None or ff not in upstream:
                raise _Manual(f"input {port} comes from {frm}.{ff}, which could not be converted")
            out[port] = upstream[ff]
        return out

    def merge(self, refs: dict[str, Any], label: str) -> tuple[str | None, dict[str, str]]:
        """The one node every input can be read from, and the column each port reads there.

        PowerCenter lets a transformation take ports from several passive transformations of the
        same pipeline. Passive steps are chained one after another here, so the latest step in
        the chain carries every column; this finds it and checks nothing was overwritten.
        """
        cols = {p: r for p, r in refs.items() if isinstance(r, _Col)}
        seqs = {p: r for p, r in refs.items() if isinstance(r, _Seq)}
        if not cols:
            if seqs:
                raise _Manual("reads only from a Sequence Generator")
            return None, {}
        imp = self.imp
        origins = {imp.origin[r.node] for r in cols.values()}
        if len(origins) > 1:
            raise _Manual("its inputs come from different pipelines that only a Joiner or Union can combine")
        base = imp.tip[origins.pop()]
        colmap: dict[str, str] = {}
        for port, r in cols.items():
            if r.col not in (self.b.cols[base] or []) or imp.prov[base].get(r.col) != imp.prov[r.node].get(r.col):
                raise _Manual(f"input {port} needs {r.node}.{r.col}, which a later step replaced")
            colmap[port] = r.col
        if seqs:
            derive = {}
            # Spark requires an ordered window, and a fixed order makes the numbers repeatable.
            order = list(dict.fromkeys(colmap.values())) or list((self.b.cols[base] or [])[:1])
            over = f"ORDER BY {', '.join(order)}" if order else ""
            for port, s in seqs.items():
                col = self.fresh(f"{s.inst}_{s.port}", base)
                n = f"ROW_NUMBER() OVER ({over})"
                if s.step != 1:
                    expr = f"{s.start} + ({n} - 1) * {s.step}"
                elif s.start != 1:
                    expr = f"{s.start - 1} + {n}"
                else:
                    expr = n
                if s.port == "CURRVAL":
                    expr = f"({expr}) + {s.step}"
                derive[col] = expr
                colmap[port] = col
            base = imp.add("derive", [base], {"columns": derive}, f"{label}_seq", "chain")
            for port, s in seqs.items():
                self.note(CONVERTED, f"{port} is numbered with ROW_NUMBER() (see {s.inst})")
        return base, colmap

    def fresh(self, wanted: str, node: str, taken: set[str] | None = None) -> str:
        """A column name not already used in ``node``."""
        existing = set(self.b.cols.get(node) or []) | (taken or set())
        name = _slug(wanted)
        i = 2
        candidate = name
        while candidate in existing:
            candidate, i = f"{name}_{i}", i + 1
        return candidate

    def resolver(self, colmap: dict[str, str], ports: list[_Port], qualifier: dict[str, str] | None = None,
                 extra: dict[str, tuple[exp.Expression, str | None]] | None = None):
        kinds = {p.name: p.kind for p in ports}

        def resolve(name: str):
            if extra and name in extra:
                return extra[name]
            if name in colmap:
                table = (qualifier or {}).get(name)
                return exp.column(colmap[name], table=table), kinds.get(name)
            if name in kinds:  # an input port nothing is connected to: PowerCenter passes NULL
                return exp.null(), kinds[name]
            return None

        return resolve

    def use_variables(self, variables: dict[str, str]) -> None:
        """Record pipeline variables a step uses, with their values for this session."""
        for var, value in variables.items():
            known = self.imp.variables.setdefault(var, value)
            if known != value:
                self.note(REVIEW, f"${{{var}}} is {value!r} for this session but {known!r} for an earlier one; "
                                  f"the spec's default is {known!r}")
            else:
                pname = "$$" + var.upper()
                where = "the parameter file" if pname in self.from_file else "the mapping's default"
                self.note(CONVERTED, f"{pname} is the pipeline variable ${{{var}}}, default {value!r} from {where}")

    def expr(self, port: str, text: str, resolve) -> exp.Expression:
        try:
            t = translate(text, resolve, self.params, as_variables=True)
        except ExpressionError as e:
            self.note(MANUAL, f"{port}: could not read {text!r} ({e}); a NULL placeholder stands in")
            return exp.null()
        for fn in t.functions:
            self.imp.functions[fn] = self.imp.functions.get(fn, 0) + 1
        for n in t.notes:
            if "is the pipeline variable" not in n:
                self.note(t.status, f"{port}: {n}")
        self.use_variables(t.variables)
        return t.sql

    def variables(self, ports: list[_Port], resolve) -> dict[str, tuple[exp.Expression, str | None]]:
        """Variable ports, inlined in evaluation order. Self or forward references are stateful."""
        out: dict[str, tuple[exp.Expression, str | None]] = {}
        variables = [p for p in ports if p.has("VARIABLE")]
        names = [p.name for p in variables]
        for i, p in enumerate(variables):
            later = set(names[i:])

            def scoped(name: str, _later=later):
                if name in _later:
                    raise ExpressionError(f"{name} is read before it is set, so it holds the previous "
                                          "row's value")
                return out.get(name) or resolve(name)

            try:
                t = translate(p.expression or "NULL", scoped, self.params, as_variables=True)
            except ExpressionError as e:
                self.note(MANUAL, f"{p.name}: {e}; a NULL placeholder stands in")
                out[p.name] = (exp.null(), p.kind)
                continue
            for fn in t.functions:
                self.imp.functions[fn] = self.imp.functions.get(fn, 0) + 1
            for n in t.notes:
                if "is the pipeline variable" not in n:
                    self.note(t.status, f"{p.name}: {n}")
            self.use_variables(t.variables)
            out[p.name] = (_p(t.sql), p.kind)
        return out

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        for name in self.order():
            inst = self.instances[name]
            ttype = inst.get("TRANSFORMATION_TYPE") or inst.get("TYPE") or "?"
            item = ScoreItem(self.imp.display.get(name, name), ttype)
            self.item = item
            before = len(self.b.nodes)
            warnings_before = len(self.b.warnings)
            try:
                self.env[name] = self.convert(name, inst, ttype)
            except _Manual as reason:
                item.flag(MANUAL, f"{reason}; a placeholder stands in")
                self.env[name] = self.placeholder(name, inst)
            for w in self.b.warnings[warnings_before:]:
                item.flag(REVIEW, w)
            del self.b.warnings[warnings_before:]
            item.nodes = [n["id"] for n in self.b.nodes[before:]]
            if ttype.lower() not in ("input transformation", "output transformation"):
                self.imp.items.append(item)  # a mapplet's Input/Output steps are plumbing
        self.item = None

    def convert(self, name: str, inst: ET.Element, ttype: str) -> dict[str, Any]:
        kind = (inst.get("TYPE") or "").upper()
        t = ttype.lower()
        if kind == "SOURCE" or t == "source definition":
            return self.source(name, inst)
        if kind == "TARGET" or t == "target definition":
            return self.target(name, inst)
        if t == "mapplet" or kind == "MAPPLET":
            raise _Manual(f"mapplet {inst.get('TRANSFORMATION_NAME') or name} isn't in the export, so it "
                          "can't be expanded")
        tdef = self.definition(inst)
        if tdef is None:
            raise _Manual(f"the definition of {inst.get('TRANSFORMATION_NAME') or name} isn't in the export "
                          "(a shortcut to another folder?)")
        template = (tdef.get("TEMPLATENAME") or "").lower()
        handler = {
            "source qualifier": self.source_qualifier,
            "application source qualifier": self.source_qualifier,
            "expression": self.expression,
            "filter": self.filter,
            "aggregator": self.aggregator,
            "joiner": self.joiner,
            "lookup procedure": self.lookup,
            "router": self.router,
            "union transformation": self.union,
            "sorter": self.sorter,
            "rank": self.rank,
            "normalizer": self.normalizer,
            "update strategy": self.update_strategy,
            "sequence": self.sequence,
            "sequence generator": self.sequence,
            "transaction control": self.passthrough,
            "input transformation": self.passthrough,
            "output transformation": self.passthrough,
        }.get(t) or ({"union transformation": self.union}.get(template))
        if handler is None:
            raise _Manual(f"{ttype} transformations have no Redcliff equivalent yet")
        return handler(name, inst, tdef)

    # ------------------------------------------------------------------ sources and targets

    def source(self, name: str, inst: ET.Element) -> dict[str, Any]:
        sdef = self.definition(inst)
        if sdef is None:
            raise _Manual("its source definition isn't in the export")
        table = _slug(sdef.get("NAME") or name)
        fields = _ports(sdef, "SOURCEFIELD")
        imp = self.imp
        if table in imp.written:  # written by an earlier mapping in this export: connect to it
            node = imp.written[table]
            have = set(self.b.cols[node] or [])
            self.item.flag(CONVERTED, f"reads {table}, which mapping {imp.writer[table]} writes, so the two connect")
            self.b.tables[table] = node
            return {f.name: _Col(node, _slug(f.name)) for f in fields if _slug(f.name) in have}
        if table in self.b.tables:
            node = self.b.tables[table]
        else:
            flat = (sdef.get("DATABASETYPE") or "").lower() == "flat file"
            params: dict[str, Any] = {"format": "csv", "path": f"{table}.csv"} if flat else \
                {"format": "table", "table": table}
            params["columns"] = {_slug(f.name): _sql_type(f) for f in fields}
            node = imp.add("source", [], params, table, "active")
            self.b.tables[table] = node
            if flat:
                self.note(CONVERTED, f"flat file source: point path at the file ({table}.csv is a guess)")
        imp.dialect_of[node] = _DIALECTS.get((sdef.get("DATABASETYPE") or "").lower())
        return {f.name: _Col(node, _slug(f.name)) for f in fields}

    def target(self, name: str, inst: ET.Element) -> dict[str, Any]:
        tdef = self.definition(inst)
        if tdef is None:
            raise _Manual("its target definition isn't in the export")
        table = _slug(tdef.get("NAME") or name)
        fields = _ports(tdef, "TARGETFIELD")
        refs = self.refs(name)
        if not refs:
            raise _Manual("nothing is connected to it")
        base, colmap = self.merge(refs, _slug(name))
        connected = [f for f in fields if f.name in colmap]
        missing = [f.name for f in fields if f.name not in colmap]
        names = [_slug(f.name) for f in connected]
        derive = {_slug(f.name): colmap[f.name] for f in connected if colmap[f.name] != _slug(f.name)}
        node = base
        imp = self.imp
        if derive:
            node = imp.add("derive", [node], {"columns": derive}, f"{_slug(name)}_renamed", "branch")
        if self.b.cols[node] != names:
            node = imp.add("select", [node], {"columns": names}, f"{_slug(name)}_rows", "branch")
        flat = (tdef.get("DATABASETYPE") or "").lower() == "flat file"
        params: dict[str, Any] = {"format": "csv", "path": f"{table}.csv"} if flat else \
            {"format": "table", "table": table}
        a = self.attrs(name, None)
        truncate = a.get("TRUNCATE TARGET TABLE OPTION", "").strip().upper() in ("YES", "1", "TRUE")
        params["mode"] = "overwrite" if truncate else "append"
        upsert = not truncate and not flat and self.upstream(base) & imp.upserts
        if upsert:
            keys = [_slug(f.name) for f in connected if f.keytype == "PRIMARY KEY"]
            if keys:
                params.update({"mode": "merge", "keys": keys})
                self.note(CONVERTED, f"rows are merged on the primary key ({', '.join(keys)}): updated where "
                                     "they exist, inserted where they don't")
            else:
                self.note(REVIEW, "an Update Strategy flags rows for update, but the target has no connected "
                                  "primary key to merge on, so rows are appended; set mode: merge and keys")
        dialect = _DIALECTS.get((tdef.get("DATABASETYPE") or "").lower())
        for key, hook in (("PRE SQL", "pre_sql"), ("POST SQL", "post_sql")):
            statements = self.hooks(self.subst(a.get(key, "")), dialect, _label(key))
            if statements:
                params[hook] = statements
        imp.add("target", [node], params, _slug(name), "branch")
        imp.written[table] = node
        imp.writer[table] = self.name
        imp.outputs.append(node)
        if truncate:
            self.note(CONVERTED, "the session truncates the target, so rows overwrite it")
        elif upsert:
            pass
        elif self.session is None:
            self.note(CONVERTED, "rows are appended, as PowerCenter inserts by default; set mode to overwrite "
                                 "if the session truncates the target (import the session to know)")
        else:
            self.note(CONVERTED, f"rows are appended: session {self.session.label} doesn't truncate the target")
        if missing:
            self.note(CONVERTED, f"{len(missing)} target column(s) aren't connected and get the database "
                                 f"default: {', '.join(missing)}")
        return {}

    def upstream(self, node: str) -> set[str]:
        """``node`` and every node it reads from, so far."""
        inputs = {n["id"]: n["inputs"] for n in self.b.nodes}
        seen, stack = set(), [node]
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack += inputs.get(n, [])
        return seen

    def hooks(self, text: str, dialect: str | None, label: str) -> list[str]:
        """Pre/post SQL as neutral statements. PowerCenter separates statements with ';'."""
        out = []
        for raw in re.split(r"(?<!\\);", text):
            raw = raw.replace("\\;", ";").strip()
            if not raw:
                continue
            try:
                tree = sqlglot.parse_one(raw, read=dialect)
            except ParseError as e:
                self.note(REVIEW, f"{label} couldn't be parsed and was left out: {raw} ({str(e).splitlines()[0]})")
                continue
            if isinstance(tree, exp.Command):
                self.note(REVIEW, f"{label} kept as written; check it runs on the target platform: {raw}")
                out.append(raw)
                continue
            out.append(normalize_identifiers(tree).sql())
            self.note(CONVERTED, f"{label} carried into the target: {out[-1]}")
        return out

    def placeholder(self, name: str, inst: ET.Element) -> dict[str, Any]:
        """Keep the pipeline whole: pass inputs through and give each output port a typed NULL."""
        tdef = self.definition(inst)
        ports = _ports(tdef, "TRANSFORMFIELD") if tdef is not None else []
        used = self.used[name]
        outputs = [p for p in ports if p.has("OUTPUT") and p.name in used] or [_Port(f, "OUTPUT") for f in sorted(used)]
        try:
            refs = {p: r for p, r in self.refs(name).items() if isinstance(r, _Col)}
            base, colmap = self.merge(refs, _slug(name))
        except _Manual:
            base, colmap = None, {}
        env: dict[str, Any] = {}
        nulls: dict[str, str] = {}
        cols_for: dict[str, str] = {}
        for p in outputs:
            if p.name in colmap and p.has("INPUT"):
                env[p.name] = _Col(base, colmap[p.name])
                continue
            col = self.fresh(p.name, base, set(nulls)) if base else _slug(p.name)
            nulls[col] = f"CAST(NULL AS {_sql_type(p).upper()})" if p.datatype else "NULL"
            cols_for[p.name] = col
        if not nulls:
            return env
        if base is not None:
            node = self.imp.add("derive", [base], {"columns": nulls}, _slug(name), "chain")
        else:
            select = ", ".join(f"{v} AS {k}" for k, v in nulls.items())
            node = self.imp.add("sql", [], {"query": f"SELECT {select} WHERE 1 = 0"}, _slug(name), "active")
        env.update({port: _Col(node, col) for port, col in cols_for.items()})
        self.note(MANUAL, f"step {node} holds NULL placeholders for {', '.join(cols_for)}")
        return env

    # ------------------------------------------------------------------ source qualifier

    def source_qualifier(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        attrs = self.attrs(name, tdef)
        refs = self.refs(name)
        imp = self.imp
        # Which source table each connected port reads, by the source instance it is wired from.
        sources: dict[str, str] = {}   # source instance -> table name
        for frm, _ff in self.incoming[name].values():
            if frm in self.instances:
                sdef = self.definition(self.instances[frm])
                if sdef is not None:
                    sources[frm] = _slug(sdef.get("NAME") or frm)
        dialects = {imp.dialect_of.get(r.node) for r in refs.values() if isinstance(r, _Col)}
        dialect = next(iter(dialects)) if len(dialects) == 1 else None

        override = self.subst(attrs.get("SQL QUERY", "").strip())
        for key in ("PRE SQL", "POST SQL"):
            if attrs.get(key, "").strip():
                self.note(REVIEW, f"{_label(key)} runs on the source database in PowerCenter; Redcliff doesn't "
                                  f"run it: {' '.join(attrs[key].split())}")
        if override:
            return self.sq_override(name, ports, override, dialect)

        projections = []
        for p in ports:
            if p.name not in self.incoming[name]:
                continue
            frm, ff = self.incoming[name][p.name]
            table = sources.get(frm)
            if table is None:
                raise _Manual(f"port {p.name} isn't wired from a source")
            col = exp.column(_slug(ff), table=table)
            projections.append(col.as_(_slug(p.name)) if _slug(ff) != _slug(p.name) else col)
        if not projections:
            raise _Manual("no ports are connected")
        tables = list(dict.fromkeys(sources.values()))
        join = self.subst(attrs.get("USER DEFINED JOIN", "").strip())
        where: list[str] = []
        if join.startswith("{") and join.endswith("}"):
            from_sql = join[1:-1]  # PowerCenter's outer join syntax: {A LEFT OUTER JOIN B ON ...}
        else:
            from_sql = ", ".join(tables)
            if join:
                where.append(f"({join})")
            elif len(tables) > 1:
                keys = self.foreign_keys(sources)
                if not keys:
                    raise _Manual("it joins several sources with no user-defined join or foreign keys")
                where.append(" AND ".join(keys))
                self.note(CONVERTED, "joined on the foreign keys in the source definitions, as PowerCenter does")
        source_filter = self.subst(attrs.get("SOURCE FILTER", "").strip())
        if source_filter:
            where.append(f"({source_filter})")
        if len(where) == 1 and where[0].startswith("(") and where[0].endswith(")"):
            where[0] = where[0][1:-1]
        distinct = attrs.get("SELECT DISTINCT", "").upper() in ("YES", "1", "TRUE")
        sql = (f"SELECT {'DISTINCT ' if distinct else ''}"
               + ", ".join(p.sql(dialect=dialect) for p in projections)
               + f" FROM {from_sql}" + (f" WHERE {' AND '.join(where)}" if where else ""))
        sorted_ports = attrs.get("NUMBER OF SORTED PORTS", "0").strip()
        if sorted_ports not in ("", "0"):
            self.note(CONVERTED, "sorted ports dropped: row order doesn't matter to set-based steps")
        node = self.run_sql(sql, dialect, _slug(name))
        return {p.name: _Col(node, _slug(p.name)) for p in ports if p.name in self.incoming[name]
                and _slug(p.name) in (self.b.cols[node] or [])}

    def foreign_keys(self, sources: dict[str, str]) -> list[str]:
        tables = set(sources.values())
        keys = []
        for frm, table in sources.items():
            sdef = self.definition(self.instances[frm])
            for f in _ports(sdef, "SOURCEFIELD"):
                if f.ref_table and _slug(f.ref_table) in tables and f.ref_column:
                    keys.append(f"{table}.{_slug(f.name)} = {_slug(f.ref_table)}.{_slug(f.ref_column)}")
        return keys

    def run_sql(self, sql: str, dialect: str | None, label: str) -> str:
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except ParseError as e:
            raise _Manual(f"its SQL could not be parsed: {str(e).splitlines()[0]}") from e
        for node in tree.find_all(exp.Column, exp.Table):  # SALES.ORDERS -> orders
            for part in ("db", "catalog"):
                if node.args.get(part) is not None:
                    node.set(part, None)
        tree = normalize_identifiers(tree)
        nid = self.b.convert_query(tree, {}, label)
        self.imp.sync()
        return nid

    def sq_override(self, name: str, ports: list[_Port], override: str, dialect: str | None) -> dict[str, Any]:
        node = self.run_sql(override, dialect, _slug(name))
        cols = self.b.cols[node]
        wired = [p for p in ports if p.name in self.used[name]]
        if cols is None:
            raise _Manual("the SQL override's columns can't be known (SELECT * on tables with no definition)")
        if len(cols) == len(ports):
            wired = ports
        if len(cols) != len(wired):
            raise _Manual(f"the SQL override returns {len(cols)} columns for {len(wired)} ports")
        self.note(CONVERTED, "the SQL override was imported as steps; its columns map to ports by position")
        return {p.name: _Col(node, c) for p, c in zip(wired, cols)}

    # ------------------------------------------------------------------ passive steps

    def expression(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        refs = self.refs(name, [p for p in ports if p.has("INPUT")])
        base, colmap = self.merge(refs, _slug(name))
        if base is None:
            raise _Manual("nothing is connected to its inputs")
        resolve = self.resolver(colmap, ports)
        variables = self.variables(ports, resolve)
        resolve = self.resolver(colmap, ports, extra=variables)
        env: dict[str, str] = {}
        derive: dict[str, str] = {}
        passed = {colmap[p.name] for p in ports if p.has("INPUT") and p.has("OUTPUT") and p.name in colmap}
        for p in ports:
            if p.has("VARIABLE"):
                continue
            if p.has("INPUT") and p.has("OUTPUT"):
                if p.name in colmap:
                    env[p.name] = colmap[p.name]
            elif p.has("OUTPUT"):
                # o_EMAIL may replace email when this step only reads email as an input (a cleanup).
                consumed = {colmap[q.name] for q in ports if q.has("INPUT") and not q.has("OUTPUT")
                            and q.name in colmap}
                col = _output_name(p.name, [c for c in self.b.cols[base] or [] if c not in consumed])
                if col in passed or col in derive:  # don't overwrite a column this step passes through
                    col = self.fresh(p.name, base, passed | set(derive))
                derive[col] = self.expr(p.name, p.expression or "NULL", resolve).sql()
                env[p.name] = col
        node = base
        if derive:
            node = self.imp.add("derive", [base], {"columns": derive}, _slug(name), "chain")
        return {port: _Col(node, col) for port, col in env.items()}

    def passthrough(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        refs = self.refs(name, ports)
        self.note(CONVERTED, "passes rows through unchanged")
        return {p.name: refs[p.name] for p in ports if p.name in refs}

    def sequence(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        a = self.attrs(name, tdef)

        def num(key: str, default: int) -> int:
            try:
                return int(float(a.get(key, "") or default))
            except ValueError:
                return default

        start = num("CURRENT VALUE", num("START VALUE", 1))
        step = num("INCREMENT BY", 1)
        self.note(REVIEW, "becomes ROW_NUMBER() where it is used, so numbers restart every run; PowerCenter "
                          "keeps counting across runs. Use a surrogate key strategy in the target.")
        return {p: _Seq(name, p, start, step) for p in ("NEXTVAL", "CURRVAL")}

    # ------------------------------------------------------------------ active steps

    def filter(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        refs = self.refs(name, ports)
        base, colmap = self.merge(refs, _slug(name))
        if base is None:
            raise _Manual("nothing is connected to it")
        condition = self.attrs(name, tdef).get("FILTER CONDITION", "").strip() or "TRUE"
        cond = self.expr("condition", condition, self.resolver(colmap, ports))
        node = base
        if not (isinstance(cond, exp.Boolean) and cond.this):
            node = self.imp.add("filter", [base], {"condition": cond.sql()}, _slug(name), "active")
        else:
            self.note(CONVERTED, "its condition is TRUE, so it keeps every row and needs no step")
        return {p: _Col(node, c) for p, c in colmap.items()}

    def aggregator(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        refs = self.refs(name, [p for p in ports if p.has("INPUT")])
        base, colmap = self.merge(refs, _slug(name))
        if base is None:
            raise _Manual("nothing is connected to its inputs")
        imp = self.imp
        resolve = self.resolver(colmap, ports)
        variables = self.variables(ports, resolve)
        resolve = self.resolver(colmap, ports, extra=variables)
        keys = [p for p in ports if p.exprtype == "GROUPBY"]
        pre: dict[str, str] = {}
        group_cols: dict[str, str] = {}
        for p in keys:
            if p.name in colmap:
                group_cols[p.name] = colmap[p.name]
            else:  # an output port grouped on its expression
                col = self.fresh(p.name, base, set(pre))
                pre[col] = self.expr(p.name, p.expression or "NULL", resolve).sql()
                group_cols[p.name] = col
        if pre:
            base = imp.add("derive", [base], {"columns": pre}, f"{_slug(name)}_keys", "chain")
        measures: dict[str, str] = {}
        post: dict[str, str] = {}
        out_cols: dict[str, str] = {}
        taken = set(group_cols.values())
        group_names = {p.name for p in keys}
        for p in ports:
            if p.name in group_names or p.has("VARIABLE") or not p.has("OUTPUT"):
                continue
            col = _slug(p.name)
            if col in taken:
                col = self.fresh(p.name + "_value", base, taken)
            taken.add(col)
            out_cols[p.name] = col
            if p.has("INPUT"):  # a pass-through port that isn't grouped
                if p.name in colmap:
                    measures[col] = f"ANY_VALUE({colmap[p.name]})"
                    self.note(REVIEW, f"{p.name} isn't grouped or aggregated; PowerCenter returns the last "
                                      "row's value, ANY_VALUE() returns any row's")
                continue
            e = self.expr(p.name, p.expression or "NULL", resolve)
            if any(a.find_ancestor(exp.Window) is None for a in e.find_all(exp.AggFunc)):
                measures[col] = e.sql()
            else:
                post[col] = e.sql()
        params: dict[str, Any] = {}
        if group_cols:
            params["group_by"] = list(dict.fromkeys(group_cols.values()))
        if measures:
            params["measures"] = measures
        if not params:
            raise _Manual("it neither groups nor aggregates anything")
        node = imp.add("aggregate", [base], params, _slug(name), "active")
        if post:
            # Non-aggregate outputs read group keys; rename ports to the group columns they read.
            node = imp.add("derive", [node], {"columns": post}, f"{_slug(name)}_derive", "chain")
        env: dict[str, Any] = {p: _Col(node, c) for p, c in group_cols.items()}
        env.update({p: _Col(node, c) for p, c in out_cols.items() if c in measures or c in post})
        return env

    def joiner(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        master = [p for p in ports if p.has("MASTER")]
        detail = [p for p in ports if not p.has("MASTER")]
        d_base, d_map = self.merge(self.refs(name, detail), f"{_slug(name)}_detail")
        m_base, m_map = self.merge(self.refs(name, master), f"{_slug(name)}_master")
        if d_base is None or m_base is None:
            raise _Manual("one side of the join has nothing connected")
        if d_base == m_base or self.imp.origin[d_base] == self.imp.origin[m_base]:
            raise _Manual("it joins a pipeline with itself (a self-join), which isn't supported yet")
        imp = self.imp
        # Master columns that clash with detail columns would be hidden by the join: rename them.
        detail_cols = set(self.b.cols[d_base] or [])
        rename: dict[str, str] = {}
        for p in master:
            col = m_map.get(p.name)
            if col is not None and col in detail_cols:
                new = self.fresh(p.name if _slug(p.name) != col else p.name + "_master", m_base,
                                 detail_cols | set(rename.values()))
                rename[col] = new
        if rename:
            m_base = imp.add("derive", [m_base], {"columns": {v: k for k, v in rename.items()}},
                             f"{_slug(name)}_master", "chain")
            m_map = {p: rename.get(c, c) for p, c in m_map.items()}
        qualifier = {**{p: d_base for p in d_map}, **{p: m_base for p in m_map}}
        condition = self.attrs(name, tdef).get("JOIN CONDITION", "").strip()
        if not condition:
            raise _Manual("it has no join condition")
        cond = self.expr("condition", condition, self.resolver({**d_map, **m_map}, ports, qualifier))
        how = {"normal join": "inner", "master outer join": "left", "detail outer join": "right",
               "full outer join": "full"}.get(self.attrs(name, tdef).get("JOIN TYPE", "Normal Join").strip().lower(), "inner")
        params = {"condition": cond.sql()} if how == "inner" else {"how": how, "condition": cond.sql()}
        node = imp.add("join", [d_base, m_base], params, _slug(name), "active")
        return {**{p: _Col(node, c) for p, c in d_map.items()}, **{p: _Col(node, c) for p, c in m_map.items()}}

    def lookup(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        a = self.attrs(name, tdef)
        if not self.incoming[name]:
            raise _Manual("it is an unconnected lookup, called from expressions with :LKP")
        if a.get("DYNAMIC LOOKUP CACHE", "").upper() in ("YES", "1"):
            raise _Manual("dynamic lookup caches (insert-else-update) have no equivalent yet")
        inputs = [p for p in ports if p.has("INPUT") and not p.has("LOOKUP")]
        lookups = [p for p in ports if p.has("LOOKUP")]
        returned = [p for p in lookups if p.has("OUTPUT") and p.name in self.used[name]]
        base, colmap = self.merge(self.refs(name, inputs), _slug(name))
        if base is None:
            raise _Manual("nothing is connected to its inputs")
        imp = self.imp
        table = _slug(a.get("LOOKUP TABLE NAME", "") or name)
        lk_dialect = None
        override = self.subst(a.get("LOOKUP SQL OVERRIDE", "").strip())
        if override:
            lk_node = self.run_sql(override, lk_dialect, f"{_slug(name)}_rows")
            lk_cols = self.b.cols[lk_node] or []
            if len(lk_cols) != len(lookups):
                raise _Manual(f"the lookup SQL override returns {len(lk_cols)} columns for {len(lookups)} ports")
            lk_map = {p.name: c for p, c in zip(lookups, lk_cols)}
        else:
            if table in imp.written:
                lk_node = imp.written[table]
            elif table in self.b.tables and set(_slug(p.name) for p in lookups) <= set(self.b.cols[self.b.tables[table]] or []):
                lk_node = self.b.tables[table]
            else:
                lk_node = imp.add("source", [], {"format": "table", "table": table,
                                                 "columns": {_slug(p.name): _sql_type(p) for p in lookups}},
                                  table, "active")
                self.b.tables.setdefault(table, lk_node)
            lk_map = {p.name: _slug(p.name) for p in lookups}
        source_filter = self.subst(a.get("LOOKUP SOURCE FILTER", "").strip())
        if source_filter:
            try:
                f = normalize_identifiers(sqlglot.parse_one(source_filter, read=lk_dialect)).sql()
            except ParseError as e:
                raise _Manual(f"its lookup source filter could not be parsed: {e}") from e
            lk_node = imp.add("filter", [lk_node], {"condition": f}, f"{_slug(name)}_filter", "active")
        condition = a.get("LOOKUP CONDITION", "").strip()
        if not condition:
            raise _Manual("it has no lookup condition")
        qualifier = {**{p.name: base for p in inputs}, **{p.name: "__lookup__" for p in lookups}}
        cond = self.expr("condition", condition, self.resolver({**colmap, **lk_map}, ports, qualifier))
        equal_keys = [c for c in cond.find_all(exp.EQ)]
        only_equality = all(isinstance(n, (exp.EQ, exp.And, exp.Column, exp.Paren)) for n in cond.walk()
                            if isinstance(n, (exp.Binary, exp.Column, exp.Paren)))
        keys = [c.name for eq in equal_keys for c in eq.find_all(exp.Column) if c.table == "__lookup__"]
        policy = a.get("LOOKUP POLICY ON MULTIPLE MATCH", "Use Any Value").strip().lower()
        wanted = [lk_map[p.name] for p in returned]
        active = policy == "return all values"
        if policy in ("use any value", "use first value", "use last value") and only_equality and keys:
            measures = {c: f"ANY_VALUE({c})" for c in wanted if c not in keys}
            params: dict[str, Any] = {"group_by": list(dict.fromkeys(keys))}
            if measures:
                params["measures"] = measures
            lk_node = imp.add("aggregate", [lk_node], params, f"{_slug(name)}_unique", "active")
            if policy != "use any value":
                self.note(REVIEW, f"'{policy.title()}' picks a row by PowerCenter's cache order; "
                                  "here any matching row is used")
            else:
                self.note(CONVERTED, "one row per lookup key, as PowerCenter's 'Use Any Value' returns")
        elif not active:
            if not only_equality:
                self.note(REVIEW, "the lookup condition isn't all '=', so several rows can match "
                                  "and multiply the input rows")
            else:
                self.note(CONVERTED, "assumes the lookup key is unique, as the 'Report Error' policy requires")
        # Returned columns that clash with the pipeline's columns would be hidden: rename them.
        left_cols = set(self.b.cols[base] or [])
        rename: dict[str, str] = {}
        for p in returned:
            col = lk_map[p.name]
            if col in left_cols:
                rename[col] = self.fresh(f"{p.name}_lkp" if _slug(p.name) == col else p.name, lk_node,
                                         left_cols | set(rename.values()))
        if rename:
            lk_node = imp.add("derive", [lk_node], {"columns": {v: k for k, v in rename.items()}},
                              f"{_slug(name)}_renamed", "active")

        def fix(n: exp.Expression) -> exp.Expression:
            if isinstance(n, exp.Column) and n.table == "__lookup__":
                return exp.column(n.name, table=lk_node)
            return n

        cond = cond.transform(fix)
        node = imp.add("join", [base, lk_node], {"how": "left", "condition": cond.sql()}, _slug(name),
                       "active" if active else "chain")
        if active:
            self.note(REVIEW, "'Return All Values' returns every match, so input rows can multiply")
        return {p.name: _Col(node, rename.get(lk_map[p.name], lk_map[p.name])) for p in returned}

    def router(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        groups = [(g.get("NAME", "").upper(), (g.get("TYPE") or "").upper(), g.get("EXPRESSION") or "")
                  for g in tdef.findall("GROUP")]
        inputs = [p for p in ports if p.group in ("", "INPUT") and p.has("INPUT")]
        base, colmap = self.merge(self.refs(name, inputs), _slug(name))
        if base is None:
            raise _Manual("nothing is connected to it")
        resolve = self.resolver(colmap, inputs)
        conditions: dict[str, exp.Expression] = {}
        for g, gtype, text in groups:
            if gtype == "OUTPUT":
                conditions[g] = self.expr(g, text or "TRUE", resolve)
        env: dict[str, Any] = {}
        for g, gtype, _text in groups:
            if "OUTPUT" not in gtype:
                continue
            outs = [p for p in ports if p.group == g]
            if not any(p.name in self.used[name] for p in outs):
                continue  # nothing reads this group
            if "DEFAULT" in gtype:
                parts = [exp.Not(this=exp.Coalesce(this=c, expressions=[exp.false()])) for c in conditions.values()]
                cond = None
                for part in parts:
                    cond = part if cond is None else exp.And(this=cond, expression=part)
            else:
                cond = conditions[g]
            node = base
            if cond is not None:
                node = self.imp.add("filter", [base], {"condition": cond.sql()}, f"{_slug(name)}_{_slug(g)}", "active")
            for p in outs:
                src = p.ref_field or re.sub(r"\d+$", "", p.name)
                if src in colmap:
                    env[p.name] = _Col(node, colmap[src])
        return env

    def union(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        groups = [(g.get("NAME", "").upper(), (g.get("TYPE") or "").upper()) for g in tdef.findall("GROUP")]
        out_group = next((g for g, t in groups if "OUTPUT" in t), None)
        outputs = [p for p in ports if p.group == out_group] if out_group else [p for p in ports if p.has("OUTPUT")]
        names = [_slug(p.name) for p in outputs]
        imp = self.imp
        branches = []
        for g, t in groups:
            if "INPUT" not in t:
                continue
            gports = [p for p in ports if p.group == g]
            refs = self.refs(name, gports)
            if not refs:
                continue
            base, colmap = self.merge(refs, f"{_slug(name)}_{_slug(g)}")
            derive = {}
            for out, p in zip(names, gports):
                src = colmap.get(p.name)
                if src is None:
                    derive[out] = "NULL"
                elif src != out:
                    derive[out] = src
            node = base
            if derive:
                node = imp.add("derive", [node], {"columns": derive}, f"{_slug(name)}_{_slug(g)}", "branch")
            if self.b.cols[node] != names:
                node = imp.add("select", [node], {"columns": names}, f"{_slug(name)}_{_slug(g)}", "branch")
            branches.append(node)
        if len(branches) < 2:
            raise _Manual("fewer than two input groups are connected")
        node = imp.add("union", branches, {}, _slug(name), "active")
        return {p.name: _Col(node, n) for p, n in zip(outputs, names)}

    def sorter(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        refs = self.refs(name, ports)
        a = self.attrs(name, tdef)
        if a.get("DISTINCT", "NO").upper() in ("YES", "1"):
            base, colmap = self.merge(refs, _slug(name))
            if base is None:
                raise _Manual("nothing is connected to it")
            cols = list(dict.fromkeys(colmap.values()))
            node = self.imp.add("aggregate", [base], {"group_by": cols}, _slug(name), "active")
            self.note(CONVERTED, "distinct rows kept; the sort itself isn't needed")
            return {p: _Col(node, c) for p, c in colmap.items()}
        self.note(CONVERTED, "sorting isn't needed: row order doesn't matter to set-based steps")
        return dict(refs)

    def rank(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        """Top or bottom N rows per group: a RANK() window and a filter on it."""
        ports = _ports(tdef, "TRANSFORMFIELD")
        refs = self.refs(name, [p for p in ports if p.has("INPUT")])
        base, colmap = self.merge(refs, _slug(name))
        if base is None:
            raise _Manual("nothing is connected to it")
        a = self.attrs(name, tdef)
        rank_port = next((p for p in ports if p.has("MASTER") or p.has("RANK")), None)  # the port marked R
        if rank_port is None or rank_port.name not in colmap:
            raise _Manual("its rank port isn't connected")
        extra = [p for p in ports if p.has("OUTPUT") and not p.has("INPUT") and p.name != "RANKINDEX"]
        if extra:
            raise _Manual(f"output port(s) {', '.join(p.name for p in extra)} compute values in the rank, "
                          "which isn't supported yet")
        groups = list(dict.fromkeys(colmap[p.name] for p in ports if p.exprtype == "GROUPBY" and p.name in colmap))
        top = a.get("TOP/BOTTOM", "Top").strip().lower() != "bottom"
        try:
            n = int(float(self.subst(a.get("NUMBER OF RANKS", "1")).strip() or 1))
        except ValueError as e:
            raise _Manual(f"its number of ranks isn't a number: {a.get('NUMBER OF RANKS')!r}") from e
        index = self.fresh("rankindex", base)
        order = f"{colmap[rank_port.name]} {'DESC' if top else 'ASC'} NULLS LAST"
        over = (f"PARTITION BY {', '.join(groups)} " if groups else "") + f"ORDER BY {order}"
        node = self.imp.add("derive", [base], {"columns": {index: f"RANK() OVER ({over})"}}, f"{_slug(name)}_index",
                            "chain")
        node = self.imp.add("filter", [node], {"condition": f"{index} <= {n}"}, _slug(name), "active")
        per = f" per {', '.join(groups)}" if groups else ""
        self.note(CONVERTED, f"keeps the {'top' if top else 'bottom'} {n} by {colmap[rank_port.name]}{per}; "
                             "tied rows share a rank index and the next one is skipped, as in PowerCenter; "
                             "NULLs rank last")
        if rank_port.kind == "string":
            self.note(REVIEW, "ranks strings: PowerCenter compares them in the session's sort order, the "
                              "target engine in its own collation")
        env: dict[str, Any] = {p: _Col(node, c) for p, c in colmap.items()}
        env["RANKINDEX"] = _Col(node, index)
        return env

    def normalizer(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        """One row per occurrence: a column that occurs N times (SALES_in1..SALES_inN) becomes N rows."""
        ports = _ports(tdef, "TRANSFORMFIELD")
        refs = self.refs(name, [p for p in ports if p.has("INPUT")])
        base, colmap = self.merge(refs, _slug(name))
        if base is None:
            raise _Manual("nothing is connected to it")
        outputs = [p for p in ports if p.has("OUTPUT")]
        inputs = {p.name for p in ports if p.has("INPUT")}
        occurs: dict[str, list[str]] = {}   # output port -> its input ports, by occurrence
        single: dict[str, str] = {}         # output port -> its one input port
        index_of: dict[str, str] = {}       # GCID_X -> X
        keys: list[str] = []                # GK_X
        for p in outputs:
            if p.name.startswith("GCID_"):
                index_of[p.name] = p.name[5:]
            elif p.name.startswith("GK_"):
                keys.append(p.name)
            elif p.has("INPUT"):
                single[p.name] = p.name
            else:
                numbered = sorted(((int(m.group(2)), q) for q in inputs
                                   if (m := re.fullmatch(rf"{re.escape(p.name)}(_IN)?(\d+)", q))), key=lambda x: x[0])
                if numbered:
                    occurs[p.name] = [q for _, q in numbered]
                elif f"{p.name}_IN" in inputs:
                    single[p.name] = f"{p.name}_IN"
                else:
                    raise _Manual(f"can't tell which input ports feed {p.name}")
        if not occurs:
            raise _Manual("no port occurs more than once, so there's nothing to normalize")
        counts = {len(v) for v in occurs.values()}
        if len(counts) != 1:
            raise _Manual("its multiple-occurring columns occur a different number of times (nested occurs)")
        used_keys = [k for k in keys if k in self.used[name]]
        if used_keys:
            raise _Manual(f"{', '.join(used_keys)} is a generated key that keeps counting across runs; "
                          "rewrite it as a surrogate key in the target")
        imp = self.imp
        names = {p.name: _slug(p.name) for p in outputs if not p.name.startswith("GK_")}
        branches = []
        for i in range(counts.pop()):
            cols: dict[str, str] = {}
            for out, src in single.items():
                cols[names[out]] = colmap.get(src, "NULL")
            for out, srcs in occurs.items():
                cols[names[out]] = colmap.get(srcs[i], "NULL")
            for gcid, out in index_of.items():
                if out in occurs:
                    cols[names[gcid]] = str(i + 1)
            derive = imp.add("derive", [base], {"columns": cols}, f"{_slug(name)}_{i + 1}_values", "branch")
            branches.append(imp.add("select", [derive], {"columns": list(cols)}, f"{_slug(name)}_{i + 1}", "branch"))
        node = imp.add("union", branches, {}, _slug(name), "active")
        n = len(branches)
        names = {p: c for p, c in names.items() if c in (self.b.cols[node] or [])}
        self.note(CONVERTED, f"each input row becomes {n} rows, one per occurrence of "
                             f"{', '.join(o.lower() for o in occurs)}" + (", numbered by " +
                             ", ".join(g.lower() for g in index_of) if index_of else ""))
        return {p: _Col(node, c) for p, c in names.items()}

    def update_strategy(self, name: str, inst: ET.Element, tdef: ET.Element) -> dict[str, Any]:
        ports = _ports(tdef, "TRANSFORMFIELD")
        refs = self.refs(name, ports)
        text = self.attrs(name, tdef).get("UPDATE STRATEGY EXPRESSION", "DD_INSERT").strip() or "DD_INSERT"
        words = set(re.findall(r"[A-Z_]+|\d+", text.upper()))
        if words <= {"DD_INSERT", "0"}:
            self.note(CONVERTED, "every row is an insert, so no step is needed")
            return dict(refs)
        deletes = bool(words & {"DD_DELETE", "2"})
        upserts = bool(words & {"DD_UPDATE", "1"}) and not deletes
        if deletes:
            self.note(REVIEW, "flags rows for delete; a merge target with 'delete_when' can delete them, but "
                              "the flag has to reach the target: set it up by hand")
        base, colmap = self.merge(refs, _slug(name))
        if base is None:
            raise _Manual("nothing is connected to it")
        node = base
        if words & {"DD_REJECT", "3"}:
            flag = self.expr("expression", text, self.resolver(colmap, ports))
            cond = _keep_unless_rejected(flag)
            node = self.imp.add("filter", [base], {"condition": cond.sql()}, _slug(name), "active")
            self.note(CONVERTED, "rows flagged DD_REJECT are filtered out")
        if upserts:
            self.imp.upserts.add(node)
            self.note(CONVERTED, "rows flagged for insert or update become a merge into the target on its "
                                 "primary key (an upsert)")
        return {p: _Col(node, c) for p, c in colmap.items()}


def _keep_unless_rejected(flag: exp.Expression) -> exp.Expression:
    """The rows an update strategy doesn't reject. IIF(c, DD_REJECT, DD_INSERT) reads as NOT c."""
    ifs = flag.args.get("ifs") or [] if isinstance(flag, exp.Case) else []
    if len(ifs) == 1 and not flag.args.get("this"):
        then, other = ifs[0].args.get("true"), flag.args.get("default")
        cond = ifs[0].this
        nums = [x.this if isinstance(x, exp.Literal) and not x.is_string else None for x in (then, other)]
        if nums == ["3", "0"]:  # rejected when cond is TRUE; NULL falls to the ELSE (insert)
            return exp.Not(this=exp.Coalesce(this=cond, expressions=[exp.false()]))
        if nums == ["0", "3"]:
            return exp.Coalesce(this=cond, expressions=[exp.false()])
    return exp.NEQ(this=_p(flag), expression=exp.Literal.number(3))


# --------------------------------------------------------------------------- the whole export


class _Importer:
    def __init__(self):
        self.b = _Builder(None, None)
        self.sources: dict[str, ET.Element] = {}
        self.targets: dict[str, ET.Element] = {}
        self.transforms: dict[str, ET.Element] = {}
        self.items: list[ScoreItem] = []
        self.functions: dict[str, int] = {}
        self.written: dict[str, str] = {}   # table -> node holding the rows written to it
        self.writer: dict[str, str] = {}    # table -> mapping name
        self.outputs: list[str] = []
        self.origin: dict[str, str] = {}
        self.tip: dict[str, str] = {}
        self.prov: dict[str, dict[str, str]] = {}
        self.dialect_of: dict[str, str | None] = {}
        self.display: dict[str, str] = {}  # flattened mapplet instance -> MAPPLET/INSTANCE
        self.variables: dict[str, str] = {}  # pipeline variables from $$PARAMS -> default value
        self.upserts: set[str] = set()       # nodes whose rows an Update Strategy flags insert-or-update
        self.synced = 0

    def sync(self) -> None:
        """Track provenance for nodes added since the last call (each is its own pipeline)."""
        for node in self.b.nodes[self.synced:]:
            nid, t, inputs = node["id"], node["type"], node["inputs"]
            cols = self.b.cols.get(nid) or []
            if t in ("filter", "select"):
                self.prov[nid] = {c: self.prov[inputs[0]].get(c, nid) for c in cols}
            elif t == "derive":
                new = node["params"]["columns"]
                self.prov[nid] = {c: nid if c in new else self.prov[inputs[0]].get(c, nid) for c in cols}
            elif t == "join":
                left, right = self.prov[inputs[0]], self.prov[inputs[1]]
                self.prov[nid] = {c: left.get(c) or right.get(c) or nid for c in cols}
            else:
                self.prov[nid] = {c: nid for c in cols}
            self.origin.setdefault(nid, nid)
            self.tip.setdefault(nid, nid)
        self.synced = len(self.b.nodes)

    def add(self, type_: str, inputs: list[str], params: dict[str, Any], base: str, mode: str) -> str:
        """mode: 'active' starts a new row set; 'chain' extends one (and becomes its tip);
        'branch' reads from one without being part of it (a target's column selection)."""
        nid = self.b.add(type_, inputs, params, base)
        self.sync()
        if mode in ("chain", "branch"):
            origin = self.origin[inputs[0]]
            self.origin[nid] = origin
            del self.tip[nid]
            if mode == "chain":
                self.tip[origin] = nid
        return nid


def _mapping_order(mappings: list[ET.Element], imp: _Importer) -> list[ET.Element]:
    """Mappings that write a table come before mappings that read it."""
    reads: dict[int, set[str]] = {}
    writes: dict[int, set[str]] = {}
    for i, m in enumerate(mappings):
        reads[i], writes[i] = set(), set()
        for inst in m.findall("INSTANCE"):
            kind = (inst.get("TYPE") or "").upper()
            table = _slug(inst.get("TRANSFORMATION_NAME") or inst.get("NAME") or "")
            if kind == "SOURCE":
                reads[i].add(table)
            elif kind == "TARGET":
                writes[i].add(table)
    order: list[int] = []

    def visit(i: int, stack: tuple[int, ...]) -> None:
        if i in order or i in stack:
            return
        for j in range(len(mappings)):
            if j != i and writes[j] & reads[i] and not (writes[i] & reads[j]):
                visit(j, stack + (i,))
        order.append(i)

    for i in range(len(mappings)):
        visit(i, ())
    return [mappings[i] for i in order]


def import_powercenter(xml: str, mapping: str | list[str] | None = None, name: str | None = None,
                       params: str | None = None) -> ImportResult:
    """Import a PowerCenter repository export (the XML from Designer or ``pmrep``).

    ``mapping`` picks mappings by name; by default every mapping in the export is imported into
    one pipeline. Mapplets are expanded. Sessions in the export apply their overrides, and
    workflows set the order. ``params`` is the text of a parameter file. The result carries a
    :class:`~redcliff.scorecard.Scorecard`.
    """
    xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml, count=1)  # powrmart.dtd is never fetched
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise PowerCenterImportError(f"could not read the XML: {e}") from e
    if root.tag != "POWERMART" and root.find(".//MAPPING") is None:
        raise PowerCenterImportError("this XML isn't a PowerCenter export (no POWERMART or MAPPING element)")

    imp = _Importer()
    folders = root.findall(".//FOLDER") or [root]
    mappings: list[tuple[ET.Element, str]] = []   # (mapping, folder name)
    mapplets: dict[str, ET.Element] = {}
    sessions: dict[str, Session] = {}
    workflows: list[Workflow] = []
    for folder in folders:
        for s in folder.findall("SOURCE"):
            imp.sources.setdefault((s.get("NAME") or "").upper(), s)
        for t in folder.findall("TARGET"):
            imp.targets.setdefault((t.get("NAME") or "").upper(), t)
        for t in folder.findall("TRANSFORMATION"):
            imp.transforms.setdefault((t.get("NAME") or "").upper(), t)
        for m in folder.findall("MAPPLET"):
            mapplets.setdefault((m.get("NAME") or "").upper(), m)
        fsessions, fworkflows = parse_folder(folder)
        sessions.update(fsessions)
        workflows += fworkflows
        mappings += [(m, (folder.get("NAME") or "")) for m in folder.findall("MAPPING")]
    if mapping is not None:
        wanted = {m.upper() for m in ([mapping] if isinstance(mapping, str) else mapping)}
        found = [(m, f) for m, f in mappings if (m.get("NAME") or "").upper() in wanted]
        if not found:
            names = ", ".join(m.get("NAME") or "?" for m, _ in mappings) or "none"
            raise PowerCenterImportError(f"no mapping named {mapping!r}; the export has: {names}")
        mappings = found
    if not mappings:
        raise PowerCenterImportError("the export has no mappings")

    # Which session runs each mapping, and where it falls in the workflows' run order.
    run_order: list[str] = []
    for wf in workflows:
        run_order += [wf.sessions[t] for t in wf.order if t in wf.sessions]
    run_order += [s for s in sessions if s not in run_order]
    session_of: dict[str, Session] = {}
    extra_sessions: dict[str, list[str]] = {}
    for sname in run_order:
        sess = sessions.get(sname)
        if sess is None:
            continue
        if sess.mapping in session_of:
            extra_sessions.setdefault(sess.mapping, []).append(sess.name)
        else:
            session_of[sess.mapping] = sess
    position = {m: i for i, m in enumerate(s.mapping for s in (sessions[n] for n in run_order if n in sessions))}
    mappings.sort(key=lambda mf: position.get((mf[0].get("NAME") or "").upper(), len(position)))

    sections = parse_param_file(params) if params else []
    folder_of: dict[int, str] = {}
    flat: list[ET.Element] = []
    missing_mapplets: dict[str, list[str]] = {}
    for m, fname in mappings:
        f, missing = flatten_mapplets(m, mapplets, imp.display, imp.transforms)
        flat.append(f)
        folder_of[id(f)] = fname
        if missing:
            missing_mapplets[(m.get("NAME") or "").upper()] = missing

    for m in _mapping_order(flat, imp):
        mname = (m.get("NAME") or "").upper()
        sess = session_of.get(mname)
        file_params = resolve_params(sections, folder_of[id(m)], sess.workflow if sess else None,
                                     sess.name if sess else None, sess.worklet if sess else None) if sections else {}
        before = len(imp.items)
        _MappingConverter(imp, m, sess, file_params).run()
        if mname in extra_sessions and len(imp.items) > before:
            imp.items[before].flag(REVIEW, f"mapping {m.get('NAME')} also runs in session(s) "
                                           f"{', '.join(extra_sessions[mname])}; only {sess.label}'s overrides "
                                           "were applied")

    imported = {(m.get("NAME") or "").upper() for m, _ in mappings}
    for wf in workflows:
        if not any(sessions.get(sn) and sessions[sn].mapping in imported for sn in wf.sessions.values()):
            continue
        _workflow_items(imp, wf, sessions)

    if not imp.b.nodes:
        raise PowerCenterImportError("nothing in the mappings could be imported")
    titled = [w for w in workflows if any(sessions.get(sn) and sessions[sn].mapping in imported
                                          for sn in w.sessions.values())]
    title = name or (titled[0].label if len(titled) == 1 else
                     mappings[0][0].get("NAME") if len(mappings) == 1 else
                     (folders[0].get("NAME") if folders and folders[0] is not root else "powercenter"))
    pipeline = Pipeline.from_dict({"version": 0, "name": _slug(title or "powercenter"),
                                   **({"variables": imp.variables} if imp.variables else {}), "nodes": imp.b.nodes})
    issues = validate(pipeline)
    if issues:  # a bug in the importer, not in the export
        raise SpecError(issues)
    card = Scorecard("powercenter", title or "powercenter", imp.items, dict(imp.functions),
                     lineage_coverage(pipeline))
    warnings = [f"{i.name}: {n}" for i in imp.items if i.status != CONVERTED for n in i.notes]
    bad = sorted({c for cols in imp.b.cols.values() if cols for c in cols if not ID_PATTERN.match(c)})
    if bad:
        warnings.append(f"column names that need quoting: {bad}")
    return ImportResult(pipeline, warnings, imp.outputs, card)


_TASK_ADVICE = {
    "command": (MANUAL, "runs shell commands; recreate it as a task in your orchestrator"),
    "email": (MANUAL, "sends an email; use your orchestrator's notifications"),
    "decision": (REVIEW, "becomes a branch in your orchestrator"),
    "assignment": (REVIEW, "sets workflow variables; pass them as pipeline parameters"),
    "timer": (MANUAL, "waits for a time; use your orchestrator's schedule or sensors"),
    "event wait": (MANUAL, "waits for an event or file; use your orchestrator's sensors"),
    "event raise": (MANUAL, "raises an event; use your orchestrator's triggers"),
    "control": (MANUAL, "stops or fails the workflow; use your orchestrator's failure handling"),
    "worklet": (MANUAL, "can't be expanded"),
}


def _workflow_items(imp: _Importer, wf: Workflow, sessions: dict[str, Session]) -> None:
    """A workflow is orchestration: its order is noted, and tasks that aren't sessions are graded."""
    steps = []
    for t in wf.order:
        if t in wf.sessions:
            sess = sessions.get(wf.sessions[t])
            shown = wf.labels.get(t, sess.label if sess else t)
            steps.append(f"{shown} (runs {sess.mapping.lower()})" if sess else shown)
        elif t in wf.tasks and wf.tasks[t].type.lower() != "start":
            steps.append(wf.tasks[t].label)
    item = ScoreItem(wf.label, "Workflow", area="orchestration")
    item.flag(REVIEW, "Redcliff builds pipelines, not schedules: recreate this run order in your orchestrator: "
                      + " → ".join(steps))
    for cond in wf.conditions:
        item.flag(REVIEW, f"link condition: {cond}")
    imp.items.append(item)
    for t in wf.order:
        task = wf.tasks.get(t)
        if task is None or task.type.lower() in ("start", "start task"):
            continue
        status, advice = _TASK_ADVICE.get(task.type.lower(), (MANUAL, "has no pipeline equivalent"))
        ti = ScoreItem(f"{wf.label}/{task.label}", task.type, area="orchestration")
        ti.flag(status, f"{task.type} task {advice}")
        for d in task.detail:
            ti.flag(status, d)
        imp.items.append(ti)


def mapping_names(xml: str) -> list[str]:
    """Names of the mappings in an export, for pickers."""
    xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml, count=1)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise PowerCenterImportError(f"could not read the XML: {e}") from e
    return [m.get("NAME") or "" for m in root.iter("MAPPING")]
