"""Talend's Java expressions to SQL, checked by evaluating the SQL in DuckDB where it matters."""

from __future__ import annotations

import pytest
from sqlglot import exp

from redcliff.scorecard import CONVERTED, REVIEW
from redcliff.talend_expr import TalendExpressionError, java_format, java_string, talend_type, translate_talend

COLS = {
    ("row1", "status"): "string", ("row1", "amount"): "decimal", ("row1", "qty"): "int",
    ("row1", "order_date"): "date", ("row2", "first_name"): "string", ("row2", "last_name"): "string",
    ("row2", "region"): "string", ("row3", "manager"): "string",
}
CONTEXT = {"minAmount": ("50", "decimal"), "fxRate": ("1.0", "number"), "dataDir": ("../data", "string")}


def resolve(row, col):
    kind = COLS.get((row, col))
    return (exp.column(col, table=row), kind) if kind else None


def sql(text: str) -> str:
    return translate_talend(text, resolve, ["row1", "row2", "row3"], CONTEXT).text()


@pytest.mark.parametrize("java, expected", [
    ('row1.status.equals("shipped")', "row1.status = 'shipped'"),
    ('!row1.status.equals("cancelled")', "row1.status <> 'cancelled'"),
    ('row1.status.equalsIgnoreCase("Shipped")', "UPPER(row1.status) = UPPER('Shipped')"),
    ("row3.manager == null", "row3.manager IS NULL"),
    ("row3.manager != null", "NOT row3.manager IS NULL"),
    ('row3.manager == null ? "UNASSIGNED" : row3.manager',
     "CASE WHEN row3.manager IS NULL THEN 'UNASSIGNED' ELSE row3.manager END"),
    ('row2.first_name + " " + row2.last_name', "row2.first_name || ' ' || row2.last_name"),
    ("row2.region.toUpperCase().trim()", "TRIM(UPPER(row2.region))"),
    ("StringHandling.UPCASE(row2.region)", "UPPER(row2.region)"),
    ("row2.first_name.substring(0, 3)", "SUBSTRING(row2.first_name, 1, 3)"),
    ("row2.first_name.substring(2)", "SUBSTRING(row2.first_name, 3)"),
    ("row2.first_name.length()", "LENGTH(row2.first_name)"),
    ('row1.status.startsWith("sh")', "row1.status LIKE 'sh%'"),
    ('row1.status.contains("ip")', "STR_POSITION(row1.status, 'ip') > 0"),
    ("row1.qty / 4", "CAST((row1.qty - row1.qty % 4) / 4 AS BIGINT)"),
    ("row1.qty * 1.5", "row1.qty * 1.5"),
    ("row1.amount.multiply(new BigDecimal(context.fxRate))", "row1.amount * CAST(${fx_rate} AS DECIMAL(38, 10))"),
    ("row1.amount.compareTo(new BigDecimal(100)) >= 0 ? 1 : 0", "CASE WHEN row1.amount >= 100 THEN 1 ELSE 0 END"),
    ("row1.amount > context.minAmount && row1.qty > 0 || row1.qty == 0",
     "row1.amount > ${min_amount} AND row1.qty > 0 OR row1.qty = 0"),
    ("(row1.qty > 0 || row1.qty < -5) && row1.amount > 0", "(row1.qty > 0 OR row1.qty < -5) AND row1.amount > 0"),
    ('TalendDate.formatDate("yyyy-MM", row1.order_date)', "TIME_TO_STR(row1.order_date, '%Y-%m')"),
    ('TalendDate.addDate(row1.order_date, 7, "dd")', "DATE_ADD(row1.order_date, 7, 'DAY')"),
    ('TalendDate.getPartOfDate("YEAR", row1.order_date)', "EXTRACT(YEAR FROM row1.order_date)"),
    ("Relational.ISNULL(row1.status)", "row1.status IS NULL"),
    ("Integer.parseInt(row1.status)", "CAST(row1.status AS INT)"),
    ("String.valueOf(row1.qty)", "CAST(row1.qty AS TEXT)"),
    ("(String) row1.qty", "CAST(row1.qty AS TEXT)"),
    ("Math.max(row1.qty, 10)", "GREATEST(row1.qty, 10)"),
    ("100L", "100"),
])
def test_translations(java, expected):
    assert sql(java) == expected


def test_variables_are_reported():
    t = translate_talend("row1.amount > context.minAmount", resolve, ["row1"], CONTEXT)
    assert t.variables == {"min_amount": "50"}
    assert t.status == CONVERTED


@pytest.mark.parametrize("java, note", [
    ('row2.first_name + " " + row2.last_name', 'writes "null"'),
    ('row1.status == "shipped"', "compares references"),
    ("Math.round(row1.qty * 1.5)", "rounds halves up"),
    ("TalendDate.getCurrentDate()", "when the job runs"),
])
def test_differences_are_flagged_for_review(java, note):
    t = translate_talend(java, resolve, ["row1", "row2"], CONTEXT)
    assert t.status == REVIEW and any(note in n for n in t.notes)


@pytest.mark.parametrize("java, error", [
    ('globalMap.get("count")', "globalMap"),
    ("row1.status.split(\",\")", ".split()"),
    ("row1.unknown", "unknown column"),
    ("context.missing", "isn't defined"),
    ("row1.amount.compareTo(row1.qty)", "compareTo outside"),
    ("row1.order_date + 1", "adding to a date"),
    ('TalendDate.formatDate("yyyy-MM-dd Q", row1.order_date)', "has no SQL equivalent"),
])
def test_no_translation_raises(java, error):
    with pytest.raises(TalendExpressionError, match=error.replace("(", r"\(").replace(")", r"\)")):
        translate_talend(java, resolve, ["row1"], CONTEXT)


def test_java_strings_and_formats():
    assert java_string('"orders"') == ("orders", {})
    assert java_string('context.dataDir + "/orders.csv"', CONTEXT) == ("${data_dir}/orders.csv", {"data_dir": "../data"})
    assert java_string("orders") == ("orders", {})
    assert java_format("yyyy-MM-dd HH:mm:ss") == "%Y-%m-%d %H:%M:%S"
    assert java_format("dd/MM/yyyy 'at' HH") == "%d/%m/%Y at %H"
    assert talend_type("id_BigDecimal", "10", "2") == ("DECIMAL(10, 2)", "decimal")
    assert talend_type("id_Date", pattern='"yyyy-MM-dd"') == ("DATE", "date")
    assert talend_type("id_Date", pattern='"yyyy-MM-dd HH:mm"') == ("TIMESTAMP", "date")


def test_translations_run_in_duckdb():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE row1 AS SELECT 'shipped' AS status, CAST(120.5 AS DECIMAL(10, 2)) AS amount, 7 AS qty, "
                "DATE '2026-09-01' AS order_date")
    from redcliff.compile import _expr

    def run(java):
        return con.sql(f"SELECT {_expr(sql(java), 'duckdb')} FROM row1").fetchone()[0]

    assert run("row1.qty / 2") == 3  # integer division, as in Java
    assert run('TalendDate.formatDate("yyyy-MM", row1.order_date)') == "2026-09"
    assert run("row1.amount.compareTo(new BigDecimal(100)) >= 0 ? 1 : 0") == 1
    assert run('row1.status.substring(0, 4).toUpperCase()') == "SHIP"
