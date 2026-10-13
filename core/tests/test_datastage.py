"""DataStage exports (.dsx and XML) to pipelines: the sample job end to end, and each stage on small jobs."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from redcliff.cli import main
from redcliff.datastage import DataStageImportError, import_datastage, is_datastage, parse_dsx
from redcliff.scorecard import CONVERTED, MANUAL, REVIEW
from redcliff.spec import dump_spec

ROOT = Path(__file__).parents[2]
SAMPLE = ROOT / "examples" / "legacy" / "datastage" / "px_order_revenue.dsx"
DATA = ROOT / "examples" / "data"
duckdb = pytest.importorskip("duckdb")


# --------------------------------------------------------------------------- building small exports

def _q(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def record(identifier: str, ole: str, props: dict, collections: dict | None = None) -> str:
    out = ["   BEGIN DSRECORD", f'      Identifier "{identifier}"', f'      OLEType "{ole}"']
    for k, v in props.items():
        if "\n" in str(v) or str(v).startswith("<"):
            out += [f"      {k} =+=+=+=", str(v), "=+=+=+="]
        else:
            out.append(f"      {k} {_q(v)}")
    for name, (cls, subs) in (collections or {}).items():
        out.append(f'      {name} "{cls}"')
        for s in subs:
            out.append("      BEGIN DSSUBRECORD")
            for k, v in s.items():
                if str(v).startswith("<"):
                    out += [f"         {k} =+=+=+=", str(v), "=+=+=+="]
                else:
                    out.append(f"         {k} {_q(v)}")
            out.append("      END DSSUBRECORD")
    out.append("   END DSRECORD")
    return "\n".join(out)


def col(name: str, sql_type: str = "12", precision: str = "20", scale: str = "0", key: bool = False,
        derivation: str = "") -> dict:
    c = {"Name": name, "SqlType": sql_type, "Precision": precision, "Scale": scale, "Nullable": "1",
         "KeyPosition": "1" if key else "0"}
    if derivation:
        c["Derivation"] = derivation
    return c


class Job:
    """A parallel job built stage by stage: stages get V0S<n>, pins V0S<n>P<m>."""

    def __init__(self, name: str = "px_test", params: list[dict] | None = None, job_type: str = "3", root: dict | None = None):
        self.name, self.params, self.job_type, self.root = name, params or [], job_type, root or {}
        self.stages: dict[str, dict] = {}
        self.pins: list[str] = []

    def stage(self, name: str, stage_type: str, props: list[tuple[str, str]] | None = None,
              ole: str = "CCustomStage", stage_vars: list[dict] | None = None) -> str:
        sid = f"V0S{len(self.stages) + 1}"
        self.stages[name] = {"id": sid, "type": stage_type, "props": props or [], "ole": ole, "in": [], "out": [],
                             "vars": stage_vars, "n": 0}
        return name

    def _pin(self, stage: str) -> str:
        s = self.stages[stage]
        s["n"] += 1
        return f"{s['id']}P{s['n']}"

    def link(self, src: str, dst: str, name: str, cols: list[dict] | None = None, in_cols: list[dict] | None = None,
             link_type: str = "1", out_props: dict | None = None, out_link_type: str | None = None) -> None:
        sp, dp = self._pin(src), self._pin(dst)
        s, d = self.stages[src], self.stages[dst]
        s["out"].append(sp)
        d["in"].append(dp)
        out_ole = "CTrxOutput" if s["ole"] == "CTransformerStage" else "CCustomOutput"
        in_ole = "CTrxInput" if d["ole"] == "CTransformerStage" else "CCustomInput"
        props = {"Name": name, "Partner": f"{d['id']}|{dp}", **(out_props or {})}
        if out_link_type:
            props["LinkType"] = out_link_type
        self.pins.append(record(sp, out_ole, props, {"Columns": ("COutputColumn", cols)} if cols else None))
        self.pins.append(record(dp, in_ole, {"Name": name, "Partner": f"{s['id']}|{sp}", "LinkType": link_type},
                                {"Columns": ("CInputColumn", in_cols)} if in_cols else None))

    def dsx(self) -> str:
        root = {"Name": self.name, **self.root}
        cols = {"Parameters": ("CParameters", self.params)} if self.params else {}
        parts = ["BEGIN HEADER", '   ExportingTool "IBM InfoSphere DataStage Export"', "END HEADER", "BEGIN DSJOB",
                 f'   Identifier "{self.name}"', record("ROOT", "CJobDefn", {**root, "JobType": self.job_type}, cols)]
        for name, s in self.stages.items():
            props = {"Name": name}
            if s["in"]:
                props["InputPins"] = "|".join(s["in"])
            if s["out"]:
                props["OutputPins"] = "|".join(s["out"])
            if s["ole"] != "CTransformerStage":
                props["StageType"] = s["type"]
            colls = {}
            if s["props"]:
                colls["Properties"] = ("CCustomProperty", [{"Name": n, "Value": v} for n, v in s["props"]])
            if s["vars"]:
                colls["StageVars"] = ("CStageVar", s["vars"])
            parts.append(record(s["id"], s["ole"], props, colls))
        parts += self.pins
        parts.append("END DSJOB")
        return "\n".join(parts) + "\n"


def connector(select: str = "", table: str = "", write_mode: str = "", table_action: str = "") -> list[tuple[str, str]]:
    usage = ""
    if select:
        usage += f"<GenerateSQL type='bool'><![CDATA[0]]></GenerateSQL><SQL><SelectStatement><![CDATA[{select}]]></SelectStatement></SQL>"
    if table:
        usage += f"<TableName><![CDATA[{table}]]></TableName>"
    if write_mode:
        usage += f"<WriteMode type='int'><![CDATA[{write_mode}]]></WriteMode>"
    if table_action:
        usage += f"<TableAction type='int'><![CDATA[{table_action}]]></TableAction>"
    return [("XMLProperties", f"<?xml version='1.0' encoding='UTF-16'?><Properties version='1.1'><Usage>{usage}</Usage></Properties>")]


ORDER_COLS = [col("ORDER_ID", "4", "10", key=True), col("CUSTOMER_ID", "4", "10"), col("STATUS"),
              col("AMOUNT", "3", "10", "2")]


def orders(job: Job, name: str = "ora_orders") -> str:
    return job.stage(name, "OracleConnectorPX", connector(select="SELECT ORDER_ID, CUSTOMER_ID, STATUS, AMOUNT FROM ORDERS"))


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    db = tmp_path_factory.mktemp("w") / "w.duckdb"
    con = duckdb.connect(str(db))
    for t in ("orders", "customers", "regions"):
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{DATA / (t + '.csv')}', header = true)")
    con.close()
    return db


def run(text: str, tmp_path: Path, warehouse: Path, *var: str):
    result = import_datastage(text, name="job")
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
    result = import_datastage(SAMPLE.read_text())
    p = result.pipeline
    assert p.name == "px_order_revenue"
    assert p.variables == {"min_amount": "0", "fx_rate": "1.0", "out_dir": "out"}
    params = {n.id: n.params for n in p.nodes}
    assert params["flt_live"] == {"condition": "status <> 'cancelled' AND amount > ${min_amount}"}
    assert params["lkp_customer_reject"] == {"condition": "id IS NULL"}
    assert params["region_revenue"]["mode"] == "overwrite"
    assert params["customer_activity"] == {"format": "table", "table": "customer_activity", "mode": "merge",
                                           "keys": ["customer_id"]}
    assert params["unknown_customers"]["path"] == "${out_dir}/unknown_customers.csv"
    assert params["xfm_prepare_vars"] == {"columns": {"sv_amount_usd": "amount * ${fx_rate}"}}
    card = result.scorecard
    assert card.origin == "datastage"
    assert item(result, "xfm_prepare").status == REVIEW  # Trim collapses inner spaces
    assert item(result, "lkp_customer").status == CONVERTED and item(result, "lkp_customer").nodes
    assert item(result, "before-job subroutine").status == MANUAL
    assert "check_landing.sh" in item(result, "before-job subroutine").notes[0]
    assert card.lineage["traced"] == 8


def test_sample_job_runs_and_matches_hand_worked_results(tmp_path, warehouse):
    """Golden: order 3 is cancelled; customer 104 has no email, so the connector's query leaves it out
    and order 5 goes to the lookup's reject file; regions join on the upper-cased name."""
    con = run(SAMPLE.read_text(), tmp_path, warehouse)
    assert con.sql("SELECT * FROM region_revenue ORDER BY region").fetchall() == [
        ("North", "Maria Lopez", D("239.99"), 0, 3), ("South", "Tom Becker", D("330.50"), 2, 3)]
    assert con.sql("SELECT * FROM customer_activity ORDER BY 1").fetchall() == [
        (101, D("120.50"), 1), (102, D("140.00"), 2), (103, D("210.00"), 2), (105, D("99.99"), 1)]
    assert (tmp_path / "out" / "unknown_customers.csv").read_text().splitlines() == \
        ["order_id,customer_id,amount", "5,104,15.25"]
    con.close()
    con = run(SAMPLE.read_text(), tmp_path, warehouse, "min_amount=50")  # insert then update keeps unseen rows
    assert con.sql("SELECT customer_id, total_usd, orders FROM customer_activity ORDER BY 1").fetchall() == [
        (101, D("120.50"), 1), (102, D("140.00"), 2), (103, D("200.00"), 1), (105, D("99.99"), 1)]


