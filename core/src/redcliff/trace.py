"""Row-level trace: which source rows are behind an output row?

Column lineage says which *columns* feed a column. A row trace answers the next question, on real
data: for this row of a step, which rows of each source made it? It's computed in DuckDB by
carrying provenance alongside the data:

- every source row gets its row number, as a one-item list in a ``__rc_<k>`` column
- filter, select and derive carry the lists along with the row
- a join puts both sides' lists on the joined row
- an aggregate merges the lists of every row in the group
- a union keeps each input's lists (empty for sources the input doesn't read)

So a revenue total traces to exactly the order rows it summed, and a joined row to the customer
it matched. Rows a filter dropped aren't in the trace: they shaped the result (column lineage
calls that INDIRECT) but no output row is made of them.

A ``sql`` step is opaque: rows pass through it untraced, and the trace says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from redcliff.columns import infer_columns
from redcliff.compile import (
    CompileError,
    JOIN_SQL,
    _ancestors,
    _expr,
    _ident,
    _node_body,
    _source_relation,
    _transpile,
)
from redcliff.spec import Pipeline

PREFIX = "__rc_"
ROWNUM = "__rc_row"


@dataclass
class TraceQuery:
    sql: str  # the step's rows, plus one provenance column per source it reads
    node: str  # the step whose rows these are (a target's input, for a target)
    columns: list[str] | None  # the step's own columns, when known
    provenance: dict[str, str]  # provenance column -> source id
    opaque: list[str] = field(default_factory=list)  # sql steps the trace can't see through

    def to_dict(self) -> dict:
        return {"sql": self.sql, "node": self.node, "columns": self.columns,
                "provenance": self.provenance, "opaque": self.opaque}


def _q(name: str) -> str:
    return _transpile(_ident(name), "duckdb")


def _numbered(relation: str) -> str:
    return f"SELECT *, ROW_NUMBER() OVER () AS {ROWNUM} FROM {relation}"


def _merged(prov: str) -> exp.Expression:
    """Every row number in the group, once each, in order."""
    return sqlglot.parse_one(f"list_sort(list_distinct(flatten(list({prov})))) AS {prov}", read="duckdb")


def trace_query(pipeline: Pipeline, node_id: str, base_dir=None) -> TraceQuery:
    """DuckDB SQL for one step's rows, each with the source row numbers behind it."""
    node = pipeline.node(node_id)
    if node.type == "target":
        node_id = node.inputs[0]
    needed = _ancestors(pipeline, node_id)
    cols = infer_columns(pipeline)
    order = [n for n in pipeline.topo_order() if n.id in needed and n.type != "target"]
    key = {n.id: f"{PREFIX}{i}" for i, n in enumerate(n for n in order if n.type == "source")}
    carried: dict[str, list[str]] = {}  # node id -> provenance columns its rows carry
    opaque: list[str] = []
    blocks: list[str] = []

    def carry(*inputs: str) -> list[str]:
        return sorted({p for i in inputs for p in carried[i]}, key=lambda p: int(p[len(PREFIX):]))

    for n in order:
        p, t = n.params, n.type
        if t == "source":
            relation = _source_relation(n, "duckdb", base_dir)
            prov = key[n.id]
            text = f"SELECT * EXCLUDE ({ROWNUM}), [{ROWNUM}] AS {prov}\nFROM ({_numbered(relation)})"
            carried[n.id] = [prov]
        elif t == "sql":
            # Hand the query its inputs without provenance, under their own names.
            plain = {i: f"{PREFIX}plain_{i}" for i in n.inputs if carried[i]}
            for i, alias in plain.items():
                blocks.append(f"{_q(alias)} AS (\n  SELECT * EXCLUDE ({', '.join(carried[i])}) FROM {_q(i)}\n)")
            tree = sqlglot.parse_one(p["query"])
            tree = tree.transform(lambda e: exp.Table(this=exp.to_identifier(plain[e.name]),
                                                      alias=exp.TableAlias(this=exp.to_identifier(e.alias or e.name)))
                                  if isinstance(e, exp.Table) and not e.db and e.name in plain else e)
            text = tree.sql("duckdb", pretty=True)
            if plain:
                opaque.append(n.id)
            carried[n.id] = []
        elif t == "filter":
            text = _transpile(_node_body(n, cols, "duckdb"), "duckdb")
            carried[n.id] = carry(n.inputs[0])
        elif t in ("select", "derive"):
            prov = carry(n.inputs[0])
            tree = sqlglot.parse_one(_node_body(n, cols, "duckdb"))
            if prov and not (t == "derive" and cols[n.inputs[0]] is None):  # SELECT *, ... carries them
                tree = tree.select(*prov, copy=False)
            text = tree.sql("duckdb", pretty=True)
            carried[n.id] = prov
        elif t == "join":
            left, right = n.inputs
            lcols, rcols = cols[left], cols[right]
            lp, rp = carried[left], carried[right]
            if lcols is None or rcols is None:
                picks = [f"{_q(left)}.*" + (f" EXCLUDE ({', '.join(lp)})" if lp else ""),
                         f"{_q(right)}.*" + (f" EXCLUDE ({', '.join(rp)})" if rp else "")]
            else:
                picks = [f"{_q(left)}.{_q(c)}" for c in lcols]
                picks += [f"{_q(right)}.{_q(c)}" for c in rcols if c not in lcols]
            for prov in carry(left, right):
                sides = [f"COALESCE({_q(s)}.{prov}, [])" for s, sp in ((left, lp), (right, rp)) if prov in sp]
                picks.append(f"{sides[0] if len(sides) == 1 else f'list_concat({sides[0]}, {sides[1]})'} AS {prov}")
            how = JOIN_SQL[p.get("how", "inner")]
            text = (f"SELECT {', '.join(picks)}\nFROM {_q(left)}\n{how} {_q(right)}\n"
                    f"  ON {_expr(p['condition'], 'duckdb')}")
            carried[n.id] = carry(left, right)
        elif t == "aggregate":
            prov = carry(n.inputs[0])
            tree = sqlglot.parse_one(_node_body(n, cols, "duckdb"))
            tree.select(*[_merged(c) for c in prov], copy=False)
            text = tree.sql("duckdb", pretty=True)
            carried[n.id] = prov
        elif t == "union":
            prov = carry(*n.inputs)
            parts = []
            for i in n.inputs:
                star = "*" + (f" EXCLUDE ({', '.join(carried[i])})" if carried[i] else "")
                extra = [c if c in carried[i] else f"CAST([] AS BIGINT[]) AS {c}" for c in prov]
                parts.append(f"SELECT {', '.join([star] + extra)} FROM {_q(i)}")
            text = "\nUNION ALL\n".join(parts)
            if p.get("distinct", False) and prov:
                merged = ", ".join(_merged(c).sql("duckdb") for c in prov)
                text = f"SELECT * EXCLUDE ({', '.join(prov)}), {merged}\nFROM (\n{text}\n)\nGROUP BY ALL"
            elif p.get("distinct", False):
                text = text.replace("UNION ALL", "UNION")
            carried[n.id] = prov
        else:
            raise CompileError(f"{n.id}: cannot trace node type {t!r}")
        indented = "\n".join("  " + line for line in text.splitlines())
        blocks.append(f"-- {n.id}: {t}\n{_q(n.id)} AS (\n{indented}\n)")

    sources = {v: k for k, v in key.items()}
    sql = "WITH\n" + ",\n\n".join(blocks) + f"\nSELECT * FROM {_q(node_id)}"
    return TraceQuery(sql=sql, node=node_id, columns=cols.get(node_id),
                      provenance={c: sources[c] for c in carried[node_id]}, opaque=opaque)


