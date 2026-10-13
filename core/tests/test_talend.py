"""Talend jobs (.item) to pipelines: the sample job end to end, and each component on small jobs."""

from __future__ import annotations

from decimal import Decimal as D
from html import escape
from pathlib import Path

import pytest

from redcliff.cli import main
from redcliff.scorecard import CONVERTED, MANUAL, REVIEW
from redcliff.spec import dump_spec
from redcliff.talend import TalendImportError, import_talend, is_talend

ROOT = Path(__file__).parents[2]
SAMPLE = ROOT / "examples" / "legacy" / "talend" / "j_order_revenue.item"
DATA = ROOT / "examples" / "data"
duckdb = pytest.importorskip("duckdb")


# --------------------------------------------------------------------------- building small jobs

def col(name: str, type_: str = "id_String", key: bool = False, length: str = "", precision: str = "") -> str:
    return (f'<column key="{str(key).lower()}" length="{length}" name="{name}" nullable="true" pattern="" '
            f'precision="{precision}" type="{type_}"/>')


def node(component: str, name: str, params: dict | None = None, tables: dict | None = None,
         metadata: dict | None = None, extra: str = "") -> str:
    out = [f'<node componentName="{component}" componentVersion="0.1" posX="0" posY="0">',
           f'<elementParameter field="TEXT" name="UNIQUE_NAME" value="{name}"/>']
    for k, v in (params or {}).items():
        out.append(f'<elementParameter field="TEXT" name="{k}" value="{escape(v)}"/>')
    for k, rows in (tables or {}).items():
        out.append(f'<elementParameter field="TABLE" name="{k}">')
        for row in rows:
            out += [f'<elementValue elementRef="{rk}" value="{escape(rv)}"/>' for rk, rv in row.items()]
        out.append("</elementParameter>")
    for mname, (connector, cols) in (metadata or {}).items():
        out.append(f'<metadata connector="{connector}" name="{mname}">{"".join(cols)}</metadata>')
    out.append(extra)
    out.append("</node>")
    return "".join(out)


def conn(source: str, target: str, label: str, connector: str = "FLOW", lookup: bool = False, metaname: str = "") -> str:
    return (f'<connection connectorName="{connector}" label="{label}" lineStyle="{8 if lookup else 0}" '
            f'metaname="{metaname or source}" source="{source}" target="{target}"/>')


def job(*parts: str, context: str = "") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><talendfile:ProcessType '
            'xmlns:talendfile="platform:/resource/org.talend.model/model/TalendFile.xsd" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" defaultContext="Default">'
            f'<context name="Default">{context}</context>' + "".join(parts) + "</talendfile:ProcessType>")


ORDERS = [col("order_id", "id_Integer", key=True), col("customer_id", "id_Integer"), col("status"),
          col("amount", "id_BigDecimal", length="10", precision="2")]
CUSTOMERS = [col("id", "id_Integer", key=True), col("first_name"), col("region")]


def orders_input(name: str = "tDBInput_1") -> str:
    return node("tDBInput", name, {"TABLE": '"orders"', "QUERY": '"SELECT order_id, customer_id, status, amount FROM orders"'},
                metadata={name: ("FLOW", ORDERS)})


def customers_input(name: str = "tDBInput_2") -> str:
    return node("tDBInput", name, {"TABLE": '"customers"'}, metadata={name: ("FLOW", CUSTOMERS)})


def output(name: str, table: str, cols: list[str], data_action: str = "INSERT", table_action: str = "DROP_CREATE") -> str:
    return node("tDBOutput", name, {"TABLE": f'"{table}"', "DATA_ACTION": data_action, "TABLE_ACTION": table_action},
                metadata={name: ("FLOW", cols)})


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    db = tmp_path_factory.mktemp("w") / "w.duckdb"
    con = duckdb.connect(str(db))
    for t in ("orders", "customers", "regions"):
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{DATA / (t + '.csv')}', header = true)")
    con.close()
    return db


def run(xml: str, tmp_path: Path, warehouse: Path, *var: str) -> duckdb.DuckDBPyConnection:
    """Import the job, write the spec next to a copy of the warehouse, run it, and return the database."""
    result = import_talend(xml, name="job")
    spec = tmp_path / "job.yaml"
    spec.write_text(dump_spec(result.pipeline, "yaml"))
    db = tmp_path / "w.duckdb"
    if not db.exists():
        db.write_bytes(warehouse.read_bytes())
    args = ["run", str(spec), "--db", str(db)]
    for v in var:
        args += ["--var", v]
    assert main(args) == 0
    return duckdb.connect(str(db), read_only=True)


def item(result, name):
    return next(i for i in result.scorecard.items if i.name == name)


# --------------------------------------------------------------------------- the sample job