def to_xml(dsx: str) -> str:
    """The same export in DataStage's XML form (DSExport, Record, Property, Collection, SubRecord)."""
    out = ["<?xml version='1.0' encoding='UTF-8'?>", "<DSExport>", '<Header ExportingTool="IBM InfoSphere DataStage Export"/>']
    for job in parse_dsx(dsx):
        out.append(f'<Job Identifier="{escape(job.name)}">')
        for r in job.records:
            out.append(f'<Record Identifier="{escape(r.identifier)}" Type="{escape(r.type[1:])}" Readonly="0">')
            for k, v in r.props.items():
                if k not in ("Identifier", "OLEType") and k not in r.collections:
                    out.append(f'<Property Name="{escape(k)}">{escape(v)}</Property>')
            for name, subs in r.collections.items():
                out.append(f'<Collection Name="{escape(name)}" Type="{escape(r.props.get(name, ""))}">')
                for s in subs:
                    out.append("<SubRecord>" + "".join(f'<Property Name="{escape(k)}">{escape(v)}</Property>'
                                                       for k, v in s.items()) + "</SubRecord>")
                out.append("</Collection>")
            out.append("</Record>")
        out.append("</Job>")
    out.append("</DSExport>")
    return "\n".join(out)


def test_the_xml_export_imports_the_same():
    text = SAMPLE.read_text()
    xml = to_xml(text)
    assert is_datastage(xml) and is_datastage(text) and is_datastage("", "jobs.dsx")
    assert import_datastage(xml).pipeline.to_dict() == import_datastage(text).pipeline.to_dict()


