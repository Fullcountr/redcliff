"""The PowerCenter expression translator: each case runs in DuckDB and must give PowerCenter's answer."""

import datetime as dt
from decimal import Decimal

import duckdb
import pytest
import sqlglot
from sqlglot import exp

from redcliff.informatica import ExpressionError, translate

TYPES = {"FIRST_NAME": "string", "LAST_NAME": "string", "AMOUNT": "number", "ORDER_DATE": "date",
         "STATUS": "string", "REGION": "string", "S": "string"}
ROW = ("SELECT '  Ana ' AS first_name, NULL::TEXT AS last_name, 120.5 AS amount, DATE '2026-09-15' AS order_date, "
       "'shipped' AS status, NULL::TEXT AS region, '09/15/2026 10:30:00' AS s")


def resolve(name):
    return (exp.column(name.lower()), TYPES[name]) if name in TYPES else None


def run(text, params=None):
    t = translate(text, resolve, params)
    duck = sqlglot.transpile(t.text(), write="duckdb")[0]
    return duckdb.sql(f"SELECT {duck} FROM ({ROW})").fetchone()[0], t


@pytest.mark.parametrize("text, want", [
    ("IIF(ISNULL(AMOUNT), 0, AMOUNT)", Decimal("120.5")),
    ("IIF(AMOUNT >= 1000, 'Y')", ""),                       # missing ELSE on a string: ''
    ("IIF(AMOUNT >= 1000, 5)", 0),                          # missing ELSE on a number: 0
    ("LTRIM(RTRIM(FIRST_NAME)) || ' ' || LAST_NAME", "Ana "),  # || treats NULL as ''
    ("CONCAT(LAST_NAME, LAST_NAME)", ""),
    ("DECODE(TRUE, ISNULL(REGION), 'UNKNOWN', UPPER(REGION))", "UNKNOWN"),
    ("DECODE(STATUS, 'shipped', 'S', 'cancelled', 'C', 'O')", "S"),
    ("DECODE(STATUS, 'x', 'X')", None),
    ("TO_CHAR(ORDER_DATE, 'YYYY-MM')", "2026-09"),
    ("TO_CHAR(ORDER_DATE)", "09/15/2026 00:00:00"),         # PowerCenter's default date format
    ("TO_DATE(S, 'MM/DD/YYYY HH24:MI:SS')", dt.datetime(2026, 9, 15, 10, 30)),
    ("ADD_TO_DATE(ORDER_DATE, 'MM', 2)", dt.datetime(2026, 11, 15)),
    ("DATE_DIFF(TO_DATE('2026-10-01', 'YYYY-MM-DD'), ORDER_DATE, 'DD')", 16),
    ("GET_DATE_PART(ORDER_DATE, 'YYYY') * 100 + GET_DATE_PART(ORDER_DATE, 'MM')", 202609),
    ("TRUNC(ORDER_DATE, 'MM')", dt.datetime(2026, 9, 1)),
    ("LAST_DAY(ORDER_DATE)", dt.date(2026, 9, 30)),
    ("TRUNC(-2.7)", Decimal("-2")),
    ("TO_INTEGER(2.5)", 3),                                 # rounds by default
    ("TO_INTEGER(-2.7, TRUE)", -2),                         # truncates with the flag
    ("TO_DECIMAL('12.345', 2)", Decimal("12.35")),
    ("SUBSTR(STATUS, 0, 4)", "ship"),                       # a start of 0 means 1
    ("SUBSTR(STATUS, -3)", "ped"),
    ("INSTR(STATUS, 'pp')", 4),
    ("IN(STATUS, 'shipped', 'pending')", True),
    ("IN(STATUS, 'SHIPPED', 'Pending', 0)", True),           # 0: case-insensitive
    ("IN(STATUS, 'SHIPPED', 1)", False),                     # 1: case-sensitive
    ("NOT ISNULL(REGION) OR AMOUNT > 100 AND STATUS = 'x'", False),
    ("REPLACECHR(1, FIRST_NAME, ' n', '')", "Aa"),
    ("REPLACESTR(1, STATUS, 'ship', 'SHIP')", "SHIPped"),
    ("LPAD(TO_CHAR(AMOUNT), 8, '0')", "000120.5"),
    ("IS_NUMBER('12.5')", True),
    ("IS_SPACES('   ')", True),
    ("REG_MATCH(STATUS, 'ship')", False),                   # REG_MATCH matches the whole string
    ("REG_MATCH(STATUS, 'ship.*')", True),
    ("MOD(7, 3) + 10 % 4", 3),
    ("-AMOUNT * 2 // a trailing comment", Decimal("-241.0")),
    ("LENGTH(STATUS) - 1 -- another comment", 6),
    ("'it''s'", "it's"),
])
def test_translation_runs_with_powercenter_semantics(text, want):
    got, t = run(text)
    assert got == want, t.text()
    assert t.status == "converted"


def test_aggregates_with_filter_conditions():
    assert translate("SUM(AMOUNT, STATUS = 'shipped')", resolve).text() == \
        "SUM(CASE WHEN status = 'shipped' THEN amount END)"
    assert translate("COUNT(*)", resolve).text() == "COUNT(*)"
    first = translate("FIRST(AMOUNT)", resolve)
    assert first.text() == "ANY_VALUE(amount)" and first.status == "review"


@pytest.mark.parametrize("text, status, note", [
    (":LKP.LKP_CUSTOMER(AMOUNT)", "manual", "unconnected lookup LKP_CUSTOMER"),
    ("$$UNKNOWN", "manual", "$$UNKNOWN has no value"),
    ("FOO(1)", "manual", "FOO() has no translation"),
    ("IIF(AMOUNT < 0, ERROR('negative'), AMOUNT)", "review", "ERROR() rejects the row"),
    ("DATE_DIFF(ORDER_DATE, ORDER_DATE, 'MM')", "review", "fractional"),
    ("IS_DATE(S, 'YYYY-MM-DD')", "manual", "IS_DATE() has no portable SQL equivalent"),
])
def test_honest_statuses(text, status, note):
    t = translate(text, resolve)
    assert t.status == status
    assert any(note in n for n in t.notes), t.notes


def test_parameters_use_their_defaults():
    got, t = run("ADD_TO_DATE($$RUN_DATE, 'DD', 1)", {"$$RUN_DATE": ("01/31/2026 00:00:00", "date")})
    assert got == dt.datetime(2026, 2, 1)
    assert t.status == "review"


@pytest.mark.parametrize("text, message", [
    ("IIF(AMOUNT > 1", "expected ',' or ')'"),
    ("NOPE + 1", "unknown port NOPE"),
    ("SUBSTR(STATUS)", "SUBSTR takes 2 to 3 argument(s), got 1"),
    ("TO_CHAR(ORDER_DATE, STATUS)", "needs a literal format"),
    ("AMOUNT @ 2", "unexpected character '@'"),
])
def test_errors_are_clear(text, message):
    with pytest.raises(ExpressionError, match=message.replace("(", r"\(").replace(")", r"\)")):
        translate(text, resolve)


def test_spark_output_keeps_concatenation_grouped():
    t = translate("STATUS || AMOUNT + 1", resolve)
    assert sqlglot.transpile(t.text(), write="spark")[0] == "CONCAT(COALESCE(status, ''), COALESCE(amount + 1, ''))"
