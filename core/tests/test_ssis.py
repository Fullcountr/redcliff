"""SSIS packages: the invented example against rows worked out by hand, and edge cases."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from xml.sax.saxutils import quoteattr as q

import duckdb
import pytest

from redcliff.cli import main
from redcliff.compile import compile_query, compile_script
from redcliff.lineage import build_graph
from redcliff.ssis import SSISImportError, import_ssis

ROOT = Path(__file__).parents[2]
DATA = ROOT / "examples" / "data"
EXAMPLE = ROOT / "examples" / "legacy" / "ssis" / "LoadRegionRevenue.dtsx"


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE orders AS SELECT * FROM read_csv_auto('{DATA / 'orders.csv'}', "
              "types = {'amount': 'DECIMAL(10,2)'})")
    for t in ("customers", "regions"):
        c.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{DATA / t}.csv')")
    return c


@pytest.fixture(scope="module")
def example():
    return import_ssis(EXAMPLE.read_text())


def rows(con, result, node, pipeline=None):
    return sorted(con.execute(compile_query(pipeline or result.pipeline, node, "duckdb")).fetchall())


# ---------------------------------------------------------------- the example, end to end


def test_region_revenue_matches_hand_worked_rows(con, example):
    # Shipped orders (conditional split), joined to cleaned customers (merge join on the sort
    # keys), the manager looked up by the upper-cased region, then totals per region.
    assert rows(con, example, "agg_region") == [
        ("NORTH", "Maria Lopez", D("239.99"), 3, 0),   # orders 2, 6, 7
        ("SOUTH", "Tom Becker", D("330.50"), 3, 2),    # orders 1, 4, 8; 120.50 and 200.00 are large
    ]
    assert rows(con, example, "dst_cancelled_orders_rows") == [(3, 101, D("45.00"))]


def test_the_package_variable_is_a_pipeline_variable(con, example):
    assert example.pipeline.variables == {"fx_rate": "1.0"}
    doubled = example.pipeline.to_dict()
    doubled["variables"] = {"fx_rate": "2.0"}
    from redcliff.spec import Pipeline

    assert rows(con, example, "agg_region", Pipeline.from_dict(doubled))[0][2] == D("479.98")


def test_scorecard(example):
    card = example.scorecard
    items = {i.name: i for i in card.items}
    assert card.origin == "ssis" and len(card.items) == 17
    assert card.counts() == {"converted": 14, "review": 2, "manual": 1}
    assert items["LoadRegionRevenue/SQL Truncate Region Revenue"].status == "converted"
    assert example.pipeline.node("dst_region_revenue").params["mode"] == "overwrite"
    assert example.pipeline.node("dst_cancelled_orders").params["mode"] == "append"
    assert "EXEC dbo.usp_refresh_region_stats" in items["LoadRegionRevenue/SQL Refresh Stats"].notes[0]
    assert items["LoadRegionRevenue/Send Done Mail"].status == "manual"
    assert items["LoadRegionRevenue"].notes[0].endswith(
        "SQL Truncate Region Revenue → DFT Load Region Revenue → SQL Refresh Stats → Send Done Mail")
    assert sum(i.area == "orchestration" for i in card.items) == 4


def test_lineage_and_a_full_script(con, example):
    graph = build_graph(example.pipeline)
    assert graph.issues == []
    assert example.scorecard.lineage["traced"] == 6
    con.execute("CREATE OR REPLACE TABLE cancelled_orders (order_id INT, customer_id INT, amount DECIMAL(10,2))")
    con.execute(compile_script(example.pipeline, "duckdb"))
    assert con.sql("SELECT COUNT(*) FROM region_revenue").fetchone()[0] == 2


def test_cli(tmp_path, capsys):
    spec, card = tmp_path / "p.yaml", tmp_path / "card.md"
    assert main(["import", str(EXAMPLE), "-o", str(spec), "--scorecard", str(card)]) == 0
    assert "scorecard: 14 of 17 converted (82%), 2 to review, 1 to rewrite by hand" in capsys.readouterr().err
    assert card.read_text().startswith("# Migration scorecard: LoadRegionRevenue")


# ---------------------------------------------------------------- small packages for edge cases

DFT = "Package\\DFT"
INT = {"dataType": "i4"}
STR = {"dataType": "wstr", "length": "20"}
NUM = {"dataType": "numeric", "precision": "10", "scale": "2"}


def R(rest: str) -> str:
    """A refId inside the data flow (no backslashes inside f-strings, for Python 3.11)."""
    return DFT + "\\" + rest


def attrs(d):
    return " ".join(f"{k}={q(str(v))}" for k, v in d.items())


def props(**p):
    return "<properties>" + "".join(f"<property name={q(k)}>{v}</property>" for k, v in p.items()) + "</properties>"


def col(comp, out, name, typ, **p):
    ref = R(f"{comp}.Outputs[{out}].Columns[{name}]")
    return f"<outputColumn {attrs({'refId': ref, 'lineageId': ref, 'name': name, **typ})}>{props(**p) if p else ''}</outputColumn>"


def incol(comp, inp, lineage, name, **p):
    ref = R(f"{comp}.Inputs[{inp}].Columns[{name}]")
    return (f"<inputColumn {attrs({'refId': ref, 'cachedName': name, 'lineageId': lineage})}>"
            f"{props(**p) if p else ''}</inputColumn>")


def L(comp, name, out="Out"):
    return R(f"{comp}.Outputs[{out}].Columns[{name}]")


def component(name, cls, p=None, inputs="", outputs=""):
    head = attrs({"refId": R(name), "componentClassID": "Microsoft." + cls, "name": name})
    return (f"<component {head}>"
            f"{props(**(p or {})) if p else ''}<inputs>{inputs}</inputs><outputs>{outputs}</outputs></component>")


def out(comp, name, cols="", **p):
    head = attrs({"refId": R(f"{comp}.Outputs[{name}]"), "name": name})
    return f"<output {head}>{props(**p) if p else ''}<outputColumns>{cols}</outputColumns></output>"


def inp(comp, name, cols="", **a):
    head = attrs({"refId": R(f"{comp}.Inputs[{name}]"), "name": name, **a})
    return f"<input {head}><inputColumns>{cols}</inputColumns></input>"


def path(start, end):
    return f"<path {attrs({'refId': DFT + f'.Paths[{start}]', 'startId': start, 'endId': end})} />"


def package(components, paths, variables="", extra_tasks=""):
    return ("<DTS:Executable xmlns:DTS='www.microsoft.com/SqlServer/Dts' DTS:refId='Package' "
            "DTS:ExecutableType='Microsoft.Package' DTS:ObjectName='Test'>"
            f"<DTS:Variables>{variables}</DTS:Variables><DTS:Executables>{extra_tasks}"
            "<DTS:Executable DTS:refId='Package\\DFT' DTS:ExecutableType='Microsoft.Pipeline' DTS:ObjectName='DFT'>"
            f"<DTS:ObjectData><pipeline><components>{''.join(components)}</components><paths>{''.join(paths)}</paths>"
            "</pipeline></DTS:ObjectData></DTS:Executable></DTS:Executables></DTS:Executable>")


ORDERS = component("SRC", "OLEDBSource", {"AccessMode": "0", "OpenRowset": "[dbo].[orders]"}, "",
                   out("SRC", "Out", col("SRC", "Out", "order_id", INT) + col("SRC", "Out", "customer_id", INT)
                       + col("SRC", "Out", "status", STR) + col("SRC", "Out", "amount", NUM)))


def dest(name, table, cols):
    def ext_ref(n):
        return R(f"{name}.Inputs[In].ExternalColumns[{n}]")

    ext = "".join(f"<externalMetadataColumn refId={q(ext_ref(n))} name={q(n)} />" for n, _l in cols)
    ins = "".join(f"<inputColumn refId={q(R(f'{name}.Inputs[In].Columns[{n}]'))} lineageId={q(lin)} "
                  f"externalMetadataColumnId={q(ext_ref(n))} />" for n, lin in cols)
    return (f"<component refId={q(R(name))} componentClassID='Microsoft.OLEDBDestination' name={q(name)}>"
            f"{props(AccessMode='3', OpenRowset=table)}<inputs><input refId={q(R(name + '.Inputs[In]'))} name='In'>"
            f"<inputColumns>{ins}</inputColumns><externalMetadataColumns>{ext}</externalMetadataColumns></input></inputs>"
            "<outputs /></component>")



def test_conditional_split_sends_a_row_to_the_first_true_output(con):
    split = component("CS", "ConditionalSplit", None, inp("CS", "In"),
                      out("CS", "Big", EvaluationOrder="0", FriendlyExpression="amount >= 100")
                      + out("CS", "Shipped", EvaluationOrder="1", FriendlyExpression='status == "shipped"')
                      + out("CS", "Rest", IsDefaultOut="true"))
    r = import_ssis(package(
        [ORDERS, split, dest("D1", "big", [("order_id", L("SRC", "order_id"))]),
         dest("D2", "shipped", [("order_id", L("SRC", "order_id"))]), dest("D3", "rest", [("order_id", L("SRC", "order_id"))])],
        [path(f"{DFT}\\SRC.Outputs[Out]", f"{DFT}\\CS.Inputs[In]"), path(f"{DFT}\\CS.Outputs[Big]", f"{DFT}\\D1.Inputs[In]"),
         path(f"{DFT}\\CS.Outputs[Shipped]", f"{DFT}\\D2.Inputs[In]"), path(f"{DFT}\\CS.Outputs[Rest]", f"{DFT}\\D3.Inputs[In]")]))
    got = {t: [x[0] for x in sorted(con.sql(compile_query(r.pipeline, r.pipeline.node(t).inputs[0], "duckdb")).fetchall())]
           for t in ("d1", "d2", "d3")}
    assert got == {"d1": [1, 4], "d2": [2, 6, 7, 8], "d3": [3, 5]}  # 1 and 4 are shipped too, but Big comes first


def test_union_aggregate_and_data_conversion(con):
    conv = component("DC", "DataConvert", None, inp("DC", "In"),
                     out("DC", "Out", col("DC", "Out", "amount_text", STR, SourceInputColumnLineageID=f"#{{{L('SRC', 'amount')}}}")))
    union = component("UA", "UnionAll", None,
                      inp("UA", "In 1", incol("UA", "In 1", L("SRC", "status"), "status", OutputColumnLineageID=f"#{{{L('UA', 's')}}}"))
                      + inp("UA", "In 2", incol("UA", "In 2", L("SRC", "amount"), "amount", OutputColumnLineageID=f"#{{{L('UA', 'n')}}}")),
                      out("UA", "Out", col("UA", "Out", "s", STR) + col("UA", "Out", "n", NUM)))
    agg = component("AG", "Aggregate", None, inp("AG", "In"), out("AG", "Out",
                    col("AG", "Out", "status", STR, AggregationType="0", AggregationColumnId=f"#{{{L('SRC', 'status')}}}")
                    + col("AG", "Out", "orders", INT, AggregationType="2")
                    + col("AG", "Out", "customers", INT, AggregationType="3", AggregationColumnId=f"#{{{L('SRC', 'customer_id')}}}")
                    + col("AG", "Out", "biggest", NUM, AggregationType="7", AggregationColumnId=f"#{{{L('SRC', 'amount')}}}")))
    mc = component("MC", "Multicast", None, inp("MC", "In"), out("MC", "A") + out("MC", "B") + out("MC", "C"))
    r = import_ssis(package(
        [ORDERS, mc, conv, union, agg, dest("D1", "converted", [("t", L("DC", "amount_text"))]),
         dest("D2", "unioned", [("s", L("UA", "s")), ("n", L("UA", "n"))]),
         dest("D3", "totals", [("status", L("AG", "status")), ("orders", L("AG", "orders")),
                               ("customers", L("AG", "customers")), ("biggest", L("AG", "biggest"))])],
        [path(f"{DFT}\\SRC.Outputs[Out]", f"{DFT}\\MC.Inputs[In]"), path(f"{DFT}\\MC.Outputs[A]", f"{DFT}\\DC.Inputs[In]"),
         path(f"{DFT}\\MC.Outputs[B]", f"{DFT}\\UA.Inputs[In 1]"), path(f"{DFT}\\SRC.Outputs[Out]", f"{DFT}\\UA.Inputs[In 2]"),
         path(f"{DFT}\\MC.Outputs[C]", f"{DFT}\\AG.Inputs[In]"), path(f"{DFT}\\DC.Outputs[Out]", f"{DFT}\\D1.Inputs[In]"),
         path(f"{DFT}\\UA.Outputs[Out]", f"{DFT}\\D2.Inputs[In]"), path(f"{DFT}\\AG.Outputs[Out]", f"{DFT}\\D3.Inputs[In]")]))

    def q_(t):
        return sorted(con.sql(compile_query(r.pipeline, r.pipeline.node(t).inputs[0], "duckdb")).fetchall(), key=repr)

    assert ("120.50",) in q_("d1")
    unioned = q_("d2")
    assert len(unioned) == 16 and ("shipped", None) in unioned and (None, D("200.00")) in unioned
    assert ("shipped", 6, 4, D("200.00")) in q_("d3")  # customers 101, 102, 103 and 105


def test_lookup_no_match_output_and_unsupported_components(con):
    lkp = component("LK", "Lookup", {"SqlCommand": "SELECT id, region FROM dbo.customers WHERE region = 'North'",
                                     "NoMatchBehavior": "1"},
                    inp("LK", "In", incol("LK", "In", L("SRC", "customer_id"), "customer_id", JoinToReferenceColumn="id")),
                    out("LK", "Lookup Match Output", col("LK", "Lookup Match Output", "region", STR, CopyFromReferenceColumn="region"))
                    + out("LK", "Lookup No Match Output"))
    script = component("SC", "ScriptComponentHost", None, inp("SC", "In"), out("SC", "Out", col("SC", "Out", "score", INT)))
    r = import_ssis(package(
        [ORDERS, lkp, script, dest("D1", "matched", [("order_id", L("SRC", "order_id")), ("region", L("LK", "region", "Lookup Match Output"))]),
         dest("D2", "unmatched", [("order_id", L("SRC", "order_id"))]),
         dest("D3", "scored", [("order_id", L("SRC", "order_id")), ("score", L("SC", "score"))])],
        [path(f"{DFT}\\SRC.Outputs[Out]", f"{DFT}\\LK.Inputs[In]"),
         path(f"{DFT}\\LK.Outputs[Lookup Match Output]", f"{DFT}\\D1.Inputs[In]"),
         path(f"{DFT}\\LK.Outputs[Lookup No Match Output]", f"{DFT}\\SC.Inputs[In]"),
         path(f"{DFT}\\SC.Outputs[Out]", f"{DFT}\\D3.Inputs[In]")]))
    matched = sorted(con.sql(compile_query(r.pipeline, r.pipeline.node("d1").inputs[0], "duckdb")).fetchall())
    assert matched == [(2, "North"), (6, "North"), (7, "North")]
    scored = sorted(con.sql(compile_query(r.pipeline, r.pipeline.node("d3").inputs[0], "duckdb")).fetchall())
    assert scored == [(1, None), (3, None), (4, None), (5, None), (8, None)]
    items = {i.name: i for i in r.scorecard.items}
    assert items["SC"].status == "manual" and "Script Component components have no Redcliff equivalent" in items["SC"].notes[0]
    assert items["D2"].status == "manual"  # nothing feeds it


def test_parameters_and_errors():
    src = component("SRC", "OLEDBSource", {"AccessMode": "2", "SqlCommand": "SELECT order_id FROM orders WHERE status = ?"},
                    "", out("SRC", "Out", col("SRC", "Out", "order_id", INT)))
    r = import_ssis(package([src, dest("D", "t", [("order_id", L("SRC", "order_id"))])],
                            [path(f"{DFT}\\SRC.Outputs[Out]", f"{DFT}\\D.Inputs[In]")]))
    assert "? parameters" in next(i for i in r.scorecard.items if i.name == "SRC").notes[0]
    with pytest.raises(SSISImportError, match="isn't an SSIS package"):
        import_ssis("<POWERMART/>")
    with pytest.raises(SSISImportError, match="no data flow"):
        import_ssis("<DTS:Executable xmlns:DTS='www.microsoft.com/SqlServer/Dts'/>")
    with pytest.raises(SSISImportError, match="2005/2008 format"):
        import_ssis("<DTS:Executable xmlns:DTS='www.microsoft.com/SqlServer/Dts'>"
                    "<DTS:Property DTS:Name='PackageFormatVersion'>3</DTS:Property></DTS:Executable>")
