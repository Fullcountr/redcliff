"""Mapplets, sessions, workflows and parameter files around PowerCenter mappings.

The main check: wf_customer_revenue.xml rebuilds m_customer_revenue the way real estates are built
(the cleanup in a mapplet, the order date from a parameter file, a session that truncates and
analyzes, a workflow around it) and must produce exactly the same rows.
"""

from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from redcliff.cli import main
from redcliff.compile import compile_query, compile_script
from redcliff.powercenter import import_powercenter
from redcliff.powercenter_workflow import parse_param_file, resolve_params, substitute
from test_powercenter import (
    BASE_INSTANCES, ORDER_LINKS, ORDERS, SQ_ORDERS, export, mapping, port, target, transform,
)

ROOT = Path(__file__).parents[2]
PC = ROOT / "examples" / "legacy" / "powercenter"
DATA = ROOT / "examples" / "data"


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    types = {"orders": {"amount": "DECIMAL(10,2)"}, "regions": {"monthly_target": "DECIMAL(12,2)"}, "customers": {}}
    for t, cast in types.items():
        extra = f", types = {cast!r}" if cast else ""
        c.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{DATA / t}.csv', header = true{extra})")
    return c


def rows(con, result, node):
    return sorted(con.execute(compile_query(result.pipeline, node, "duckdb")).fetchall(), key=repr)


@pytest.fixture(scope="module")
def workflow():
    return import_powercenter((PC / "wf_customer_revenue.xml").read_text(),
                              params=(PC / "wf_customer_revenue.par").read_text())


@pytest.fixture(scope="module")
def original():
    return import_powercenter((PC / "m_customer_revenue.xml").read_text())


# ---------------------------------------------------------------- the workflow example, end to end


def test_workflow_example_gives_exactly_the_original_rows(con, workflow, original):
    assert len(workflow.outputs) == len(original.outputs) == 2
    for mine, theirs in zip(workflow.outputs, original.outputs):
        assert rows(con, workflow, mine) == rows(con, original, theirs)
    assert rows(con, workflow, workflow.outputs[0])[0][:4] == ("NORTH", "2026-09", "Maria Lopez", Decimal("159.99"))


def test_mapplet_is_inlined_under_its_instance_name(workflow):
    ids = [n.id for n in workflow.pipeline.nodes]
    assert "mplt_clean__exp_clean_customer" in ids
    assert not any("input" in i or "output" in i for i in ids)  # the mapplet's plumbing adds no steps
    names = {i.name for i in workflow.scorecard.items}
    assert "MPLT_CLEAN/EXP_CLEAN_CUSTOMER" in names
    assert not any(n.endswith(("/Input", "/INPUT", "/OUTPUT", "/Output")) for n in names)


def test_session_overrides_reach_the_targets(workflow):
    region = workflow.pipeline.node("region_monthly_revenue")
    assert region.params["mode"] == "overwrite"  # the session truncates it
    assert region.params["post_sql"] == ["ANALYZE TABLE region_monthly_revenue COMPUTE STATISTICS"]
    assert workflow.pipeline.node("order_exceptions").params["mode"] == "append"
    spark = compile_script(workflow.pipeline, "spark")
    assert spark.index("INSERT OVERWRITE TABLE region_monthly_revenue") < spark.index("ANALYZE TABLE region_monthly_revenue")


def test_workflow_tasks_are_graded_and_ordered(workflow):
    items = {i.name: i for i in workflow.scorecard.items}
    wf = items["wf_customer_revenue"]
    assert wf.status == "review"
    assert "cmd_check_landing → s_m_customer_revenue (runs m_customer_revenue) → email_done" in wf.notes[0]
    assert any("$cmd_check_landing.Status = SUCCEEDED" in n for n in wf.notes)
    assert items["wf_customer_revenue/cmd_check_landing"].status == "manual"
    assert "test -s /data/landing/orders.done" in items["wf_customer_revenue/cmd_check_landing"].notes[1]
    assert items["wf_customer_revenue/email_done"].status == "manual"
    sq = items["SQ_ORDERS"]
    assert sq.status == "review"
    assert any("ALTER SESSION SET NLS_DATE_FORMAT" in n and "doesn't run it" in n for n in sq.notes)
    assert "$$MIN_ORDER_DATE is the pipeline variable ${min_order_date}, default '2026-09-02' from the parameter file" \
        in sq.notes
    assert workflow.pipeline.variables == {"min_order_date": "2026-09-02"}
    assert workflow.pipeline.name == "wf_customer_revenue"


