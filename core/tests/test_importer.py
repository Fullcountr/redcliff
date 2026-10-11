"""Importer tests.

The core check closes the loop: run the original SQL in DuckDB, import it, compile the pipeline
back to DuckDB SQL, run that, and require the same rows. Order only counts when the original
query has a top-level ORDER BY.
"""

from pathlib import Path

import duckdb
import pytest
import sqlglot

from redcliff.compile import compile_query, compile_script
from redcliff.importer import SQLImportError, import_sql
from redcliff.lineage import Ref, blast_radius, build_graph, trace_to_sources

DATA = Path(__file__).parents[2] / "examples" / "data"
TABLES = ["orders", "customers", "web_events", "app_events"]


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    for t in TABLES:
        c.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{DATA / t}.csv', header = true)")
    return c


@pytest.fixture(scope="module")
def schema(con):
    return {t: {name: typ for name, typ, *_ in con.execute(f"DESCRIBE {t}").fetchall()} for t in TABLES}


def same_rows(con, original: str, compiled: str) -> None:
    want = con.execute(original).fetchall()
    got = con.execute(compiled).fetchall()
    ordered = sqlglot.parse_one(original, read="duckdb").args.get("order") is not None
    if ordered:
        assert got == want
    else:
        assert sorted(map(repr, got)) == sorted(map(repr, want))


def roundtrip(con, schema, sql, **kw):
    result = import_sql(sql, dialect="duckdb", schema=schema, **kw)
    compiled = compile_query(result.pipeline, result.outputs[-1], "duckdb")
    same_rows(con, sql, compiled)
    return result


def types(result) -> list[str]:
    return [n.type for n in result.pipeline.topo_order()]


# ---------------------------------------------------------------- five queries, rising difficulty


def test_q1_filter_and_select(con, schema):
    r = roundtrip(con, schema, "SELECT order_id, amount FROM orders WHERE status = 'shipped'")
    assert types(r) == ["source", "filter", "select"]
    assert r.warnings == []


def test_q2_join_filter_aggregate(con, schema):
    r = roundtrip(con, schema, """
        SELECT c.region, SUM(o.amount) AS revenue, COUNT(*) AS orders
        FROM orders o JOIN customers c ON o.customer_id = c.id
        WHERE o.status = 'shipped' GROUP BY c.region""")
    assert types(r) == ["source", "source", "join", "filter", "aggregate"]
    agg = r.pipeline.node("result")
    assert agg.params == {"group_by": ["region"], "measures": {"revenue": "SUM(amount)", "orders": "COUNT(*)"}}
    assert r.warnings == []


def test_q3_ctes_having_left_join_order_by(con, schema):
    r = roundtrip(con, schema, """
        WITH shipped AS (SELECT * FROM orders WHERE status = 'shipped'),
        per_customer AS (
          SELECT customer_id, SUM(amount) AS total, COUNT(*) AS n FROM shipped
          GROUP BY customer_id HAVING SUM(amount) > 50)
        SELECT p.customer_id, c.first_name || ' ' || c.last_name AS name, p.total,
               ROUND(p.total / p.n, 2) AS avg_order
        FROM per_customer p LEFT JOIN customers c ON p.customer_id = c.id
        ORDER BY p.total DESC""")
    ids = [n.id for n in r.pipeline.nodes]
    assert "shipped" in ids and "per_customer" in ids  # CTE names survive as node ids
    assert r.pipeline.node("result_join").params["how"] == "left"
    assert any("ORDER BY kept as a SQL step" in w for w in r.warnings)


def test_q4_derived_table_union_window(con, schema):
    r = roundtrip(con, schema, """
        SELECT channel, user_id, ROW_NUMBER() OVER (PARTITION BY channel ORDER BY event_time) AS seq
        FROM (SELECT 'web' AS channel, user_id, event_time FROM web_events
              UNION ALL SELECT 'app', user_id, event_time FROM app_events) t""")
    assert "union" in types(r)
    assert r.warnings == []  # the second branch's unnamed 'app' is positional, so no warning


