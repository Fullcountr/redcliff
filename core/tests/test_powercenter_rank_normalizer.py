"""Rank, Normalizer and worklets: the store mart example, worked out by hand, plus edge cases."""

from decimal import Decimal as D
from pathlib import Path

import duckdb
import pytest

from redcliff.compile import compile_query
from redcliff.powercenter import import_powercenter
from redcliff.powercenter_workflow import parse_param_file, resolve_params
from test_powercenter import (
    BASE_INSTANCES, ORDER_LINKS, ORDERS, SQ_ORDERS, export, mapping, port, target, transform,
)

ROOT = Path(__file__).parents[2]
PC = ROOT / "examples" / "legacy" / "powercenter"
DATA = ROOT / "examples" / "data"
STORE = PC / "wf_store_mart.xml"


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    money = {f"q{i}_sales": "DECIMAL(12,2)" for i in range(1, 5)}
    c.execute(f"CREATE TABLE store_sales AS SELECT * FROM read_csv('{DATA / 'store_sales.csv'}', header = true, "
              f"types = {money!r})")
    c.execute(f"CREATE TABLE orders AS SELECT * FROM read_csv_auto('{DATA / 'orders.csv'}', header = true, "
              "types = {'amount': 'DECIMAL(10,2)'})")
    return c


@pytest.fixture(scope="module")
def store():
    return import_powercenter(STORE.read_text())


def rows(con, result, node):
    return sorted(con.execute(compile_query(result.pipeline, result.pipeline.node(node).inputs[0], "duckdb")).fetchall())


# ---------------------------------------------------------------- the store mart example


def test_best_two_quarters_per_store(con, store):
    # The Normalizer makes a row per store and quarter; the Rank keeps the top 2 sales per store.
    # Store 2 ties at 90.00 in Q1 and Q2: both get rank 2, as PowerCenter's RANKINDEX does.
    # Store 3 has no Q1 figure: NULL ranks last, so it isn't among the best.
    assert rows(con, store, "best_quarters") == [
        (1, "North", 2, D("250.00"), 2), (1, "North", 4, D("300.00"), 1),
        (2, "North", 1, D("90.00"), 2), (2, "North", 2, D("90.00"), 2), (2, "North", 4, D("120.00"), 1),
        (3, "South", 2, D("50.00"), 2), (3, "South", 3, D("80.00"), 1),
        (4, "South", 1, D("200.00"), 1), (4, "South", 3, D("30.00"), 2),
    ]


def test_best_store_per_region(con, store):
    # Year totals: store 1 825.00, store 2 360.00 (North); store 3 170.00, store 4 260.00 (South).
    assert rows(con, store, "top_stores") == [("North", 1, D("825.00"), 1), ("South", 4, D("260.00"), 1)]


def test_store_mart_scorecard(store):
    card = store.scorecard
    by = {i.name: i for i in card.items}
    assert (card.counts()["converted"], len(card.items)) == (8, 11)
    assert by["NRM_QUARTERS"].status == "converted"
    assert "each input row becomes 4 rows, one per occurrence of sales, numbered by gcid_sales" in by["NRM_QUARTERS"].notes
    assert by["RNK_BEST_QUARTERS"].status == "converted"
    assert "tied rows share a rank index" in by["RNK_BEST_QUARTERS"].notes[0]
    flow = by["wf_store_mart"].notes[0]
    assert flow.endswith("wklt_load/s_m_store_sales (runs m_store_sales) → wklt_load/cmd_archive_extract → email_done")
    assert by["wf_store_mart/wklt_load/cmd_archive_extract"].status == "manual"  # a shell command
    assert store.pipeline.node("best_quarters").params["mode"] == "overwrite"  # the worklet's session applies


def test_rank_compiles_to_a_window_and_a_filter(store):
    index = store.pipeline.node("rnk_best_quarters_index").params["columns"]["rankindex"]
    assert index == "RANK() OVER (PARTITION BY store_id ORDER BY sales DESC NULLS LAST)"
    assert store.pipeline.node("rnk_best_quarters").params == {"condition": "rankindex <= 2"}


# ---------------------------------------------------------------- edge cases


def simple(transforms, instances, connectors, targets):
    m = mapping("m_test", [SQ_ORDERS, *transforms], BASE_INSTANCES + instances, ORDER_LINKS + connectors)
    return import_powercenter(export([m], [ORDERS], targets))


def rank(ports, **attrs):
    return transform("RNK", "Rank", ports, {"Top/Bottom": "Top", "Number Of Ranks": "1", **attrs})


