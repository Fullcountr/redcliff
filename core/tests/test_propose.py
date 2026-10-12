"""M2: profile files and propose a Kimball model, with the reason for every choice."""

from __future__ import annotations

from pathlib import Path

import pytest

from redcliff.cli import main
from redcliff.model import load_model, validate_model
from redcliff.propose import (
    ColumnProfile,
    TableProfile,
    candidate_references,
    profile_files,
    propose_model,
)

ROOT = Path(__file__).parents[2]
DATA = ROOT / "examples" / "data"


def table(name, rows, *cols):
    return TableProfile(name, f"{name}.csv", rows, [ColumnProfile(*c) for c in cols])


def test_candidate_keys_and_references_need_matching_names():
    customers = table("customers", 3, ("id", "BIGINT", 0, 3), ("name", "VARCHAR", 0, 3))
    orders = table("orders", 5, ("order_id", "BIGINT", 0, 5), ("customer_id", "BIGINT", 0, 3),
                   ("store_id", "BIGINT", 0, 2), ("amount", "DECIMAL(10,2)", 0, 5))
    assert customers.candidate_keys() == ["id", "name"]
    assert orders.candidate_keys() == ["order_id"]
    # customer_id matches customers.id by name; store_id has no table, amount isn't a key
    assert candidate_references([customers, orders]) == [("orders", "customer_id", "customers", "id")]


def test_kinds_roles_and_reasons():
    customers = table("customers", 3, ("id", "BIGINT", 0, 3), ("full_name", "VARCHAR", 0, 3),
                      ("segment", "VARCHAR", 0, 2), ("updated_at", "TIMESTAMP", 0, 3))
    orders = table("orders", 5, ("order_id", "BIGINT", 0, 5), ("customer_id", "BIGINT", 0, 3),
                   ("order_date", "DATE", 0, 4), ("quantity", "INTEGER", 0, 4), ("amount", "DECIMAL(10,2)", 0, 5))
    p = propose_model([customers, orders], [("orders", "customer_id", "customers", "id")], name="shop")
    m = p.model
    assert validate_model(m) == []
    assert [(e.id, e.kind) for e in m.entities] == [("dim_customer", "dimension"), ("dim_date", "dimension"),
                                                   ("fct_orders", "fact")]
    dim = m.entity("dim_customer")
    roles = {a.name: (a.role, a.history, a.track, a.source) for a in dim.attributes}
    assert roles["customer_key"][0] == "surrogate_key"
    assert roles["customer_id"] == ("business_key", None, True, "id")  # renamed from the generic id
    assert roles["full_name"][1] == "scd1"  # a name: corrections overwrite
    assert roles["updated_at"][2] is False  # an audit column isn't tracked
    assert roles["segment"] == (None, None, True, None)  # tracked: SCD2
    fact = m.entity("fct_orders")
    assert fact.grain == "one order (order_id)"
    froles = {a.name: a.role for a in fact.attributes}
    assert froles["customer_key"] is None and froles["customer_id"] == "durable_key"
    assert froles["amount"] == froles["quantity"] == "measure" and froles["order_id"] == "business_key"
    assert fact.attribute("order_date_key").source.startswith("YEAR(order_date)")
    assert {(r.source, r.target) for r in m.relationships} == {
        ("fct_orders.customer_key", "dim_customer.customer_key"), ("fct_orders.order_date_key", "dim_date.date_key")}
    assert "dim_customer.full_name" in p.reasons and "fct_orders" in p.reasons and "dim_date" in p.reasons


def test_profile_files_on_the_sample_data(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    p = profile_files(duckdb.connect(), [DATA / "orders.csv", DATA / "customers.csv"], base_dir=tmp_path)
    assert validate_model(p.model) == []
    assert p.model.entity("dim_customer").source.endswith("examples/data/customers.csv")
    assert p.model.entity("fct_orders").attribute("customer_key")
    assert any("appears as customers.id" in r for r in p.reasons["fct_orders.customer_key"])


def test_cli_propose(tmp_path, capsys):
    pytest.importorskip("duckdb")
    out = tmp_path / "model.yaml"
    assert main(["model", "propose", str(DATA / "orders.csv"), str(DATA / "customers.csv"), "--name", "sales",
                 "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "dim_customer: a dimension" in err and "wrote" in err
    model = load_model(out)
    assert model.name == "sales" and validate_model(model) == []
    assert model.entity("fct_orders").source == "../../" + "/".join(DATA.relative_to(ROOT).parts) + "/orders.csv" \
        or model.entity("fct_orders").source.endswith("orders.csv")
