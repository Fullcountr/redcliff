"""PowerCenter importer tests.

The main check runs the invented sample mapping end to end: import the XML, compile each target's
rows to DuckDB SQL, run it on the sample data, and compare with rows worked out by hand from what
the mapping does. Smaller synthetic mappings cover each transformation type and the honest
statuses (review, manual) of the scorecard.
"""

from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import quoteattr

import duckdb
import pytest

from redcliff.compile import compile_query, compile_pyspark, compile_script
from redcliff.lineage import Ref, build_graph, trace_to_sources
from redcliff.powercenter import PowerCenterImportError, import_powercenter, mapping_names

ROOT = Path(__file__).parents[2]
DATA = ROOT / "examples" / "data"
SAMPLE = ROOT / "examples" / "legacy" / "powercenter" / "m_customer_revenue.xml"


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    # Types as the (invented) Oracle definitions declare them.
    types = {"orders": {"amount": "DECIMAL(10,2)"}, "regions": {"monthly_target": "DECIMAL(12,2)"}, "customers": {}}
    for t, cast in types.items():
        types_arg = f", types = {cast!r}" if cast else ""
        c.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{DATA / t}.csv', header = true{types_arg})")
    return c


def rows(con, result, node: str):
    return con.execute(compile_query(result.pipeline, node, "duckdb")).fetchall()


# ---------------------------------------------------------------- the sample mapping, end to end


@pytest.fixture(scope="module")
def sample():
    return import_powercenter(SAMPLE.read_text(encoding="utf-8"))


def test_sample_monthly_revenue_matches_hand_worked_rows(con, sample):
    # Orders from 2026-09-02 on (source filter), shipped (router), joined to cleaned customers,
    # with the region manager looked up: SOUTH has orders 4 and 8, NORTH has 6 and 7.
    got = sorted(rows(con, sample, sample.outputs[0]))
    assert got == [
        ("NORTH", "2026-09", "Maria Lopez", Decimal("159.99"), 2, 0, Decimal("80.00")),
        ("SOUTH", "2026-09", "Tom Becker", Decimal("210.00"), 2, 1, Decimal("105.00")),
    ]


def test_sample_exceptions_skip_rejected_rows_and_number_from_the_sequence(con, sample):
    # The router's DEFAULT group holds order 3 (cancelled) and 5 (pending); the update strategy
    # rejects cancelled rows, and the sequence starts at its current value, 1000.
    got = rows(con, sample, sample.outputs[1])
    assert got == [(1000, 5, "pending", Decimal("15.25"), "Dana Kim", None)]


def test_sample_shape_reads_like_hand_written_steps(sample):
    nodes = {n.id: n for n in sample.pipeline.nodes}
    assert nodes["sq_orders"].params == {"condition": "order_date >= STR_TO_DATE('2026-09-02', '%Y-%m-%d')"}
    assert nodes["exp_clean_customer"].params["columns"]["full_name"] == \
        "CONCAT(COALESCE(TRIM(first_name), ''), ' ', COALESCE(TRIM(last_name), ''))"
    assert nodes["jnr_orders_customers"].params == {"how": "left", "condition": "exp_clean_customer.id = sq_orders.customer_id"}
    assert nodes["lkp_region_unique"].params == {"group_by": ["region"], "measures": {"manager": "ANY_VALUE(manager)"}}
    assert nodes["rtr_by_status_default1"].params == {"condition": "NOT COALESCE(status = 'shipped', FALSE)"}
    assert nodes["upd_exceptions"].params == {"condition": "NOT COALESCE(status = 'cancelled', FALSE)"}
    assert [n.type for n in sample.pipeline.nodes].count("target") == 2
    assert sample.outputs == ["agg_region_month", "order_exceptions_rows"]


def test_sample_scorecard_is_honest(sample):
    card = sample.scorecard
    assert card.counts() == {"converted": 12, "review": 2, "manual": 0}
    by_name = {i.name: i for i in card.items}
    assert by_name["AGG_REGION_MONTH"].status == "review"
    assert "ANY_VALUE" in by_name["AGG_REGION_MONTH"].notes[0]
    assert by_name["SEQ_EXCEPTION_ID"].status == "review"
    assert by_name["LKP_REGION"].nodes == ["regions", "lkp_region_unique", "lkp_region"]
    assert card.lineage == {"columns": 13, "traced": 12, "untraced": ["order_exceptions.exception_id"]}
    assert card.functions["LTRIM"] == 4
    md = card.to_markdown()
    assert "**12 of 14** converted automatically (86%)" in md
    assert card.to_dict()["automated"] == round(12 / 14, 4)


