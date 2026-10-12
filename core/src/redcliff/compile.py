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
from redcliff.spec import TABLE_PATTERN, Node, Pipeline, bind

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


def _table(node: Node) -> str:
    """The table a source or target names, checked so that pasting it into code can only name a table."""
    table = node.params["table"]
    if not isinstance(table, str) or not TABLE_PATTERN.match(table):
        raise CompileError(f"{node.id}: {table!r} is not a table name")
    return table


def _one_line(text) -> str:
    """Spec text for a comment in generated code: one line, so it can't start a statement."""
    return " ".join(str(text).split())


def _doc_text(text) -> str:
    """Spec text for a Python docstring or comment: one line, with no way to close the docstring."""
    return _one_line(text).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


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
        return _table(node)
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
                views.append(f"{node.id} <- {node.params['format']} {_one_line(node.params['path'])}")
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


def variables_note(variables: dict, prefix: str) -> str:
    """Comment lines naming the variable values baked into generated code."""
    if not variables:
        return ""
    lines = [f"{prefix}Variables, with the values used here (change them with --var NAME=VALUE):"]
    lines += [f"{prefix}  {k} = {v!r}" for k, v in variables.items()]
    return "\n".join(lines) + "\n"


def _with(blocks: list[str]) -> str:
    return "WITH\n" + ",\n\n".join(blocks) if blocks else ""


# --------------------------------------------------------------------------- public API


def compile_query(pipeline: Pipeline, node_id: str, dialect: str = "duckdb", base_dir=None) -> str:
    """A SELECT returning one node's output: for previews, tests and ad hoc runs."""
    _check_dialect(dialect)
    pipeline = bind(pipeline)
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


def _qualify(neutral: str, table: str) -> exp.Expression:
    """An expression over the incoming rows, with bare columns qualified by ``table``."""
    return sqlglot.parse_one(neutral).transform(
        lambda e: exp.column(e.name, table=table) if isinstance(e, exp.Column) and not e.table else e)


def _merge_statement(target: Node, dialect: str, blocks: list[str], columns: list[str] | None) -> str:
    """MERGE the target's input into its table: update rows whose keys match, insert the rest,
    and delete matched rows where ``delete_when`` holds."""
    p = target.params
    if not columns:
        raise CompileError(f"{target.id}: a merge target needs its input's columns; declare source columns")
    keys = p["keys"]
    missing = [k for k in keys if k not in columns]
    if missing:
        raise CompileError(f"{target.id}: merge keys {missing} aren't columns of {target.inputs[0]!r}")
    q = lambda c: _transpile(_ident(c), dialect)  # noqa: E731
    src = _transpile(_ident(target.inputs[0]), dialect)
    inner = f"{_with(blocks)}\nSELECT * FROM {src}" if blocks else f"SELECT * FROM {src}"
    using = "\n".join("  " + line for line in inner.splitlines())
    on = " AND ".join(f"existing.{q(k)} = incoming.{q(k)}" for k in keys)
    lines = [f"MERGE INTO {_table(target)} AS existing", f"USING (\n{using}\n) AS incoming", f"ON {on}"]
    delete = p.get("delete_when")
    if delete:
        cond = _qualify(delete, "incoming").sql(dialect)
        lines.append(f"WHEN MATCHED AND {cond} THEN DELETE")
    updates = [c for c in columns if c not in keys]
    if updates:
        lines.append("WHEN MATCHED THEN UPDATE SET " + ", ".join(f"{q(c)} = incoming.{q(c)}" for c in updates))
    insert_when = f" AND NOT ({_qualify(delete, 'incoming').sql(dialect)})" if delete else ""
    lines.append(f"WHEN NOT MATCHED{insert_when} THEN INSERT ({', '.join(q(c) for c in columns)}) "
                 f"VALUES ({', '.join(f'incoming.{q(c)}' for c in columns)})")
    head = f"-- {target.id}: target (merge on {', '.join(keys)})\n"
    if dialect == "duckdb":  # the first run creates the table
        head += (f"CREATE TABLE IF NOT EXISTS {_table(target)} AS\n{inner}\nLIMIT 0;\n\n"
                 f"-- {target.id}: merge\n")
    else:
        head += "-- Spark runs MERGE on tables with row-level updates: Delta Lake or Iceberg.\n"
    return head + "\n".join(lines)


