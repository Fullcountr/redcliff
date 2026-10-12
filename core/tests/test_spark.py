"""Generated Spark code, run on a real (local) Spark.

Every pipeline here is compiled three ways and must give DuckDB's rows on the same sample data:
the Spark SQL query for each target's input, the full Spark SQL script (which writes real tables
and directories), and the PySpark script. Spark runs with its defaults, ANSI mode included.

Skipped unless PySpark and Java are installed: pip install "pyspark>=4" (CI has a job for it).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import time
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
import sqlglot
import yaml
from sqlglot import exp

pyspark = pytest.importorskip("pyspark", reason="pip install 'pyspark>=4' to run the Spark tests")
if shutil.which("java") is None and not os.environ.get("JAVA_HOME"):
    pytest.skip("Spark needs Java", allow_module_level=True)

from pyspark.sql import SparkSession  # noqa: E402

from redcliff.compile import compile_pyspark, compile_query, compile_script  # noqa: E402
from redcliff.importer import import_sql  # noqa: E402
from redcliff.informatica import translate  # noqa: E402
from redcliff.powercenter import import_powercenter  # noqa: E402
from redcliff.spec import Pipeline  # noqa: E402

ROOT = Path(__file__).parents[2]
DATA = ROOT / "examples" / "data"
SPECS = ROOT / "examples" / "specs"

# The sample tables with explicit types, identical in Spark and DuckDB.
TABLES = {
    "orders": "order_id INT, customer_id INT, order_date DATE, status STRING, amount DECIMAL(10,2)",
    "customers": "id INT, first_name STRING, last_name STRING, email STRING, region STRING, signup_date DATE",
    "regions": "region STRING, manager STRING, monthly_target DECIMAL(12,2)",
    "web_events": "event_id STRING, user_id INT, event_time TIMESTAMP, event_name STRING",
    "app_events": "event_id STRING, user_id INT, event_time TIMESTAMP, event_name STRING",
}


@pytest.fixture(scope="module")
def spark(tmp_path_factory):
    # PySpark hands timestamps to Python in the process's local time zone; pin it to UTC.
    os.environ["TZ"] = "UTC"
    time.tzset()
    warehouse = tmp_path_factory.mktemp("warehouse")
    session = (
        SparkSession.builder.master("local[2]").appName("redcliff-tests")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.sql.warehouse.dir", str(warehouse))
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    for table, ddl in TABLES.items():
        session.read.option("header", True).schema(ddl).csv(str(DATA / f"{table}.csv")).createOrReplaceTempView(table)
    yield session
    session.stop()


@pytest.fixture(scope="module")
def duck():
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    for table, ddl in TABLES.items():
        cols = ", ".join(f"'{c}': '{t.replace('STRING', 'VARCHAR')}'"
                         for c, t in (part.strip().split(" ", 1) for part in re.split(r",\s*(?![^()]*\))", ddl)))
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv('{DATA / table}.csv', header = true, "
                    f"columns = {{{cols}}})")
    return con


# ---------------------------------------------------------------- comparing rows


def norm(value):
    """Engines differ in numeric and temporal types, not in values."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), 6)
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).isoformat(sep=" ")
    return value


def rows(values) -> list[tuple]:
    return sorted((tuple(norm(v) for v in r) for r in values), key=repr)


def duck_rows(duck, pipeline: Pipeline, node_id: str, base_dir=None):
    return rows(duck.execute(compile_query(pipeline, node_id, "duckdb", base_dir)).fetchall())


def register_file_sources(spark, pipeline: Pipeline, base_dir: Path) -> None:
    """Spark SQL reads CSV and Parquet sources through temp views named after the node."""
    for node in pipeline.nodes:
        if node.type == "source" and node.params["format"] != "table":
            path = str((base_dir / node.params["path"]).resolve())
            reader = spark.read
            if node.params["format"] == "csv":
                reader = reader.option("header", True)
                if node.params.get("columns"):
                    reader = reader.schema(", ".join(f"{c} {exp.DataType.build(t).sql('spark')}"
                                                     for c, t in node.params["columns"].items()))
                df = reader.csv(path)
            else:
                df = reader.parquet(path)
            df.createOrReplaceTempView(node.id)


# ---------------------------------------------------------------- the pipelines


def example(name: str) -> Pipeline:
    return Pipeline.from_dict(yaml.safe_load((SPECS / f"{name}.yaml").read_text()))


def teradata() -> Pipeline:
    schema = json.loads((ROOT / "examples" / "legacy" / "schema.json").read_text())
    sql = (ROOT / "examples" / "legacy" / "teradata_revenue.sql").read_text()
    return import_sql(sql, dialect="teradata", schema=schema, name="teradata_revenue").pipeline


def powercenter() -> Pipeline:
    xml = (ROOT / "examples" / "legacy" / "powercenter" / "m_customer_revenue.xml").read_text()
    return import_powercenter(xml).pipeline


def workflow() -> Pipeline:
    pc = ROOT / "examples" / "legacy" / "powercenter"
    return import_powercenter((pc / "wf_customer_revenue.xml").read_text(),
                              params=(pc / "wf_customer_revenue.par").read_text()).pipeline


PIPELINES = {
    "orders_revenue": lambda: example("orders_revenue"),
    "customer_cleanup": lambda: example("customer_cleanup"),
    "daily_events": lambda: example("daily_events"),
    "teradata_revenue": teradata,
    "m_customer_revenue": powercenter,
    "wf_customer_revenue": workflow,  # mapplet, session overrides, parameter file, post-SQL
}


@pytest.fixture
def workdir(tmp_path):
    """A copy of the examples' layout, so file targets are written to a temp folder."""
    (tmp_path / "specs").mkdir()
    (tmp_path / "data").symlink_to(DATA)
    return tmp_path / "specs"