def test_sample_lineage_runs_through_the_whole_mapping(sample):
    graph = build_graph(sample.pipeline)
    assert graph.issues == []
    traced = trace_to_sources(graph, Ref("region_monthly_revenue", "revenue"))
    assert traced[Ref("orders", "amount")] == ("DIRECT", "AGGREGATION")
    region = trace_to_sources(graph, Ref("region_monthly_revenue", "region"))
    assert region == {Ref("customers", "region"): ("DIRECT", "TRANSFORMATION")}
    manager = trace_to_sources(graph, Ref("region_monthly_revenue", "manager"))
    assert manager[Ref("regions", "manager")][0] == "DIRECT"


def test_sample_compiles_to_spark_and_pyspark(sample):
    spark = compile_script(sample.pipeline, "spark")
    assert "DATE_FORMAT(order_date, 'yyyy-MM')" in spark
    assert "INSERT INTO region_monthly_revenue" in spark or "region_monthly_revenue" in spark
    assert "ANY_VALUE(manager)" in spark
    assert "def " in compile_pyspark(sample.pipeline) or "spark" in compile_pyspark(sample.pipeline)


def test_mapping_names_and_selection():
    text = SAMPLE.read_text(encoding="utf-8")
    assert mapping_names(text) == ["m_customer_revenue"]
    with pytest.raises(PowerCenterImportError, match="no mapping named 'nope'"):
        import_powercenter(text, mapping="nope")


def test_not_powercenter_xml():
    with pytest.raises(PowerCenterImportError, match="could not read the XML"):
        import_powercenter("SELECT 1")
    with pytest.raises(PowerCenterImportError, match="isn't a PowerCenter export"):
        import_powercenter("<root><thing/></root>")


# ---------------------------------------------------------------- a tiny export builder for tests


def field(tag: str, spec: str, **extra) -> str:
    name, _, dtype = spec.partition(":")
    dtype = dtype or "string"
    prec, scale = ("10", "2") if dtype == "decimal" else ("20", "0")
    attrs = {"NAME": name, "DATATYPE": dtype, "PRECISION": prec, "SCALE": scale, **extra}
    return f"<{tag} " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items()) + "/>"


def source(name: str, *fields: str, dbtype: str = "Oracle") -> str:
    body = "".join(field("SOURCEFIELD", f) for f in fields)
    return f'<SOURCE NAME="{name}" DATABASETYPE="{dbtype}" DBDNAME="DB" OWNERNAME="OWN">{body}</SOURCE>'


def target(name: str, *fields: str) -> str:
    body = "".join(field("TARGETFIELD", f) for f in fields)
    return f'<TARGET NAME="{name}" DATABASETYPE="Oracle">{body}</TARGET>'


def port(spec: str, porttype: str = "INPUT/OUTPUT", expr: str = "", **extra) -> str:
    more = {"PORTTYPE": porttype, **({"EXPRESSION": expr} if expr else {}), **extra}
    return field("TRANSFORMFIELD", spec, **more)


def transform(name: str, ttype: str, ports: list[str], attrs: dict[str, str] | None = None,
              groups: list[str] | None = None, reusable: bool = False, template: str = "") -> str:
    a = "".join(f"<TABLEATTRIBUTE NAME={quoteattr(k)} VALUE={quoteattr(v)}/>" for k, v in (attrs or {}).items())
    extra = f' TEMPLATENAME="{template}"' if template else ""
    return (f'<TRANSFORMATION NAME="{name}" TYPE="{ttype}" REUSABLE="{"YES" if reusable else "NO"}"{extra}>'
            + "".join(groups or []) + "".join(ports) + a + "</TRANSFORMATION>")


def export(mappings: list[str], sources: list[str] = (), targets: list[str] = (), folder_transforms=()) -> str:
    return ("<POWERMART><REPOSITORY NAME='R'><FOLDER NAME='F'>" + "".join(sources) + "".join(targets)
            + "".join(folder_transforms) + "".join(mappings) + "</FOLDER></REPOSITORY></POWERMART>")