def source_rows_query(pipeline: Pipeline, source_id: str, rows: list[int], base_dir=None) -> str:
    """DuckDB SQL for the given rows (1-based, in file order) of a source, numbered."""
    node = pipeline.node(source_id)
    if node.type != "source":
        raise CompileError(f"{source_id} is not a source")
    ids = ", ".join(str(int(r)) for r in rows) or "NULL"
    return (f"SELECT {ROWNUM} AS \"row\", * EXCLUDE ({ROWNUM})\n"
            f"FROM ({_numbered(_source_relation(node, 'duckdb', base_dir))})\n"
            f"WHERE {ROWNUM} IN ({ids})\nORDER BY {ROWNUM}")


def trace_rows(con, pipeline: Pipeline, node_id: str, where: str | None = None, base_dir=None) -> list[dict]:
    """Run a trace: every row of the step (or those matching ``where``) with its source rows.

    Returns [{"row": {column: value}, "sources": {source id: [{column: value}, ...]}}].
    """
    tq = trace_query(pipeline, node_id, base_dir)
    sql = f"SELECT * FROM ({tq.sql})" + (f" WHERE {_expr(where, 'duckdb')}" if where else "")
    rel = con.sql(sql)
    names = rel.columns
    out = []
    for values in rel.fetchall():
        record = dict(zip(names, values))
        row = {k: v for k, v in record.items() if k not in tq.provenance}
        sources = {}
        for prov, source in tq.provenance.items():
            ids = record[prov] or []
            src = con.sql(source_rows_query(pipeline, source, ids, base_dir))
            sources[source] = [dict(zip(src.columns, r)) for r in src.fetchall()]
        out.append({"row": row, "sources": sources})
    return out