def test_sample_job_imports_with_a_scorecard():
    result = import_talend(SAMPLE.read_text(), name="j_order_revenue")
    p = result.pipeline
    assert p.variables == {"min_amount": "0", "fx_rate": "1.0", "out_dir": "out"}
    targets = {n.id: n.params for n in p.nodes if n.type == "target"}
    assert targets["region_revenue"]["mode"] == "overwrite"
    assert targets["customer_activity"] == {"format": "table", "table": "customer_activity", "mode": "merge",
                                            "keys": ["customer_id"]}
    assert targets["unknown_customers"]["path"] == "${out_dir}/unknown_customers.csv"
    card = result.scorecard
    assert card.origin == "talend"
    assert item(result, "tMap_1").status == REVIEW and item(result, "tMap_1").nodes
    assert item(result, "tJava_1").status == MANUAL and item(result, "tJava_1").area == "orchestration"
    assert item(result, "tMysqlConnection_1").status == CONVERTED
    assert item(result, "tFilterRow_1").status == CONVERTED
    assert "OnSubjobOk" in item(result, "j_order_revenue").notes[0]
    assert card.lineage["traced"] >= 9
    params = {n.id: n.params for n in p.nodes}
    assert params["map_orders_row4_filter"] == {"condition": "NOT email IS NULL"}  # the lookup filter
    assert params["map_orders_row4"] == {"how": "left", "condition": "live_orders.customer_id = map_orders_row4_filter.id"}
    assert params["map_orders_row5"]["condition"] == "UPPER(map_orders_row4.region) = map_orders_row5_columns.row5_region"
    assert params["map_orders_unknown_customers_filter"] == {"condition": "id IS NULL"}  # inner join rejects
    assert params["live_orders"] == {"condition": "status <> 'cancelled' AND amount > ${min_amount}"}


def test_sample_job_runs_and_matches_hand_worked_results(tmp_path, warehouse):
    """Golden: orders 3 (cancelled) is filtered out; customer 104 has no email, so the lookup filter
    drops it and order 5 goes to the reject file; regions match on the upper-cased name."""
    con = run(SAMPLE.read_text(), tmp_path, warehouse)
    assert con.sql("SELECT * FROM region_revenue ORDER BY region").fetchall() == [
        ("North", "Maria Lopez", D("239.99"), 3, 0),
        ("South", "Tom Becker", D("330.50"), 3, 2),
    ]
    assert [r[0] for r in con.sql("DESCRIBE region_revenue").fetchall()] == \
        ["region", "manager", "revenue", "order_count", "large_orders"]
    assert con.sql("SELECT column_type FROM (DESCRIBE region_revenue) WHERE column_name = 'revenue'").fetchone()[0] == \
        "DECIMAL(14,2)"
    assert con.sql("SELECT customer_id, last_month, total_usd, orders FROM customer_activity ORDER BY 1").fetchall() == [
        (101, "2026-09", D("120.50"), 1), (102, "2026-09", D("140.00"), 2),
        (103, "2026-09", D("210.00"), 2), (105, "2026-09", D("99.99"), 1),
    ]
    assert (tmp_path / "out" / "unknown_customers.csv").read_text().splitlines() == \
        ["order_id,customer_id,amount", "5,104,15.25"]
    con.close()
    # Run again with a higher minimum: INSERT_OR_UPDATE updates 103 and keeps rows it didn't see.
    con = run(SAMPLE.read_text(), tmp_path, warehouse, "min_amount=50")
    assert con.sql("SELECT customer_id, total_usd, orders FROM customer_activity ORDER BY 1").fetchall() == [
        (101, D("120.50"), 1), (102, D("140.00"), 2), (103, D("200.00"), 1),
        (105, D("99.99"), 1),
    ]


def test_cli_and_detection(tmp_path, capsys):
    assert is_talend(SAMPLE.read_text()) and is_talend("", "job.item")
    out = tmp_path / "job.yaml"
    assert main(["import", str(SAMPLE), "-o", str(out), "--scorecard", str(tmp_path / "card.md")]) == 0
    err = capsys.readouterr().err
    assert "scorecard:" in err and "tJava_1" in err
    assert "# Migration scorecard: j_order_revenue" in (tmp_path / "card.md").read_text()
    with pytest.raises(TalendImportError):
        import_talend("<root/>")


# --------------------------------------------------------------------------- components

