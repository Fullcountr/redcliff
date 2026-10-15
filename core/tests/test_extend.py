"""Growing a model with new files: extend_model keeps what's there and offers only what's new."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from redcliff import web
from redcliff.cli import main
from redcliff.extend import Change, apply_changes, extend_files, link
from redcliff.generate import model_pipeline, run_model_checks
from redcliff.model import Model, load_model, validate_model
from redcliff.propose import profile_files, to_data_vault
from redcliff.spec import dump_spec

duckdb = pytest.importorskip("duckdb")

CUSTOMERS = "id,first_name,last_name,email,region,signup_date\n" \
            "101,Ana,Silva,ana@x.com,South,2025-01-15\n102,Ben,Okafor,ben@x.com,North,2025-03-02\n" \
            "103,Cy,Wei,cy@x.com,South,2025-06-20\n"
ORDERS = "order_id,customer_id,order_date,status,amount\n1,101,2026-09-01,shipped,10.00\n" \
         "2,102,2026-09-01,shipped,20.00\n3,103,2026-09-02,pending,30.00\n"
ORDERS_OCT = "order_id,customer_id,order_date,status,amount\n4,101,2026-10-01,shipped,40.00\n" \
             "5,103,2026-10-02,cancelled,50.00\n"
STATUSES = "status,description\nshipped,Left the warehouse\npending,Waiting for payment\ncancelled,Cancelled\n"
PRODUCTS = "product_id,product_name,category\n10,Desk lamp,Home\n11,Notebook,Office\n12,Mug,Kitchen\n"
ORDER_ITEMS = "order_item_id,order_id,product_id,quantity,price\n1,1,10,2,3.00\n2,2,11,1,20.00\n3,3,12,4,7.50\n"
RETURNS = "return_id,customer_id,refund\n1,101,5.00\n2,103,12.50\n"


def write(folder: Path, **files: str) -> None:
    for name, text in files.items():
        (folder / f"{name}.csv").write_text(text)


@pytest.fixture
def base(tmp_path) -> tuple[Path, Model]:
    """A model proposed from customers and orders, then edited by hand: dim_customer renamed to
    dim_client with its surrogate key renamed, and a description added."""
    write(tmp_path, customers=CUSTOMERS, orders=ORDERS)
    model = profile_files(duckdb.connect(), [tmp_path / "customers.csv", tmp_path / "orders.csv"], tmp_path).model
    data = json.loads(json.dumps(model.to_dict()).replace("dim_customer", "dim_client").replace("customer_key", "client_sk"))
    data["entities"][0]["description"] = "Everyone who has bought from us."
    model = Model.from_dict(data)
    assert validate_model(model) == []
    return tmp_path, model


def defaults(ext) -> list[str]:
    return [c.id for c in ext.changes if c.default]


def test_new_files_add_entities_and_references_both_ways(base):
    folder, model = base
    write(folder, statuses=STATUSES, products=PRODUCTS, order_items=ORDER_ITEMS, returns=RETURNS)
    ext = extend_files(duckdb.connect(), model, folder,
                       [folder / f"{n}.csv" for n in ("statuses", "products", "order_items", "returns")])
    ids = {c.id: c for c in ext.changes}
    assert {"entity:dim_status", "entity:dim_product", "entity:fct_order_items", "entity:fct_returns"} <= set(ids)
    # existing -> new: orders' status values all appear in statuses
    rel = ids["relationship:fct_orders.status_key"]
    assert rel.relationship.to_dict() == {"from": "fct_orders.status_key", "to": "dim_status.status_key"}
    assert rel.needs == ["entity:dim_status"] and rel.roles["status"]["role"] == "durable_key"
    # new -> existing: follows the renamed dimension and its renamed key
    ret = ids["relationship:fct_returns.client_sk"]
    assert ret.relationship.to_dict() == {"from": "fct_returns.client_sk", "to": "dim_client.client_sk"}
    assert ret.needs == ["entity:fct_returns"]
    # new -> new
    assert ids["relationship:fct_order_items.product_key"].needs == ["entity:fct_order_items", "entity:dim_product"]
    # lookups travel with their relationship, not with the entity
    assert ids["entity:fct_returns"].entity.attribute("client_sk") is None
    assert ids["entity:fct_returns"].entity.attribute("customer_id").role is None

    grown = apply_changes(model, ext.changes, defaults(ext))
    assert validate_model(grown) == []
    client = grown.entity("dim_client")
    assert client.description == "Everyone who has bought from us."  # the hand edit survives
    assert [e.id for e in grown.entities][:2] == ["dim_client", "dim_date"]
    assert grown.entity("fct_returns").attribute("customer_id").role == "durable_key"

    # The load pipeline runs, and the new lookups find their rows.
    spec = folder / "load.yaml"
    spec.write_text(dump_spec(model_pipeline(grown, folder), "yaml"))
    db = folder / "w.duckdb"
    assert main(["run", str(spec), "--db", str(db), "--var", "load_ts=TIMESTAMP '2026-10-03 00:00:00'"]) == 0
    con = duckdb.connect(str(db))
    assert con.sql("SELECT o.order_id, s.status FROM fct_orders o JOIN dim_status s USING (status_key) "
                   "ORDER BY 1").fetchall() == [(1, "shipped"), (2, "shipped"), (3, "pending")]
    assert con.sql("SELECT r.return_id, c.first_name FROM fct_returns r JOIN dim_client c USING (client_sk) "
                   "ORDER BY 1").fetchall() == [(1, "Ana"), (2, "Cy")]
    assert con.sql("SELECT SUM(i.quantity) FROM fct_order_items i JOIN dim_product p USING (product_key) "
                   "WHERE p.category = 'Kitchen'").fetchall() == [(4,)]
    results = run_model_checks(con, grown)
    assert results and all(r["passed"] for r in results), [r for r in results if not r["passed"]]


def test_skipping_an_entity_skips_what_needs_it(base):
    folder, model = base
    write(folder, products=PRODUCTS, order_items=ORDER_ITEMS)
    ext = extend_files(duckdb.connect(), model, folder, [folder / "products.csv", folder / "order_items.csv"])
    grown = apply_changes(model, ext.changes, [i for i in defaults(ext) if i != "entity:dim_product"])
    assert grown.entity("dim_product") is None
    items = grown.entity("fct_order_items")
    assert items.attribute("product_key") is None and items.attribute("product_id").role is None
    assert validate_model(grown) == []


def test_a_file_shaped_like_a_loaded_one_is_appended(base):
    folder, model = base
    write(folder, orders_october=ORDERS_OCT)
    ext = extend_files(duckdb.connect(), model, folder, [folder / "orders_october.csv"])
    ids = {c.id: c for c in ext.changes}
    append, separate = ids["append:orders_october"], ids["entity:fct_orders_october"]
    assert append.default and not separate.default
    assert append.excludes == [separate.id] and separate.excludes == [append.id]
    grown = apply_changes(model, ext.changes, defaults(ext))
    assert grown.entity("fct_orders").append == ["orders_october.csv"]
    assert grown.entity("fct_orders_october") is None
    assert validate_model(grown) == []

    spec = folder / "load.yaml"
    spec.write_text(dump_spec(model_pipeline(grown, folder), "yaml"))
    db = folder / "w.duckdb"
    assert main(["run", str(spec), "--db", str(db), "--var", "load_ts=TIMESTAMP '2026-10-03 00:00:00'"]) == 0
    con = duckdb.connect(str(db))
    assert con.sql("SELECT COUNT(*), SUM(amount) FROM fct_orders").fetchall() == [(5, 150)]
    assert con.sql("SELECT COUNT(*) FROM dim_date WHERE date_key > 0").fetchall() == [(4,)]

    # Taking the separate entity instead (the CLI drops the append when you take its alternative).
    alone = apply_changes(model, ext.changes, ["entity:fct_orders_october"])
    assert alone.entity("fct_orders_october") is not None and not alone.entity("fct_orders").append


def test_two_new_files_of_one_shape_become_one_entity(base):
    folder, model = base
    events = "event_id,user_id,event_name\n"
    write(folder, app_events=events + "a1,101,open\n", web_events=events + "w1,102,view\nw2,103,signup\n")
    ext = extend_files(duckdb.connect(), model, folder, [folder / "app_events.csv", folder / "web_events.csv"])
    ids = {c.id: c for c in ext.changes}
    append = ids["append:web_events"]
    assert append.needs == ["entity:dim_app_event"] and append.default
    grown = apply_changes(model, ext.changes, defaults(ext))
    assert grown.entity("dim_app_event").append == ["web_events.csv"] and grown.entity("dim_web_event") is None
    # without its entity, the append has nowhere to go
    assert apply_changes(model, ext.changes, ["append:web_events"]).entity("dim_app_event") is None


def test_nothing_new_and_tables_the_model_left_out(base):
    folder, model = base
    write(folder, regions="region,manager\nNorth,Maria\n")
    ext = extend_files(duckdb.connect(), model, folder, [folder / "customers.csv"])
    assert ext.changes == [] and ext.notes == ["customers.csv is in the model already (dim_client)"]
    ext = extend_files(duckdb.connect(), model, folder, [folder / "regions.csv"])
    assert [c.id for c in ext.changes] == ["entity:dim_region"]


def test_a_data_vault_model_grows_hubs_satellites_and_links(base):
    folder, model = base
    vault = to_data_vault(profile_files(duckdb.connect(), [folder / "customers.csv", folder / "orders.csv"], folder)).model
    write(folder, products=PRODUCTS, order_items=ORDER_ITEMS)
    ext = extend_files(duckdb.connect(), vault, folder, [folder / "products.csv", folder / "order_items.csv"])
    grown = apply_changes(vault, ext.changes, defaults(ext))
    assert validate_model(grown) == []
    assert {"hub_product", "sat_product", "hub_order_item", "sat_order_item"} <= {e.id for e in grown.entities}
    assert any(e.kind == "link" and "product" in e.id for e in grown.entities)
    assert model_pipeline(grown, folder).nodes


def test_drawing_a_relationship_by_hand(base):
    folder, model = base
    write(folder, statuses=STATUSES)
    ext = extend_files(duckdb.connect(), model, folder, [folder / "statuses.csv"])
    no_link = apply_changes(model, ext.changes, ["entity:dim_status"])
    assert not any(r.target.startswith("dim_status") for r in no_link.relationships)
    linked, done = link(no_link, "fct_orders.status", "dim_status")
    assert ("fct_orders.status_key", "dim_status.status_key") in [(r.source, r.target) for r in linked.relationships]
    assert linked.entity("fct_orders").attribute("status_key").source == "status"
    assert linked.entity("fct_orders").attribute("status").role == "durable_key"
    assert validate_model(linked) == [] and len(done) == 3
    with pytest.raises(ValueError):
        link(no_link, "fct_orders.nope", "dim_status")
    with pytest.raises(ValueError, match="outrigger"):
        link(no_link, "dim_client.region", "dim_status")
    with pytest.raises(ValueError, match="points at a dimension"):
        link(no_link, "fct_orders.status", "fct_orders")
    refused = web.model_link_check(no_link.to_dict(), "dim_client.region", "dim_status", {})
    refused = json.loads(refused) if isinstance(refused, str) else refused
    assert not refused["ok"] and "outrigger" in refused["error"]

    # The check the browser runs before linking: how many values the key has.
    con = duckdb.connect()
    write(folder, orders=ORDERS.replace("pending", "lost"))
    for name in ("orders", "statuses"):
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM read_csv_auto('{folder / name}.csv', header = true)")
    check = web.model_link_check(no_link.to_dict(), "fct_orders.status", "dim_status",
                                 {"orders.csv": "orders", "statuses.csv": "statuses"})
    assert json.loads(check)["ok"] if isinstance(check, str) else check["ok"]
    sql = (json.loads(check) if isinstance(check, str) else check)["sql"]
    assert con.sql(sql).fetchall() == [(2, 1)]  # shipped and lost; only shipped is a status


def test_changes_round_trip_and_the_browser_functions(base):
    folder, model = base
    write(folder, statuses=STATUSES)
    ext = extend_files(duckdb.connect(), model, folder, [folder / "statuses.csv"])
    again = [Change.from_dict(json.loads(json.dumps(c.to_dict()))) for c in ext.changes]
    assert [c.to_dict() for c in again] == [c.to_dict() for c in ext.changes]

    def call(fn, *args):
        r = fn(*args)
        r = json.loads(r) if isinstance(r, str) else r
        assert r.pop("ok"), r
        return r

    con = duckdb.connect()
    for name in ("customers", "orders", "statuses"):
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM read_csv_auto('{folder / name}.csv', header = true)")
    tables = {t: {r[0]: str(r[1]) for r in con.execute(f"DESCRIBE {t}").fetchall()} for t in ("customers", "orders", "statuses")}
    queries = call(web.model_profile_queries, tables)["queries"]
    rows = {t: list(con.execute(q).fetchone()) for t, q in queries.items()}
    files = {t: f"{t}.csv" for t in tables}
    checks = call(web.model_reference_queries, tables, rows, files, ["statuses"])["checks"]
    assert checks and all("statuses" in (c["ref"][0], c["ref"][2]) for c in checks)
    held = [c["ref"] for c in checks if con.execute(c["sql"]).fetchone()[0] == 0]
    out = call(web.model_extend, model.to_dict(), tables, rows, held, ["statuses"], files)
    assert [c["id"] for c in out["changes"]] == ["entity:dim_status", "relationship:fct_orders.status_key"]
    grown = call(web.model_apply, model.to_dict(), out["changes"], ["entity:dim_status", "relationship:fct_orders.status_key"])
    assert any(e["id"] == "dim_status" for e in grown["model"]["entities"])
    linked = call(web.model_link, model.to_dict(), "fct_orders.customer_id", "dim_client")
    assert linked["done"]

    # The load, as the browser asks for it: its files by name, with their headers, so lineage runs through.
    data = grown["model"]
    for e in data["entities"]:
        if e.get("source"):
            e["source"] = f"data/{e['source']}"  # a model kept elsewhere still finds the browser's files
    headers = {f"{t}.csv": list(cols) for t, cols in tables.items()}
    load = call(web.model_load_pipeline, data, headers)
    sources = {n["id"]: n["params"] for n in load["spec"]["nodes"] if n["type"] == "source" and "path" in n["params"]}
    assert sources["src_dim_status"]["path"] == "statuses.csv" and list(sources["src_dim_status"]["columns"]) == ["status", "description"]
    assert call(web.analyze, load["spec"])["issues"] == []


def test_cli_extend(base, capsys):
    folder, model = base
    (folder / "model.yaml").write_text(yaml.safe_dump(model.to_dict(), sort_keys=False))
    write(folder, statuses=STATUSES, orders_october=ORDERS_OCT)
    out = folder / "model.yaml"
    assert main(["model", "extend", str(out), str(folder / "statuses.csv"), str(folder / "orders_october.csv"),
                 "--skip", "relationship:fct_orders.status_key", "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "+ entity:dim_status" in err and "  relationship:fct_orders.status_key" in err
    grown = load_model(out)
    assert grown.entity("dim_status") and grown.entity("fct_orders").append == ["orders_october.csv"]
    assert main(["model", "validate", str(out)]) == 0
    assert main(["model", "extend", str(out), str(folder / "statuses.csv"), "--skip", "nope"]) == 2