HISTORY_MODES = ("scd1", "scd2", "insert_new", "insert_changed")
BEGINNING = "TIMESTAMP '1900-01-01 00:00:00'"  # first versions are valid from the start, so old facts find them


def _history_statement(target: Node, dialect: str, blocks: list[str], columns: list[str] | None,
                       relation: str | None = None) -> str:
    """The statements for a history mode, in order. ``relation`` replaces the CTE chain (PySpark views).

    - scd1: overwrite changed columns; insert new keys (with a surrogate key when asked)
    - scd2: overwrite ``scd1`` columns on every version, refresh ``ignore`` columns on the current
      one, close versions whose tracked columns changed, insert new versions; valid_from, valid_to
      and is_current are added
    - insert_new: insert rows whose keys aren't in the table (Data Vault hubs and links)
    - insert_changed: insert rows whose ``compare`` column differs from the latest row for their
      keys (Data Vault satellites, on a hashdiff)
    The table is created on the first run, empty, with the incoming rows' columns.
    """
    p = target.params
    mode = p["mode"]
    if not columns:
        raise CompileError(f"{target.id}: a {mode} target needs its input's columns; declare source columns")
    keys = p["keys"]
    missing = [k for k in keys if k not in columns]
    if missing:
        raise CompileError(f"{target.id}: keys {missing} aren't columns of {target.inputs[0]!r}")
    q = lambda c: _transpile(_ident(c), dialect)  # noqa: E731
    table = _table(target)
    if relation is None:
        src = _transpile(_ident(target.inputs[0]), dialect)
        inner = f"{_with(blocks)}\nSELECT * FROM {src}" if blocks else f"SELECT * FROM {src}"
        incoming = "(\n" + "\n".join("  " + line for line in inner.splitlines()) + "\n)"
    else:
        incoming = relation
    on = " AND ".join(f"existing.{q(k)} = incoming.{q(k)}" for k in keys)
    first = q(keys[0])
    differ = lambda cols: " OR ".join(f"existing.{q(c)} IS DISTINCT FROM incoming.{q(c)}" for c in cols)  # noqa: E731
    sets = lambda cols: ", ".join(f"{q(c)} = incoming.{q(c)}" for c in cols)  # noqa: E731
    sk = p.get("surrogate_key")
    make_sk = bool(sk) and sk not in columns
    scd2 = mode == "scd2"
    out = [f"-- {target.id}: target ({mode} on {', '.join(keys)})"]

    extra_create = (f"CAST(NULL AS BIGINT) AS {q(sk)}, " if make_sk else "")
    tail_create = (", CAST(NULL AS TIMESTAMP) AS valid_from, CAST(NULL AS TIMESTAMP) AS valid_to, "
                   "CAST(NULL AS BOOLEAN) AS is_current") if scd2 else ""
    out.append(f"CREATE TABLE IF NOT EXISTS {table} AS\nSELECT {extra_create}incoming.*{tail_create}\n"
               f"FROM {incoming} AS incoming\nLIMIT 0")
    if mode in ("insert_new", "insert_changed"):
        cols = ", ".join(q(c) for c in columns)
        if mode == "insert_new":
            existing = f"(SELECT DISTINCT {', '.join(q(k) for k in keys)} FROM {table})"
            where = f"existing.{first} IS NULL"
        else:
            cmp_, order = q(p["compare"]), q(p["order_by"])
            part = ", ".join(q(k) for k in keys)
            existing = (f"(\n  SELECT {part}, {cmp_} FROM (\n    SELECT {part}, {cmp_}, ROW_NUMBER() OVER "
                        f"(PARTITION BY {part} ORDER BY {order} DESC) AS rn FROM {table}\n  ) AS ranked WHERE rn = 1\n)")
            where = f"existing.{first} IS NULL OR existing.{cmp_} IS DISTINCT FROM incoming.{cmp_}"
        out.append(f"INSERT INTO {table} ({cols})\nSELECT {', '.join(f'incoming.{q(c)}' for c in columns)}\n"
                   f"FROM {incoming} AS incoming\nLEFT JOIN {existing} AS existing ON {on}\nWHERE {where}")
        return ";\n\n".join([out[0] + "\n" + out[1]] + out[2:])

    attrs = [c for c in columns if c not in keys and c != sk]
    type1 = attrs if mode == "scd1" else [c for c in p.get("scd1") or [] if c in attrs]
    ignore = [c for c in p.get("ignore") or [] if c in attrs] if scd2 else []
    tracked = [c for c in attrs if c not in type1 and c not in ignore] if scd2 else []
    if type1:
        out.append(f"MERGE INTO {table} AS existing\nUSING {incoming} AS incoming\nON {on}\n"
                   f"WHEN MATCHED AND ({differ(type1)}) THEN UPDATE SET {sets(type1)}")
    if scd2 and (tracked or ignore):
        as_of = _expr(p.get("as_of") or "CURRENT_TIMESTAMP", dialect)
        whens = []
        if tracked:
            whens.append(f"WHEN MATCHED AND ({differ(tracked)}) THEN UPDATE SET valid_to = {as_of}, is_current = FALSE")
        if ignore:
            whens.append(f"WHEN MATCHED AND ({differ(ignore)}) THEN UPDATE SET {sets(ignore)}")
        out.append(f"MERGE INTO {table} AS existing\nUSING {incoming} AS incoming\nON {on} AND existing.is_current\n"
                   + "\n".join(whens))
    names = ([q(sk)] if make_sk else []) + [q(c) for c in columns] + (["valid_from", "valid_to", "is_current"] if scd2 else [])
    values = ([f"COALESCE(mx.m, 0) + ROW_NUMBER() OVER (ORDER BY {', '.join(f'incoming.{q(k)}' for k in keys)})"]
              if make_sk else []) + [f"incoming.{q(c)}" for c in columns]
    joins = [f"LEFT JOIN (SELECT {', '.join(q(k) for k in keys)} FROM {table}"
             + (" WHERE is_current" if scd2 else "") + f") AS existing ON {on}"]
    if scd2:
        as_of = _expr(p.get("as_of") or "CURRENT_TIMESTAMP", dialect)
        seen_on = " AND ".join(f"seen.{q(k)} = incoming.{q(k)}" for k in keys)
        joins.append(f"LEFT JOIN (SELECT DISTINCT {', '.join(q(k) for k in keys)} FROM {table}) AS seen ON {seen_on}")
        values += [f"CASE WHEN seen.{first} IS NULL THEN {BEGINNING} ELSE {as_of} END", "CAST(NULL AS TIMESTAMP)", "TRUE"]
    if make_sk:
        joins.append(f"CROSS JOIN (SELECT MAX({q(sk)}) AS m FROM {table}) AS mx")
    out.append(f"INSERT INTO {table} ({', '.join(names)})\nSELECT {', '.join(values)}\nFROM {incoming} AS incoming\n"
               + "\n".join(joins) + f"\nWHERE existing.{first} IS NULL")
    if sk and p.get("unknown_member"):
        u_names = [q(sk)] + (["valid_from", "valid_to", "is_current"] if scd2 else [])
        u_values = ["-1"] + ([BEGINNING, "CAST(NULL AS TIMESTAMP)", "TRUE"] if scd2 else [])
        out.append(f"-- {target.id}: the unknown member, for facts whose key isn't in the dimension\n"
                   f"INSERT INTO {table} ({', '.join(u_names)})\nSELECT {', '.join(u_values)}\n"
                   f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {q(sk)} = -1)")
    return ";\n\n".join([out[0] + "\n" + out[1]] + out[2:])


