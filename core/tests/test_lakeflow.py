"""Generated Lakeflow Declarative Pipelines, run with open-source Spark (`spark-pipelines run`).

For every pipeline: generate the project, run it, and require every target's materialized view to
hold the rows the compiled DuckDB query returns. Skipped unless pyspark[pipelines] and Java are there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

pyspark = pytest.importorskip("pyspark", reason="pip install 'pyspark[pipelines]>=4.1'")
pytest.importorskip("pyspark.pipelines")
pytest.importorskip("grpc", reason="pip install 'pyspark[pipelines]>=4.1' (Spark Connect)")
if shutil.which("java") is None and not os.environ.get("JAVA_HOME"):
    pytest.skip("Spark needs Java", allow_module_level=True)

from redcliff.compile import compile_query  # noqa: E402
from redcliff.importer import import_sql  # noqa: E402
from redcliff.lakeflow import compile_lakeflow  # noqa: E402
from redcliff.powercenter import import_powercenter  # noqa: E402
from redcliff.spec import Pipeline  # noqa: E402
from redcliff.ssis import import_ssis  # noqa: E402
from redcliff.talend import import_talend  # noqa: E402
from test_dbt import TABLES, create_tables, norm  # noqa: E402

ROOT = Path(__file__).parents[2]
DATA = ROOT / "examples" / "data"
SPECS = ROOT / "examples" / "specs"
PC = ROOT / "examples" / "legacy" / "powercenter"
CLI = Path(sys.executable).parent / "spark-pipelines"


def example(name):
    return Pipeline.from_dict(yaml.safe_load((SPECS / f"{name}.yaml").read_text()))


PIPELINES = {
    "orders_revenue": lambda: example("orders_revenue"),
    "customer_cleanup": lambda: example("customer_cleanup"),
    "daily_events": lambda: example("daily_events"),
    "teradata_revenue": lambda: import_sql((ROOT / "examples/legacy/teradata_revenue.sql").read_text(), dialect="teradata",
                                           schema=json.loads((ROOT / "examples/legacy/schema.json").read_text()),
                                           name="teradata_revenue").pipeline,
    "wf_customer_revenue": lambda: import_powercenter((PC / "wf_customer_revenue.xml").read_text(),
                                                      params=(PC / "wf_customer_revenue.par").read_text()).pipeline,
    "wf_store_mart": lambda: import_powercenter((PC / "wf_store_mart.xml").read_text()).pipeline,
    "ssis_region_revenue": lambda: import_ssis((ROOT / "examples/legacy/ssis/LoadRegionRevenue.dtsx").read_text()).pipeline,
    "talend_order_revenue": lambda: import_talend(  # tMap lookups (filtered, inner with rejects), merge
        (ROOT / "examples/legacy/talend/j_order_revenue.item").read_text(), name="j_order_revenue").pipeline,
}

# Stand-ins for the warehouse tables that table sources read in place.
TEST_SOURCES = "from pyspark import pipelines as dp\n" + "".join(
    f"\n\n@dp.materialized_view(name={t!r})\ndef {t}():\n"
    f"    return spark.read.option('header', True).schema({ddl.replace('VARCHAR', 'STRING')!r}).csv({str(DATA / t) + '.csv'!r})\n"
    for t, ddl in TABLES.items())


@pytest.mark.parametrize("name", PIPELINES)
def test_lakeflow_pipeline_runs_and_matches_duckdb(tmp_path, name):
    pipeline = PIPELINES[name]()
    files = compile_lakeflow(pipeline, SPECS, storage=(tmp_path / "storage").as_uri())
    for rel, content in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content)
    if any(n.type == "source" and n.params["format"] == "table" for n in pipeline.nodes):
        (tmp_path / "transformations" / "_test_sources.py").write_text(TEST_SOURCES)
    env = {**os.environ, "SPARK_HOME": os.path.dirname(pyspark.__file__),
           "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", "TZ": "UTC"}
    run = subprocess.run([str(CLI), "run"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600)
    assert "Run is COMPLETED" in run.stdout + run.stderr, (run.stdout + run.stderr)[-3000:]

    reference = duckdb.connect()
    create_tables(reference)
    for target in (n for n in pipeline.nodes if n.type == "target"):
        table = target.params["table"].split(".")[-1] if target.params["format"] == "table" else target.id
        if target.params.get("mode") in ("merge", "scd1", "scd2", "insert_new", "insert_changed"):
            table += "_incoming"  # the pipeline computes the rows; the MERGE runs as a job task
        got = duckdb.sql(f"SELECT * FROM read_parquet('{tmp_path}/spark-warehouse/{table}/*.parquet')").fetchall()
        want = reference.execute(compile_query(pipeline, target.inputs[0], "duckdb", SPECS)).fetchall()
        assert norm(got) == norm(want), f"{name}: {target.id}"


def test_job_tasks_are_noted_in_the_module():
    pipeline = import_powercenter((PC / "wf_customer_revenue.xml").read_text(),
                                  params=(PC / "wf_customer_revenue.par").read_text()).pipeline
    code = compile_lakeflow(pipeline)["transformations/wf_customer_revenue.py"]
    assert "region_monthly_revenue: run after the pipeline: ANALYZE TABLE region_monthly_revenue COMPUTE STATISTICS" in code
    assert "@dp.materialized_view(name='region_monthly_revenue')" in code
    compile(code, "pipeline.py", "exec")