def test_bottom_rank_without_groups(con):
    r = simple([rank([port("ORDER_ID:decimal"), port("AMOUNT:decimal", "INPUT/OUTPUT/MASTER"),
                      port("RANKINDEX:integer", "OUTPUT")], **{"Top/Bottom": "Bottom", "Number Of Ranks": "2"})],
               [("RNK", "TRANSFORMATION", "Rank"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.ORDER_ID -> RNK.ORDER_ID", "SQ_ORDERS.AMOUNT -> RNK.AMOUNT",
                "RNK.ORDER_ID -> T.ORDER_ID", "RNK.RANKINDEX -> T.R"], [target("T", "ORDER_ID:decimal", "R:decimal")])
    got = sorted(con.execute(compile_query(r.pipeline, r.outputs[0], "duckdb")).fetchall())
    assert got == [(5, 2), (8, 1)]  # the two smallest amounts: 10.00 ranks 1, 15.25 ranks 2
    assert next(i for i in r.scorecard.items if i.name == "RNK").notes[0].startswith("keeps the bottom 2 by amount;")


def test_ranking_strings_is_reviewed_and_a_missing_rank_port_is_manual():
    r = simple([rank([port("ORDER_ID:decimal"), port("STATUS", "INPUT/OUTPUT/MASTER")])],
               [("RNK", "TRANSFORMATION", "Rank"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.ORDER_ID -> RNK.ORDER_ID", "SQ_ORDERS.STATUS -> RNK.STATUS", "RNK.ORDER_ID -> T.ORDER_ID"],
               [target("T", "ORDER_ID:decimal")])
    assert next(i for i in r.scorecard.items if i.name == "RNK").status == "review"
    r = simple([rank([port("ORDER_ID:decimal"), port("STATUS")])],
               [("RNK", "TRANSFORMATION", "Rank"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.ORDER_ID -> RNK.ORDER_ID", "RNK.ORDER_ID -> T.ORDER_ID"], [target("T", "ORDER_ID:decimal")])
    item = next(i for i in r.scorecard.items if i.name == "RNK")
    assert item.status == "manual" and "rank port isn't connected" in item.notes[0]


def test_a_used_generated_key_is_manual():
    nrm = transform("NRM", "Normalizer", [port("ORDER_ID:decimal"), port("V1:decimal", "INPUT"),
                                          port("V2:decimal", "INPUT"), port("V:decimal", "OUTPUT"),
                                          port("GK_V:decimal", "OUTPUT"), port("GCID_V:decimal", "OUTPUT")])
    r = simple([nrm], [("NRM", "TRANSFORMATION", "Normalizer"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.ORDER_ID -> NRM.ORDER_ID", "SQ_ORDERS.AMOUNT -> NRM.V1", "SQ_ORDERS.CUSTOMER_ID -> NRM.V2",
                "NRM.V -> T.V", "NRM.GK_V -> T.K"], [target("T", "V:decimal", "K:decimal")])
    item = next(i for i in r.scorecard.items if i.name == "NRM")
    assert item.status == "manual" and "GK_V is a generated key" in item.notes[0]


def test_normalizer_with_unnumbered_inputs(con):
    nrm = transform("NRM", "Normalizer", [port("ORDER_ID:decimal"), port("V1:decimal", "INPUT"),
                                          port("V2:decimal", "INPUT"), port("V:decimal", "OUTPUT"),
                                          port("GCID_V:decimal", "OUTPUT")])
    r = simple([nrm], [("NRM", "TRANSFORMATION", "Normalizer"), ("T", "TARGET", "Target Definition")],
               ["SQ_ORDERS.ORDER_ID -> NRM.ORDER_ID", "SQ_ORDERS.AMOUNT -> NRM.V1", "SQ_ORDERS.CUSTOMER_ID -> NRM.V2",
                "NRM.ORDER_ID -> T.ORDER_ID", "NRM.V -> T.V", "NRM.GCID_V -> T.N"],
               [target("T", "ORDER_ID:decimal", "V:decimal", "N:decimal")])
    got = sorted(r for r in con.execute(compile_query(r.pipeline, r.outputs[0], "duckdb")).fetchall() if r[0] == 1)
    assert got == [(1, D("101.00"), 2), (1, D("120.50"), 1)]


def test_worklet_parameters_and_missing_worklets():
    sections = parse_param_file("[Global]\n$$N=1\n[F.WF:wf.WT:wkl.ST:s]\n$$N=3\n[F.WF:wf.ST:s]\n$$N=2\n")
    assert resolve_params(sections, "F", "wf", "s", "wkl")["$$N"] == "3"
    assert resolve_params(sections, "F", "wf", "s")["$$N"] == "2"  # not in that worklet
    other = ('<TASKINSTANCE DESCRIPTION="" ISENABLED="YES" NAME="wklt_other" REUSABLE="YES" '
             'TASKNAME="wklt_shared_elsewhere" TASKTYPE="Worklet" />')
    xml = STORE.read_text().replace('<WORKFLOWLINK CONDITION="" FROMTASK="Start" TOTASK="wklt_load" />',
                                    other + '<WORKFLOWLINK CONDITION="" FROMTASK="Start" TOTASK="wklt_load" />')
    r = import_powercenter(xml)
    item = next(i for i in r.scorecard.items if i.name == "wf_store_mart/wklt_other")
    assert item.status == "manual" and "worklet wklt_shared_elsewhere isn't in the export" in item.notes