def test_cli(tmp_path, capsys):
    out = tmp_path / "job.yaml"
    assert main(["import", str(SAMPLE), "-o", str(out), "--scorecard", str(tmp_path / "card.md")]) == 0
    err = capsys.readouterr().err
    assert "scorecard:" in err and "before-job subroutine" in err
    assert "# Migration scorecard: px_order_revenue" in (tmp_path / "card.md").read_text()
    with pytest.raises(DataStageImportError):
        import_datastage("BEGIN HEADER\nEND HEADER\n")


# --------------------------------------------------------------------------- stages

def test_transformer_constraints_otherwise_and_chained_stage_variables(tmp_path, warehouse):
    job = Job(params=[{"Name": "Big", "Default": "100", "ParamType": "3"}])
    orders(job)
    job.stage("xfm", "CTransformerStage", ole="CTransformerStage", stage_vars=[
        {"Name": "svDouble", "Expression": "lnk_in.AMOUNT * 2"},
        {"Name": "svBig", "Expression": "If svDouble >= Big Then 1 Else 0"},
    ])
    job.stage("ora_big", "OracleConnectorPX", connector(table="BIG_ORDERS", write_mode="0", table_action="2"))
    job.stage("ora_rest", "OracleConnectorPX", connector(table="OTHER_ORDERS", write_mode="0", table_action="2"))
    job.link("ora_orders", "xfm", "lnk_in", ORDER_COLS)
    job.link("xfm", "ora_big", "lnk_big", [col("ORDER_ID", "4", "10", derivation="lnk_in.ORDER_ID"),
                                           col("LABEL", derivation='UpCase(lnk_in.STATUS) : "!"'),
                                           col("DOUBLED", "3", "12", "2", derivation="svDouble")],
             out_props={"Constraint": "svBig = 1"})
    job.link("xfm", "ora_rest", "lnk_rest", [col("ORDER_ID", "4", "10", derivation="lnk_in.ORDER_ID")],
             out_props={"Reject": "1"})
    con = run(job.dsx(), tmp_path, warehouse)
    assert con.sql("SELECT order_id, label, doubled FROM big_orders WHERE order_id = 7").fetchone() == \
        (7, "SHIPPED!", D("199.98"))
    big = {r[0] for r in con.sql("SELECT order_id FROM big_orders").fetchall()}
    assert big == {1, 2, 4, 6, 7}  # amount * 2 >= 100
    assert {r[0] for r in con.sql("SELECT order_id FROM other_orders").fetchall()} == {3, 5, 8}
    stages = import_datastage(job.dsx()).pipeline.to_dict()["nodes"]
    assert sum(n["id"].startswith("xfm_vars") for n in stages) == 2  # svBig reads svDouble: a second step
    carry = Job()
    orders(carry)
    carry.stage("xfm", "CTransformerStage", ole="CTransformerStage", stage_vars=[
        {"Name": "svChanged", "Expression": "If lnk_in.STATUS <> svPrev Then 1 Else 0"},
        {"Name": "svPrev", "Expression": "lnk_in.STATUS"},
    ])
    carry.stage("seq", "PxSequentialFile", [("file", "out/c.csv")])
    carry.link("ora_orders", "xfm", "lnk_in", ORDER_COLS)
    carry.link("xfm", "seq", "lnk_out", [col("ORDER_ID", "4", "10", derivation="lnk_in.ORDER_ID")])
    result = import_datastage(carry.dsx())
    assert item(result, "xfm").status == MANUAL and "previous row" in item(result, "xfm").notes[0]