def test_q5_subquery_in_where_falls_back_with_a_warning(con, schema):
    r = roundtrip(con, schema, """
        SELECT DISTINCT c.region FROM customers c
        WHERE c.id IN (SELECT customer_id FROM orders WHERE amount > 100)
        ORDER BY c.region""")
    raw = r.pipeline.node("result")
    assert raw.type == "sql" and set(raw.inputs) == {"customers", "orders"}
    assert any("subquery outside FROM kept as a SQL step" in w for w in r.warnings)


# ---------------------------------------------------------------- tricky shapes


def test_join_column_clash_is_renamed_not_lost(con, schema):
    r = roundtrip(con, schema, """
        SELECT w.user_id, a.event_id AS app_event, w.event_id AS web_event
        FROM web_events w JOIN app_events a ON w.user_id = a.user_id""")
    assert "a_renamed" in [n.id for n in r.pipeline.nodes]


def test_qualified_column_hidden_by_left_side_is_renamed(con, schema):
    # Only a.event_time is referenced, but web_events also has event_time and the join would
    # keep the left one; the importer must rename a's copy first.
    r = roundtrip(con, schema, """
        SELECT a.event_time FROM web_events w JOIN app_events a ON w.user_id = a.user_id""")
    assert "a_renamed" in [n.id for n in r.pipeline.nodes]


@pytest.mark.parametrize("sql", [
    "SELECT o.order_id, c.region FROM orders o JOIN customers c ON o.customer_id = c.id",
    "SELECT o.order_id, c.region FROM orders o CROSS JOIN customers c",
    "SELECT o.order_id, c.region FROM orders o FULL OUTER JOIN customers c ON o.customer_id = c.id",
    "SELECT o.order_id, c.region FROM orders o RIGHT JOIN customers c ON o.customer_id = c.id",
    "SELECT web_events.user_id FROM web_events JOIN app_events USING (user_id)",
])
def test_join_kinds(con, schema, sql):
    roundtrip(con, schema, sql)


@pytest.mark.parametrize("sql", [
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY 1",
    "SELECT CAST(event_time AS DATE) AS day, COUNT(*) AS n FROM web_events GROUP BY CAST(event_time AS DATE)",
    "SELECT status, SUM(amount) FROM orders GROUP BY status",
    "SELECT status FROM orders GROUP BY status HAVING COUNT(*) > 1",
    "SELECT status, UPPER(status) AS loud, MAX(amount) AS top FROM orders GROUP BY status",
    "SELECT SUM(amount) AS total, AVG(amount) AS mean FROM orders",
    "SELECT SUM(CASE WHEN status = 'shipped' THEN amount ELSE 0 END) AS shipped_total FROM orders",
])
def test_aggregate_shapes(con, schema, sql):
    roundtrip(con, schema, sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders",
    "SELECT o.*, c.region FROM orders o JOIN customers c ON o.customer_id = c.id",
    "SELECT DISTINCT region FROM customers",
    "SELECT DISTINCT * FROM web_events",
    "SELECT id, LOWER(TRIM(email)) AS email FROM customers WHERE email IS NOT NULL",
    "SELECT user_id FROM web_events UNION SELECT user_id FROM app_events",
    "SELECT user_id FROM web_events UNION ALL SELECT user_id FROM app_events UNION ALL SELECT customer_id FROM orders",
    "SELECT * FROM orders ORDER BY amount DESC LIMIT 3",
    "SELECT order_id FROM orders QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY amount DESC) = 1",
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT * FROM n",
])
def test_more_shapes(con, schema, sql):
    roundtrip(con, schema, sql)


def test_qualify_and_recursive_ctes_are_warned():
    r = import_sql("SELECT order_id FROM orders QUALIFY ROW_NUMBER() OVER (ORDER BY amount) = 1")
    assert any("QUALIFY kept as a SQL step" in w for w in r.warnings)
    r = import_sql("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT * FROM n")
    assert any("recursive CTE kept as a SQL step" in w for w in r.warnings)


# ---------------------------------------------------------------- scripts and dialects