def mapping(name: str, transforms: list[str], instances: list[tuple[str, str, str]],
            connectors: list[str], variables: str = "") -> str:
    """instances: (name, kind, transformation type). connectors: 'A.F -> B.G'."""
    inst = []
    for iname, kind, ttype in instances:
        assoc = ""
        if ttype == "Source Qualifier":
            srcs = {c.split("->")[0].strip().split(".")[0] for c in connectors
                    if c.split("->")[1].strip().split(".")[0] == iname}
            assoc = "".join(f'<ASSOCIATED_SOURCE_INSTANCE NAME="{s}"/>' for s in sorted(srcs))
        inst.append(f'<INSTANCE NAME="{iname}" TRANSFORMATION_NAME="{iname}" TRANSFORMATION_TYPE="{ttype}" '
                    f'TYPE="{kind}">{assoc}</INSTANCE>')
    conns = []
    for c in connectors:
        left, right = [x.strip() for x in c.split("->")]
        fi, ff = left.split(".")
        ti, tf = right.split(".")
        conns.append(f'<CONNECTOR FROMINSTANCE="{fi}" FROMFIELD="{ff}" TOINSTANCE="{ti}" TOFIELD="{tf}"/>')
    return f'<MAPPING NAME="{name}">' + "".join(transforms) + "".join(inst) + "".join(conns) + variables + "</MAPPING>"


ORDERS = source("ORDERS", "ORDER_ID:decimal", "CUSTOMER_ID:decimal", "ORDER_DATE:date", "STATUS", "AMOUNT:decimal")
CUSTOMERS = source("CUSTOMERS", "ID:decimal", "FIRST_NAME", "LAST_NAME", "EMAIL", "REGION")
SQ_ORDERS = transform("SQ_ORDERS", "Source Qualifier", [port("ORDER_ID:decimal"), port("CUSTOMER_ID:decimal"),
                      port("STATUS"), port("AMOUNT:decimal")])
ORDER_LINKS = [f"ORDERS.{f} -> SQ_ORDERS.{f}" for f in ("ORDER_ID", "CUSTOMER_ID", "STATUS", "AMOUNT")]
BASE_INSTANCES = [("ORDERS", "SOURCE", "Source Definition"), ("SQ_ORDERS", "TRANSFORMATION", "Source Qualifier")]


def simple(transforms, instances, connectors, targets, sources=(ORDERS,), **kw):
    m = mapping("m_test", [SQ_ORDERS, *transforms], BASE_INSTANCES + instances, ORDER_LINKS + connectors)
    return import_powercenter(export([m], list(sources), targets, **kw))


# ---------------------------------------------------------------- transformation types


def test_union_projects_each_group_by_position(con):
    union = transform("UN_ALL", "Custom Transformation", [
        port("ORDER_ID:decimal", "INPUT", GROUP="A"), port("AMOUNT:decimal", "INPUT", GROUP="A"),
        port("ORDER_ID1:decimal", "INPUT", GROUP="B"), port("AMOUNT1:decimal", "INPUT", GROUP="B"),
        port("ID:decimal", "OUTPUT", GROUP="OUT"), port("VALUE:decimal", "OUTPUT", GROUP="OUT"),
    ], groups=['<GROUP NAME="A" TYPE="INPUT"/>', '<GROUP NAME="B" TYPE="INPUT"/>', '<GROUP NAME="OUT" TYPE="OUTPUT"/>'],
        template="Union Transformation")
    fil = transform("FIL_BIG", "Filter", [port("ORDER_ID:decimal"), port("AMOUNT:decimal")],
                    {"Filter Condition": "AMOUNT >= 100"})
    r = simple([fil, union], [("FIL_BIG", "TRANSFORMATION", "Filter"), ("UN_ALL", "TRANSFORMATION", "Custom Transformation"),
                              ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.ORDER_ID -> FIL_BIG.ORDER_ID", "SQ_ORDERS.AMOUNT -> FIL_BIG.AMOUNT",
                "FIL_BIG.ORDER_ID -> UN_ALL.ORDER_ID", "FIL_BIG.AMOUNT -> UN_ALL.AMOUNT",
                "SQ_ORDERS.ORDER_ID -> UN_ALL.ORDER_ID1", "SQ_ORDERS.AMOUNT -> UN_ALL.AMOUNT1",
                "UN_ALL.ID -> T.ID", "UN_ALL.VALUE -> T.VALUE"], [target("T", "ID:decimal", "VALUE:decimal")])
    assert r.scorecard.counts()["manual"] == 0
    got = rows(con, r, r.outputs[0])
    assert len(got) == 8 + 2  # every order, plus the two over 100 again
    assert r.pipeline.node("un_all").type == "union"


