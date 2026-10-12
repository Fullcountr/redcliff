"""M3 and M4: generate the pipeline that loads a model, and run it twice to see history kept.

Golden runs are DuckDB; Spark SQL, PySpark and Lakeflow are checked for shape, and the dbt project
is built with dbt-duckdb when it's installed.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from redcliff.cli import main
from redcliff.compile import compile_pyspark, compile_script
from redcliff.dbt import compile_dbt, write_dbt_project
from redcliff.generate import model_checks, model_pipeline, run_model_checks
from redcliff.lakeflow import compile_lakeflow
from redcliff.model import Model, load_model
from redcliff.propose import profile_files, to_data_vault
from redcliff.spec import Pipeline, dump_spec, validate

ROOT = Path(__file__).parents[2]
MODELS = ROOT / "examples" / "models"
duckdb = pytest.importorskip("duckdb")

CUSTOMERS_1 = "id,first_name,last_name,email,region,signup_date\n" \
              "101,Ana,Silva,ana@x.com,South,2025-01-15\n102,Ben,Okafor,ben@x.com,North,2025-03-02\n"
CUSTOMERS_2 = "id,first_name,last_name,email,region,signup_date\n" \
              "101,Anna,Silva,ana@x.com,South,2025-01-15\n102,Ben,Okafor,ben@x.com,East,2025-03-02\n" \
              "103,Cy,Wei,cy@x.com,South,2025-06-20\n"
ORDERS_1 = "order_id,customer_id,order_date,status,amount\n1,101,2026-09-01,shipped,10.00\n2,102,2026-09-01,shipped,20.00\n"
ORDERS_2 = ORDERS_1 + "3,102,2026-10-05,shipped,30.00\n4,103,2026-10-05,pending,40.00\n5,999,2026-10-06,shipped,5.00\n"


def sales_model(folder: Path) -> Model:
    """The example model, reading data files in ``folder``."""
    data = load_model(MODELS / "sales.yaml").to_dict()
    for e in data["entities"]:
        if e.get("source"):
            e["source"] = Path(e["source"]).name
    return Model.from_dict(data)


def run(spec: Path, db: Path, *var: str) -> None:
    args = ["run", str(spec), "--db", str(db)]
    for v in var:
        args += ["--var", v]
    assert main(args) == 0


def rows(db: Path, sql: str) -> list[tuple]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.sql(sql).fetchall()
    finally:
        con.close()


def test_the_example_models_generate_valid_pipelines():
    for name in ("sales.yaml", "sales_vault.yaml"):
        pipeline = model_pipeline(load_model(MODELS / name), MODELS)
        assert validate(pipeline) == []
    committed = Pipeline.from_dict(__import__("yaml").safe_load((MODELS / "load_sales.yaml").read_text()))
    assert committed.to_dict() == model_pipeline(load_model(MODELS / "sales.yaml"), MODELS).to_dict(), \
        "examples/models/load_sales.yaml is stale: redcliff model pipeline sales.yaml -o load_sales.yaml"


def test_kimball_load_keeps_history_and_looks_up_point_in_time(tmp_path):
    (tmp_path / "customers.csv").write_text(CUSTOMERS_1)
    (tmp_path / "orders.csv").write_text(ORDERS_1)
    model = sales_model(tmp_path)
    spec = tmp_path / "load.yaml"
    spec.write_text(dump_spec(model_pipeline(model, tmp_path), "yaml"))
    db = tmp_path / "w.duckdb"
    run(spec, db, "load_ts=TIMESTAMP '2026-09-15 00:00:00'")

    (tmp_path / "customers.csv").write_text(CUSTOMERS_2)  # Ana's name corrected, Ben moved, Cy is new
    (tmp_path / "orders.csv").write_text(ORDERS_2)
    run(spec, db, "load_ts=TIMESTAMP '2026-10-01 00:00:00'")

    dim = rows(db, "SELECT customer_key, customer_id, first_name, region, CAST(valid_from AS DATE), "
                   "CAST(valid_to AS DATE), is_current FROM dim_customer ORDER BY customer_key")
    assert dim == [
        (-1, None, None, None, __import__("datetime").date(1900, 1, 1), None, True),  # the unknown member
        (1, 101, "Anna", "South", __import__("datetime").date(1900, 1, 1), None, True),  # type 1: no new version
        (2, 102, "Ben", "North", __import__("datetime").date(1900, 1, 1), __import__("datetime").date(2026, 10, 1), False),
        (3, 102, "Ben", "East", __import__("datetime").date(2026, 10, 1), None, True),  # moved: a new version
        (4, 103, "Cy", "South", __import__("datetime").date(1900, 1, 1), None, True),
    ]
    facts = rows(db, "SELECT order_id, customer_key, customer_id, order_date_key FROM fct_orders ORDER BY order_id")
    assert facts == [
        (1, 1, 101, 20260901), (2, 2, 102, 20260901),  # September: Ben's North version
        (3, 3, 102, 20261005),  # October: Ben's East version (as it was then)
        (4, 4, 103, 20261005), (5, -1, 999, 20261006),  # an unknown customer gets the unknown member
    ]
    as_is = rows(db, "SELECT f.order_id, d.region FROM fct_orders f JOIN dim_customer d "
                     "ON d.customer_id = f.customer_id AND d.is_current WHERE f.customer_id = 102 ORDER BY 1")
    assert as_is == [(2, "East"), (3, "East")]  # Type 7: the durable key gives today's version
    assert rows(db, "SELECT COUNT(*) FROM dim_date WHERE date_key > 0") == [(3,)]
    con = duckdb.connect(str(db), read_only=True)
    results = run_model_checks(con, model)
    con.close()
    assert results and all(r["passed"] for r in results), [r for r in results if not r["passed"]]


def test_model_checks_catch_broken_history(tmp_path):
    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE dim_customer AS SELECT * FROM (VALUES "
                "(1, 101, TIMESTAMP '1900-01-01', NULL::TIMESTAMP, TRUE), "
                "(2, 101, TIMESTAMP '2026-01-01', NULL::TIMESTAMP, TRUE)) "
                "AS t(customer_key, customer_id, valid_from, valid_to, is_current)")
    model = Model.from_dict({"version": 0, "name": "m", "entities": [{"id": "dim_customer", "kind": "dimension", "attributes": [
        {"name": "customer_key", "type": "bigint", "role": "surrogate_key"},
        {"name": "customer_id", "type": "bigint", "role": "business_key"}]}]})
    failed = {r["check"] for r in run_model_checks(con, model) if not r["passed"]}
    assert failed == {"one current row per customer_id", "no overlapping versions"}
    assert len(model_checks(model)) == 3


def test_history_modes_compile_for_spark_pyspark_lakeflow():
    pipeline = model_pipeline(load_model(MODELS / "sales.yaml"), MODELS)
    spark = compile_script(pipeline, "spark")
    assert "MERGE INTO dim_customer AS existing" in spark and "INSERT INTO dim_customer (customer_key" in spark
    assert "valid_to IS NULL" in spark  # the point-in-time lookup
    code = compile_pyspark(pipeline)
    ast.parse(code)
    assert "createOrReplaceTempView('dim_customer_incoming')" in code and "spark.sql('MERGE INTO dim_customer" in code
    lake = next(v for k, v in compile_lakeflow(pipeline).items() if k.endswith(".py"))
    ast.parse(lake)
    assert "keep history of (SCD2) dim_customer from dim_customer_incoming" in lake


def test_dbt_project_uses_a_snapshot_and_builds(tmp_path):
    files = compile_dbt(model_pipeline(load_model(MODELS / "sales.yaml"), MODELS), "duckdb", MODELS)
    snap = files["snapshots/dim_customer.sql"]
    assert "{% snapshot dim_customer %}" in snap and "check_cols=['email', 'region', 'signup_date']" not in snap
    assert "strategy='check'" in snap and "AS customer_key, *" in snap
    assert "snapshot-paths: [snapshots]" in files["dbt_project.yml"]
    assert any("{{ ref('dim_customer') }}" in text for path, text in files.items() if path.startswith("models/"))
    dbt = shutil.which("dbt") or str(Path(sys.executable).parent / "dbt")
    if not Path(dbt).exists():
        pytest.skip("dbt isn't installed")
    write_dbt_project(files, tmp_path)
    done = subprocess.run([dbt, "build", "--profiles-dir", "."], cwd=tmp_path, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-2000:]
    db = next(tmp_path.glob("*.duckdb"))
    assert rows(db, "SELECT COUNT(*) FROM fct_orders WHERE customer_key = -1") == [(0,)]


def test_vault_load_inserts_only_changes(tmp_path):
    (tmp_path / "customers.csv").write_text(CUSTOMERS_1)
    (tmp_path / "orders.csv").write_text(ORDERS_1)
    kimball = profile_files(duckdb.connect(), [tmp_path / "orders.csv", tmp_path / "customers.csv"], base_dir=tmp_path)
    vault = to_data_vault(kimball).model
    assert [e.id for e in vault.entities] == ["hub_customer", "sat_customer", "hub_order", "lnk_order_customer", "sat_order"]
    spec = tmp_path / "load.yaml"
    spec.write_text(dump_spec(model_pipeline(vault, tmp_path), "yaml"))
    db = tmp_path / "w.duckdb"
    run(spec, db, "load_ts=TIMESTAMP '2026-09-15 00:00:00'")
    run(spec, db, "load_ts=TIMESTAMP '2026-09-16 00:00:00'")  # the same data again: nothing new
    assert rows(db, "SELECT COUNT(*) FROM sat_customer") == [(2,)]
    (tmp_path / "customers.csv").write_text(CUSTOMERS_2)
    (tmp_path / "orders.csv").write_text(ORDERS_2)
    run(spec, db, "load_ts=TIMESTAMP '2026-10-01 00:00:00'")
    assert rows(db, "SELECT COUNT(*) FROM hub_customer") == [(3,)]
    # Ana's name and Ben's region changed, Cy is new: three satellite rows, Ben's unchanged twin isn't
    assert rows(db, "SELECT COUNT(*) FROM sat_customer") == [(5,)]
    # the link's customer hash matches the hub's for the same business key
    assert rows(db, "SELECT COUNT(*) FROM lnk_order_customer l JOIN hub_customer h ON h.customer_hk = l.customer_hk") == [(4,)]
    assert rows(db, "SELECT customer_hk = MD5('102') FROM hub_customer WHERE customer_id = 102") == [(True,)]
    assert rows(db, "SELECT region FROM customer_current WHERE customer_id = 102") == [("East",)]  # the mart
    con = duckdb.connect(str(db), read_only=True)
    assert all(r["passed"] for r in run_model_checks(con, vault)) is False  # order 5's customer 999 has no hub row
    con.close()


def test_history_params_are_checked():
    def issues(params):
        data = {"version": 0, "name": "p", "nodes": [
            {"id": "s", "type": "source", "params": {"format": "csv", "path": "x.csv", "columns": {"id": "int", "v": "int"}}},
            {"id": "t", "type": "target", "inputs": ["s"], "params": {"format": "table", "table": "t", **params}}]}
        return " ".join(str(i) for i in validate(Pipeline.from_dict(data)))
    assert issues({"mode": "scd2", "keys": ["id"]}) == ""
    assert "needs 'keys'" in issues({"mode": "scd2"})
    assert "'compare'" in issues({"mode": "insert_changed", "keys": ["id"]})
    assert "only used with 'mode: scd2'" in issues({"mode": "scd1", "keys": ["id"], "scd1": ["v"]})
    assert "needs a 'surrogate_key'" in issues({"mode": "scd2", "keys": ["id"], "unknown_member": True})
    assert "needs a table target" in issues({"mode": "scd2", "keys": ["id"], "format": "csv", "path": "o.csv"})


def test_cli_pipeline_and_test(tmp_path, capsys):
    (tmp_path / "customers.csv").write_text(CUSTOMERS_1)
    (tmp_path / "orders.csv").write_text(ORDERS_1)
    model = tmp_path / "sales.yaml"
    model.write_text(__import__("redcliff.model", fromlist=["dump_model"]).dump_model(sales_model(tmp_path)))
    spec = tmp_path / "out" / "load.yaml"
    spec.parent.mkdir()
    assert main(["model", "pipeline", str(model), "-o", str(spec)]) == 0
    assert "path: ../customers.csv" in spec.read_text()  # paths follow the spec's folder
    run(spec, tmp_path / "w.duckdb")
    capsys.readouterr()
    assert main(["model", "test", str(model), "--db", str(tmp_path / "w.duckdb")]) == 0
    assert "7 passed, 0 failed" in capsys.readouterr().out
