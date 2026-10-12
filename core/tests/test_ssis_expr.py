"""SSIS expressions, translated and run in DuckDB against SSIS's answers."""

import datetime as dt
import re
from decimal import Decimal

import duckdb
import pytest
import sqlglot
from sqlglot import exp

from redcliff.ssis_expr import SSISExpressionError, translate_ssis, variable_name

ROW = ("SELECT 120.50::DECIMAL(10,2) AS amount, 7 AS qty, 2 AS packs, '  South ' AS region, NULL::VARCHAR AS email, "
       "'shipped' AS status, DATE '2026-09-15' AS order_date")
KINDS = {"amount": "number", "qty": "int", "packs": "int", "region": "string", "email": "string",
         "status": "string", "order_date": "date", "Order Date": "date"}
VARS = {"User::FxRate": ("1.5", "number"), "User::Region": ("North", "string")}


def resolve(name):
    col = "order_date" if name == "Order Date" else name
    return (exp.column(col), KINDS[name]) if name in KINDS else None


def run(text):
    t = translate_ssis(text, resolve, VARS, package="Pkg")
    sql = t.text().replace("${fx_rate}", "1.5").replace("${region}", "North")
    return duckdb.sql(f"SELECT {sqlglot.transpile(sql, write='duckdb')[0]} FROM ({ROW})").fetchone()[0], t


@pytest.mark.parametrize("text, want", [
    ('ISNULL(email) ? "none" : email', "none"),
    ("REPLACENULL(email, \"n/a\")", "n/a"),
    ('UPPER(TRIM(region)) + "-" + status', "SOUTH-shipped"),
    ('LEN(TRIM(region))', 5),
    ('SUBSTRING(status, 1, 4)', "ship"),
    ('LEFT(status, 2) + RIGHT(status, 2)', "shed"),
    ('qty / packs', 3),                                   # integer division, as in SSIS
    ('-qty / packs', -3),                                 # truncated toward zero
    ('qty / 2.0', Decimal("3.5")),
    ('qty % packs', 1),
    ('(DT_WSTR, 10)qty + "x"', "7x"),
    ('(DT_NUMERIC, 12, 2)(amount * @[User::FxRate])', Decimal("180.75")),
    ('ROUND(amount * @[User::FxRate], 0)', Decimal("181")),
    ('DATEADD("dd", 1, order_date)', dt.datetime(2026, 9, 16)),
    ('DATEDIFF("dd", order_date, (DT_DBTIMESTAMP)"2026-10-01")', 16),
    ('DATEPART("mm", order_date) * 100 + DAY(order_date)', 915),
    ('YEAR([Order Date])', 2026),
    ('amount >= 100 && !(status == "cancelled") ? 1 : 0', 1),
    ('status != "shipped" || ISNULL(email)', True),
    ('NULL(DT_WSTR, 20)', None),
    ('@[User::Region] == "North"', True),
    ('FINDSTRING(status, "pp", 1)', 4),
    ('@[System::PackageName]', "Pkg"),
])
def test_translation_runs_with_ssis_semantics(text, want):
    got, t = run(text)
    assert got == want, t.text()


def test_variables_and_statuses():
    t = translate_ssis("amount * @[User::FxRate]", resolve, VARS)
    assert t.text() == "amount * ${fx_rate}" and t.variables == {"fx_rate": "1.5"} and t.status == "converted"
    assert translate_ssis("GETUTCDATE()", resolve).status == "review"
    assert translate_ssis("@[System::StartTime]", resolve).status == "review"
    assert variable_name("User::MinOrderDate") == "min_order_date"


@pytest.mark.parametrize("text, message", [
    ("NOPE(1)", "NOPE() has no translation"),
    ("missing + 1", "unknown column missing"),
    ("@[User::Undefined]", "isn't defined"),
    ("SUBSTRING(status, 1)", "SUBSTRING takes 3 argument(s), got 2"),
    ("FINDSTRING(status, \"p\", 2)", "occurrence other than 1"),
    ("order_date + 1", "use DATEADD"),
    ("amount $ 2", "unexpected character '$'"),
    ("(amount", "expected ')'"),
])
def test_errors(text, message):
    with pytest.raises(SSISExpressionError, match=re.escape(message)):
        translate_ssis(text, resolve, VARS)
