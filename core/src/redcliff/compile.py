"""Compile a pipeline spec into readable SQL (DuckDB, Spark) or a PySpark script.

SQL output is a chain of named CTEs, one per node, each preceded by a comment naming the node.
Node expressions are written in neutral SQL in the spec; sqlglot transpiles them per dialect.

Semantics the compiler relies on (see docs/spec.md):
- Inside a join, each side is referred to by its input node id.
- A derive sees only its input's columns; a derive column with an existing name replaces it.
- When both sides of a join share a column name, the left side's column is kept.
"""

from __future__ import annotations

import functools
import keyword
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from redcliff.columns import Columns, infer_columns
from redcliff.spec import Node, Pipeline

DIALECTS = ("duckdb", "spark")
JOIN_SQL = {"inner": "JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}


class CompileError(ValueError):
    """The pipeline is valid but can't be expressed for the requested target."""


# --------------------------------------------------------------------------- helpers


def _check_dialect(dialect: str) -> None:
    if dialect not in DIALECTS:
        raise CompileError(f"unsupported dialect {dialect!r}; expected one of {list(DIALECTS)}")


@functools.cache
def _keywords() -> frozenset[str]:
    """Words that need quoting as identifiers in any supported dialect.

    Tokenizer keywords include phrases such as "ORDER BY", so the first word counts too.
    """
    words: set[str] = set()
    for name in DIALECTS:
        dialect = Dialect.get_or_raise(name)
        words |= {k.split()[0] for k in dialect.tokenizer_class.KEYWORDS}
        words |= set(getattr(dialect.generator_class, "RESERVED_KEYWORDS", ()))
    return frozenset(w.upper() for w in words)


def _ident(name: str) -> str:
    """Neutral-SQL identifier, double-quoted only when it collides with a keyword."""
    return f'"{name}"' if name.upper() in _keywords() else name


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _path(node: Node, base_dir: str | Path | None) -> str:
    raw = node.params["path"]
    if base_dir is None or Path(raw).is_absolute():
        return raw
    return str((Path(base_dir) / raw).resolve())


def _type_sql(type_name: str, dialect: str) -> str:
    return exp.DataType.build(type_name).sql(dialect)


def _transpile(neutral_sql: str, dialect: str) -> str:
    return sqlglot.transpile(neutral_sql, write=dialect, pretty=True)[0]


def _statement(neutral_sql: str, dialect: str) -> str:
    """A one-line statement (pre/post SQL); pretty printing only pads these."""
    return sqlglot.transpile(neutral_sql, write=dialect)[0]


def _expr(neutral: str, dialect: str) -> str:
    return sqlglot.parse_one(neutral).sql(dialect)


def _ancestors(pipeline: Pipeline, node_id: str) -> set[str]:
    seen: set[str] = set()
    stack = [node_id]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(pipeline.node(nid).inputs)
    return seen


# --------------------------------------------------------------------------- per-node SQL


def _source_relation(node: Node, dialect: str, base_dir) -> str | None:
    """What a source reads from. None means "registered as a view named after the node"."""
    p = node.params
    if p["format"] == "table":
        return p["table"]
    if dialect == "spark":
        return None  # file sources are registered as temp views by the caller (see header)
    path = _literal(_path(node, base_dir))
    if p["format"] == "parquet":
        return f"read_parquet({path})"
    cols = p.get("columns")
    if not cols:
        return f"read_csv_auto({path}, header = true)"
    struct = ", ".join(f"{_literal(c)}: {_literal(_type_sql(t, 'duckdb'))}" for c, t in cols.items())
    return f"read_csv({path}, header = true, columns = {{{struct}}})"


def _node_body(node: Node, cols: dict[str, Columns], dialect: str) -> str:
    """The SELECT for a non-source node, in neutral SQL with identifiers ready to transpile."""
    p = node.params
    t = node.type
    src = [_ident(i) for i in node.inputs]

    if t == "filter":
        return f"SELECT * FROM {src[0]} WHERE {p['condition']}"

    if t == "select":
        return f"SELECT {', '.join(_ident(c) for c in p['columns'])} FROM {src[0]}"

    if t == "derive":
        known = cols[node.inputs[0]]
        derived = p["columns"]
        if known is None:
            # Without a known schema, a derive can only add columns: replacing one would
            # duplicate it. Declare 'columns' on the source to replace existing columns.
            extra = ", ".join(f"{e} AS {_ident(c)}" for c, e in derived.items())
            return f"SELECT *, {extra} FROM {src[0]}"
        items = [f"{derived[c]} AS {_ident(c)}" if c in derived else _ident(c) for c in known]
        items += [f"{e} AS {_ident(c)}" for c, e in derived.items() if c not in known]
        return f"SELECT {', '.join(items)} FROM {src[0]}"

    if t == "join":
        left, right = node.inputs
        lcols, rcols = cols[left], cols[right]
        if lcols is None or rcols is None:
            select = f"{_ident(left)}.*, {_ident(right)}.*"
        else:
            picks = [f"{_ident(left)}.{_ident(c)}" for c in lcols]
            picks += [f"{_ident(right)}.{_ident(c)}" for c in rcols if c not in lcols]
            select = ", ".join(picks)
        how = JOIN_SQL[p.get("how", "inner")]
        return f"SELECT {select} FROM {src[0]} {how} {src[1]} ON {p['condition']}"

    if t == "aggregate":
        group_by = [_ident(c) for c in p.get("group_by", [])]
        measures = [f"{e} AS {_ident(c)}" for c, e in (p.get("measures") or {}).items()]
        sql = f"SELECT {', '.join(group_by + measures)} FROM {src[0]}"
        return sql + (f" GROUP BY {', '.join(group_by)}" if group_by else "")

    if t == "sql":
        return p["query"]  # inputs are referenced by node id, which is each CTE's name

    if t == "union":
        op = " UNION " if p.get("distinct", False) else " UNION ALL "
        return op.join(f"SELECT * FROM {s}" for s in src)

    raise CompileError(f"{node.id}: cannot compile node type {t!r}")


def _cte_chain(pipeline: Pipeline, needed: set[str], dialect: str, base_dir) -> tuple[list[str], list[str]]:
    """CTE text blocks for the needed nodes, plus Spark views the caller must register."""
    cols = infer_columns(pipeline)
    blocks: list[str] = []
    views: list[str] = []
    for node in pipeline.topo_order():
        if node.id not in needed or node.type == "target":
            continue
        if node.type == "source":
            relation = _source_relation(node, dialect, base_dir)
            if relation is None:
                views.append(f"{node.id} <- {node.params['format']} {node.params['path']}")
                continue
            if relation == node.id:
                continue  # the table already has the node's name
            body = f"SELECT * FROM {relation}"
            text = body if dialect == "duckdb" else _transpile(body, dialect)
        else:
            text = _transpile(_node_body(node, cols, dialect), dialect)
        name = _transpile(_ident(node.id), dialect)
        indented = "\n".join("  " + line for line in text.splitlines())
        blocks.append(f"-- {node.id}: {node.type}\n{name} AS (\n{indented}\n)")
    return blocks, views


def _with(blocks: list[str]) -> str:
    return "WITH\n" + ",\n\n".join(blocks) if blocks else ""


# --------------------------------------------------------------------------- public API


def compile_query(pipeline: Pipeline, node_id: str, dialect: str = "duckdb", base_dir=None) -> str:
    """A SELECT returning one node's output: for previews, tests and ad hoc runs."""
    _check_dialect(dialect)
    node = pipeline.node(node_id)
    if node.type == "target":
        raise CompileError(f"{node_id} is a target; query its input {node.inputs[0]!r} instead")
    blocks, views = _cte_chain(pipeline, _ancestors(pipeline, node_id), dialect, base_dir)
    final = f"SELECT * FROM {_transpile(_ident(node_id), dialect)}"
    head = _views_header(views)
    return head + (f"{_with(blocks)}\n{final}" if blocks else final)


def _views_header(views: list[str]) -> str:
    if not views:
        return ""
    lines = "\n".join(f"--   {v}" for v in views)
    return f"-- Register these file sources as temp views first:\n{lines}\n"


def _write_statement(target: Node, dialect: str, base_dir, blocks: list[str]) -> str:
    p = target.params
    src = _transpile(_ident(target.inputs[0]), dialect)
    query = f"SELECT * FROM {src}"
    mode = p.get("mode", "overwrite")
    fmt = p["format"]
    with_sql = _with(blocks)

    if dialect == "duckdb":
        full = f"{with_sql}\n{query}" if with_sql else query
        if fmt == "table":
            if mode == "append":
                return f"-- {target.id}: target\nINSERT INTO {p['table']}\n{full}"
            return f"-- {target.id}: target\nCREATE OR REPLACE TABLE {p['table']} AS\n{full}"
        if mode == "append":
            raise CompileError(f"{target.id}: DuckDB cannot append to a {fmt} file; use a table target")
        options = "FORMAT parquet" if fmt == "parquet" else "FORMAT csv, HEADER true"
        inner = "\n".join("  " + line for line in full.splitlines())
        return f"-- {target.id}: target\nCOPY (\n{inner}\n) TO {_literal(_path(target, base_dir))} ({options})"

    # spark
    if fmt == "table":
        verb = "INSERT INTO" if mode == "append" else "INSERT OVERWRITE TABLE"
        insert = f"{verb} {p['table']}"
    else:
        if mode == "append":
            raise CompileError(f"{target.id}: Spark SQL cannot append to a directory; use PySpark or a table")
        options = " OPTIONS (header 'true')" if fmt == "csv" else ""
        insert = f"INSERT OVERWRITE DIRECTORY {_literal(_path(target, base_dir))} USING {fmt}{options}"
    head = f"{with_sql}\n" if with_sql else ""
    return f"-- {target.id}: target\n{head}{insert}\n{query}"


def _hooks(target: Node, dialect: str) -> tuple[list[str], list[str]]:
    """A target's pre/post SQL as statements for the script. DuckDB lists them in the header instead."""
    if dialect == "duckdb":
        return [], []

    def stmts(key: str, label: str) -> list[str]:
        return [f"-- {target.id}: {label}\n{_statement(sql, dialect)}" for sql in target.params.get(key) or []]

    return stmts("pre_sql", "pre-SQL"), stmts("post_sql", "post-SQL")


def _hook_comments(target: Node) -> list[str]:
    return [f"{target.id} {label}: {' '.join(sql.split())}"
            for key, label in (("pre_sql", "before"), ("post_sql", "after"))
            for sql in target.params.get(key) or []]


def compile_script(pipeline: Pipeline, dialect: str = "duckdb", base_dir=None) -> str:
    """A runnable script that writes every target. One statement per target."""
    _check_dialect(dialect)
    targets = [n for n in pipeline.topo_order() if n.type == "target"]
    if not targets:
        raise CompileError("the pipeline has no target to write")
    statements: list[str] = []
    all_views: list[str] = []
    for target in targets:
        blocks, views = _cte_chain(pipeline, _ancestors(pipeline, target.id), dialect, base_dir)
        all_views += [v for v in views if v not in all_views]
        pre, post = _hooks(target, dialect)
        statements += pre
        statements.append(_write_statement(target, dialect, base_dir, blocks))
        statements += post
    header = f"-- Redcliff pipeline: {pipeline.name} ({dialect})\n"
    if dialect == "duckdb":
        notes = [line for t in targets for line in _hook_comments(t)]
        if notes:
            header += "-- Pre/post SQL for the target platform (not run by this DuckDB preview script):\n"
            header += "".join(f"--   {line}\n" for line in notes)
    if dialect == "duckdb" and any(t.params["format"] != "table" for t in targets):
        header += "-- Output folders must exist before running: DuckDB's COPY does not create them.\n"
    return header + _views_header(all_views) + ";\n\n".join(statements) + ";\n"


# --------------------------------------------------------------------------- PySpark


def compile_pyspark(pipeline: Pipeline, base_dir=None) -> str:
    """A standalone PySpark script (DataFrame API) that reads sources and writes targets."""
    cols = infer_columns(pipeline)
    lines = [
        f'"""Redcliff pipeline: {pipeline.name}. Generated code; edit the spec, not this file."""',
        "",
        "from pyspark.sql import SparkSession",
        "from pyspark.sql import functions as F",
        "",
        f"spark = SparkSession.builder.appName({pipeline.name!r}).getOrCreate()",
    ]

    def v(node_id: str) -> str:  # Python variable for a node; avoids keywords like `class`
        return node_id + "_" if keyword.iskeyword(node_id) else node_id

    for node in pipeline.topo_order():
        p, t, var = node.params, node.type, v(node.id)
        ins = [v(i) for i in node.inputs]
        lines += ["", f"# {node.id}: {t}"]
        if t == "source":
            if p["format"] == "table":
                lines.append(f"{var} = spark.table({p['table']!r})")
                continue
            reader = "spark.read"
            if p["format"] == "csv":
                reader += '.option("header", True)'
                if p.get("columns"):
                    ddl = ", ".join(f"{c} {_type_sql(ty, 'spark')}" for c, ty in p["columns"].items())
                    reader += f".schema({ddl!r})"
            lines.append(f"{var} = {reader}.{p['format']}({_path(node, base_dir)!r})")
        elif t in ("filter", "select", "derive", "join", "aggregate", "union"):
            lines.append(f"{var} = {_dataframe(node, ins, cols)}")
        elif t == "sql":
            for name, df in zip(node.inputs, ins):
                lines.append(f"{df}.createOrReplaceTempView({name!r})")
            query = _transpile(p["query"], "spark")
            lines.append(f"{var} = spark.sql({query!r})")
        elif t == "target":
            for sql in p.get("pre_sql") or []:
                lines.append(f"spark.sql({_statement(sql, 'spark')!r})  # pre-SQL")
            writer = f'{ins[0]}.write.mode({p.get("mode", "overwrite")!r})'
            if p["format"] == "table":
                lines.append(f"{writer}.saveAsTable({p['table']!r})")
            else:
                if p["format"] == "csv":
                    writer += '.option("header", True)'
                lines.append(f"{writer}.{p['format']}({_path(node, base_dir)!r})")
            for sql in p.get("post_sql") or []:
                lines.append(f"spark.sql({_statement(sql, 'spark')!r})  # post-SQL")
    return "\n".join(lines) + "\n"


def _dataframe(node: Node, ins: list[str], cols: dict[str, Columns]) -> str:
    """The DataFrame expression for one transform step, given its inputs as Python expressions."""
    p, t = node.params, node.type

    def ex(neutral: str) -> str:
        return f"F.expr({_expr(neutral, 'spark')!r})"

    if t == "filter":
        return f"{ins[0]}.where({ex(p['condition'])})"
    if t == "select":
        return f"{ins[0]}.select({', '.join(repr(c) for c in p['columns'])})"
    if t == "derive":
        pairs = ", ".join(f"{c!r}: {ex(e)}" for c, e in p["columns"].items())
        return f"{ins[0]}.withColumns({{{pairs}}})"
    if t == "join":
        left, right = node.inputs
        how = {"full": "full_outer"}.get(p.get("how", "inner"), p.get("how", "inner"))
        expr = f"{ins[0]}.alias({left!r}).join({ins[1]}.alias({right!r}), {ex(p['condition'])}, {how!r})"
        lcols, rcols = cols[left], cols[right]
        if lcols is not None and rcols is not None:
            picks = [f"{left}.{c}" for c in lcols] + [f"{right}.{c}" for c in rcols if c not in lcols]
            expr += f".select({', '.join(repr(c) for c in picks)})"
        return expr
    if t == "aggregate":
        aggs = ", ".join(f"{ex(e)}.alias({c!r})" for c, e in (p.get("measures") or {}).items())
        group_by = p.get("group_by", [])
        keys = ", ".join(repr(c) for c in group_by)
        if group_by and not aggs:
            return f"{ins[0]}.select({keys}).distinct()"
        if group_by:
            return f"{ins[0]}.groupBy({keys}).agg({aggs})"
        return f"{ins[0]}.agg({aggs})"
    if t == "union":
        chain = ins[0] + "".join(f".union({i})" for i in ins[1:])
        return chain + (".distinct()" if p.get("distinct", False) else "")
    raise CompileError(f"{node.id}: no DataFrame form for node type {t!r}")
