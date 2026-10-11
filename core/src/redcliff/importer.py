"""Turn legacy SQL into a Redcliff pipeline spec.

``import_sql`` reads one or more statements (SELECTs, CTEs, ``CREATE TABLE ... AS``,
``INSERT INTO ... SELECT``) in any sqlglot dialect and maps each logical step to a node:
sources, joins, filters, aggregates, derived columns, selects and unions.

Anything without a node type yet (ORDER BY, LIMIT, subqueries in WHERE, QUALIFY, ...) becomes
a ``sql`` node holding that SELECT, plus a warning. Nothing is dropped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from redcliff.columns import Columns, output_columns
from redcliff.spec import ID_PATTERN, Node, Pipeline, SpecError, validate


class SQLImportError(ValueError):
    """The SQL could not be read at all."""


@dataclass
class ImportResult:
    pipeline: Pipeline
    warnings: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)  # final node of each statement, in order


class _Fallback(Exception):
    """This SELECT can't be mapped to nodes; keep it as a SQL step."""


def _arg(e: exp.Expression, name: str):
    """An argument by name; newer sqlglot spells some with a trailing underscore (``from_``)."""
    value = e.args.get(name)
    return value if value is not None else e.args.get(name + "_")


def _contains_aggregate(e: exp.Expression) -> bool:
    return any(agg.find_ancestor(exp.Window) is None for agg in e.find_all(exp.AggFunc))