def test_tjoin_with_reject(tmp_path, warehouse):
    xml = job(
        orders_input(), customers_input(),
        node("tJoin", "tJoin_1", {"USE_INNER_JOIN": "true"},
             {"JOIN_KEY": [{"INPUT_COLUMN": "customer_id", "LOOKUP_COLUMN": "id"}]},
             {"tJoin_1": ("FLOW", [col("order_id", "id_Integer"), col("first_name"), col("amount", "id_BigDecimal")])}),
        output("tDBOutput_1", "named_orders", [col("order_id", "id_Integer"), col("first_name"),
                                                col("amount", "id_BigDecimal", length="10", precision="2")]),
        output("tDBOutput_2", "orphans", [col("order_id", "id_Integer"), col("customer_id", "id_Integer")]),
        conn("tDBInput_1", "tJoin_1", "row1"), conn("tDBInput_2", "tJoin_1", "row2", lookup=True),
        conn("tJoin_1", "tDBOutput_1", "row3"),
        conn("tJoin_1", "tDBOutput_2", "row4", connector="REJECT", metaname="REJECT"),
    )
    con = run(xml, tmp_path, warehouse)
    assert con.sql("SELECT COUNT(*) FROM named_orders").fetchone()[0] == 8
    assert con.sql("SELECT first_name FROM named_orders WHERE order_id = 4").fetchone()[0] == "Chen"
    assert con.sql("SELECT COUNT(*) FROM orphans").fetchone()[0] == 0


def test_tmap_output_filter_and_reject(tmp_path, warehouse):
    mapper = (
        '<nodeData xsi:type="TalendMapper:MapperData">'
        '<outputTables name="big" activateExpressionFilter="true" expressionFilter="row1.amount.compareTo(new BigDecimal(100)) &gt; 0">'
        '<mapperTableEntries name="order_id" expression="row1.order_id" type="id_Integer"/>'
        '<mapperTableEntries name="label" expression="row1.status.toUpperCase() + &quot;!&quot;" type="id_String"/>'
        "</outputTables>"
        '<outputTables name="rest" reject="true">'
        '<mapperTableEntries name="order_id" expression="row1.order_id" type="id_Integer"/>'
        "</outputTables>"
        '<inputTables name="row1"><mapperTableEntries name="order_id" type="id_Integer"/></inputTables>'
        "</nodeData>")
    xml = job(
        orders_input(),
        node("tMap", "tMap_1", metadata={"big": ("FLOW", [col("order_id", "id_Integer"), col("label")]),
                                         "rest": ("FLOW", [col("order_id", "id_Integer")])}, extra=mapper),
        output("tDBOutput_1", "big_orders", [col("order_id", "id_Integer"), col("label")]),
        output("tDBOutput_2", "other_orders", [col("order_id", "id_Integer")]),
        conn("tDBInput_1", "tMap_1", "row1"),
        conn("tMap_1", "tDBOutput_1", "big", metaname="big"), conn("tMap_1", "tDBOutput_2", "rest", metaname="rest"),
    )
    con = run(xml, tmp_path, warehouse)
    assert con.sql("SELECT order_id, label FROM big_orders ORDER BY 1").fetchall() == [(1, "SHIPPED!"), (4, "SHIPPED!")]
    assert con.sql("SELECT COUNT(*) FROM other_orders").fetchone()[0] == 6


def test_filter_row_advanced_or_and_aggregate(tmp_path, warehouse):
    xml = job(
        orders_input(),
        node("tFilterRow", "tFilterRow_1", {"LOGICAL_OP": "||", "USE_ADVANCED": "true",
                                            "ADVANCED_COND": 'input_row.status.equals("pending")'},
             {"CONDITIONS": [{"INPUT_COLUMN": "amount", "FUNCTION": "", "OPERATOR": ">=", "RVALUE": "100"}]}),
        node("tAggregateRow", "tAggregateRow_1", tables={
            "GROUPBYS": [{"OUTPUT_COLUMN": "state", "INPUT_COLUMN": "status"}],
            "OPERATIONS": [{"OUTPUT_COLUMN": "n", "FUNCTION": "count", "INPUT_COLUMN": "order_id", "IGNORE_NULL": "false"},
                           {"OUTPUT_COLUMN": "top", "FUNCTION": "max", "INPUT_COLUMN": "amount", "IGNORE_NULL": "false"}]},
             metadata={"tAggregateRow_1": ("FLOW", [col("state"), col("n", "id_Integer"), col("top", "id_BigDecimal")])}),
        output("tDBOutput_1", "by_state", [col("state"), col("n", "id_Integer"), col("top", "id_BigDecimal", length="10",
                                                                                       precision="2")]),
        conn("tDBInput_1", "tFilterRow_1", "row1"), conn("tFilterRow_1", "tAggregateRow_1", "row2", connector="FILTER"),
        conn("tAggregateRow_1", "tDBOutput_1", "row3"),
    )
    con = run(xml, tmp_path, warehouse)
    assert con.sql("SELECT state, n, top FROM by_state ORDER BY 1").fetchall() == [
        ("pending", 1, D("15.25")), ("shipped", 2, D("200.00"))]


