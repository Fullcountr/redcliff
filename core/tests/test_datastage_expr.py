"""DataStage Transformer expressions to SQL, checked by evaluating the SQL in DuckDB where it matters."""

from __future__ import annotations

import pytest
from sqlglot import exp

from redcliff.datastage_expr import (DataStageExpressionError, ds_format, param_name, sql_type, substitute_params,
                                     translate_datastage)
from redcliff.scorecard import CONVERTED, REVIEW

COLS = {
    ("lnk", "STATUS"): "string", ("lnk", "AMOUNT"): "decimal", ("lnk", "QTY"): "int", ("lnk", "ORDER_DATE"): "date",
    ("lnk", "FIRST_NAME"): "string", ("lnk", "LAST_NAME"): "string", ("lnk", "REGION"): "string",
}
STAGE_VARS = {"svTotal": ("sv_total", "decimal")}
PARAMS = {"MinAmount": ("50", "number"), "RunDate": ("2026-09-01", "date"), "Region": ("North", "string")}


def resolve(link, col):
    if link is None:
        hit = STAGE_VARS.get(col)
        return (exp.column(hit[0]), hit[1]) if hit else None
    kind = COLS.get((link, col))
    return (exp.column(col.lower()), kind) if kind else None


def sql(text: str) -> str:
    return translate_datastage(text, resolve, PARAMS).text()


@pytest.mark.parametrize("ds, expected", [
    ('lnk.STATUS = "shipped"', "status = 'shipped'"),
    ("lnk.STATUS <> 'cancelled'", "status <> 'cancelled'"),
    ('lnk.STATUS # "x"', "status <> 'x'"),
    ('If IsNull(lnk.REGION) Then "UNKNOWN" Else UpCase(lnk.REGION)',
     "CASE WHEN region IS NULL THEN 'UNKNOWN' ELSE UPPER(region) END"),
    ('lnk.FIRST_NAME : " " : lnk.LAST_NAME', "first_name || ' ' || last_name"),
    ("lnk.FIRST_NAME[1, 3]", "SUBSTRING(first_name, 1, 3)"),
    ("Left(lnk.FIRST_NAME, 2)", "LEFT(first_name, 2)"),
    ("Len(TrimLeadingTrailing(lnk.FIRST_NAME))", "LENGTH(TRIM(first_name))"),
    ('NullToValue(lnk.REGION, "UNASSIGNED")', "COALESCE(region, 'UNASSIGNED')"),
    ("NullToZero(lnk.AMOUNT) * 2", "COALESCE(amount, 0) * 2"),
    ("IsNotNull(lnk.REGION) And lnk.QTY > 0", "NOT region IS NULL AND qty > 0"),
    ("Not(lnk.QTY > 0) Or lnk.QTY = 5", "NOT (qty > 0) OR qty = 5"),
    ("lnk.AMOUNT > MinAmount", "amount > ${min_amount}"),
    ("lnk.AMOUNT > #MinAmount#", "amount > ${min_amount}"),
    ("svTotal + 1", "sv_total + 1"),
    ("lnk.QTY / 4", "qty / 4"),
    ("Mod(lnk.QTY, 3)", "qty % 3"),
    ('DateToString(lnk.ORDER_DATE, "%yyyy-%mm")', "TIME_TO_STR(order_date, '%Y-%m')"),
    ('StringToDate("2026-09-01")', "STR_TO_DATE('2026-09-01', '%Y-%m-%d')"),
    ("YearFromDate(lnk.ORDER_DATE)", "EXTRACT(YEAR FROM order_date)"),
    ("DaysSinceFromDate(lnk.ORDER_DATE, RunDate)", "DATEDIFF(order_date, CAST('${run_date}' AS DATE), DAY)"),
    ('Field(lnk.REGION, "-", 2)', "SPLIT_PART(region, '-', 2)"),
    ('Ereplace(lnk.REGION, "N", "S")', "REPLACE(region, 'N', 'S')"),
    ("lnk.QTY ^ 2", "POWER(qty, 2)"),
    ("SetNull()", "NULL"),
])
def test_translations(ds, expected):
    assert sql(ds) == expected


def test_variables_and_status():
    t = translate_datastage("lnk.AMOUNT > MinAmount And lnk.REGION = Region", resolve, PARAMS)
    assert t.variables == {"min_amount": "50", "region": "North"} and t.status == CONVERTED


@pytest.mark.parametrize("ds, note", [
    ("Trim(lnk.FIRST_NAME)", "runs of spaces"),
    ('lnk.FIRST_NAME : lnk.QTY', "DataStage formats it"),
    ("DecimalToString(lnk.AMOUNT)", "leading zeros"),
    ("CurrentDate()", "query runs"),
    ("@INROWNUM", "ROW_NUMBER()"),
])
def test_differences_are_flagged_for_review(ds, note):
    t = translate_datastage(ds, resolve, PARAMS)
    assert t.status == REVIEW and any(note in n for n in t.notes)


@pytest.mark.parametrize("ds, error", [
    ("lnk.UNKNOWN", "unknown column"),
    ("Nope", "unknown name"),
    ("Count(lnk.REGION, \"a\")", "has no translation"),
    ("lnk.REGION[2]", "last n characters"),
    ("lnk.ORDER_DATE + 1", "date arithmetic"),
    ('DateToString(lnk.ORDER_DATE, "%q")', "has no SQL equivalent"),
    ("#Missing#", "isn't defined"),
])
def test_no_translation_raises(ds, error):
    with pytest.raises(DataStageExpressionError, match=error.replace("(", r"\(").replace(")", r"\)")):
        translate_datastage(ds, resolve, PARAMS)


def test_helpers():
    assert sql_type("3", 10, 2) == ("DECIMAL(10, 2)", "decimal")
    assert sql_type("12") == ("TEXT", "string") and sql_type("-5") == ("BIGINT", "int") and sql_type("9")[0] == "DATE"
    assert ds_format("%yyyy-%mm-%dd %hh:%nn:%ss") == "%Y-%m-%d %H:%M:%S"
    assert param_name("MinAmount") == "min_amount" and param_name("ps_sales.Region") == "ps_sales_region"
    assert substitute_params("#OutDir#/x.csv", {"OutDir": ("out", "string")}) == ("${out_dir}/x.csv", {"out_dir": "out"})


def test_translations_run_in_duckdb():
    duckdb = pytest.importorskip("duckdb")
    from redcliff.compile import _expr

    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 'North-East' AS region, 7 AS qty, DATE '2026-09-01' AS order_date, "
                "'Ann' AS first_name, 'Lee' AS last_name, CAST(120.5 AS DECIMAL(10, 2)) AS amount, "
                "'shipped' AS status")

    def run(ds):
        return con.sql(f"SELECT {_expr(sql(ds), 'duckdb')} FROM t").fetchone()[0]

    assert run("lnk.QTY / 2") == 3.5  # DataStage divides as a decimal, not as integers
    assert run('Field(lnk.REGION, "-", 2)') == "East"
    assert run("lnk.FIRST_NAME[2, 2]") == "nn"
    assert run('DateToString(lnk.ORDER_DATE, "%yyyy-%mm")') == "2026-09"
    assert run("If lnk.AMOUNT >= 100 Then 1 Else 0") == 1
