"""Data files beyond CSV: JSON sources and targets, delimited text with any separator, and models
proposed from (and loaded out of) TSV, JSON and Parquet files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from redcliff.cli import main
from redcliff.compile import compile_pyspark, compile_query, compile_script
from redcliff.dbt import compile_dbt
from redcliff.extend import apply_changes, extend_files
from redcliff.generate import model_pipeline
from redcliff.lakeflow import compile_lakeflow
from redcliff.model import validate_model
from redcliff.propose import profile_files
from redcliff.spec import Pipeline, validate

duckdb = pytest.importorskip("duckdb")

SPEC = """
version: 0
name: formats
nodes:
  - id: items
    type: source
    params: {format: json, path: items.json}
  - id: skus
    type: source
    params: {format: csv, path: skus.txt, delimiter: "|"}
  - id: priced
    type: join
    inputs: [items, skus]
    params: {how: inner, condition: items.sku = skus.sku}
  - id: by_order
    type: aggregate
    inputs: [priced]
    params: {group_by: [order_id], measures: {lines: COUNT(*), revenue: SUM(quantity * price)}}
  - id: out
    type: target
    inputs: [by_order]
    params: {format: json, path: by_order.json}
"""


def files(folder: Path, array: bool = True) -> None:
    items = [{"item_id": 1, "order_id": 1, "sku": "A", "quantity": 2}, {"item_id": 2, "order_id": 1, "sku": "B", "quantity": 1},
             {"item_id": 3, "order_id": 2, "sku": "A", "quantity": 5}]
    (folder / "items.json").write_text(json.dumps(items) if array else "\n".join(json.dumps(i) for i in items) + "\n")
    (folder / "skus.txt").write_text("sku|label|price\nA|Lamp|3.50\nB|Mug, large|7.25\n")


@pytest.mark.parametrize("array", [True, False], ids=["array", "lines"])
def test_json_and_pipe_delimited_sources_run_in_duckdb(tmp_path, array):
    files(tmp_path, array)
    pipeline = Pipeline.from_dict(yaml.safe_load(SPEC))
    assert validate(pipeline) == []
    got = duckdb.connect().execute(compile_query(pipeline, "by_order", "duckdb", tmp_path) + " ORDER BY 1").fetchall()
    assert [(o, n, float(r)) for o, n, r in got] == [(1, 2, 14.25), (2, 1, 17.5)]
    spec = tmp_path / "formats.yaml"
    spec.write_text(SPEC)
    assert main(["run", str(spec)]) == 0
    written = [json.loads(line) for line in (tmp_path / "by_order.json").read_text().splitlines()]
    assert sorted((w["order_id"], w["lines"]) for w in written) == [(1, 2), (2, 1)]


def test_every_output_reads_json_and_the_separator():
    pipeline = Pipeline.from_dict(yaml.safe_load(SPEC))
    assert "read_json_auto('items.json')" in compile_script(pipeline, "duckdb")
    assert "delim = '|'" in compile_script(pipeline, "duckdb")
    assert "COPY (" in compile_script(pipeline, "duckdb") and "(FORMAT json)" in compile_script(pipeline, "duckdb")
    spark = compile_script(pipeline, "spark")
    assert "skus <- csv skus.txt (sep '|')" in spark and "USING json" in spark
    py = compile_pyspark(pipeline)
    assert "spark.read.json('items.json')" in py and """.option("sep", '|').csv('skus.txt')""" in py
    assert ".json('by_order.json')" in py
    assert """.option("sep", '|')""" in "".join(compile_lakeflow(pipeline).values())
    dbt = compile_dbt(pipeline, "duckdb")
    assert "read_json_auto('items.json')" in dbt["models/src_items.sql"]
    assert 'delimiter: "|"' in dbt["seeds/seeds.yml"]


def test_a_separator_is_one_character_for_csv():
    for params, ok in [({"format": "csv", "path": "a.tsv", "delimiter": "\t"}, True),
                       ({"format": "csv", "path": "a.txt", "delimiter": "||"}, False),
                       ({"format": "parquet", "path": "a.parquet", "delimiter": ","}, False),
                       ({"format": "json", "path": "a.json"}, True), ({"format": "xml", "path": "a.xml"}, False)]:
        spec = {"version": 0, "name": "x", "nodes": [{"id": "a", "type": "source", "params": params}]}
        assert (validate(Pipeline.from_dict(spec)) == []) is ok, params


def test_models_from_tsv_json_and_parquet_files_load_and_grow(tmp_path):
    (tmp_path / "customers.tsv").write_text("id\tname\tregion\n101\tAna\tSouth\n102\tBen\tNorth\n")
    (tmp_path / "orders.json").write_text(json.dumps([
        {"order_id": 1, "customer_id": 101, "amount": 10.5}, {"order_id": 2, "customer_id": 102, "amount": 20.0},
        {"order_id": 3, "customer_id": 101, "amount": 5.25}]))
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM (VALUES ('North', 'Maria'), ('South', 'Tom')) t(region, manager)) "
                f"TO '{tmp_path / 'regions.parquet'}' (FORMAT parquet)")
    model = profile_files(con, [tmp_path / "customers.tsv", tmp_path / "orders.json"], tmp_path).model
    assert {e.id for e in model.entities} == {"dim_customer", "fct_orders"}
    ext = extend_files(con, model, tmp_path, [tmp_path / "regions.parquet"])
    grown = apply_changes(model, ext.changes, [c.id for c in ext.changes if c.default])
    assert grown.entity("dim_region").source == "regions.parquet" and validate_model(grown) == []

    load = model_pipeline(grown, tmp_path)
    sources = {n.id: n.params for n in load.nodes if n.type == "source" and "path" in n.params}
    assert sources["src_dim_customer"]["delimiter"] == "\t" and list(sources["src_dim_customer"]["columns"]) == ["id", "name", "region"]
    assert sources["src_fct_orders"]["format"] == "json" and "amount" in sources["src_fct_orders"]["columns"]
    assert sources["src_dim_region"]["format"] == "parquet" and list(sources["src_dim_region"]["columns"]) == ["region", "manager"]
    spec = tmp_path / "load.yaml"
    from redcliff.spec import dump_spec

    spec.write_text(dump_spec(load, "yaml"))
    db = tmp_path / "w.duckdb"
    assert main(["run", str(spec), "--db", str(db), "--var", "load_ts=TIMESTAMP '2026-10-01 00:00:00'"]) == 0
    w = duckdb.connect(str(db))
    assert w.sql("SELECT f.order_id, c.name FROM fct_orders f JOIN dim_customer c USING (customer_key) ORDER BY 1").fetchall() \
        == [(1, "Ana"), (2, "Ben"), (3, "Ana")]
    assert w.sql("SELECT manager FROM dim_region WHERE region = 'South'").fetchall() == [("Tom",)]

    # What the browser hands over instead of files on disk: names, columns and separators.
    known = model_pipeline(grown, "/nowhere", files={"customers.tsv": {"columns": ["id", "name", "region"], "delimiter": "\t"},
                                                     "orders.json": ["order_id", "customer_id", "amount"]})
    params = {n.id: n.params for n in known.nodes if n.type == "source" and "path" in n.params}
    assert params["src_dim_customer"]["delimiter"] == "\t" and list(params["src_fct_orders"]["columns"]) == \
        ["order_id", "customer_id", "amount"]


def test_a_table_source_the_browser_knows_declares_its_columns():
    """An Excel sheet in the playground is a table; the load reads it with its columns declared."""
    from redcliff.model import Attribute, Entity, Model

    model = Model(name="x", entities=[Entity(id="dim_store", kind="dimension", source="stores", attributes=[
        Attribute("store_key", "bigint", role="surrogate_key"), Attribute("store_id", "bigint", role="business_key"),
        Attribute("city", "varchar")])])
    load = model_pipeline(model, files={"stores": {"columns": ["store_id", "city", "opened"]}})
    src = next(n for n in load.nodes if n.id == "src_dim_store")
    assert src.params == {"format": "table", "table": "stores",
                          "columns": {"store_id": "BIGINT", "city": "VARCHAR", "opened": "VARCHAR"}}
    assert validate(load) == []
