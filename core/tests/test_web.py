"""Tests for the JSON bridge the browser app calls."""

import json
from pathlib import Path

import duckdb
import yaml

from redcliff import web

SPECS = Path(__file__).parents[2] / "examples" / "specs"
DATA = Path(__file__).parents[2] / "examples" / "data"


def spec(name: str) -> dict:
    return yaml.safe_load((SPECS / f"{name}.yaml").read_text())


def call(fn, *args, **kwargs) -> dict:
    out = json.loads(fn(*args, **kwargs))
    assert isinstance(out, dict) and "ok" in out
    return out


def test_analyze_returns_columns_and_lineage():
    out = call(web.analyze, spec("orders_revenue"))
    assert out["ok"] and out["valid"] and out["issues"] == []
    assert out["columns"]["revenue_by_region"] == ["region", "revenue", "order_count"]
    assert {"from": "orders.amount", "to": "shipped.amount", "type": "DIRECT", "subtype": "IDENTITY",
            "explicit": False} in out["edges"]
    assert {"from": "shipped.status", "node": "shipped", "subtype": "FILTER"} not in out["dataset_edges"]
    assert out["order"][0] in ("orders", "customers")


def test_analyze_accepts_json_text_and_reports_structural_issues():
    bad = {"version": 0, "name": "b", "nodes": [{"id": "a", "type": "filter", "inputs": ["x"],
                                                 "params": {"condition": "1"}}]}
    out = call(web.analyze, json.dumps(bad))
    assert out["ok"] and not out["valid"]
    assert out["issues"] == [{"node": "a", "message": "input 'x' does not exist"}]


def test_preview_query_reads_files_by_base_name():
    out = call(web.preview_query, spec("orders_revenue"), "out")
    assert out["node"] == "revenue_by_region"  # a target previews its input
    assert "read_csv('orders.csv'" in out["sql"]
    con = duckdb.connect()
    con.execute(f"SET file_search_path = '{DATA}'")
    assert sorted(con.sql(out["sql"]).fetchall())[0][0] == "North"


def test_code_targets():
    s = spec("customer_cleanup")
    assert "COPY (" in call(web.code, s, "duckdb")["code"]
    assert "INSERT OVERWRITE DIRECTORY" in call(web.code, s, "spark")["code"]
    assert "spark.read" in call(web.code, s, "pyspark")["code"]
    assert call(web.code, s, "spark", "cleaned")["code"].rstrip().endswith("SELECT * FROM cleaned")


def test_code_without_targets_compiles_the_last_node():
    out = call(web.import_sql, "SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
    code = call(web.code, out["spec"], "duckdb")["code"]
    assert code.rstrip().endswith("SELECT * FROM result")


def test_import_impact_and_trace():
    schema = {"orders": {"order_id": "int", "status": "text", "amount": "double"}}
    out = call(web.import_sql, "SELECT status, SUM(amount) AS total FROM orders WHERE amount > 0 GROUP BY status",
               "duckdb", json.dumps(schema))
    assert out["ok"] and out["warnings"] == []
    s = out["spec"]
    assert call(web.impact, s, "orders", "amount")["breaks"] == ["result", "result_filter"]
    assert call(web.trace, s, "result", "total")["sources"] == [
        {"column": "orders.amount", "type": "DIRECT", "subtype": "AGGREGATION"}
    ]


def test_errors_come_back_as_data():
    out = call(web.import_sql, "SELECT FROM WHERE (")
    assert not out["ok"] and "could not parse" in out["error"]
    out = call(web.preview_query, spec("orders_revenue"), "nope")
    assert not out["ok"]
