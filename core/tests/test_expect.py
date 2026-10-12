"""Data quality expectations: parsing, DuckDB results, the CLI, and dbt and Lakeflow output."""

from pathlib import Path

import duckdb
import pytest
import yaml

from redcliff.cli import main
from redcliff.dbt import compile_dbt
from redcliff.expect import parse_expectations, run_checks
from redcliff.lakeflow import compile_lakeflow
from redcliff.lineage import build_graph
from redcliff.spec import Pipeline, dump_spec, validate

ROOT = Path(__file__).parents[2]
SPECS = ROOT / "examples" / "specs"


def example(**expect) -> Pipeline:
    data = yaml.safe_load((SPECS / "orders_revenue.yaml").read_text())
    for n in data["nodes"]:
        if n["id"] in expect:
            n["expect"] = expect[n["id"]]
    return Pipeline.from_dict(data)


def test_the_example_passes():
    results = run_checks(duckdb.connect(), example(), base_dir=SPECS)
    assert [(r.expectation.node, r.expectation.name, r.passed) for r in results] == [
        ("orders", "unique_order_id", True), ("orders", "not_null_customer_id", True),
        ("orders", "accepted_values_status", True), ("revenue_by_region", "revenue_is_positive", True),
        ("revenue_by_region", "row_count", True)]


def test_failures_with_their_rows():
    p = example(orders=[{"accepted_values": {"column": "status", "values": ["shipped", "pending"]}},
                        {"unique": ["customer_id"]}],
                revenue_by_region=[{"condition": "revenue > 250", "severity": "warn"},
                                   {"row_count": {"min": 3, "max": 10}}])
    r = {x.expectation.name: x for x in run_checks(duckdb.connect(), p, base_dir=SPECS)}
    assert r["accepted_values_status"].failures == 1 and r["accepted_values_status"].sample[0][3] == "cancelled"
    assert r["unique_customer_id"].failures == 3  # customers 101, 102 and 103 have two orders each
    assert sorted(row[0] for row in r["unique_customer_id"].sample) == [101, 102, 103]
    assert r["condition_revenue_250"].sample[0][0] == "North" and r["condition_revenue_250"].expectation.severity == "warn"
    assert r["row_count"].sample == [[2]]


def test_nulls_pass_row_level_checks():
    con = duckdb.connect()
    p = example(orders=[{"condition": "CASE WHEN order_id = 1 THEN NULL ELSE TRUE END"}])
    assert run_checks(con, p, base_dir=SPECS)[0].passed


@pytest.mark.parametrize("raw, problem", [
    ("x", "'expect' must be a list"),
    ([{"not_null": "a", "unique": "a"}], "exactly one of"),
    ([{"nope": 1}], "unknown keys ['nope']"),
    ([{"not_null": ["a"]}], "not_null takes a column name"),
    ([{"accepted_values": {"column": "a"}}], "accepted_values takes"),
    ([{"condition": "a >"}], "not valid SQL"),
    ([{"row_count": {"min": -1}}], "whole numbers"),
    ([{"not_null": "a", "severity": "fatal"}], "'severity' must be one of"),
    ([{"not_null": "a"}, {"not_null": "a"}], "two checks are named 'not_null_a'"),
    ([{"not_null": "a", "name": "Bad Name"}], "'name' must be lowercase"),
])
def test_problems(raw, problem):
    assert any(problem in m for m in parse_expectations("n", raw)[1])
    assert any(problem in str(i) for i in validate(example(orders=raw)))


def test_unknown_columns_and_variables():
    graph = build_graph(example(shipped=[{"not_null": "customr_id"}, {"condition": "amout > 0"}]))
    msgs = [str(i) for i in graph.issues]
    assert any("'customr_id'" in m for m in msgs) and any("'amout'" in m for m in msgs)
    p = example(orders=[{"condition": "amount <= ${max_amount}"}])
    p.variables = {"max_amount": 100}
    assert run_checks(duckdb.connect(), p, base_dir=SPECS)[0].failures == 2  # 120.50 and 200.00


def test_cli(tmp_path, capsys):
    data = example(revenue_by_region=[{"row_count": {"min": 5}}]).to_dict()
    for n in data["nodes"]:
        if n["params"].get("path"):
            n["params"]["path"] = str((SPECS / n["params"]["path"]).resolve()) if n["type"] == "source" \
                else str(tmp_path / "out.parquet")
    spec = tmp_path / "p.yaml"
    spec.write_text(dump_spec(data))
    assert main(["test", str(spec)]) == 1
    out = capsys.readouterr().out
    assert "FAIL  revenue_by_region.row_count  (row count >= 5): 1 failing row" in out
    assert "3 passed, 1 failed, 0 warned" in out
    assert main(["run", str(spec)]) == 1  # a failed expectation stops the write
    assert "not writing" in capsys.readouterr().err and not (tmp_path / "out.parquet").exists()
    assert main(["run", str(spec), "--skip-checks"]) == 0 and (tmp_path / "out.parquet").exists()


def test_dbt_and_lakeflow():
    files = compile_dbt(example(), "duckdb", SPECS)
    test = files["tests/revenue_by_region__revenue_is_positive.sql"]
    assert "{{ ref('int_revenue_by_region') }}" in test and "WHERE\n  NOT (\n    revenue > 0\n  )" in test
    assert "{{ ref('orders') }}" in files["tests/orders__unique_order_id.sql"]
    assert "materialized='view'" in files["models/int_revenue_by_region.sql"]
    warn = compile_dbt(example(shipped=[{"not_null": "status", "severity": "warn"}]))["tests/shipped__not_null_status.sql"]
    assert "config(severity='warn')" in warn
    code = compile_lakeflow(example(), SPECS)["transformations/orders_revenue.py"]
    assert "@expect('revenue_is_positive', 'revenue > 0')" in code
    assert "@expect('not_null_customer_id', 'NOT customer_id IS NULL')" in code or \
        "@expect('not_null_customer_id', 'customer_id IS NOT NULL')" in code
    assert "check order_id is unique (unique_order_id) after the pipeline" in code
    compile(code, "pipeline.py", "exec")