@pytest.mark.parametrize("name", PIPELINES)
def test_spark_sql_queries_match_duckdb(spark, duck, workdir, name):
    pipeline = PIPELINES[name]()
    register_file_sources(spark, pipeline, workdir)
    for target in (n for n in pipeline.nodes if n.type == "target"):
        node = target.inputs[0]
        got = rows(spark.sql(compile_query(pipeline, node, "spark", workdir)).collect())
        assert got == duck_rows(duck, pipeline, node, workdir), f"{name}: {node}"
        assert got, f"{name}: {node} returned no rows, so the comparison proves little"


def _statements(script: str) -> list[str]:
    body = "\n".join(line for line in script.splitlines() if not line.startswith("--"))
    return [s.strip() for s in body.split(";\n") if s.strip()]


@pytest.mark.parametrize("name", PIPELINES)
def test_spark_sql_script_writes_every_target(spark, duck, workdir, name):
    pipeline = PIPELINES[name]()
    register_file_sources(spark, pipeline, workdir)
    for target in (n for n in pipeline.nodes if n.type == "target"):
        if target.params["format"] == "table":  # INSERT needs the table; give it the input's schema
            table = target.params["table"]
            spark.sql(f"DROP TABLE IF EXISTS {table}")
            spark.sql(compile_query(pipeline, target.inputs[0], "spark", workdir)).limit(0) \
                .write.saveAsTable(table)
    for statement in _statements(compile_script(pipeline, "spark", workdir)):
        spark.sql(statement)
    for target in (n for n in pipeline.nodes if n.type == "target"):
        p = target.params
        written = (spark.table(p["table"]) if p["format"] == "table" else
                   spark.read.option("header", True).option("inferSchema", True).csv(str(workdir / p["path"]))
                   if p["format"] == "csv" else spark.read.parquet(str(workdir / p["path"])))
        want = duck_rows(duck, pipeline, target.inputs[0], workdir)
        got = rows(written.collect())
        if p["format"] == "csv":  # CSV round trips lose types; compare as text
            got, want = [tuple(map(str, r)) for r in got], [tuple(map(str, r)) for r in want]
            got, want = sorted(got), sorted(want)
        assert got == want, f"{name}: {target.id}"


@pytest.mark.parametrize("name", PIPELINES)
def test_pyspark_script_writes_every_target(spark, duck, workdir, name):
    pipeline = PIPELINES[name]()
    for target in (n for n in pipeline.nodes if n.type == "target" and n.params["format"] == "table"):
        spark.sql(f"DROP TABLE IF EXISTS {target.params['table']}")
    exec(compile(compile_pyspark(pipeline, workdir), f"<{name}.py>", "exec"), {"__name__": "pipeline"})
    for target in (n for n in pipeline.nodes if n.type == "target"):
        p = target.params
        if p["format"] == "table":
            written = spark.table(p["table"])
        elif p["format"] == "parquet":
            written = spark.read.parquet(str(workdir / p["path"]))
        else:
            continue  # CSV targets are covered by the Spark SQL script test
        assert rows(written.collect()) == duck_rows(duck, pipeline, target.inputs[0], workdir), f"{name}: {target.id}"


# ---------------------------------------------------------------- Informatica expressions on Spark

EXPRESSIONS = [
    "IIF(ISNULL(amount), 0, amount)",
    "LTRIM(RTRIM(first_name)) || ' ' || last_name",
    "DECODE(TRUE, ISNULL(region), 'UNKNOWN', UPPER(region))",
    "TO_CHAR(order_date, 'YYYY-MM')",
    "TO_DATE('09/15/2026 10:30:00', 'MM/DD/YYYY HH24:MI:SS')",
    "ADD_TO_DATE(order_date, 'MM', 2)",
    "ADD_TO_DATE(order_date, 'DD', 10)",
    "DATE_DIFF(TO_DATE('2026-10-01', 'YYYY-MM-DD'), order_date, 'DD')",
    "GET_DATE_PART(order_date, 'YYYY') * 100 + GET_DATE_PART(order_date, 'MM')",
    "TRUNC(order_date, 'MM')",
    "LAST_DAY(order_date)",
    "TO_INTEGER(amount)",
    "TO_INTEGER(amount, TRUE)",
    "SUBSTR(status, 0, 4)",
    "INSTR(status, 'pp')",
    "IN(status, 'shipped', 'pending')",
    "REPLACESTR(1, status, 'ship', 'SHIP')",
    "LPAD(TO_CHAR(order_id), 5, '0')",
    "REG_MATCH(status, 'ship.*')",
    "MOD(order_id, 3)",
    "IS_NUMBER('12.5')",
]


@pytest.mark.parametrize("text", EXPRESSIONS)
def test_informatica_expressions_agree_on_spark_and_duckdb(spark, duck, text):
    t = translate(text, lambda n: (sqlglot.exp.column(n.lower()), None)
                  if n.lower() in {"amount", "first_name", "last_name", "region", "order_date", "status",
                                   "order_id"} else None)
    if text.startswith(("TO_CHAR(order_date", "TRUNC(order_date", "LAST_DAY", "ADD_TO_DATE", "DATE_DIFF",
                        "GET_DATE_PART")):
        t = translate(text, lambda n: (sqlglot.exp.column(n.lower()), "date"))
    sql_from = ("FROM orders JOIN customers ON orders.customer_id = customers.id")
    neutral = f"SELECT {t.text()} AS v {sql_from}"
    spark_sql = sqlglot.transpile(neutral, write="spark")[0]
    duck_sql = sqlglot.transpile(neutral, write="duckdb")[0]
    assert rows(spark.sql(spark_sql).collect()) == rows(duck.execute(duck_sql).fetchall()), spark_sql