def test_filter_where_clauses_once_and_reject(tmp_path, warehouse):
    job = Job()
    orders(job)
    job.stage("flt", "PxFilter", [("where", "AMOUNT >= 100"), ("target", "0"), ("where", "STATUS = 'shipped'"),
                                  ("target", "1"), ("first", "true")])
    job.stage("seq_big", "PxSequentialFile", [("file", "out/big.csv")])
    job.stage("seq_shipped", "PxSequentialFile", [("file", "out/shipped.csv")])
    job.stage("seq_rest", "PxSequentialFile", [("file", "out/rest.csv")])
    job.link("ora_orders", "flt", "lnk_in", ORDER_COLS)
    job.link("flt", "seq_big", "lnk_big")
    job.link("flt", "seq_shipped", "lnk_shipped")
    job.link("flt", "seq_rest", "lnk_rest", out_link_type="3")
    run(job.dsx(), tmp_path, warehouse)

    def ids(name):
        return sorted(int(line.split(",")[0]) for line in (tmp_path / "out" / name).read_text().splitlines()[1:])

    assert ids("big.csv") == [1, 4]
    assert ids("shipped.csv") == [2, 6, 7, 8]  # shipped, but not already sent to big
    assert ids("rest.csv") == [3, 5]


def test_join_funnel_aggregator_and_sequential_files(tmp_path, warehouse):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "extra.csv").write_text("ORDER_ID,CUSTOMER_ID,STATUS,AMOUNT\n9,101,shipped,5.00\n")
    job = Job()
    orders(job)
    job.stage("seq_extra", "PxSequentialFile", [("file", "in/extra.csv")])
    job.stage("ora_customers", "OracleConnectorPX", connector(select="SELECT ID, REGION FROM CUSTOMERS"))
    job.stage("fun", "PxFunnel")
    job.stage("jn", "PxJoin", [("key", "CUSTOMER_ID"), ("operator", "innerjoin")])
    job.stage("agg", "PxAggregator", [("key", "REGION"), ("reduce", "AMOUNT"), ("mean", "AVG_AMOUNT"),
                                       ("reduce", "AMOUNT"), ("min", "SMALLEST"), ("countField", "N")])
    job.stage("seq_out", "PxSequentialFile", [("file", "out/by_region.csv")])
    job.link("ora_orders", "fun", "lnk_a", ORDER_COLS)
    job.link("seq_extra", "fun", "lnk_b", ORDER_COLS)
    job.link("fun", "jn", "lnk_all", ORDER_COLS)
    job.link("ora_customers", "jn", "lnk_customers", [col("CUSTOMER_ID", "4", "10", key=True), col("REGION")])
    job.link("jn", "agg", "lnk_joined", [col("REGION"), col("AMOUNT", "3", "10", "2")])
    job.link("agg", "seq_out", "lnk_out", [col("REGION"), col("AVG_AMOUNT", "8"), col("SMALLEST", "3", "10", "2"),
                                            col("N", "4", "10")])
    run(job.dsx(), tmp_path, warehouse)
    rows = sorted((tmp_path / "out" / "by_region.csv").read_text().splitlines()[1:])
    # South: 120.50, 45.00 (no filter here), 200.00 and 10.00 from the table, 5.00 from the extra file
    assert rows == ["East,15.25,15.25,1", "North,79.99666666666667,60.0,3", "South,76.1,5.0,5"]


