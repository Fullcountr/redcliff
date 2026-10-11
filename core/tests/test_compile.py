"""Compiler tests.

Golden tests run the generated DuckDB SQL on the sample data and compare against rows worked
out by hand. Spark SQL is checked for parseability and PySpark for valid Python; running them
needs a Spark cluster, which CI doesn't have yet.
"""

import ast
import shutil
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
import sqlglot

from redcliff import Pipeline, load_spec
from redcliff.columns import infer_columns
from redcliff.compile import CompileError, compile_pyspark, compile_query, compile_script

ROOT = Path(__file__).parents[2]
SPECS = ROOT / "examples" / "specs"
EXAMPLES = sorted(SPECS.glob("*.yaml"))


def run(sql: str) -> list[tuple]:
    return sorted(duckdb.sql(sql).fetchall(), key=repr)


def columns_of(sql: str) -> list[str]:
    return [d[0] for d in duckdb.sql(sql).description]


# ---------------------------------------------------------------- golden: the examples


def test_orders_revenue():
    p = load_spec(SPECS / "orders_revenue.yaml")
    sql = compile_query(p, "revenue_by_region", base_dir=SPECS)
    assert columns_of(sql) == ["region", "revenue", "order_count"]
    assert run(sql) == [("North", Decimal("239.99"), 3), ("South", Decimal("330.50"), 3)]


def test_customer_cleanup():
    p = load_spec(SPECS / "customer_cleanup.yaml")
    sql = compile_query(p, "contact_list", base_dir=SPECS)
    assert columns_of(sql) == ["id", "full_name", "email", "region"]
    assert run(sql) == [
        (101, "Ana Silva", "ana.silva@example.com", "South"),
        (102, "Ben Okafor", "ben.okafor@example.com", "North"),
        (103, "Chen Wei", "chen.wei@example.com", "South"),
        (105, "Eli Novak", "eli.novak@example.com", "North"),
    ]


def test_daily_events():
    import datetime as dt

    p = load_spec(SPECS / "daily_events.yaml")
    sql = compile_query(p, "daily_counts", base_dir=SPECS)
    assert columns_of(sql) == ["event_day", "channel", "events", "users"]
    d1, d2 = dt.date(2026, 9, 1), dt.date(2026, 9, 2)
    assert run(sql) == sorted(
        [(d1, "web", 2, 2), (d2, "web", 2, 2), (d1, "app", 2, 2), (d2, "app", 1, 1)], key=repr
    )


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_full_script_writes_targets(path, tmp_path):
    """The DuckDB script runs end to end and writes the same rows the query returns."""
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    spec_dir = tmp_path / "examples" / "specs"
    p = load_spec(spec_dir / path.name)
    (spec_dir / "out").mkdir()  # COPY doesn't create folders; the script header says so
    duckdb.connect().execute(compile_script(p, "duckdb", base_dir=spec_dir))

    target = next(n for n in p.nodes if n.type == "target")
    written = spec_dir / target.params["path"]
    assert written.exists()
    reader = "read_parquet" if target.params["format"] == "parquet" else "read_csv_auto"
    expected = run(compile_query(p, target.inputs[0], base_dir=spec_dir))
    assert run(f"SELECT * FROM {reader}('{written}')") == expected


# ---------------------------------------------------------------- Spark and PySpark


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_spark_sql_parses(path):
    p = load_spec(path)
    for node in p.nodes:
        if node.type in ("source", "target"):
            continue
        sql = compile_query(p, node.id, "spark")
        body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        sqlglot.parse_one(body, read="spark")  # raises if Spark can't parse it


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_spark_script_has_one_statement_per_target(path):
    p = load_spec(path)
    script = compile_script(p, "spark")
    targets = [n for n in p.nodes if n.type == "target"]
    assert script.count("INSERT ") == len(targets)
    assert "Register these file sources as temp views" in script


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_pyspark_is_valid_python(path):
    code = compile_pyspark(load_spec(path))
    ast.parse(code)
    assert "spark.read" in code and ".write.mode(" in code


def test_pyspark_derive_and_union():
    code = compile_pyspark(load_spec(SPECS / "daily_events.yaml"))
    assert "web_events.withColumns({'channel': F.expr(\"'web'\")" in code
    assert "all_events = web_tagged.union(app_tagged)" in code


# ---------------------------------------------------------------- one behaviour per test


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "a.csv").write_text("id,name,score\n1,x,10\n2,y,20\n3,z,30\n")
    (tmp_path / "b.csv").write_text("id,label\n1,one\n2,two\n4,four\n")
    return tmp_path


def pipe(*nodes, a_cols=True) -> Pipeline:
    a = {"format": "csv", "path": "a.csv"}
    if a_cols:
        a["columns"] = {"id": "int", "name": "text", "score": "int"}
    base = [
        {"id": "a", "type": "source", "params": a},
        {"id": "b", "type": "source", "params": {"format": "csv", "path": "b.csv",
                                                 "columns": {"id": "int", "label": "text"}}},
    ]
    return Pipeline.from_dict({"version": 0, "name": "t", "nodes": base + list(nodes)})


def test_left_join_keeps_unmatched_and_left_wins_on_shared_names(data_dir):
    p = pipe({"id": "j", "type": "join", "inputs": ["a", "b"],
              "params": {"how": "left", "condition": "a.id = b.id"}})
    sql = compile_query(p, "j", base_dir=data_dir)
    assert columns_of(sql) == ["id", "name", "score", "label"]
    assert run(sql) == [(1, "x", 10, "one"), (2, "y", 20, "two"), (3, "z", 30, None)]