def test_unite_filter_columns_and_insert_new(tmp_path, warehouse):
    two = [col("order_id", "id_Integer", key=True), col("status")]
    xml = job(
        orders_input("tDBInput_1"), orders_input("tDBInput_3"),
        node("tFilterColumns", "tFilterColumns_1", metadata={"tFilterColumns_1": ("FLOW", two)}),
        node("tFilterColumns", "tFilterColumns_2", metadata={"tFilterColumns_2": ("FLOW", two)}),
        node("tUnite", "tUnite_1", metadata={"tUnite_1": ("FLOW", two)}),
        node("tUniqRow", "tUniqRow_1", tables={"UNIQUE_KEY": [
            {"SCHEMA_COLUMN": "order_id", "KEY_ATTRIBUTE": "true", "CASE_SENSITIVE": "true"},
            {"SCHEMA_COLUMN": "status", "KEY_ATTRIBUTE": "false", "CASE_SENSITIVE": "true"}]}),
        output("tDBOutput_1", "seen", two, data_action="INSERT_IGNORE", table_action="CREATE_IF_NOT_EXISTS"),
        conn("tDBInput_1", "tFilterColumns_1", "row1"), conn("tDBInput_3", "tFilterColumns_2", "row2"),
        conn("tFilterColumns_1", "tUnite_1", "row3"), conn("tFilterColumns_2", "tUnite_1", "row4"),
        conn("tUnite_1", "tUniqRow_1", "row5"), conn("tUniqRow_1", "tDBOutput_1", "row6", connector="UNIQUE"),
    )
    result = import_talend(xml)
    assert next(n for n in result.pipeline.nodes if n.type == "target").params["mode"] == "insert_new"
    con = run(xml, tmp_path, warehouse)
    assert con.sql("SELECT COUNT(*), COUNT(DISTINCT order_id) FROM seen").fetchone() == (8, 8)


def test_file_input_and_output_with_context(tmp_path, warehouse):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "people.csv").write_text("id,name\n1,ann\n2,bo\n")
    ctx = '<contextParameter name="inDir" type="id_Directory" value="in"/>'
    xml = job(
        node("tFileInputDelimited", "tFileInputDelimited_1",
             {"FILENAME": 'context.inDir + "/people.csv"', "FIELDSEPARATOR": '","', "HEADER": "1"},
             metadata={"tFileInputDelimited_1": ("FLOW", [col("id", "id_Integer"), col("name")])}),
        node("tFileOutputDelimited", "tFileOutputDelimited_1", {"FILENAME": '"out/people_copy.csv"', "APPEND": "false"},
             metadata={"tFileOutputDelimited_1": ("FLOW", [col("id", "id_Integer"), col("name")])}),
        conn("tFileInputDelimited_1", "tFileOutputDelimited_1", "row1"),
        context=ctx,
    )
    result = import_talend(xml)
    assert result.pipeline.variables == {"in_dir": "in"}
    run(xml, tmp_path, warehouse)
    assert (tmp_path / "out" / "people_copy.csv").read_text().splitlines() == ["id,name", "1,ann", "2,bo"]


def test_unknown_components_get_placeholders_and_truncates_overwrite():
    xml = job(
        orders_input(),
        node("tJavaRow", "tJavaRow_1", {"CODE": "output_row.x = input_row.status;"},
             metadata={"tJavaRow_1": ("FLOW", [col("x")])}),
        output("tDBOutput_1", "xs", [col("x")], table_action="NONE"),
        node("tDBRow", "tDBRow_1", {"QUERY": '"TRUNCATE TABLE xs"'}),
        node("tUniqRow", "tUniqRow_1", tables={"UNIQUE_KEY": [{"SCHEMA_COLUMN": "order_id", "KEY_ATTRIBUTE": "true"}]}),
        output("tDBOutput_2", "dups", ORDERS),
        conn("tDBInput_1", "tJavaRow_1", "row1"), conn("tJavaRow_1", "tDBOutput_1", "row2"),
        conn("tDBInput_1", "tUniqRow_1", "row3"), conn("tUniqRow_1", "tDBOutput_2", "row4", connector="DUPLICATE"),
        conn("tDBRow_1", "tDBInput_1", "OnSubjobOk", connector="SUBJOB_OK"),
    )
    result = import_talend(xml)
    assert item(result, "tJavaRow_1").status == MANUAL
    assert item(result, "tUniqRow_1").status == MANUAL
    assert any(n.id.startswith("tjavarow_1_placeholder") for n in result.pipeline.nodes)
    xs = next(n for n in result.pipeline.nodes if n.type == "target" and n.params["table"] == "xs")
    assert xs.params["mode"] == "append"  # the TRUNCATE runs after the load is imported: order matters
    assert item(result, "tDBRow_1").status == REVIEW
