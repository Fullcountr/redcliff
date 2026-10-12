"""Row-level trace: the traced rows are the step's rows, and each traces to the right source rows."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
import yaml

from redcliff.compile import compile_query
from redcliff.importer import import_sql
from redcliff.powercenter import import_powercenter
from redcliff.spec import Pipeline
from redcliff.trace import source_rows_query, trace_query, trace_rows

ROOT = Path(__file__).parents[2]
DATA = ROOT / "examples" / "data"
SPECS = ROOT / "examples" / "specs"
PC = ROOT / "examples" / "legacy" / "powercenter"

TABLES = {
    "orders": "order_id INT, customer_id INT, order_date DATE, status VARCHAR, amount DECIMAL(10,2)",
    "customers": "id INT, first_name VARCHAR, last_name VARCHAR, email VARCHAR, region VARCHAR, signup_date DATE",
    "regions": "region VARCHAR, manager VARCHAR, monthly_target DECIMAL(12,2)",
}


@pytest.fixture
def con():
    c = duckdb.connect()
    for table, ddl in TABLES.items():
        cols = ", ".join(f"'{n}': '{t}'" for n, t in (part.split(" ", 1) for part in re.split(r",\s*(?![^()]*\))", ddl)))
        c.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv('{DATA / table}.csv', header = true, columns = {{{cols}}})")
    return c


def example(name):
    return Pipeline.from_dict(yaml.safe_load((SPECS / f"{name}.yaml").read_text()))


PIPELINES = {
    "orders_revenue": lambda: example("orders_revenue"),
    "customer_cleanup": lambda: example("customer_cleanup"),
    "daily_events": lambda: example("daily_events"),
    "teradata_revenue": lambda: import_sql((ROOT / "examples/legacy/teradata_revenue.sql").read_text(), dialect="teradata",
                                           schema=json.loads((ROOT / "examples/legacy/schema.json").read_text()),
                                           name="teradata_revenue").pipeline,
    "m_customer_revenue": lambda: import_powercenter((PC / "m_customer_revenue.xml").read_text()).pipeline,
}


def norm(rows):
    return sorted((tuple(float(v) if isinstance(v, Decimal) else v for v in r) for r in rows), key=repr)


@pytest.mark.parametrize("name", PIPELINES)
def test_tracing_never_changes_a_steps_rows(con, name):
    """Every step of every example: the traced query minus provenance is the step's query."""
    pipeline = PIPELINES[name]()
    for node in pipeline.nodes:
        if node.type == "target":
            continue
        tq = trace_query(pipeline, node.id, SPECS)
        rel = con.sql(tq.sql)
        keep = [i for i, c in enumerate(rel.columns) if c not in tq.provenance]
        got = [tuple(r[i] for i in keep) for r in rel.fetchall()]
        want = con.sql(compile_query(pipeline, node.id, "duckdb", SPECS)).fetchall()
        assert norm(got) == norm(want), f"{name}: {node.id}"
        if tq.columns is not None:
            assert [rel.columns[i] for i in keep] == tq.columns


def test_an_aggregate_traces_to_the_rows_it_summed(con):
    rows = trace_rows(con, example("orders_revenue"), "out", base_dir=SPECS)
    south = next(r for r in rows if r["row"]["region"] == "South")
    assert [o["order_id"] for o in south["sources"]["orders"]] == [1, 4, 8]  # shipped orders only
    assert [c["id"] for c in south["sources"]["customers"]] == [101, 103]
    assert sum(o["amount"] for o in south["sources"]["orders"]) == south["row"]["revenue"] == Decimal("330.50")
    assert [o["row"] for o in south["sources"]["orders"]] == [1, 4, 8]  # row numbers in file order


@pytest.mark.parametrize("name", PIPELINES)
def test_every_traced_row_is_rebuilt_from_its_source_rows(con, name):
    """Recompute each traced row from only its source rows: the row must come back."""
    pipeline = PIPELINES[name]()
    for target in (n for n in pipeline.nodes if n.type == "target"):
        tq = trace_query(pipeline, target.id, SPECS)
        if tq.opaque:
            continue  # rows went through a sql step; see test_a_sql_step_is_opaque
        local = _with_table_sources(pipeline)
        for i, traced in enumerate(trace_rows(con, pipeline, target.id, base_dir=SPECS)):
            con.execute(f"CREATE SCHEMA r_{target.id}_{i}")
            for source, rows in traced["sources"].items():
                node = pipeline.node(source)
                table = node.params["table"] if node.params["format"] == "table" else source
                picked = source_rows_query(pipeline, source, [r["row"] for r in rows], SPECS)
                con.execute(f'CREATE TABLE r_{target.id}_{i}.{table} AS SELECT * EXCLUDE ("row") FROM ({picked})')
            con.execute(f"SET schema = 'r_{target.id}_{i}'")
            rebuilt = norm(con.sql(compile_query(local, target.inputs[0], "duckdb")).fetchall())
            con.execute("SET schema = 'main'")
            assert norm([tuple(traced["row"].values())])[0] in rebuilt, f"{name}: {target.id} {traced['row']}"


def _with_table_sources(pipeline: Pipeline) -> Pipeline:
    """The same pipeline reading every source from a table named after it."""
    data = pipeline.to_dict()
    for n in data["nodes"]:
        if n["type"] == "source" and n["params"]["format"] != "table":
            n["params"] = {"format": "table", "table": n["id"], **({"columns": n["params"]["columns"]}
                                                                  if n["params"].get("columns") else {})}
    return Pipeline.from_dict(data)


def test_a_sql_step_is_opaque(con):
    pipeline = PIPELINES["teradata_revenue"]()
    tq = trace_query(pipeline, "top_customer_per_region", SPECS)
    assert tq.opaque == ["top_customer_per_region"] and tq.provenance == {}
    tq = trace_query(pipeline, "regional_revenue", SPECS)
    assert set(tq.provenance.values()) == {"orders", "customers"} and not tq.opaque


def test_union_and_left_join_keep_each_inputs_rows(con):
    pipeline = Pipeline.from_dict({"name": "u", "nodes": [
        {"id": "orders", "type": "source", "params": {"format": "table", "table": "orders"}},
        {"id": "customers", "type": "source", "params": {"format": "table", "table": "customers"}},
        {"id": "big", "type": "filter", "inputs": ["orders"], "params": {"condition": "amount >= 100"}},
        {"id": "late", "type": "filter", "inputs": ["orders"], "params": {"condition": "order_id >= 7"}},
        {"id": "both", "type": "union", "inputs": ["big", "late"], "params": {"distinct": True}},
        {"id": "named", "type": "join", "inputs": ["both", "customers"],
         "params": {"how": "left", "condition": "both.customer_id = customers.id AND customers.region = 'North'"}},
    ]})
    rows = {r["row"]["order_id"]: r for r in trace_rows(con, pipeline, "named")}
    assert sorted(rows) == [1, 4, 7, 8]  # 1 and 4 are big, 7 and 8 late; no order is both
    assert [o["order_id"] for o in rows[7]["sources"]["orders"]] == [7]
    assert [c["id"] for c in rows[7]["sources"]["customers"]] == [105]  # North
    assert rows[1]["sources"]["customers"] == []  # the left join found no North customer