def test_sorter_distinct_and_plain():
    srt = transform("SRT_STATUS", "Sorter", [port("STATUS")], {"Distinct": "YES"})
    r = simple([srt], [("SRT_STATUS", "TRANSFORMATION", "Sorter"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.STATUS -> SRT_STATUS.STATUS", "SRT_STATUS.STATUS -> T.STATUS"], [target("T", "STATUS")])
    assert r.pipeline.node("srt_status").params == {"group_by": ["status"]}
    srt = transform("SRT_STATUS", "Sorter", [port("STATUS")], {"Distinct": "NO"})
    r = simple([srt], [("SRT_STATUS", "TRANSFORMATION", "Sorter"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.STATUS -> SRT_STATUS.STATUS", "SRT_STATUS.STATUS -> T.STATUS"], [target("T", "STATUS")])
    assert "srt_status" not in {n.id for n in r.pipeline.nodes}
    assert r.scorecard.counts() == {"converted": 4, "review": 0, "manual": 0}


def test_unsupported_transformation_becomes_a_typed_placeholder(con):
    jtx = transform("JTX_X", "Java", [port("STATUS", "INPUT"), port("CODE:decimal", "OUTPUT")])
    r = simple([jtx], [("JTX_X", "TRANSFORMATION", "Java"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.STATUS -> JTX_X.STATUS", "JTX_X.CODE -> T.CODE"], [target("T", "CODE:decimal")])
    item = next(i for i in r.scorecard.items if i.name == "JTX_X")
    assert item.status == "manual"
    assert "Java transformations have no Redcliff equivalent yet" in item.notes[0]
    assert r.pipeline.node("jtx_x").params == {"columns": {"code": "CAST(NULL AS DECIMAL(10,2))"}}
    assert all(row == (None,) for row in rows(con, r, r.outputs[0]))
    assert r.scorecard.lineage["untraced"] == ["t.code"]
    assert any("JTX_X" in w for w in r.warnings)


def test_stateful_variable_port_is_manual():
    exp_ = transform("EXP_RUNNING", "Expression", [
        port("AMOUNT:decimal", "INPUT"),
        port("v_TOTAL:decimal", "LOCAL VARIABLE", "v_TOTAL + AMOUNT"),
        port("o_TOTAL:decimal", "OUTPUT", "v_TOTAL"),
    ])
    r = simple([exp_], [("EXP_RUNNING", "TRANSFORMATION", "Expression"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.AMOUNT -> EXP_RUNNING.AMOUNT", "EXP_RUNNING.o_TOTAL -> T.TOTAL"], [target("T", "TOTAL:decimal")])
    item = next(i for i in r.scorecard.items if i.name == "EXP_RUNNING")
    assert item.status == "manual"
    assert "previous row" in item.notes[0]


def test_unconnected_input_port_is_null_and_parameters_use_defaults(con):
    exp_ = transform("EXP_X", "Expression", [
        port("AMOUNT:decimal", "INPUT"), port("BONUS:decimal", "INPUT"),
        port("o_TOTAL:decimal", "OUTPUT", "IIF(ISNULL(BONUS), AMOUNT * $$RATE, AMOUNT + BONUS)"),
    ])
    variables = '<MAPPINGVARIABLE NAME="$$RATE" DATATYPE="decimal" DEFAULTVALUE="2" ISPARAM="YES"/>'
    m = mapping("m_test", [SQ_ORDERS, exp_], BASE_INSTANCES + [("EXP_X", "TRANSFORMATION", "Expression"),
                                                               ("T", "TARGET", "Target Definition")],
                ORDER_LINKS + ["SQ_ORDERS.AMOUNT -> EXP_X.AMOUNT", "EXP_X.o_TOTAL -> T.TOTAL"], variables)
    r = import_powercenter(export([m], [ORDERS], [target("T", "TOTAL:decimal")]))
    assert r.pipeline.node("exp_x").params == {
        "columns": {"total": "CASE WHEN NULL IS NULL THEN amount * 2 ELSE amount + NULL END"}}
    item = next(i for i in r.scorecard.items if i.name == "EXP_X")
    assert item.status == "review" and "$$RATE" in item.notes[0]
    assert sorted(rows(con, r, r.outputs[0]))[0] == (Decimal("20.00"),)


def test_two_sources_in_one_qualifier_with_a_user_defined_join(con):
    sq = transform("SQ_JOINED", "Source Qualifier", [port("ORDER_ID:decimal"), port("AMOUNT:decimal"), port("REGION")],
                   {"User Defined Join": "ORDERS.CUSTOMER_ID = CUSTOMERS.ID", "Source Filter": "CUSTOMERS.REGION = 'North'"})
    m = mapping("m_test", [sq], [("ORDERS", "SOURCE", "Source Definition"), ("CUSTOMERS", "SOURCE", "Source Definition"),
                                 ("SQ_JOINED", "TRANSFORMATION", "Source Qualifier"), ("T", "TARGET", "Target Definition")],
                ["ORDERS.ORDER_ID -> SQ_JOINED.ORDER_ID", "ORDERS.AMOUNT -> SQ_JOINED.AMOUNT",
                 "CUSTOMERS.REGION -> SQ_JOINED.REGION", "SQ_JOINED.ORDER_ID -> T.ORDER_ID",
                 "SQ_JOINED.AMOUNT -> T.AMOUNT", "SQ_JOINED.REGION -> T.REGION"])
    r = import_powercenter(export([m], [ORDERS, CUSTOMERS], [target("T", "ORDER_ID:decimal", "AMOUNT:decimal", "REGION")]))
    assert r.scorecard.counts()["manual"] == 0
    assert sorted(rows(con, r, r.outputs[0])) == [(2, Decimal("80.00"), "North"), (6, Decimal("60.00"), "North"),
                                                  (7, Decimal("99.99"), "North")]


def test_sql_override_is_imported_as_steps(con):
    sq = transform("SQ_ORDERS", "Source Qualifier", [port("STATUS"), port("TOTAL:decimal")],
                   {"Sql Query": "SELECT STATUS, SUM(AMOUNT) FROM SALES.ORDERS GROUP BY STATUS"})
    m = mapping("m_test", [sq], BASE_INSTANCES + [("T", "TARGET", "Target Definition")],
                ["ORDERS.STATUS -> SQ_ORDERS.STATUS", "ORDERS.AMOUNT -> SQ_ORDERS.TOTAL",
                 "SQ_ORDERS.STATUS -> T.STATUS", "SQ_ORDERS.TOTAL -> T.TOTAL"])
    r = import_powercenter(export([m], [ORDERS], [target("T", "STATUS", "TOTAL:decimal")]))
    assert dict(rows(con, r, r.outputs[0]))["shipped"] == Decimal("570.49")
    assert r.pipeline.node("sq_orders").type == "aggregate"


def test_folder_export_connects_mappings_through_their_tables(con):
    # m_load_big writes BIG_ORDERS; m_report reads it. The report is listed first on purpose.
    fil = transform("FIL_BIG", "Filter", [port("ORDER_ID:decimal"), port("AMOUNT:decimal")], {"Filter Condition": "AMOUNT > 90"})
    load = mapping("m_load_big", [SQ_ORDERS, fil], BASE_INSTANCES + [("FIL_BIG", "TRANSFORMATION", "Filter"),
                                                                     ("BIG_ORDERS", "TARGET", "Target Definition")],
                   ORDER_LINKS + ["SQ_ORDERS.ORDER_ID -> FIL_BIG.ORDER_ID", "SQ_ORDERS.AMOUNT -> FIL_BIG.AMOUNT",
                                  "FIL_BIG.ORDER_ID -> BIG_ORDERS.ORDER_ID", "FIL_BIG.AMOUNT -> BIG_ORDERS.AMOUNT"])
    sq_big = transform("SQ_BIG", "Source Qualifier", [port("ORDER_ID:decimal"), port("AMOUNT:decimal")])
    agg = transform("AGG_ALL", "Aggregator", [port("AMOUNT:decimal", "INPUT"), port("TOTAL:decimal", "OUTPUT", "SUM(AMOUNT)")])
    report = mapping("m_report", [sq_big, agg], [("BIG_ORDERS", "SOURCE", "Source Definition"),
                                                 ("SQ_BIG", "TRANSFORMATION", "Source Qualifier"),
                                                 ("AGG_ALL", "TRANSFORMATION", "Aggregator"),
                                                 ("BIG_TOTAL", "TARGET", "Target Definition")],
                     ["BIG_ORDERS.ORDER_ID -> SQ_BIG.ORDER_ID", "BIG_ORDERS.AMOUNT -> SQ_BIG.AMOUNT",
                      "SQ_BIG.AMOUNT -> AGG_ALL.AMOUNT", "AGG_ALL.TOTAL -> BIG_TOTAL.TOTAL"])
    big_src = source("BIG_ORDERS", "ORDER_ID:decimal", "AMOUNT:decimal")
    r = import_powercenter(export([report, load], [ORDERS, big_src],
                                  [target("BIG_ORDERS", "ORDER_ID:decimal", "AMOUNT:decimal"), target("BIG_TOTAL", "TOTAL:decimal")]))
    assert "big_orders" not in {n.id for n in r.pipeline.nodes if n.type == "source"}
    assert rows(con, r, r.outputs[-1]) == [(Decimal("420.49"),)]
    traced = trace_to_sources(build_graph(r.pipeline), Ref("big_total", "total"))
    assert traced[Ref("orders", "amount")] == ("DIRECT", "AGGREGATION")
    assert r.pipeline.name == "f"


def test_reusable_transformation_from_the_folder():
    exp_ = transform("EXP_UPPER", "Expression", [port("STATUS", "INPUT"), port("o_STATUS", "OUTPUT", "UPPER(STATUS)")],
                     reusable=True)
    m = mapping("m_test", [SQ_ORDERS], BASE_INSTANCES + [("EXP_UPPER", "TRANSFORMATION", "Expression"),
                                                         ("T", "TARGET", "Target Definition")],
                ORDER_LINKS + ["SQ_ORDERS.STATUS -> EXP_UPPER.STATUS", "EXP_UPPER.o_STATUS -> T.STATUS"])
    r = import_powercenter(export([m], [ORDERS], [target("T", "STATUS")], folder_transforms=[exp_]))
    assert r.pipeline.node("exp_upper").params == {"columns": {"status": "UPPER(status)"}}


def test_update_strategy_with_updates_needs_review():
    upd = transform("UPD_X", "Update Strategy", [port("ORDER_ID:decimal")],
                    {"Update Strategy Expression": "DD_UPDATE"})
    r = simple([upd], [("UPD_X", "TRANSFORMATION", "Update Strategy"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.ORDER_ID -> UPD_X.ORDER_ID", "UPD_X.ORDER_ID -> T.ORDER_ID"], [target("T", "ORDER_ID:decimal")])
    item = next(i for i in r.scorecard.items if i.name == "UPD_X")
    assert item.status == "review" and "MERGE" in item.notes[0]


def test_master_column_clash_in_a_joiner_is_renamed(con):
    sq_c = transform("SQ_CUSTOMERS", "Source Qualifier", [port("ID:decimal"), port("REGION")])
    jnr = transform("JNR", "Joiner", [port("ORDER_ID:decimal"), port("CUSTOMER_ID:decimal"),
                                      port("ID:decimal", "INPUT/OUTPUT/MASTER"), port("ORDER_ID1:decimal", "INPUT/OUTPUT/MASTER")],
                    {"Join Condition": "ID = CUSTOMER_ID", "Join Type": "Normal Join"})
    m = mapping("m_test", [SQ_ORDERS, sq_c, jnr],
                BASE_INSTANCES + [("CUSTOMERS", "SOURCE", "Source Definition"),
                                  ("SQ_CUSTOMERS", "TRANSFORMATION", "Source Qualifier"),
                                  ("JNR", "TRANSFORMATION", "Joiner"), ("T", "TARGET", "Target Definition")],
                ORDER_LINKS + ["CUSTOMERS.ID -> SQ_CUSTOMERS.ID", "CUSTOMERS.REGION -> SQ_CUSTOMERS.REGION",
                               "SQ_ORDERS.ORDER_ID -> JNR.ORDER_ID", "SQ_ORDERS.CUSTOMER_ID -> JNR.CUSTOMER_ID",
                               "SQ_CUSTOMERS.ID -> JNR.ID", "SQ_CUSTOMERS.ID -> JNR.ORDER_ID1",
                               "JNR.ORDER_ID -> T.A", "JNR.ORDER_ID1 -> T.B"])
    r = import_powercenter(export([m], [ORDERS, CUSTOMERS], [target("T", "A:decimal", "B:decimal")]))
    got = sorted(rows(con, r, r.outputs[0]))
    assert got[0] == (1, 101)  # order 1 belongs to customer 101


def test_scorecard_for_sql_imports():
    from redcliff.importer import import_sql

    r = import_sql("CREATE TABLE a AS SELECT * FROM orders WHERE amount > 10; "
                   "SELECT status FROM a QUALIFY ROW_NUMBER() OVER (PARTITION BY status ORDER BY amount) = 1",
                   dialect="duckdb")
    card = r.scorecard
    assert [i.status for i in card.items] == ["converted", "review"]
    assert card.items[0].kind == "CREATE TABLE AS"