def test_lookup_continue_drop_and_unkeyed_references():
    def build(action: str, keyed: bool) -> str:
        job = Job()
        orders(job)
        job.stage("ora_customers", "OracleConnectorPX", connector(select="SELECT ID, REGION FROM CUSTOMERS"))
        job.stage("lkp", "PxLookup", [("ifNotFound", action)])
        job.stage("seq_out", "PxSequentialFile", [("file", "out/x.csv")])
        job.link("ora_orders", "lkp", "lnk_in", ORDER_COLS)
        job.link("ora_customers", "lkp", "lnk_ref", [col("ID", "4", "10", key=keyed), col("REGION")],
                 in_cols=[col("ID", "4", "10", key=keyed, derivation="lnk_in.CUSTOMER_ID")], link_type="2")
        job.link("lkp", "seq_out", "lnk_out", [col("ORDER_ID", "4", "10", derivation="lnk_in.ORDER_ID"),
                                               col("REGION", derivation="lnk_ref.REGION")])
        return job.dsx()

    keep = import_datastage(build("continue", True))
    join = next(n for n in keep.pipeline.nodes if n.type == "join")
    assert join.params["how"] == "left" and item(keep, "lkp").status == CONVERTED
    assert not any(n.id.endswith("_unique") for n in keep.pipeline.nodes)
    drop = import_datastage(build("drop", False))
    assert next(n for n in drop.pipeline.nodes if n.type == "join").params == {
        "condition": "ora_orders.customer_id = lkp_lnk_ref_unique.id"}
    assert item(drop, "lkp").status == REVIEW  # the reference isn't keyed: one row per key is kept
    fail = import_datastage(build("fail", True))
    assert item(fail, "lkp").status == REVIEW and "stops the job" in " ".join(item(fail, "lkp").notes)


def test_unknown_stages_jobs_and_write_modes():
    job = Job(root={"AfterSubr": "MyRoutine\\\\arg"})
    orders(job)
    job.stage("pivot", "PxPivot")
    job.stage("ora_out", "OracleConnectorPX", connector(table="OUT", write_mode="3"))
    job.link("ora_orders", "pivot", "lnk_in", ORDER_COLS)
    job.link("pivot", "ora_out", "lnk_out", [col("ORDER_ID", "4", "10"), col("STATUS")])
    result = import_datastage(job.dsx())
    assert item(result, "pivot").status == MANUAL and "PxPivot" in item(result, "pivot").notes[0]
    assert item(result, "ora_out").status == MANUAL  # insert then update needs key columns
    assert item(result, "after-job subroutine").status == MANUAL
    seq = Job(name="seq_nightly", job_type="2").dsx()
    with pytest.raises(DataStageImportError, match="no parallel jobs"):
        import_datastage(seq)
    both = seq.replace("END DSJOB\n", "END DSJOB\n" + job.dsx().split("END HEADER\n", 1)[1], 1)
    combined = import_datastage(both)
    assert item(combined, "seq_nightly").status == REVIEW
    assert item(combined, "px_test/pivot").status == MANUAL  # stages are named per job when there are several
