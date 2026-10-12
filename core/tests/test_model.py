"""Data models (M1): the model spec, its validator, and DDL / DBML / Mermaid / dbt import and export."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redcliff.cli import main
from redcliff.model import (
    Model,
    dump_model,
    json_schema,
    from_dbml,
    from_ddl,
    load_model,
    parse_model,
    to_dbml,
    to_dbt_schema,
    to_ddl,
    to_mermaid,
    validate_model,
)

ROOT = Path(__file__).parents[2]
SALES = ROOT / "examples" / "models" / "sales.yaml"


def sales() -> Model:
    return load_model(SALES)


def messages(model: Model) -> str:
    return "\n".join(str(i) for i in validate_model(model))


def test_the_example_model_is_valid():
    model = sales()
    assert validate_model(model) == []
    assert [e.kind for e in model.entities] == ["dimension", "dimension", "fact"]
    assert model.entity("dim_customer").scd2 and not model.entity("dim_date").scd2


def test_a_model_round_trips_through_yaml_and_json():
    model = sales()
    for fmt in ("yaml", "json"):
        assert parse_model(dump_model(model, fmt)).to_dict() == model.to_dict()


@pytest.mark.parametrize("change, message", [
    (lambda d: d["entities"][2].pop("grain"), "needs a 'grain'"),
    (lambda d: d["entities"][0]["attributes"][1].pop("role"), "needs a business key"),
    (lambda d: d["entities"][0].update(id="Dim-Customer"), "must match"),
    (lambda d: d["entities"][0]["attributes"][3].update(type="not a type"), "is not a SQL type"),
    (lambda d: d["entities"][2]["attributes"][4].update(history="scd2"), "applies to dimension attributes"),
    (lambda d: d["entities"][0]["attributes"][0].update(history="scd1"), "a key isn't tracked"),
    (lambda d: d["relationships"].append({"from": "fct_orders.status", "to": "dim_customer.region"}), "isn't dim_customer's key"),
    (lambda d: d["relationships"].append({"from": "fct_orders.status", "to": "dim_date.date_key"}), "types differ"),
    (lambda d: d["relationships"].append({"from": "fct_orders.nope", "to": "dim_date.date_key"}), "not an entity.attribute"),
    (lambda d: d.update(style="anchor"), "'style' must be one of"),
])
def test_validation_finds_problems(change, message):
    data = sales().to_dict()
    change(data)
    assert message in messages(Model.from_dict(data))


def test_ddl_runs_in_duckdb_with_keys():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute(to_ddl(sales()))
    con.execute("INSERT INTO dim_customer (customer_key, customer_id) VALUES (1, 101)")
    con.execute("INSERT INTO dim_date (date_key, calendar_date) VALUES (20260901, DATE '2026-09-01')")
    con.execute("INSERT INTO fct_orders (order_id, customer_key, order_date_key, amount) VALUES (1, 1, 20260901, 9.5)")
    with pytest.raises(duckdb.ConstraintException):  # the foreign key holds
        con.execute("INSERT INTO fct_orders (order_id, customer_key) VALUES (2, 99)")


def test_ddl_dialects_and_reserved_words():
    model = sales()
    assert "PRIMARY KEY" not in to_ddl(model, "spark") and "-- fct_orders.customer_key references" in to_ddl(model, "spark")
    assert "VARCHAR" in to_ddl(model, "snowflake")
    model.entity("dim_date").attributes.append(type(model.entity("dim_date").attributes[0])("order", "int"))
    assert '"order" INT' in to_ddl(model, "duckdb") and "`order` INT" in to_ddl(model, "spark")


def test_dbml_round_trips_roles_and_relationships():
    model = sales()
    back = from_dbml(to_dbml(model), name="sales").model
    for e in model.entities:
        got = back.entity(e.id)
        assert [(a.name, a.role, a.history, a.track) for a in got.attributes] == \
            [(a.name, a.role, a.history, a.track) for a in e.attributes]
    assert [r.to_dict() for r in back.relationships] == [r.to_dict() for r in model.relationships]


def test_dbml_import():
    text = """
    Project shop { database_type: 'PostgreSQL' }
    Table dim_product [note: 'Products'] {
      product_key int [pk]
      sku varchar [not null, note: 'business key']
      name varchar
    }
    Table fct_line {
      order_id int
      line_no int
      product_key int [ref: > dim_product.product_key]
      qty int
      price decimal(8,2)
      indexes {
        (order_id, line_no) [pk]
      }
    }
    Table tag { id int [pk] }
    Ref: tag.id <> dim_product.product_key
    """
    result = from_dbml(text, name="shop")
    model = result.model
    assert model.entity("dim_product").description == "Products"
    assert model.entity("dim_product").key() == ["product_key"]
    assert [a.role for a in model.entity("fct_line").attributes] == \
        ["business_key", "business_key", None, "measure", "measure"]
    assert [r.to_dict() for r in model.relationships] == [{"from": "fct_line.product_key", "to": "dim_product.product_key"}]
    assert any("many-to-many" in w for w in result.warnings) and any("grain" in w for w in result.warnings)


def test_ddl_import():
    sql = """
    CREATE TABLE hub_customer (customer_hk CHAR(32) PRIMARY KEY, customer_id VARCHAR(20), load_dts TIMESTAMP);
    CREATE TABLE sat_customer (
      customer_hk CHAR(32) REFERENCES hub_customer (customer_hk),
      load_dts TIMESTAMP, name VARCHAR(100),
      PRIMARY KEY (customer_hk, load_dts));
    CREATE INDEX ix ON sat_customer (name);
    """
    result = from_ddl(sql, "postgres", name="vault")
    model = result.model
    assert model.style == "data_vault"
    assert [e.kind for e in model.entities] == ["hub", "satellite"]
    assert model.entity("hub_customer").key() == ["customer_hk"]
    assert model.entity("sat_customer").key() == ["customer_hk", "load_dts"]
    assert [r.to_dict() for r in model.relationships] == [{"from": "sat_customer.customer_hk", "to": "hub_customer.customer_hk"}]
    assert any(w.startswith("skipped: CREATE INDEX") for w in result.warnings)  # never dropped silently


def test_ddl_import_round_trips_the_example():
    back = from_ddl(to_ddl(sales()), name="sales").model
    assert [e.id for e in back.entities] == ["dim_customer", "dim_date", "fct_orders"]
    assert len(back.relationships) == 2
    assert back.entity("fct_orders").attribute("customer_id").role is None  # an id is not a measure
    assert back.entity("fct_orders").attribute("amount").role == "measure"


def test_mermaid_and_dbt():
    model = sales()
    mermaid = to_mermaid(model)
    assert mermaid.startswith("erDiagram")
    assert "bigint customer_key PK" in mermaid and "int order_date_key FK" in mermaid
    assert 'dim_customer ||--o{ fct_orders : "customer_key"' in mermaid
    dbt = yaml.safe_load(to_dbt_schema(model))
    cols = {m["name"]: {c["name"]: c.get("tests", []) for c in m["columns"]} for m in dbt["models"]}
    assert cols["dim_customer"]["customer_key"] == ["not_null", "unique"]
    assert cols["dim_customer"]["customer_id"] == ["not_null"]  # SCD2: the business key repeats per version
    assert cols["dim_date"]["date_key"] == ["not_null", "unique"]
    assert cols["fct_orders"]["customer_key"] == [{"relationships": {"to": "ref('dim_customer')", "field": "customer_key"}}]


def test_cli(tmp_path, capsys):
    assert main(["model", "validate", str(SALES)]) == 0
    assert "valid (3 entities, 2 relationships)" in capsys.readouterr().out
    assert main(["model", "export", str(SALES), "--to", "mermaid"]) == 0
    assert capsys.readouterr().out.startswith("erDiagram")
    dbml = tmp_path / "sales.dbml"
    assert main(["model", "export", str(SALES), "--to", "dbml", "-o", str(dbml)]) == 0
    out = tmp_path / "back.yaml"
    assert main(["model", "import", str(dbml), "-o", str(out)]) == 0
    assert main(["model", "validate", str(out)]) == 1  # the fact's grain doesn't survive DBML: asked for
    assert "set the fact's grain" in capsys.readouterr().err


def test_json_schema_accepts_the_example_and_exports():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(yaml.safe_load(SALES.read_text()), json_schema())
    data = sales().to_dict()
    data["entities"][2].pop("grain")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, json_schema())
