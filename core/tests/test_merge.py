"""Merge targets: update rows whose keys match, insert the rest, delete matched rows on request."""

from decimal import Decimal as D
from pathlib import Path

import duckdb
import pytest
import yaml

from redcliff.compile import CompileError, compile_pyspark, compile_script
from redcliff.dbt import compile_dbt
from redcliff.lakeflow import compile_lakeflow
from redcliff.lineage import Ref, blast_radius, build_graph
from redcliff.spec import Pipeline, validate

ROOT = Path(__file__).parents[2]
SPECS = ROOT / "examples" / "specs"


def merging(**params) -> Pipeline:
    data = yaml.safe_load((SPECS / "orders_revenue.yaml").read_text())
    out = next(n for n in data["nodes"] if n["id"] == "out")
    out["params"] = {"format": "table", "table": "revenue", "mode": "merge", "keys": ["region"], **params}
    return Pipeline.from_dict(data)


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE revenue (region VARCHAR, revenue DECIMAL(38,2), order_count BIGINT)")
    c.execute("INSERT INTO revenue VALUES ('North', 1.00, 1), ('West', 5.00, 5)")
    return c


def run(con, pipeline):
    con.execute(compile_script(pipeline, "duckdb", SPECS))
    return sorted(con.sql("SELECT * FROM revenue").fetchall())


def test_upsert(con):
    # North is updated, South inserted, West (not in this run) left alone.
    assert run(con, merging()) == [("North", D("239.99"), 3), ("South", D("330.50"), 3), ("West", D("5.00"), 5)]
    assert run(con, merging()) == [("North", D("239.99"), 3), ("South", D("330.50"), 3), ("West", D("5.00"), 5)]


def test_delete_when(con):
    # North matches and is deleted; a North row that didn't exist wouldn't be inserted either.
    assert run(con, merging(delete_when="region = 'North'")) == [("South", D("330.50"), 3), ("West", D("5.00"), 5)]
    assert run(con, merging(delete_when="region = 'North'")) == [("South", D("330.50"), 3), ("West", D("5.00"), 5)]


def test_the_first_run_creates_the_table():
    con = duckdb.connect()
    assert run(con, merging()) == [("North", D("239.99"), 3), ("South", D("330.50"), 3)]


def test_validation():
    def issues(**params):
        data = merging().to_dict()
        out = next(n for n in data["nodes"] if n["id"] == "out")
        out["params"] = params
        return [str(i) for i in validate(Pipeline.from_dict(data))]

    assert any("needs 'keys'" in i for i in issues(format="table", table="t", mode="merge"))
    assert any("needs a table target" in i for i in issues(format="csv", path="x.csv", mode="merge", keys=["region"]))
    assert any("'keys' is only used with 'mode: merge'" in i for i in issues(format="table", table="t", keys=["region"]))
    graph = build_graph(merging(keys=["regoin"]))
    assert any("regoin" in str(i) for i in graph.issues)
    with pytest.raises(CompileError, match="aren't columns"):
        compile_script(merging(keys=["regoin"]), "duckdb", SPECS)


def test_lineage_knows_the_keys_and_the_delete_condition():
    graph = build_graph(merging(delete_when="order_count < 3"))
    assert "out" in blast_radius(graph, Ref("revenue_by_region", "region")).rows
    assert "out" in blast_radius(graph, Ref("revenue_by_region", "order_count")).rows


def test_other_outputs():
    p = merging(delete_when="order_count < 3")
    spark = compile_script(p, "spark", SPECS)
    assert "MERGE INTO revenue AS existing" in spark and "Delta Lake or Iceberg" in spark
    pyspark = compile_pyspark(p, SPECS)
    assert ".mergeInto('revenue', F.expr('revenue.region = incoming.region'))" in pyspark
    assert ".whenMatched(F.expr('incoming.order_count < 3')).delete()" in pyspark
    compile(pyspark, "pipeline.py", "exec")
    model = compile_dbt(p, "databricks")["models/revenue.sql"]
    assert "unique_key=['region']" in model and "incremental_strategy='merge'" in model
    assert "WHERE NOT (order_count < 3)" in model
    assert "incremental_strategy='delete+insert'" in compile_dbt(merging(), "duckdb")["models/revenue.sql"]
    lake = compile_lakeflow(p)["transformations/orders_revenue.py"]
    assert "@dp.materialized_view(name='revenue_incoming')" in lake and "MERGE revenue_incoming INTO revenue" in lake