def test_full_join(data_dir):
    p = pipe({"id": "j", "type": "join", "inputs": ["a", "b"],
              "params": {"how": "full", "condition": "a.id = b.id"}})
    assert len(run(compile_query(p, "j", base_dir=data_dir))) == 4


def test_derive_replaces_in_place_and_appends_new(data_dir):
    p = pipe({"id": "d", "type": "derive", "inputs": ["a"],
              "params": {"columns": {"score": "score * 2", "big": "score > 15"}}})
    sql = compile_query(p, "d", base_dir=data_dir)
    assert columns_of(sql) == ["id", "name", "score", "big"]
    assert run(sql) == [(1, "x", 20, False), (2, "y", 40, True), (3, "z", 60, True)]


def test_derive_without_known_columns_adds(data_dir):
    p = pipe({"id": "d", "type": "derive", "inputs": ["a"], "params": {"columns": {"doubled": "score * 2"}}},
             a_cols=False)
    sql = compile_query(p, "d", base_dir=data_dir)
    assert columns_of(sql) == ["id", "name", "score", "doubled"]


def test_union_distinct_and_all(data_dir):
    sel = {"id": "s", "type": "select", "inputs": ["a"], "params": {"columns": ["id"]}}
    sel_b = {"id": "sb", "type": "select", "inputs": ["b"], "params": {"columns": ["id"]}}
    for distinct, n in ((False, 6), (True, 4)):
        p = pipe(sel, sel_b, {"id": "u", "type": "union", "inputs": ["s", "sb"], "params": {"distinct": distinct}})
        assert len(run(compile_query(p, "u", base_dir=data_dir))) == n


def test_aggregate_without_group_by(data_dir):
    p = pipe({"id": "g", "type": "aggregate", "inputs": ["a"],
              "params": {"measures": {"total": "SUM(score)", "n": "COUNT(*)"}}})
    assert run(compile_query(p, "g", base_dir=data_dir)) == [(60, 3)]


def test_keyword_ids_are_quoted(data_dir):
    p = pipe({"id": "order", "type": "filter", "inputs": ["a"], "params": {"condition": "score > 10"}})
    sql = compile_query(p, "order", base_dir=data_dir)
    assert '"order" AS (' in sql
    assert len(run(sql)) == 2
    assert "`order`" in compile_query(p, "order", "spark")
    py = pipe({"id": "class", "type": "filter", "inputs": ["a"], "params": {"condition": "score > 10"}})
    code = compile_pyspark(py)
    ast.parse(code)
    assert "class_ = a.where(" in code


def test_csv_without_declared_columns_is_auto_read(data_dir):
    p = pipe({"id": "f", "type": "filter", "inputs": ["a"], "params": {"condition": "score >= 20"}}, a_cols=False)
    sql = compile_query(p, "f", base_dir=data_dir)
    assert "read_csv_auto" in sql
    assert len(run(sql)) == 2


def test_table_source_and_target_in_duckdb(data_dir):
    p = Pipeline.from_dict({"version": 0, "name": "t", "nodes": [
        {"id": "src", "type": "source", "params": {"format": "table", "table": "raw_scores"}},
        {"id": "hi", "type": "filter", "inputs": ["src"], "params": {"condition": "score > 1"}},
        {"id": "out", "type": "target", "inputs": ["hi"], "params": {"format": "table", "table": "hi_scores"}},
    ]})
    con = duckdb.connect()
    con.execute("CREATE TABLE raw_scores AS SELECT * FROM (VALUES (1), (2), (3)) t(score)")
    con.execute(compile_script(p, "duckdb"))
    assert con.sql("SELECT COUNT(*) FROM hi_scores").fetchone() == (2,)


def test_source_named_like_its_table_gets_no_cte():
    p = Pipeline.from_dict({"version": 0, "name": "t", "nodes": [
        {"id": "scores", "type": "source", "params": {"format": "table", "table": "scores"}},
        {"id": "hi", "type": "filter", "inputs": ["scores"], "params": {"condition": "score > 1"}},
    ]})
    sql = compile_query(p, "hi")
    assert "scores AS (" not in sql and "FROM scores" in sql


def test_append_to_file_is_refused():
    p = pipe({"id": "out", "type": "target", "inputs": ["a"],
              "params": {"format": "parquet", "path": "o.parquet", "mode": "append"}})
    for dialect in ("duckdb", "spark"):
        with pytest.raises(CompileError, match="append"):
            compile_script(p, dialect)


def test_querying_a_target_or_unknown_dialect_is_refused():
    p = pipe({"id": "out", "type": "target", "inputs": ["a"], "params": {"format": "csv", "path": "o.csv"}})
    with pytest.raises(CompileError, match="is a target"):
        compile_query(p, "out")
    with pytest.raises(CompileError, match="unsupported dialect"):
        compile_query(p, "a", "postgres")


def test_query_only_includes_ancestors(data_dir):
    p = pipe({"id": "fa", "type": "filter", "inputs": ["a"], "params": {"condition": "score > 1"}},
             {"id": "fb", "type": "filter", "inputs": ["b"], "params": {"condition": "id > 1"}})
    sql = compile_query(p, "fa", base_dir=data_dir)
    assert "b.csv" not in sql and "fb AS (" not in sql


def test_infer_columns():
    p = load_spec(SPECS / "daily_events.yaml")
    cols = infer_columns(p)
    assert cols["web_tagged"] == ["event_id", "user_id", "event_time", "event_name", "channel", "event_day"]
    assert cols["daily_counts"] == ["event_day", "channel", "events", "users"]
    assert cols["out"] == []
