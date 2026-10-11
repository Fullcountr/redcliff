"""The full loop through the command line, on a realistic legacy Teradata job.

import -> validate -> compile -> lineage -> run, then check the tables it wrote against
answers worked out by hand from the sample data.
"""

import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from redcliff.cli import main

ROOT = Path(__file__).parents[2]
SCHEMA = {
    "orders": {"order_id": "int", "customer_id": "int", "order_date": "date", "status": "text",
               "amount": "decimal(10,2)"},
    "customers": {"id": "int", "first_name": "text", "last_name": "text", "email": "text",
                  "region": "text", "signup_date": "date"},
}


@pytest.fixture
def work(tmp_path):
    """A folder with the legacy SQL, a schema file and a DuckDB warehouse holding the raw tables."""
    shutil.copy(ROOT / "examples" / "legacy" / "teradata_revenue.sql", tmp_path / "job.sql")
    (tmp_path / "schema.json").write_text(json.dumps(SCHEMA))
    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    for table, cols in SCHEMA.items():
        spec = ", ".join(f"'{c}': '{t.upper()}'" for c, t in cols.items())
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv("
                    f"'{ROOT / 'examples' / 'data' / table}.csv', header = true, columns = {{{spec}}})")
    con.close()
    return tmp_path


def test_full_loop(work, capsys):
    spec = str(work / "pipeline.yaml")

    # 1. import the legacy job
    assert main(["import", str(work / "job.sql"), "--dialect", "teradata",
                 "--schema", str(work / "schema.json"), "-o", spec]) == 0
    err = capsys.readouterr().err
    assert "imported 10 nodes (2 targets)" in err
    assert "skipped 6 BTEQ dot-command line(s)" in err
    assert "QUALIFY kept as a SQL step" in err

    # 2. validate, including column checks
    assert main(["validate", spec]) == 0
    assert "valid (10 nodes)" in capsys.readouterr().out

    # 3. compile to Spark SQL and PySpark
    assert main(["compile", spec, "--to", "spark"]) == 0
    spark = capsys.readouterr().out
    assert spark.count("INSERT OVERWRITE TABLE") == 2
    assert main(["compile", spec, "--to", "pyspark"]) == 0
    compile(capsys.readouterr().out, "pipeline.py", "exec")  # valid Python

    # 4. lineage and blast radius
    assert main(["lineage", spec, "--column", "regional_revenue.revenue"]) == 0
    assert "orders.amount  (direct, aggregation)" in capsys.readouterr().out
    assert main(["lineage", spec, "--impact", "orders.status"]) == 0
    out = capsys.readouterr().out
    assert "a rename breaks:  shipped_orders_filter" in out
    assert "regional_revenue" in out and "top_customer_per_region" in out

    # 5. preview a node, then run the whole job in DuckDB
    assert main(["run", spec, "--db", str(work / "warehouse.duckdb"), "--node", "customer_totals"]) == 0
    preview = capsys.readouterr().out
    assert preview.splitlines()[0] == "customer_id | total_spent | order_count | last_order"
    assert main(["run", spec, "--db", str(work / "warehouse.duckdb")]) == 0
    assert "wrote regional_revenue, top_customer_per_region" in capsys.readouterr().out

    con = duckdb.connect(str(work / "warehouse.duckdb"))
    assert con.sql("SELECT * FROM regional_revenue ORDER BY region").fetchall() == [
        ("North", Decimal("239.99"), 3, "standard"),
        ("South", Decimal("330.50"), 3, "gold"),
    ]
    assert con.sql("SELECT * FROM top_customer_per_region ORDER BY region").fetchall() == [
        ("North", "Ben", "Okafor", Decimal("140.00")),
        ("South", "Chen", "Wei", Decimal("210.00")),
    ]


def test_import_matches_running_the_original_sql(work):
    """The converted job writes exactly what the original SQL computes."""
    spec = str(work / "pipeline.yaml")
    main(["import", str(work / "job.sql"), "--dialect", "teradata", "--schema", str(work / "schema.json"),
          "-o", spec])
    main(["run", spec, "--db", str(work / "warehouse.duckdb")])
    con = duckdb.connect(str(work / "warehouse.duckdb"))
    want = con.sql("""
        WITH t AS (SELECT customer_id, SUM(amount) AS total_spent, COUNT(*) AS order_count
                   FROM orders WHERE status = 'shipped' GROUP BY customer_id)
        SELECT c.region, SUM(t.total_spent), SUM(t.order_count),
               CASE WHEN SUM(t.total_spent) >= 300 THEN 'gold' ELSE 'standard' END
        FROM t JOIN customers c ON t.customer_id = c.id GROUP BY c.region ORDER BY 1""").fetchall()
    assert con.sql("SELECT * FROM regional_revenue ORDER BY region").fetchall() == want


def test_errors_are_reported_not_raised(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 0\nname: bad\nnodes:\n  - {id: a, type: filter, inputs: [zz], params: {condition: x}}\n")
    assert main(["validate", str(bad)]) == 1
    assert "input 'zz' does not exist" in capsys.readouterr().err
    (tmp_path / "broken.sql").write_text("SELECT FROM WHERE (")
    assert main(["import", str(tmp_path / "broken.sql")]) == 1
    assert "could not parse" in capsys.readouterr().err


def test_column_issues_fail_validation(tmp_path, capsys):
    spec = tmp_path / "p.yaml"
    spec.write_text("""version: 0
name: p
nodes:
  - {id: a, type: source, params: {format: csv, path: a.csv, columns: {id: int}}}
  - {id: f, type: filter, inputs: [a], params: {condition: idd > 1}}
""")
    assert main(["validate", str(spec)]) == 1
    assert "unknown column 'idd'; available: id" in capsys.readouterr().err


def test_installed_command_runs():
    exe = shutil.which("redcliff")
    if exe is None:
        pytest.skip("package not installed with its console script")
    out = subprocess.run([exe, "--help"], capture_output=True, text=True, check=True).stdout
    assert "Legacy ETL in, clean Spark out." in out