class _Builder:
    def __init__(self, dialect: str | None, schema: dict[str, dict[str, str]] | None):
        self.dialect = dialect
        self.schema = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in (schema or {}).items()}
        self.nodes: list[dict[str, Any]] = []
        self.cols: dict[str, Columns] = {}
        self.warnings: list[str] = []
        self.tables: dict[str, str] = {}      # table name -> node id (sources and created tables)
        self.created: dict[str, str] = {}     # tables created by CREATE TABLE AS -> node id
        self.read_after_create: set[str] = set()
        self.positional = 0  # >0 while converting later UNION branches, whose names don't matter

    # ------------------------------------------------------------------ nodes

    def new_id(self, base: str) -> str:
        slug = re.sub(r"[^a-z0-9_]", "_", base.lower()).strip("_") or "node"
        if not re.match(r"[a-z_]", slug[0]):
            slug = "_" + slug
        taken = {n["id"] for n in self.nodes}
        candidate, i = slug, 2
        while candidate in taken:
            candidate, i = f"{slug}_{i}", i + 1
        return candidate

    def add(self, type_: str, inputs: list[str], params: dict[str, Any], base: str) -> str:
        nid = self.new_id(base)
        node = {"id": nid, "type": type_, "inputs": inputs, "params": params}
        self.nodes.append(node)
        self.cols[nid] = output_columns(Node.from_dict(node), [self.cols[i] for i in inputs])
        return nid

    def rename_last(self, nid: str, name: str | None) -> str:
        """Give a chain's final node a readable name. Safe: nothing references it yet."""
        if not name or self.nodes[-1]["id"] != nid or self.nodes[-1]["type"] == "source":
            return nid
        wanted = self.new_id(name)
        if wanted != re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_"):
            return nid  # the plain name is taken; keep the generated one
        self.nodes[-1]["id"] = wanted
        self.cols[wanted] = self.cols.pop(nid)
        return wanted

    # ------------------------------------------------------------------ relations

    def source_for(self, table: exp.Table) -> str:
        key = exp.table_(table.name, db=table.db or None, catalog=table.catalog or None).sql()
        if key in self.created:
            self.read_after_create.add(key)
        if key in self.tables:
            return self.tables[key]
        columns = self.schema.get(key) or self.schema.get(table.name)
        params: dict[str, Any] = {"format": "table", "table": key}
        if columns:
            params["columns"] = dict(columns)
        nid = self.add("source", [], params, table.name)
        self.tables[key] = nid
        return nid

    def relation(self, rel: exp.Expression, scope: dict[str, str]) -> tuple[str, str]:
        """(alias, node id) for one FROM/JOIN item."""
        if isinstance(rel, exp.Table):
            if not rel.db and rel.name in scope:
                return rel.alias_or_name, scope[rel.name]
            if not isinstance(rel.this, exp.Identifier):
                raise _Fallback("a table function in FROM")
            return rel.alias_or_name, self.source_for(rel)
        if isinstance(rel, exp.Subquery):
            alias = rel.alias or self.new_id("subquery")
            return alias, self.convert_query(rel.this, scope, alias)
        raise _Fallback(f"{type(rel).__name__.upper()} in FROM")

    # ------------------------------------------------------------------ queries

    def convert_query(self, q: exp.Expression, scope: dict[str, str], name: str | None) -> str:
        scope = dict(scope)
        with_ = _arg(q, "with")
        if with_ is not None:
            if with_.args.get("recursive"):
                return self.raw(q, scope, "a recursive CTE", name)
            for cte in with_.expressions:
                scope[cte.alias] = self.convert_query(cte.this, scope, cte.alias)
            q = q.copy()
            q.set("with_" if "with_" in q.args else "with", None)

        if isinstance(q, exp.Subquery):
            return self.convert_query(q.this, scope, name)
        if isinstance(q, exp.Union) and not isinstance(q, (exp.Intersect, exp.Except)):
            return self.convert_union(q, scope, name)
        if isinstance(q, exp.Select):
            try:
                return self.convert_select(q, scope, name)
            except _Fallback as reason:
                return self.raw(q, scope, str(reason), name)
        what = type(q).__name__.upper()
        return self.raw(q, scope, what, name)

    def convert_union(self, q: exp.Union, scope: dict[str, str], name: str | None) -> str:
        tail = {k: q.args.get(k) for k in ("order", "limit", "offset") if q.args.get(k) is not None}
        if tail:
            return self.raw(q, scope, "ORDER BY or LIMIT on a UNION", name)
        distinct = bool(q.args.get("distinct"))
        parts: list[exp.Expression] = []

        def flatten(e: exp.Expression) -> None:
            if (type(e) is exp.Union and bool(e.args.get("distinct")) == distinct
                    and _arg(e, "with") is None and not any(e.args.get(k) for k in tail)):
                flatten(e.this)
                flatten(e.expression)
            else:
                parts.append(e)

        flatten(q)
        base = name or "union"
        inputs = [self.convert_query(parts[0], scope, f"{base}_part1")]
        self.positional += 1
        try:
            inputs += [self.convert_query(p, scope, f"{base}_part{i + 2}") for i, p in enumerate(parts[1:])]
        finally:
            self.positional -= 1
        nid = self.add("union", inputs, {"distinct": distinct} if distinct else {}, base)
        return self.rename_last(nid, name)

    def convert_select(self, sel: exp.Select, scope: dict[str, str], name: str | None) -> str:
        base = name or "step"
        for arg in ("qualify", "laterals", "pivots", "windows", "connect", "match", "into", "kind"):
            if sel.args.get(arg):
                raise _Fallback(arg.upper())
        if sel.args.get("distinct") is not None and sel.args["distinct"].args.get("on"):
            raise _Fallback("DISTINCT ON")
        body_parts = [*sel.expressions, sel.args.get("where"), sel.args.get("having"), sel.args.get("group")]
        if any(p is not None and p.find(exp.Select) for p in body_parts):
            raise _Fallback("a subquery outside FROM")
        group = sel.args.get("group")
        if group is not None and any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
            raise _Fallback("ROLLUP, CUBE or GROUPING SETS")
        from_ = _arg(sel, "from")
        if from_ is None:
            raise _Fallback("a SELECT without FROM")

        # 1. Relations, in join order.
        rels = [self.relation(from_.this, scope)]
        joins = sel.args.get("joins") or []
        for j in joins:
            if j.args.get("method") or (j.args.get("kind") or "").upper() == "NATURAL":
                raise _Fallback("a NATURAL join")
            rels.append(self.relation(j.this, scope))
        aliases = [a for a, _ in rels]

        # 2. Column-name clashes: a later table's column that an earlier table also has
        #    would be hidden by the join (left side wins), so rename it first.
        refs = [c for p in [*body_parts, *[j.args.get("on") for j in joins], sel.args.get("order")]
                if p is not None for c in p.find_all(exp.Column)]
        rename: dict[tuple[str, str], str] = {}
        for idx, (alias, rid) in enumerate(rels):
            if idx == 0:
                continue
            earlier = rels[:idx]
            needed = []
            for c in refs:
                if c.table != alias:
                    continue
                clash = any((self.cols[eid] is not None and c.name in self.cols[eid])
                            or any(r.table == ea and r.name == c.name for r in refs)
                            for ea, eid in earlier)
                if clash and c.name not in needed:
                    needed.append(c.name)
            if any(self.cols[eid] is None for _, eid in earlier) and refs:
                self.warn_once(f"{base}: an input has no known columns, so join column clashes "
                               "can't be checked; pass a schema for safer imports")
            if needed:
                new = {f"{alias}_{c}": c for c in needed}
                rid = self.add("derive", [rid], {"columns": new}, f"{alias}_renamed")
                rels[idx] = (alias, rid)
                for c in needed:
                    rename[(alias, c)] = f"{alias}_{c}"

        # 3. Joins, left to right.
        current = rels[0][1]
        holder = {rels[0][0]: current}
        for j, (alias, rid) in zip(joins, rels[1:]):
            def in_join(a: str | None, c: str) -> tuple[str | None, str]:
                if a is None:
                    return None, c
                node = rid if a == alias else holder.get(a)
                return node, rename.get((a, c), c)

            using = j.args.get("using")
            on = j.args.get("on")
            if using:
                cond = " AND ".join(f"{current}.{u.name} = {rid}.{u.name}" for u in using)
            elif on is not None:
                cond = self.rewrite(on, in_join).sql()
            else:
                cond = "1 = 1"  # CROSS JOIN
            side = (j.args.get("side") or "").lower()
            how = {"left": "left", "right": "right", "full": "full"}.get(side, "inner")
            params = {"condition": cond} if how == "inner" else {"how": how, "condition": cond}
            current = self.add("join", [current, rid], params, f"{base}_join")
            for a in aliases:
                if a in holder or a == alias:
                    holder[a] = current

        def flat(a: str | None, c: str) -> tuple[None, str]:
            return None, rename.get((a, c), c) if a else c

        # 4. WHERE
        where = sel.args.get("where")
        if where is not None:
            current = self.add("filter", [current], {"condition": self.rewrite(where.this, flat).sql()},
                               f"{base}_filter")

        # 5. SELECT list, with or without aggregation.
        projections = sel.expressions
        lone_star = len(projections) == 1 and isinstance(projections[0], exp.Star)
        if any(isinstance(p.unalias(), exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
               for p in projections) and not lone_star:
            projections = self.expand_stars(projections, rels)

        aggregating = group is not None or any(_contains_aggregate(p) for p in projections)
        if aggregating:
            current = self.aggregate(sel, projections, current, base, flat)
        elif not lone_star:
            current = self.project(projections, current, base, flat)

        distinct = sel.args.get("distinct") is not None
        if distinct:
            names = self.cols[current]
            if names is None:
                raise _Fallback("DISTINCT * on a table with unknown columns")
            current = self.add("aggregate", [current], {"group_by": list(names)}, f"{base}_distinct")

        # 6. ORDER BY / LIMIT: no node type yet, so a small SQL step.
        tail = [k for k in ("order", "limit", "offset") if sel.args.get(k) is not None]
        if tail:
            wrapper = exp.select("*").from_(current)
            for k in tail:
                part = sel.args[k].copy()
                wrapper.set(k, self.rewrite(part, flat) if k == "order" else part)
            current = self.add("sql", [current], {"query": wrapper.sql()}, f"{base}_sorted")
            current = self.rename_last(current, name)
            words = {"order": "ORDER BY", "limit": "LIMIT", "offset": "OFFSET"}
            self.warnings.append(f"{current}: {' and '.join(words[t] for t in tail)} kept as a SQL step "
                                 "(Redcliff has no ordering node yet)")
            return current
        return self.rename_last(current, name)

    def expand_stars(self, projections: list[exp.Expression], rels: list[tuple[str, str]]) -> list[exp.Expression]:
        out: list[exp.Expression] = []
        for p in projections:
            target = None
            if isinstance(p, exp.Star):
                target = rels
            elif isinstance(p, exp.Column) and isinstance(p.this, exp.Star):
                target = [r for r in rels if r[0] == p.table]
            if target is None:
                out.append(p)
                continue
            for alias, rid in target:
                if self.cols[rid] is None:
                    raise _Fallback("t.* on a table with unknown columns")
                out += [exp.column(c, table=alias) for c in self.cols[rid]]
        return out

    def output_name(self, p: exp.Expression, i: int, kind: str) -> str:
        if isinstance(p, exp.Alias):
            return p.alias
        if isinstance(p, exp.Column):
            return p.name
        if isinstance(p, exp.AggFunc):
            arg = p.this
            suffix = arg.name if isinstance(arg, exp.Column) else "all" if isinstance(arg, exp.Star) else str(i)
            gen = f"{p.key.lower()}_{suffix}"
        else:
            gen = f"{kind}_{i}"
        if not self.positional:
            self.warnings.append(f"unnamed expression {p.sql()!r} named {gen!r}")
        return gen

    def project(self, projections, current, base, flat) -> str:
        names, derive = [], {}
        for i, p in enumerate(projections, 1):
            out = self.output_name(p, i, "expr")
            expr = self.rewrite(p.unalias(), flat)
            if not (isinstance(expr, exp.Column) and expr.name == out):
                derive[out] = expr.sql()
            names.append(out)
        if len(set(names)) != len(names):
            raise _Fallback("duplicate output column names")
        if derive:
            current = self.add("derive", [current], {"columns": derive}, f"{base}_derive")
        if self.cols[current] != names:
            current = self.add("select", [current], {"columns": names}, f"{base}_select")
        return current

    def aggregate(self, sel, projections, current, base, flat) -> str:
        group = sel.args.get("group")
        group_exprs = []
        for g in (group.expressions if group is not None else []):
            if isinstance(g, exp.Literal) and g.is_int:  # GROUP BY 1
                g = projections[int(g.this) - 1].unalias()
            group_exprs.append(self.rewrite(g, flat))

        pre: dict[str, str] = {}
        keys: list[str] = []
        key_of: dict[str, str] = {}  # expression SQL -> group column name
        for i, g in enumerate(group_exprs, 1):
            if isinstance(g, exp.Column):
                keys.append(g.name)
                key_of[g.sql()] = g.name
                continue
            match = next((p.alias for p in projections
                          if isinstance(p, exp.Alias) and self.rewrite(p.this, flat).sql() == g.sql()), None)
            gname = match or f"group_{i}"
            pre[gname] = g.sql()
            keys.append(gname)
            key_of[g.sql()] = gname
        if pre:
            current = self.add("derive", [current], {"columns": pre}, f"{base}_keys")

        measures: dict[str, str] = {}
        outputs: list[tuple[str, str]] = []  # (output name, column after aggregation)
        post: dict[str, str] = {}
        for i, p in enumerate(projections, 1):
            out = self.output_name(p, i, "measure")
            expr = self.rewrite(p.unalias(), flat)
            sql = expr.sql()
            if sql in key_of:
                outputs.append((out, key_of[sql]))
            elif _contains_aggregate(expr):
                measures[out] = sql
                outputs.append((out, out))
            else:
                post[out] = sql  # an expression over group keys, after aggregating
                outputs.append((out, out))

        having = sel.args.get("having")
        hidden: list[str] = []
        having_sql = None
        if having is not None:
            cond = self.rewrite(having.this, flat)
            by_sql = {v: k for k, v in measures.items()}

            def lift(node: exp.Expression) -> exp.Expression:
                if isinstance(node, exp.AggFunc) and node.find_ancestor(exp.Window) is None:
                    s = node.sql()
                    if s not in by_sql:
                        hname = f"_having_{len(hidden) + 1}"
                        measures[hname] = s
                        by_sql[s] = hname
                        hidden.append(hname)
                    return exp.column(by_sql[s])
                return node

            having_sql = cond.transform(lift).sql()

        params: dict[str, Any] = {}
        if keys:
            params["group_by"] = keys
        if measures:
            params["measures"] = measures
        if not params:
            raise _Fallback("an aggregate with nothing to compute")
        current = self.add("aggregate", [current], params, f"{base}_agg")
        if having_sql:
            current = self.add("filter", [current], {"condition": having_sql}, f"{base}_having")

        renames = {out: col for out, col in outputs if out != col}
        derive = {**{out: col for out, col in renames.items()}, **post}
        if derive:
            current = self.add("derive", [current], {"columns": derive}, f"{base}_derive")
        names = [out for out, _ in outputs]
        if self.cols[current] != names:
            current = self.add("select", [current], {"columns": names}, f"{base}_select")
        return current

    # ------------------------------------------------------------------ fallback

    def raw(self, q: exp.Expression, scope: dict[str, str], reason: str, name: str | None) -> str:
        """Keep a query as one SQL step, pointing its tables at the right nodes."""
        q = q.copy()
        local = {cte.alias for cte in q.find_all(exp.CTE)}
        inputs: list[str] = []
        for table in list(q.find_all(exp.Table)):
            if not table.name or (not table.db and table.name in local):
                continue
            nid = scope[table.name] if (not table.db and table.name in scope) else self.source_for(table)
            if nid not in inputs:
                inputs.append(nid)
            alias = table.alias_or_name
            replacement = exp.table_(nid, alias=alias if alias != nid else None)
            table.replace(replacement)
        nid = self.add("sql", inputs, {"query": q.sql()}, name or "sql_step")
        nid = self.rename_last(nid, name)
        self.warnings.append(f"{nid}: {reason} kept as a SQL step")
        return nid

    # ------------------------------------------------------------------ helpers

    def rewrite(self, e: exp.Expression, mapping) -> exp.Expression:
        """Copy of an expression with each column re-pointed by ``mapping(qualifier, name)``."""
        e = e.copy()

        def fix(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
                qualifier, col = mapping(node.table or None, node.name)
                return exp.column(col, table=qualifier)
            return node

        return e.transform(fix)

    def warn_once(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # ------------------------------------------------------------------ statements

    def statement(self, stmt: exp.Expression, index: int) -> str | None:
        if isinstance(stmt, exp.Create) and isinstance(stmt.expression, exp.Query):
            table = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
            key = table.sql()
            nid = self.convert_query(stmt.expression, {}, table.name)
            self.tables[key] = nid
            if (stmt.args.get("kind") or "").upper() == "TABLE":
                self.created[key] = nid
            return nid
        if isinstance(stmt, exp.Insert) and isinstance(stmt.expression, exp.Query):
            table = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
            nid = self.convert_query(stmt.expression, {}, f"{table.name}_rows")
            mode = "overwrite" if stmt.args.get("overwrite") else "append"
            self.add("target", [nid], {"format": "table", "table": table.sql(), "mode": mode},
                     f"{table.name}_out")
            return nid
        if isinstance(stmt, exp.Query):
            return self.convert_query(stmt, {}, "result" if index == 0 else f"result_{index + 1}")
        snippet = " ".join(stmt.sql().split())[:60]
        self.warnings.append(f"skipped statement with no pipeline equivalent: {snippet}")
        return None

    def finish(self) -> None:
        """CREATE TABLE AS results that nothing reads afterwards are the pipeline's outputs."""
        for key, nid in self.created.items():
            if key not in self.read_after_create:
                self.add("target", [nid], {"format": "table", "table": key}, f"{key.split('.')[-1]}_out")


def _strip_bteq(sql: str) -> tuple[str, int]:
    """Drop Teradata BTEQ dot-commands (.LOGON, .IF ERRORCODE ...), which aren't SQL."""
    kept, dropped = [], 0
    for line in sql.splitlines():
        if line.lstrip().startswith("."):
            dropped += 1
        else:
            kept.append(line)
    return "\n".join(kept), dropped


def import_sql(
    sql: str,
    dialect: str | None = None,
    name: str = "imported",
    schema: dict[str, dict[str, str]] | None = None,
) -> ImportResult:
    """Import SQL into a validated pipeline.

    ``schema`` maps table names to {column: type}; with it, sources get declared columns,
    join clashes are detected precisely and lineage works end to end.
    """
    sql, dropped = _strip_bteq(sql)
    b = _Builder(dialect, schema)
    if dropped:
        b.warnings.append(f"skipped {dropped} BTEQ dot-command line(s)")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as e:
        raise SQLImportError(f"could not parse the SQL: {str(e).splitlines()[0]}") from e
    if not statements:
        raise SQLImportError("no SQL statements found")

    outputs = []
    for i, stmt in enumerate(statements):
        out = b.statement(normalize_identifiers(stmt, dialect=dialect), i)
        if out:
            outputs.append(out)
    b.finish()
    if not b.nodes:
        raise SQLImportError("nothing in the SQL maps to a pipeline")

    pipeline = Pipeline.from_dict({"version": 0, "name": name, "nodes": b.nodes})
    issues = validate(pipeline)
    if issues:  # a bug in the importer, not in the user's SQL
        raise SpecError(issues)
    bad_names = [c for cols in b.cols.values() if cols for c in cols if not ID_PATTERN.match(c)]
    if bad_names:
        b.warnings.append(f"column names that need quoting: {sorted(set(bad_names))}")
    return ImportResult(pipeline, b.warnings, outputs)
