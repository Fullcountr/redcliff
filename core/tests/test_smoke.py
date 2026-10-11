"""Smoke tests: the package imports and its core dependency behaves as the spike showed."""

import sqlglot
from sqlglot.lineage import lineage

import redcliff


def test_version():
    assert redcliff.__version__ == "0.0.1"


def test_teradata_qualify_transpiles_to_duckdb():
    sql = "SELECT TOP 5 * FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1"
    out = sqlglot.transpile(sql, read="teradata", write="duckdb")[0]
    assert "QUALIFY" in out
    assert out.endswith("LIMIT 5")


def test_direct_column_lineage():
    sql = """
    SELECT c.region, SUM(o.amount) AS revenue
    FROM orders o JOIN customers c ON o.customer_id = c.id
    GROUP BY c.region
    """
    schema = {
        "orders": {"customer_id": "int", "amount": "double"},
        "customers": {"id": "int", "region": "text"},
    }
    node = lineage("revenue", sql, schema=schema)
    sources = {n.name for n in node.walk() if not n.downstream}
    assert sources == {"o.amount"}