def _write_statement(target: Node, dialect: str, base_dir, blocks: list[str],
                     columns: list[str] | None = None) -> str:
    p = target.params
    if p.get("mode") == "merge":
        return _merge_statement(target, dialect, blocks, columns)
    if p.get("mode") in HISTORY_MODES:
        return _history_statement(target, dialect, blocks, columns)
    src = _transpile(_ident(target.inputs[0]), dialect)
    query = f"SELECT * FROM {src}"
    mode = p.get("mode", "overwrite")
    fmt = p["format"]
    with_sql = _with(blocks)

    if dialect == "duckdb":
        full = f"{with_sql}\n{query}" if with_sql else query
        if fmt == "table":
            if mode == "append":
                return f"-- {target.id}: target\nINSERT INTO {_table(target)}\n{full}"
            return f"-- {target.id}: target\nCREATE OR REPLACE TABLE {_table(target)} AS\n{full}"
        if mode == "append":
            raise CompileError(f"{target.id}: DuckDB cannot append to a {fmt} file; use a table target")
        options = "FORMAT parquet" if fmt == "parquet" else "FORMAT csv, HEADER true"
        inner = "\n".join("  " + line for line in full.splitlines())
        return f"-- {target.id}: target\nCOPY (\n{inner}\n) TO {_literal(_path(target, base_dir))} ({options})"

    # spark
    if fmt == "table":
        verb = "INSERT INTO" if mode == "append" else "INSERT OVERWRITE TABLE"
        insert = f"{verb} {_table(target)}"
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
    variables = dict(pipeline.variables)
    pipeline = bind(pipeline)
    targets = [n for n in pipeline.topo_order() if n.type == "target"]
    if not targets:
        raise CompileError("the pipeline has no target to write")
    statements: list[str] = []
    all_views: list[str] = []
    cols = infer_columns(pipeline)
    for target in targets:
        blocks, views = _cte_chain(pipeline, _ancestors(pipeline, target.id), dialect, base_dir)
        all_views += [v for v in views if v not in all_views]
        pre, post = _hooks(target, dialect)
        statements += pre
        statements.append(_write_statement(target, dialect, base_dir, blocks, cols.get(target.inputs[0])))
        statements += post
    header = f"-- Redcliff pipeline: {_one_line(pipeline.name)} ({dialect})\n" + variables_note(variables, "-- ")
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
    variables = dict(pipeline.variables)
    pipeline = bind(pipeline)
    cols = infer_columns(pipeline)
    lines = [
        f'"""Redcliff pipeline: {_doc_text(pipeline.name)}. Generated code; edit the spec, not this file."""',
        *([variables_note(variables, "# ").rstrip("\n")] if variables else []),
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
            if p.get("mode") == "merge":
                lines += _pyspark_merge(node, ins[0])
            elif p.get("mode") in HISTORY_MODES:
                view = f"{node.id}_incoming"
                lines.append(f"{ins[0]}.createOrReplaceTempView({view!r})")
                lines.append(f"# {node.id}: {p['mode']}; Spark runs MERGE on tables with row-level updates (Delta, Iceberg)")
                text = _history_statement(node, "spark", [], cols.get(node.inputs[0]), relation=view)
                for statement in text.split(";\n\n"):
                    body = "\n".join(line for line in statement.splitlines() if not line.startswith("--"))
                    lines.append(f"spark.sql({body!r})")
            elif p["format"] == "table":
                lines.append(f"{writer}.saveAsTable({p['table']!r})")
            else:
                if p["format"] == "csv":
                    writer += '.option("header", True)'
                lines.append(f"{writer}.{p['format']}({_path(node, base_dir)!r})")
            for sql in p.get("post_sql") or []:
                lines.append(f"spark.sql({_statement(sql, 'spark')!r})  # post-SQL")
    return "\n".join(lines) + "\n"


def _pyspark_merge(node: Node, df: str) -> list[str]:
    """DataFrame.mergeInto (Spark 4): needs a table format with row-level updates (Delta, Iceberg)."""
    p = node.params
    table = _table(node)
    name = table.split(".")[-1]
    on = " AND ".join(f"{name}.{k} = incoming.{k}" for k in p["keys"])
    out = ["# Spark runs MERGE on tables with row-level updates: Delta Lake or Iceberg.",
           f'({df}.alias("incoming")', f"    .mergeInto({table!r}, F.expr({on!r}))"]
    if p.get("delete_when"):
        cond = _qualify(p["delete_when"], "incoming").sql("spark")
        out.append(f"    .whenMatched(F.expr({cond!r})).delete()")
        out.append("    .whenMatched().updateAll()")
        out.append(f"    .whenNotMatched(F.expr({f'NOT ({cond})'!r})).insertAll()")
    else:
        out += ["    .whenMatched().updateAll()", "    .whenNotMatched().insertAll()"]
    out.append("    .merge())")
    return out


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
