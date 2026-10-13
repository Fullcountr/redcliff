"""Generated dbt projects, run with dbt-duckdb.

For every pipeline: generate the project, `dbt build` it (seeds, models, hooks), and require every
target to hold the rows the compiled DuckDB query returns. Skipped unless dbt-duckdb is installed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pytest
import yaml

pytest.importorskip("dbt.adapters.duckdb", reason="pip install dbt-duckdb to run the dbt tests")

from dbt.cli.main import dbtRunner  # noqa: E402

from redcliff.compile import compile_query  # noqa: E402
from redcliff.dbt import compile_dbt, write_dbt_project  # noqa: E402
from redcliff.importer import import_sql  # noqa: E402
from redcliff.powercenter import import_powercenter  # noqa: E402
from redcliff.spec import Pipeline, bind  # noqa: E402
from redcliff.ssis import import_ssis  # noqa: E402
from redcliff.talend import import_talend  # noqa: E402

ROOT = Path(__file__).parents[2]
DATA = ROOT / "examples" / "data"
SPECS = ROOT / "examples" / "specs"
PC = ROOT / "examples" / "legacy" / "powercenter"

TABLES = {
    "orders": "order_id INT, customer_id INT, order_date DATE, status VARCHAR, amount DECIMAL(10,2)",
    "customers": "id INT, first_name VARCHAR, last_name VARCHAR, email VARCHAR, region VARCHAR, signup_date DATE",
    "regions": "region VARCHAR, manager VARCHAR, monthly_target DECIMAL(12,2)",
    "store_sales": "store_id INT, region VARCHAR, q1_sales DECIMAL(12,2), q2_sales DECIMAL(12,2), "
                   "q3_sales DECIMAL(12,2), q4_sales DECIMAL(12,2)",
}


def create_tables(con) -> None:
    for table, ddl in TABLES.items():
        cols = ", ".join(f"'{c}': '{t}'" for c, t in (part.split(" ", 1) for part in re.split(r",\s*(?![^()]*\))", ddl)))
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_csv('{DATA / table}.csv', header = true, "
                    f"columns = {{{cols}}})")


def example(name):
    return Pipeline.from_dict(yaml.safe_load((SPECS / f"{name}.yaml").read_text()))


def without_hooks(pipeline: Pipeline) -> Pipeline:
    """Platform SQL like ANALYZE TABLE ... COMPUTE STATISTICS is Spark's, not DuckDB's."""
    data = pipeline.to_dict()
    for n in data["nodes"]:
        n.get("params", {}).pop("pre_sql", None)
        n.get("params", {}).pop("post_sql", None)
    return Pipeline.from_dict(data)


PIPELINES = {
    "orders_revenue": lambda: example("orders_revenue"),
    "customer_cleanup": lambda: example("customer_cleanup"),
    "daily_events": lambda: example("daily_events"),
    "teradata_revenue": lambda: import_sql((ROOT / "examples/legacy/teradata_revenue.sql").read_text(), dialect="teradata",
                                           schema=json.loads((ROOT / "examples/legacy/schema.json").read_text()),
                                           name="teradata_revenue").pipeline,
    "m_customer_revenue": lambda: import_powercenter((PC / "m_customer_revenue.xml").read_text()).pipeline,
    "wf_customer_revenue": lambda: without_hooks(import_powercenter(
        (PC / "wf_customer_revenue.xml").read_text(), params=(PC / "wf_customer_revenue.par").read_text()).pipeline),
    "wf_store_mart": lambda: import_powercenter((PC / "wf_store_mart.xml").read_text()).pipeline,
    "ssis_region_revenue": lambda: import_ssis((ROOT / "examples/legacy/ssis/LoadRegionRevenue.dtsx").read_text()).pipeline,
    "talend_order_revenue": lambda: import_talend(  # tMap lookups (filtered, inner with rejects), merge
        (ROOT / "examples/legacy/talend/j_order_revenue.item").read_text(), name="j_order_revenue").pipeline,
}


def norm(rows):
    return sorted((tuple(round(float(v), 6) if isinstance(v, (int, float)) and not isinstance(v, bool)
                         else float(v) if v.__class__.__name__ == "Decimal" else v for v in r) for r in rows), key=repr)


@pytest.mark.parametrize("name", PIPELINES)
def test_dbt_build_matches_the_compiled_pipeline(tmp_path, monkeypatch, name):
    pipeline = PIPELINES[name]()
    project = tmp_path / "project"
    files = compile_dbt(pipeline, "duckdb", SPECS)
    write_dbt_project(files, project)
    db = project / f"{yaml.safe_load(files['dbt_project.yml'])['name']}.duckdb"
    with duckdb.connect(str(db)) as con:
        create_tables(con)

    monkeypatch.chdir(project)  # as a user runs it: external file targets are relative to the project
    result = dbtRunner().invoke(["build", "--project-dir", str(project), "--profiles-dir", str(project), "--quiet"])
    errors = [f"{r.node.name}: {r.message}" for r in (result.result or []) if str(r.status) in ("error", "fail")]
    assert result.success, f"dbt build failed for {name}: {result.exception or errors}"

    reference = duckdb.connect()
    create_tables(reference)
    with duckdb.connect(str(db)) as built:  # dbt still holds a connection, so same settings
        for target in (n for n in bind(pipeline).nodes if n.type == "target"):  # paths with variables filled in
            want = norm(reference.execute(compile_query(pipeline, target.inputs[0], "duckdb", SPECS)).fetchall())
            p = target.params
            if p["format"] == "table":
                got = built.execute(f"SELECT * FROM main.{p['table'].split('.')[-1]}").fetchall()
            else:
                reader = "read_parquet" if p["format"] == "parquet" else "read_csv_auto"
                got = built.execute(f"SELECT * FROM {reader}('{project / p['path']}')").fetchall()
            assert norm(got) == want, f"{name}: {target.id}"


def test_project_shape_for_a_powercenter_import():
    pipeline = import_powercenter((PC / "wf_customer_revenue.xml").read_text(),
                                  params=(PC / "wf_customer_revenue.par").read_text()).pipeline
    files = compile_dbt(pipeline, "databricks")
    target = files["models/region_monthly_revenue.sql"]
    assert "materialized='table'" in target  # the model is named after its table, so no alias
    assert "post_hook=['ANALYZE TABLE region_monthly_revenue COMPUTE STATISTICS']" in target
    assert "materialized='incremental'" in files["models/order_exceptions.sql"]  # mode: append
    assert "{{ source('raw', 'orders') }} AS orders" in files["models/int_sq_orders.sql"]
    assert "profiles.yml" not in files  # only DuckDB gets a ready-to-run profile
    docs = yaml.safe_load(files["models/schema.yml"])
    revenue = next(m for m in docs["models"] if m["name"] == "region_monthly_revenue")
    assert {"name": "revenue", "description": "From orders.amount (aggregation)."} in revenue["columns"]


def test_a_failing_expectation_fails_dbt_build(tmp_path, monkeypatch):
    data = example("orders_revenue").to_dict()
    agg = next(n for n in data["nodes"] if n["id"] == "revenue_by_region")
    agg["expect"] = [{"row_count": {"min": 5}}, {"condition": "revenue > 250", "severity": "warn"}]
    files = compile_dbt(Pipeline.from_dict(data), "duckdb", SPECS)
    project = tmp_path / "project"
    write_dbt_project(files, project)
    monkeypatch.chdir(project)
    result = dbtRunner().invoke(["build", "--project-dir", str(project), "--profiles-dir", str(project), "--quiet"])
    statuses = {r.node.name: str(r.status) for r in result.result or []}
    assert not result.success
    assert statuses["revenue_by_region__row_count"] == "fail"
    assert statuses["revenue_by_region__condition_revenue_250"] == "warn"