def test_without_the_parameter_file_the_default_date_applies(con):
    r = import_powercenter((PC / "wf_customer_revenue.xml").read_text())
    got = rows(con, r, r.outputs[0])
    # From 2026-01-01 every shipped order counts: SOUTH has 1, 4 and 8; NORTH has 2, 6 and 7.
    assert [(g[0], g[3], g[4]) for g in got] == [("NORTH", Decimal("239.99"), 3), ("SOUTH", Decimal("330.50"), 3)]
    sq = next(i for i in r.scorecard.items if i.name == "SQ_ORDERS")
    assert any("default '2026-01-01' from the mapping's default" in n for n in sq.notes)


def test_cli_takes_a_parameter_file(tmp_path, capsys):
    out = tmp_path / "p.yaml"
    assert main(["import", str(PC / "wf_customer_revenue.xml"), "--params", str(PC / "wf_customer_revenue.par"),
                 "-o", str(out)]) == 0
    text = out.read_text()
    assert "STR_TO_DATE('${min_order_date}'" in text and "min_order_date: '2026-09-02'" in text
    assert "scorecard:" in capsys.readouterr().err


# ---------------------------------------------------------------- parameter files

PARAMS = """
[Global]
$$A=global
$$B=global
$$C=global
[SALES.WF:wf_x]
$$B=workflow
$$C=workflow
[SALES.WF:wf_x.ST:s_x]
$$C=session
# a comment
[OTHER.WF:wf_x.ST:s_x]
$$C=other folder
"""


def test_parameter_precedence_follows_powercenter():
    sections = parse_param_file(PARAMS)
    assert resolve_params(sections, "sales", "wf_x", "s_x") == {"$$A": "global", "$$B": "workflow", "$$C": "session"}
    assert resolve_params(sections, "sales", "wf_y", "s_y") == {"$$A": "global", "$$B": "global", "$$C": "global"}


def test_substitute_reports_used_and_missing():
    text, used, missing = substitute("d >= '$$FROM' AND r = '$$REGION'", {"$$FROM": "2026-01-01"})
    assert text == "d >= '2026-01-01' AND r = '$$REGION'"
    assert used == ["$$FROM"] and missing == ["$$REGION"]


# ---------------------------------------------------------------- synthetic exports

SESSION_OVERRIDE = """
<SESSION NAME="s_test" MAPPINGNAME="m_test" REUSABLE="YES">
  <SESSTRANSFORMATIONINST SINSTANCENAME="SQ_ORDERS" TRANSFORMATIONNAME="SQ_ORDERS" TRANSFORMATIONTYPE="Source Qualifier">
    <ATTRIBUTE NAME="Source Filter" VALUE="ORDERS.STATUS = 'shipped'"/>
  </SESSTRANSFORMATIONINST>
  <SESSTRANSFORMATIONINST SINSTANCENAME="T" TRANSFORMATIONNAME="T" TRANSFORMATIONTYPE="Target Definition">
    <ATTRIBUTE NAME="Pre SQL" VALUE="DELETE FROM T WHERE ORDER_ID &lt; 0; TRUNCATE TABLE STG_T"/>
  </SESSTRANSFORMATIONINST>
</SESSION>
"""


def test_session_source_filter_override_and_two_pre_sql_statements(con):
    m = mapping("m_test", [SQ_ORDERS], BASE_INSTANCES + [("T", "TARGET", "Target Definition")],
                ORDER_LINKS + ["SQ_ORDERS.ORDER_ID -> T.ORDER_ID"])
    r = import_powercenter(export([m], [ORDERS], [target("T", "ORDER_ID:decimal")], folder_transforms=[SESSION_OVERRIDE]))
    assert len(rows(con, r, r.outputs[0])) == 6  # only shipped orders
    assert r.pipeline.node("t").params["pre_sql"] == ["DELETE FROM t WHERE order_id < 0", "TRUNCATE TABLE stg_t"]


def _mapplet(name: str, body: str) -> str:
    return f'<MAPPLET NAME="{name}">{body}</MAPPLET>'


