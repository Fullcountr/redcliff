"""Legacy jobs and data models: propose a model from what a job does, map a job onto a model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redcliff.cli import main
from redcliff.importer import import_sql
from redcliff.jobmodel import job_evidence, map_job, propose_from_job
from redcliff.model import dump_model, load_model, validate_model
from redcliff.powercenter import import_powercenter
from redcliff.spec import Pipeline
from redcliff.ssis import import_ssis

ROOT = Path(__file__).parents[2]
LEGACY = ROOT / "examples" / "legacy"
DATA = ROOT / "examples" / "data"
MODELS = ROOT / "examples" / "models"


def teradata() -> Pipeline:
    schema = json.loads((LEGACY / "schema.json").read_text())
    return import_sql((LEGACY / "teradata_revenue.sql").read_text(), dialect="teradata", name="teradata_revenue",
                      schema=schema).pipeline


def powercenter() -> Pipeline:
    return import_powercenter((LEGACY / "powercenter" / "m_customer_revenue.xml").read_text()).pipeline


def ssis() -> Pipeline:
    return import_ssis((LEGACY / "ssis" / "LoadRegionRevenue.dtsx").read_text()).pipeline


def job(nodes: list[dict]) -> Pipeline:
    return Pipeline.from_dict({"version": 0, "name": "job", "nodes": nodes})


def table(id_: str, columns: dict[str, str]) -> dict:
    return {"id": id_, "type": "source", "params": {"format": "table", "table": id_, "columns": columns}}


def test_evidence_from_a_teradata_job():
    ev = job_evidence(teradata())
    assert set(ev.tables) == {"orders", "customers"}
    assert ev.sources == {"orders": "orders", "customers": "customers"}
    assert list(ev.references) == [("orders", "customer_id", "customers", "id")]
    assert "regional_revenue_join" in ev.references[("orders", "customer_id", "customers", "id")]
    assert set(ev.keys["customers"]) == {"id"}
    assert set(ev.keys["orders"]) == {"order_id"}  # by name: nothing in the job says otherwise
    assert set(ev.measures) == {"orders"} and set(ev.measures["orders"]) == {"amount"}
    assert "customer_totals" in ev.measures["orders"]["amount"]
    assert "status" in ev.used["orders"]  # filtered on, so the job reads it
    assert "signup_date" not in ev.used["customers"]


@pytest.mark.parametrize("load", [powercenter, ssis])
def test_lookups_are_keys_and_make_dimensions(load):
    proposal, ev = propose_from_job(load())
    assert "looks regions rows up by region" in ev.keys["regions"]["region"]
    assert ("customers", "region", "regions", "region") in ev.references
    model = proposal.model
    assert validate_model(model) == []
    kinds = {e.id: e.kind for e in model.entities}
    assert kinds == {"dim_customer": "dimension", "dim_region": "dimension", "dim_date": "dimension",
                     "fct_orders": "fact"}
    fact = model.entity("fct_orders")
    assert fact.attribute("order_id").role == "business_key"
    assert fact.attribute("amount").role == "measure"
    assert fact.attribute("customer_key").source == "customer_id"
    assert model.entity("dim_region").attribute("region").role == "business_key"
    assert any("summed by the job" in r for r in proposal.reasons["fct_orders.amount"])
    assert any("snowflake" in r for r in proposal.reasons["dim_customer.region"])


def test_a_join_with_no_key_side_is_not_a_reference():
    pipeline = job([
        table("a", {"code": "text", "x": "int"}),
        table("b", {"code": "text", "y": "int"}),
        {"id": "j", "type": "join", "inputs": ["a", "b"], "params": {"condition": "a.code = b.code"}},
        {"id": "out", "type": "target", "inputs": ["j"], "params": {"format": "table", "table": "out"}},
    ])
    ev = job_evidence(pipeline)
    assert ev.references == {} and ev.keys == {}
    assert ev.joins == [("a", "code", "b", "code", "j")]


def test_distinct_and_totals_are_not_lookups():
    pipeline = job([
        table("orders", {"order_id": "int", "customer_id": "int", "qty": "int"}),
        {"id": "regions_seen", "type": "aggregate", "inputs": ["orders"], "params": {"group_by": ["customer_id"]}},
        {"id": "totals", "type": "aggregate", "inputs": ["orders"],
         "params": {"group_by": ["customer_id"], "measures": {"units": "SUM(qty)"}}},
        {"id": "o1", "type": "target", "inputs": ["regions_seen"], "params": {"format": "table", "table": "o1"}},
        {"id": "o2", "type": "target", "inputs": ["totals"], "params": {"format": "table", "table": "o2"}},
    ])
    ev = job_evidence(pipeline)
    assert ev.keys == {"orders": {"order_id": "named like the key of orders; nothing in the job contradicts it"}}
    assert set(ev.measures["orders"]) == {"qty"}


def test_merge_keys_identify_rows():
    pipeline = job([
        table("accounts", {"acct_no": "text", "balance": "decimal(10,2)"}),
        {"id": "out", "type": "target", "inputs": ["accounts"],
         "params": {"format": "table", "table": "acct_copy", "mode": "merge", "keys": ["acct_no"]}},
    ])
    assert "matches acct_copy rows on acct_no" in job_evidence(pipeline).keys["accounts"]["acct_no"]


def test_map_a_job_onto_an_existing_model_by_table_name():
    result = map_job(teradata(), load_model(MODELS / "sales.yaml"), MODELS, LEGACY)
    assert result.tables == {"orders": ["fct_orders"], "customers": ["dim_customer"]}
    assert result.coverage() == 1.0
    revenue = next(c for c in result.columns if c.table == "regional_revenue" and c.column == "revenue")
    assert revenue.inputs == [{"table": "orders", "column": "amount", "kind": "direct", "attributes": ["fct_orders.amount"]}]
    md = result.to_markdown()
    assert "8 of 8 columns" in md and "`regional_revenue.revenue` | `orders.amount` → `fct_orders.amount`" in md


def test_map_shows_what_a_model_misses():
    result = map_job(ssis(), load_model(MODELS / "sales_vault.yaml"), MODELS, LEGACY / "ssis")
    assert result.tables["regions"] == []
    manager = next(c for c in result.columns if c.column == "manager")
    assert not manager.covered
    assert result.unmapped == {"regions": ["manager", "region"]}
    assert 0 < result.coverage() < 1
    assert "**no**" in result.to_markdown()


def test_a_model_proposed_from_a_job_loads_the_legacy_tables(tmp_path):
    """Golden: propose from the PowerCenter mapping, generate the load, run it on the job's tables."""
    duckdb = pytest.importorskip("duckdb")
    from redcliff.generate import model_pipeline, run_model_checks
    from redcliff.spec import dump_spec

    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    for t in ("orders", "customers", "regions"):
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{DATA / (t + '.csv')}', header = true)")
    con.close()
    proposal, _ev = propose_from_job(powercenter())
    (tmp_path / "model.yaml").write_text(dump_model(proposal.model))
    (tmp_path / "load.yaml").write_text(dump_spec(model_pipeline(proposal.model, tmp_path, tmp_path), "yaml"))
    assert main(["run", str(tmp_path / "load.yaml"), "--db", str(db)]) == 0
    con = duckdb.connect(str(db), read_only=True)
    assert con.sql("SELECT COUNT(*) FROM fct_orders").fetchone()[0] == con.sql("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert con.sql("SELECT COUNT(*) FROM dim_region WHERE region_key <> -1").fetchone()[0] == \
        con.sql("SELECT COUNT(*) FROM regions").fetchone()[0]
    results = run_model_checks(con, proposal.model)
    assert results and all(r["passed"] for r in results), [r for r in results if not r["passed"]]


def test_cli_propose_from_a_job_and_map(tmp_path, capsys):
    out = tmp_path / "model.yaml"
    assert main(["model", "propose", str(LEGACY / "ssis" / "LoadRegionRevenue.dtsx"), "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "100%" in err and "looks regions rows up by region" in err
    assert load_model(out).entity("fct_orders") is not None
    assert main(["model", "propose", str(LEGACY / "ssis" / "LoadRegionRevenue.dtsx"), "--style", "data_vault",
                 "-o", str(tmp_path / "vault.yaml")]) == 0
    assert load_model(tmp_path / "vault.yaml").entity("hub_order") is not None
    capsys.readouterr()
    assert main(["model", "map", str(MODELS / "sales.yaml"), str(LEGACY / "teradata_revenue.sql"),
                 "--dialect", "teradata", "--schema", str(LEGACY / "schema.json"), "-o", str(tmp_path / "m.json")]) == 0
    report = json.loads((tmp_path / "m.json").read_text())
    assert report["coverage"] == 1.0 and report["total"] == 8
    assert main(["model", "propose", str(DATA / "orders.csv"), str(LEGACY / "teradata_revenue.sql")]) == 2