def test_create_table_chain_becomes_one_pipeline_with_a_target(con, schema):
    script = """
        CREATE TABLE shipped AS SELECT * FROM orders WHERE status = 'shipped';
        CREATE TABLE revenue AS
          SELECT c.region, SUM(s.amount) AS revenue
          FROM shipped s JOIN customers c ON s.customer_id = c.id GROUP BY c.region;
    """
    r = import_sql(script, dialect="duckdb", schema=schema)
    targets = [n for n in r.pipeline.nodes if n.type == "target"]
    assert [t.params["table"] for t in targets] == ["revenue"]  # shipped is read later: intermediate
    assert "shipped" in r.pipeline.node("revenue_join").inputs

    run = duckdb.connect()
    for t in TABLES:
        run.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{DATA / t}.csv', header = true)")
    run.execute(compile_script(r.pipeline, "duckdb"))
    got = sorted(run.execute("SELECT * FROM revenue").fetchall(), key=repr)
    want = sorted(con.execute("""SELECT c.region, SUM(o.amount) FROM orders o JOIN customers c
        ON o.customer_id = c.id WHERE o.status = 'shipped' GROUP BY c.region""").fetchall(), key=repr)
    assert got == want


def test_insert_select_becomes_a_target():
    r = import_sql("INSERT INTO mart.daily SELECT order_date, SUM(amount) AS total FROM orders GROUP BY order_date")
    target = next(n for n in r.pipeline.nodes if n.type == "target")
    assert target.params == {"format": "table", "table": "mart.daily", "mode": "append"}


def test_teradata_bteq_script():
    script = """
.LOGON tdprod/etl_user,password;
.SET WIDTH 200;
SEL o.customer_id, SUM(o.amount) AS total
FROM sales.orders o
WHERE o.status = 'shipped'
GROUP BY 1;
.IF ERRORCODE <> 0 THEN .QUIT 8;
.LOGOFF;
"""
    r = import_sql(script, dialect="teradata")
    assert any("4 BTEQ dot-command line(s)" in w for w in r.warnings)
    src = r.pipeline.node("orders")
    assert src.params["table"] == "sales.orders"
    spark = compile_query(r.pipeline, r.outputs[-1], "spark")
    assert "SUM(amount) AS total" in spark and "GROUP BY" in spark


def test_unparseable_sql_is_a_clear_error():
    with pytest.raises(SQLImportError, match="could not parse"):
        import_sql("SELECT FROM WHERE (")
    with pytest.raises(SQLImportError, match="no SQL statements"):
        import_sql("   ")


def test_unsupported_statements_are_warned_not_dropped():
    r = import_sql("UPDATE orders SET status = 'x'; SELECT * FROM orders")
    assert any("skipped statement with no pipeline equivalent: UPDATE" in w for w in r.warnings)


def test_without_schema_imports_but_warns_about_clash_checks():
    r = import_sql("SELECT a.x, b.y FROM a JOIN b ON a.id = b.id")
    assert any("join column clashes can't be checked" in w for w in r.warnings)


# ---------------------------------------------------------------- lineage on imported pipelines


def test_lineage_works_on_an_imported_pipeline(schema):
    r = import_sql("""
        SELECT c.region, SUM(o.amount) AS revenue FROM orders o
        JOIN customers c ON o.customer_id = c.id WHERE o.status = 'shipped' GROUP BY c.region""",
        dialect="duckdb", schema=schema)
    g = build_graph(r.pipeline)
    assert g.issues == []
    srcs = {str(k): v for k, v in trace_to_sources(g, Ref("result", "revenue")).items()}
    assert srcs == {"orders.amount": ("DIRECT", "AGGREGATION")}
    assert blast_radius(g, Ref("orders", "status")).breaks == {"result_filter"}


def test_lineage_sees_through_a_sql_step(schema):
    r = import_sql("SELECT order_id, amount FROM orders ORDER BY amount DESC LIMIT 2",
                   dialect="duckdb", schema=schema)
    g = build_graph(r.pipeline)
    assert g.issues == []
    assert {str(k) for k in trace_to_sources(g, Ref("result", "amount"))} == {"orders.amount"}