def test_nested_mapplets_expand(con):
    io_in = transform("Input", "Input Transformation", [port("AMOUNT:decimal", "OUTPUT")])
    io_out = transform("Output", "Output Transformation", [port("DOUBLED:decimal", "INPUT")])
    inner = _mapplet("mplt_inner", io_in + transform("EXP_X2", "Expression", [
        port("AMOUNT:decimal", "INPUT"), port("o_DOUBLED:decimal", "OUTPUT", "AMOUNT * 2")]) + io_out
        + '<INSTANCE NAME="Input" TRANSFORMATION_NAME="Input" TRANSFORMATION_TYPE="Input Transformation" TYPE="TRANSFORMATION"/>'
        + '<INSTANCE NAME="EXP_X2" TRANSFORMATION_NAME="EXP_X2" TRANSFORMATION_TYPE="Expression" TYPE="TRANSFORMATION"/>'
        + '<INSTANCE NAME="Output" TRANSFORMATION_NAME="Output" TRANSFORMATION_TYPE="Output Transformation" TYPE="TRANSFORMATION"/>'
        + '<CONNECTOR FROMINSTANCE="Input" FROMFIELD="AMOUNT" TOINSTANCE="EXP_X2" TOFIELD="AMOUNT"/>'
        + '<CONNECTOR FROMINSTANCE="EXP_X2" FROMFIELD="o_DOUBLED" TOINSTANCE="Output" TOFIELD="DOUBLED"/>')
    outer = _mapplet("OUTER", io_in + io_out
        + '<INSTANCE NAME="Input" TRANSFORMATION_NAME="Input" TRANSFORMATION_TYPE="Input Transformation" TYPE="TRANSFORMATION"/>'
        + '<INSTANCE NAME="INNER" TRANSFORMATION_NAME="mplt_inner" TRANSFORMATION_TYPE="Mapplet" TYPE="MAPPLET"/>'
        + '<INSTANCE NAME="Output" TRANSFORMATION_NAME="Output" TRANSFORMATION_TYPE="Output Transformation" TYPE="TRANSFORMATION"/>'
        + '<CONNECTOR FROMINSTANCE="Input" FROMFIELD="AMOUNT" TOINSTANCE="INNER" TOFIELD="AMOUNT"/>'
        + '<CONNECTOR FROMINSTANCE="INNER" FROMFIELD="DOUBLED" TOINSTANCE="Output" TOFIELD="DOUBLED"/>')
    m = mapping("m_test", [SQ_ORDERS], BASE_INSTANCES + [("OUTER", "MAPPLET", "Mapplet"), ("T", "TARGET", "Target Definition")],
                ORDER_LINKS + ["SQ_ORDERS.AMOUNT -> OUTER.AMOUNT", "OUTER.DOUBLED -> T.DOUBLED"])
    r = import_powercenter(export([m], [ORDERS], [target("T", "DOUBLED:decimal")], folder_transforms=[inner, outer]))
    assert "outer__inner__exp_x2" in {n.id for n in r.pipeline.nodes}
    assert "OUTER/INNER/EXP_X2" in {i.name for i in r.scorecard.items}
    assert r.scorecard.counts()["manual"] == 0
    assert max(v for (v,) in rows(con, r, r.outputs[0])) == Decimal("400.00")


def test_a_missing_mapplet_is_manual():
    m = mapping("m_test", [SQ_ORDERS], BASE_INSTANCES + [("MPLT_GONE", "MAPPLET", "Mapplet"), ("T", "TARGET", "Target Definition")],
                ORDER_LINKS + ["SQ_ORDERS.AMOUNT -> MPLT_GONE.AMOUNT", "MPLT_GONE.OUT -> T.OUT"])
    r = import_powercenter(export([m], [ORDERS], [target("T", "OUT:decimal")]))
    item = next(i for i in r.scorecard.items if i.name == "MPLT_GONE")
    assert item.status == "manual" and "isn't in the export" in item.notes[0]


def test_a_parameter_with_no_value_in_sql_is_manual():
    sq = transform("SQ_ORDERS", "Source Qualifier", [port("ORDER_ID:decimal")], {"Source Filter": "ORDERS.STATUS = '$$NOPE'"})
    m = mapping("m_test", [sq], BASE_INSTANCES + [("T", "TARGET", "Target Definition")],
                ["ORDERS.ORDER_ID -> SQ_ORDERS.ORDER_ID", "SQ_ORDERS.ORDER_ID -> T.ORDER_ID"])
    r = import_powercenter(export([m], [ORDERS], [target("T", "ORDER_ID:decimal")]))
    item = next(i for i in r.scorecard.items if i.name == "SQ_ORDERS")
    assert item.status == "manual" and "$$NOPE" in item.notes[0]


def test_workflow_tasks_are_marked_as_orchestration(workflow):
    areas = {i.name: i.area for i in workflow.scorecard.items}
    assert areas["wf_customer_revenue"] == areas["wf_customer_revenue/email_done"] == "orchestration"
    assert areas["SQ_ORDERS"] == "pipeline"
    assert "3 of the 17 are workflow tasks" in workflow.scorecard.to_markdown()
