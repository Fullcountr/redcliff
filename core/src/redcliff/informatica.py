"""Translate Informatica PowerCenter expressions into neutral SQL.

PowerCenter has its own expression language: ``IIF(ISNULL(AMOUNT), 0, AMOUNT)``,
``DECODE(TRUE, ...)``, ``TO_DATE(s, 'MM/DD/YYYY')``, ``SUM(AMOUNT, STATUS = 'X')``,
``:LKP.LKP_CUSTOMER(ID)``, ``$$RUN_DATE``. This module parses that language and builds a
sqlglot expression, so the result transpiles to DuckDB, Spark or anything else Redcliff targets.

Every translation carries a status, so a migration report can be honest about it:

- ``converted``: the SQL means the same thing.
- ``review``: the SQL runs, but differs from PowerCenter in an edge case (a note says which).
- ``manual``: there is no faithful translation; a NULL placeholder stands in and a note says why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from sqlglot import exp
from sqlglot.dialects.oracle import Oracle
from sqlglot.time import format_time

CONVERTED, REVIEW, MANUAL = "converted", "review", "manual"
_RANK = {CONVERTED: 0, REVIEW: 1, MANUAL: 2}


def worst(*statuses: str) -> str:
    return max(statuses, key=_RANK.__getitem__, default=CONVERTED)


class ExpressionError(ValueError):
    """The expression could not be read at all."""


def kind_of_type(datatype: str | None) -> str | None:
    """'string', 'number' or 'date' for a PowerCenter or database datatype name."""
    if not datatype:
        return None
    t = datatype.lower()
    if "date" in t or "time" in t:
        return "date"
    if any(w in t for w in ("char", "text", "string", "clob", "xml")):
        return "string"
    return "number"


# A resolver maps a port name (any case) to (expression, kind), or None if it isn't known.
Resolver = Callable[[str], "tuple[exp.Expression, str | None] | None"]


@dataclass
class Translation:
    sql: exp.Expression
    status: str = CONVERTED
    notes: list[str] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)
    references: set[str] = field(default_factory=set)  # port names used, upper case

    def text(self) -> str:
        return self.sql.sql()


# --------------------------------------------------------------------------- tokens

_TOKEN = re.compile(r"""
    (?P<ws>\s+|--[^\n]*|//[^\n]*)
  | (?P<string>'(?:[^']|'')*')
  | (?P<number>\d+\.\d*|\.\d+|\d+)
  | (?P<lookup>:(?:LKP|SP|MCR)\.[A-Za-z_][A-Za-z0-9_$#]*)
  | (?P<param>\$\$?[A-Za-z_][A-Za-z0-9_]*)
  | (?P<name>[A-Za-z_][A-Za-z0-9_$#]*)
  | (?P<op>\|\||<>|!=|\^=|<=|>=|[-+*/%=<>(),])
""", re.VERBOSE | re.IGNORECASE)


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ExpressionError(f"unexpected character {text[pos]!r} at position {pos}")
        pos = m.end()
        if m.lastgroup != "ws":
            out.append((m.lastgroup, m.group()))
    out.append(("end", ""))
    return out


# --------------------------------------------------------------------------- formats

_DATE_UNITS = {
    "Y": "YEAR", "YY": "YEAR", "YYY": "YEAR", "YYYY": "YEAR",
    "Q": "QUARTER",
    "MM": "MONTH", "MON": "MONTH", "MONTH": "MONTH",
    "D": "DAY", "DD": "DAY", "DDD": "DAY", "DY": "DAY", "DAY": "DAY",
    "HH": "HOUR", "HH12": "HOUR", "HH24": "HOUR",
    "MI": "MINUTE", "SS": "SECOND", "MS": "MILLISECOND", "US": "MICROSECOND",
}
_DEFAULT_DATE_FORMAT = "MM/DD/YYYY HH24:MI:SS"  # PowerCenter's default date format


def _strftime(fmt: str) -> str:
    """A PowerCenter (Oracle-style) date format as the strftime format sqlglot uses."""
    return format_time(fmt, Oracle.TIME_MAPPING, Oracle.TIME_TRIE) or fmt


def _p(e: exp.Expression) -> exp.Expression:
    """Parenthesize an operator expression used as an operand."""
    if isinstance(e, (exp.Binary, exp.Not, exp.Neg, exp.Is, exp.In, exp.Between)):
        return exp.Paren(this=e)
    return e


# --------------------------------------------------------------------------- parser

_COMPARE = {"=": exp.EQ, "<>": exp.NEQ, "!=": exp.NEQ, "^=": exp.NEQ,
            "<": exp.LT, ">": exp.GT, "<=": exp.LTE, ">=": exp.GTE}
_ARITH = {"+": exp.Add, "-": exp.Sub, "*": exp.Mul, "/": exp.Div, "%": exp.Mod}
_UPDATE_CONSTANTS = {"DD_INSERT": 0, "DD_UPDATE": 1, "DD_DELETE": 2, "DD_REJECT": 3}
_AGGREGATES = {"SUM", "AVG", "COUNT", "MAX", "MIN", "MEDIAN", "STDDEV", "VARIANCE", "FIRST", "LAST",
               "PERCENTILE", "CUME", "MOVINGAVG", "MOVINGSUM"}


class _Parser:
    def __init__(self, text: str, resolve: Resolver, params: dict[str, tuple[str, str | None]]):
        self.toks = _tokens(text)
        self.i = 0
        self.resolve = resolve
        self.params = params
        self.status = CONVERTED
        self.notes: list[str] = []
        self.functions: set[str] = set()
        self.references: set[str] = set()
        self.kinds: dict[int, str | None] = {}  # id(expression) -> kind, for TRUNC/ROUND/IIF

    # ---------------------------------------------------------------- bookkeeping

    def note(self, status: str, message: str) -> None:
        self.status = worst(self.status, status)
        if message not in self.notes:
            self.notes.append(message)

    def kind(self, e: exp.Expression) -> str | None:
        if id(e) in self.kinds:
            return self.kinds[id(e)]
        if isinstance(e, exp.Paren):
            return self.kind(e.this)
        if isinstance(e, (exp.StrToTime, exp.DateAdd, exp.CurrentTimestamp, exp.TimestampTrunc, exp.LastDay)):
            return "date"
        if isinstance(e, (exp.TimeToStr, exp.Concat, exp.DPipe, exp.Upper, exp.Lower, exp.Trim, exp.Substring)):
            return "string"
        if isinstance(e, exp.Literal):
            return "string" if e.is_string else "number"
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Length, exp.DateDiff,
                          exp.Extract, exp.StrPosition)):
            return "number"
        if isinstance(e, exp.Case):
            for branch in [*(w.args.get("true") for w in e.args.get("ifs") or []), e.args.get("default")]:
                if branch is not None and self.kind(branch):
                    return self.kind(branch)
        return None

    def tagged(self, e: exp.Expression, kind: str | None) -> exp.Expression:
        self.kinds[id(e)] = kind
        return e

    # ---------------------------------------------------------------- token helpers

    def peek(self, offset: int = 0) -> tuple[str, str]:
        return self.toks[min(self.i + offset, len(self.toks) - 1)]

    def take(self) -> tuple[str, str]:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, value: str) -> None:
        kind, text = self.take()
        if text.upper() != value:
            raise ExpressionError(f"expected {value!r}, found {text or 'the end'!r}")

    def word(self, value: str) -> bool:
        kind, text = self.peek()
        return kind == "name" and text.upper() == value

    # ---------------------------------------------------------------- grammar

    def parse(self) -> exp.Expression:
        e = self.or_()
        if self.peek()[0] != "end":
            raise ExpressionError(f"unexpected {self.peek()[1]!r}")
        return e

    def or_(self) -> exp.Expression:
        e = self.and_()
        while self.word("OR"):
            self.take()
            e = exp.Or(this=e, expression=self.and_())
        return e

    def and_(self) -> exp.Expression:
        e = self.not_()
        while self.word("AND"):
            self.take()
            e = exp.And(this=_p(e) if isinstance(e, exp.Or) else e, expression=self.not_())
        return e

    def not_(self) -> exp.Expression:
        if self.word("NOT"):
            self.take()
            return exp.Not(this=_p(self.not_()))
        return self.compare()

    def compare(self) -> exp.Expression:
        e = self.concat()
        while self.peek()[1] in _COMPARE:
            op = _COMPARE[self.take()[1]]
            e = op(this=e, expression=self.concat())
        return e

    def concat(self) -> exp.Expression:
        parts = [self.additive()]
        while self.peek()[1] == "||":
            self.take()
            parts.append(self.additive())
        if len(parts) == 1:
            return parts[0]
        return self.joined(parts)

    def joined(self, parts: list[exp.Expression]) -> exp.Expression:
        """PowerCenter's || treats NULL as an empty string; SQL's || returns NULL. So: COALESCE."""
        safe = [p if isinstance(p, exp.Literal) else exp.Coalesce(this=p, expressions=[exp.Literal.string("")])
                for p in parts]
        return self.tagged(exp.Concat(expressions=safe), "string")

    def additive(self) -> exp.Expression:
        e = self.term()
        while self.peek()[1] in ("+", "-"):
            op = _ARITH[self.take()[1]]
            e = op(this=e, expression=self.term())
        return e

    def term(self) -> exp.Expression:
        e = self.unary()
        while self.peek()[1] in ("*", "/", "%"):
            op = _ARITH[self.take()[1]]
            e = op(this=e, expression=self.unary())
        return e

    def unary(self) -> exp.Expression:
        if self.peek()[1] == "-":
            self.take()
            inner = self.unary()
            if isinstance(inner, exp.Literal) and not inner.is_string:
                return exp.Literal.number("-" + inner.this)
            return exp.Neg(this=_p(inner))
        if self.peek()[1] == "+":
            self.take()
            return self.unary()
        return self.primary()

    def primary(self) -> exp.Expression:
        kind, text = self.take()
        if kind == "string":
            return exp.Literal.string(text[1:-1].replace("''", "'"))
        if kind == "number":
            return exp.Literal.number(text)
        if text == "(":
            e = self.or_()
            self.expect(")")
            return exp.Paren(this=e)
        if kind == "lookup":
            args = self.arguments() if self.peek()[1] == "(" else []
            what = {":LKP": "unconnected lookup", ":SP": "stored procedure call",
                    ":MCR": "macro"}[text.split(".")[0].upper()]
            self.note(MANUAL, f"{what} {text.split('.', 1)[1]} has no SQL equivalent yet; "
                              "a NULL placeholder stands in")
            del args
            return exp.null()
        if kind == "param":
            return self.parameter(text)
        if kind == "name":
            upper = text.upper()
            if self.peek()[1] == "(":
                return self.call(upper, self.arguments())
            return self.name(upper)
        raise ExpressionError(f"unexpected {text or 'end of expression'!r}")

    def arguments(self) -> list[exp.Expression]:
        self.expect("(")
        args: list[exp.Expression] = []
        if self.peek()[1] == ")":
            self.take()
            return args
        while True:
            if self.peek()[1] == "*":  # COUNT(*)
                self.take()
                args.append(exp.Star())
            else:
                args.append(self.or_())
            kind, text = self.take()
            if text == ")":
                return args
            if text != ",":
                raise ExpressionError(f"expected ',' or ')', found {text or 'the end'!r}")

    def name(self, upper: str) -> exp.Expression:
        if upper == "TRUE":
            return exp.true()
        if upper == "FALSE":
            return exp.false()
        if upper == "NULL":
            return exp.null()
        if upper in ("SYSDATE", "SYSTIMESTAMP", "SESSSTARTTIME"):
            self.functions.add(upper)
            return exp.CurrentTimestamp()
        if upper in _UPDATE_CONSTANTS:
            return exp.Literal.number(_UPDATE_CONSTANTS[upper])
        resolved = self.resolve(upper)
        if resolved is None:
            raise ExpressionError(f"unknown port {upper}")
        self.references.add(upper)
        e, kind = resolved
        e = e.copy()
        return self.tagged(e, kind)

    def parameter(self, text: str) -> exp.Expression:
        name = text.upper()
        if name in self.params:
            value, kind = self.params[name]
            self.note(REVIEW, f"mapping parameter {name} is fixed at its default {value!r}; "
                              "make it a pipeline variable if it changes between runs")
            if kind == "number":
                try:
                    float(value)
                    return exp.Literal.number(value)
                except ValueError:
                    pass
            if kind == "date":
                return self.tagged(exp.StrToTime(this=exp.Literal.string(value),
                                                 format=exp.Literal.string(_strftime(_DEFAULT_DATE_FORMAT))),
                                   "date")
            return exp.Literal.string(value)
        self.note(MANUAL, f"{name} has no value Redcliff can use; a NULL placeholder stands in")
        return exp.null()

    # ---------------------------------------------------------------- functions

    def call(self, name: str, args: list[exp.Expression]) -> exp.Expression:
        self.functions.add(name)
        handler = getattr(self, f"fn_{name.lower()}", None)
        if handler is not None:
            return handler(args)
        if name in _SAME_NAME:
            lo, hi = _SAME_NAME[name]
            self.arity(name, args, lo, hi)
            return exp.func(name, *args)
        if name in _AGGREGATES:
            return self.aggregate(name, args)
        self.note(MANUAL, f"{name}() has no translation yet; a NULL placeholder stands in")
        return exp.null()

    def arity(self, name: str, args: list, lo: int, hi: int | None) -> None:
        if len(args) < lo or (hi is not None and len(args) > hi):
            want = f"{lo}" if lo == hi else f"{lo} to {hi}" if hi is not None else f"at least {lo}"
            raise ExpressionError(f"{name} takes {want} argument(s), got {len(args)}")

    def aggregate(self, name: str, args: list[exp.Expression]) -> exp.Expression:
        self.arity(name, args, 1, 2)
        value = args[0]
        if any(isinstance(n, exp.AggFunc) for n in value.find_all(exp.AggFunc)):
            self.note(MANUAL, f"nested aggregate {name}(...) has no direct SQL equivalent")
            return exp.null()
        if len(args) == 2:  # PowerCenter's filter condition: SUM(AMOUNT, STATUS = 'X')
            value = exp.Case().when(args[1], exp.Literal.number(1) if isinstance(value, exp.Star) else value)
        cls = {"SUM": exp.Sum, "AVG": exp.Avg, "COUNT": exp.Count, "MAX": exp.Max, "MIN": exp.Min,
               "MEDIAN": exp.Median, "STDDEV": exp.Stddev, "VARIANCE": exp.Variance}.get(name)
        if cls is not None:
            return cls(this=value)
        if name in ("FIRST", "LAST"):
            self.note(REVIEW, f"{name}() depends on row order, which SQL doesn't keep; ANY_VALUE() is used")
            return exp.AnyValue(this=value)
        self.note(MANUAL, f"{name}() has no translation yet; a NULL placeholder stands in")
        return exp.null()

    def fn_iif(self, a):
        self.arity("IIF", a, 2, 3)
        if len(a) == 3:
            other = a[2]
        else:  # PowerCenter returns 0 or '' (by the type of the TRUE value) when the ELSE is left out
            other = {"number": exp.Literal.number(0), "string": exp.Literal.string("")}.get(
                self.kind(a[1]) or "", exp.null())
        return self.tagged(exp.Case().when(a[0], a[1]).else_(other), self.kind(a[1]) or self.kind(other))

    def fn_decode(self, a):
        self.arity("DECODE", a, 3, None)
        value, rest = a[0], a[1:]
        default = rest.pop() if len(rest) % 2 else None
        case = exp.Case()
        always = isinstance(value, exp.Boolean) and value.this
        for search, result in zip(rest[::2], rest[1::2]):
            cond = search if always else exp.EQ(this=_p(value), expression=_p(search))
            case = case.when(cond, result)
        if default is not None:
            case = case.else_(default)
        return self.tagged(case, self.kind(rest[1]))

    def fn_isnull(self, a):
        self.arity("ISNULL", a, 1, 1)
        return exp.Is(this=_p(a[0]), expression=exp.null())

    def fn_in(self, a):
        self.arity("IN", a, 2, None)
        values = a[1:]
        if len(values) > 1 and isinstance(values[-1], exp.Literal) and not values[-1].is_string \
                and values[-1].this in ("0", "1") and len(values) >= 2:
            # A trailing 0/1 is PowerCenter's case-sensitivity flag only when the other values are strings.
            if all(isinstance(v, exp.Literal) and v.is_string for v in values[:-1]):
                if values[-1].this == "0":
                    self.note(REVIEW, "IN(..., 0) compares case-insensitively in PowerCenter; "
                                      "this compares exactly")
                values = values[:-1]
        return exp.In(this=_p(a[0]), expressions=values)

    def fn_is_spaces(self, a):
        self.arity("IS_SPACES", a, 1, 1)
        return exp.EQ(this=exp.Trim(this=a[0]), expression=exp.Literal.string(""))

    def fn_is_number(self, a):
        self.arity("IS_NUMBER", a, 1, 1)
        return exp.Not(this=exp.Is(this=exp.TryCast(this=a[0], to=exp.DataType.build("DOUBLE")),
                                   expression=exp.null()))

    def fn_is_date(self, a):
        self.arity("IS_DATE", a, 1, 2)
        # A "try to parse" with a format doesn't survive neutral SQL (the safe flag is dropped), and
        # a failing parse would error instead of returning FALSE. Say so rather than guess.
        self.note(MANUAL, "IS_DATE() has no portable SQL equivalent yet (DuckDB: TRY_STRPTIME, "
                          "Spark: TRY_TO_TIMESTAMP); a NULL placeholder stands in")
        return exp.null()

    def trim(self, a, position: str | None, name: str):
        self.arity(name, a, 1, 2)
        kwargs = {"position": position} if position else {}
        if len(a) == 2:
            kwargs["expression"] = a[1]
        return self.tagged(exp.Trim(this=a[0], **kwargs), "string")

    def fn_ltrim(self, a):
        if len(a) == 1 and isinstance(a[0], exp.Trim) and a[0].args.get("position") == "TRAILING" \
                and not a[0].args.get("expression"):
            return self.tagged(exp.Trim(this=a[0].this), "string")  # LTRIM(RTRIM(x)) is TRIM(x)
        return self.trim(a, "LEADING", "LTRIM")

    def fn_rtrim(self, a):
        if len(a) == 1 and isinstance(a[0], exp.Trim) and a[0].args.get("position") == "LEADING" \
                and not a[0].args.get("expression"):
            return self.tagged(exp.Trim(this=a[0].this), "string")
        return self.trim(a, "TRAILING", "RTRIM")

    def fn_substr(self, a):
        self.arity("SUBSTR", a, 2, 3)
        start = a[1]
        if isinstance(start, exp.Literal) and start.this == "0":
            start = exp.Literal.number(1)  # PowerCenter treats a start of 0 as 1
        elif not (isinstance(start, exp.Literal) or (isinstance(start, exp.Neg) and isinstance(start.this, exp.Literal))):
            self.note(REVIEW, "SUBSTR treats a start position of 0 as 1; check computed start positions")
        return self.tagged(exp.Substring(this=a[0], start=start, length=a[2] if len(a) == 3 else None), "string")

    def fn_instr(self, a):
        self.arity("INSTR", a, 2, 4)
        if len(a) > 2:
            self.note(MANUAL, "INSTR with a start position or occurrence has no portable SQL equivalent")
            return exp.null()
        return exp.StrPosition(this=a[0], substr=a[1])

    def fn_concat(self, a):
        self.arity("CONCAT", a, 2, 2)
        return self.joined(a)

    def fn_replacestr(self, a):
        self.arity("REPLACESTR", a, 4, None)
        flag, subject, olds, new = a[0], a[1], a[2:-1], a[-1]
        if not (isinstance(flag, exp.Literal) and flag.this != "0"):
            self.note(REVIEW, "REPLACESTR with a case-insensitive flag is translated case-sensitively")
        e = subject
        for old in olds:
            e = exp.func("REPLACE", e, old, new)
        return self.tagged(e, "string")

    def fn_replacechr(self, a):
        self.arity("REPLACECHR", a, 4, 4)
        flag, subject, chars, new = a
        if not (isinstance(chars, exp.Literal) and chars.is_string):
            self.note(MANUAL, "REPLACECHR with computed characters has no translation yet")
            return exp.null()
        if not (isinstance(flag, exp.Literal) and flag.this != "0"):
            self.note(REVIEW, "REPLACECHR with a case-insensitive flag is translated case-sensitively")
        replacement = new if not (isinstance(new, exp.Null)) else exp.Literal.string("")
        if isinstance(replacement, exp.Literal) and len(replacement.this) > 1:
            replacement = exp.Literal.string(replacement.this[0])  # only the first character is used
        e = subject
        for ch in dict.fromkeys(chars.this):
            e = exp.func("REPLACE", e, exp.Literal.string(ch), replacement)
        return self.tagged(e, "string")

    def pad(self, a, name: str):
        self.arity(name, a, 2, 3)
        fill = a[2] if len(a) == 3 else exp.Literal.string(" ")
        return self.tagged(exp.func(name, a[0], a[1], fill), "string")

    def fn_lpad(self, a):
        return self.pad(a, "LPAD")

    def fn_rpad(self, a):
        return self.pad(a, "RPAD")

    def fn_initcap(self, a):
        self.arity("INITCAP", a, 1, 1)
        self.note(REVIEW, "INITCAP isn't available in every engine (DuckDB lacks it)")
        return self.tagged(exp.Initcap(this=a[0]), "string")

    # dates ------------------------------------------------------------------

    def date_format(self, fmt: exp.Expression | None, name: str) -> str:
        if fmt is None:
            return _strftime(_DEFAULT_DATE_FORMAT)
        if not (isinstance(fmt, exp.Literal) and fmt.is_string):
            raise ExpressionError(f"{name} needs a literal format string")
        return _strftime(fmt.this)

    def unit(self, fmt: exp.Expression, name: str) -> str:
        if isinstance(fmt, exp.Literal) and fmt.is_string and fmt.this.upper() in _DATE_UNITS:
            return _DATE_UNITS[fmt.this.upper()]
        raise ExpressionError(f"{name} needs a date part such as 'YYYY', 'MM' or 'DD'")

    def fn_to_date(self, a):
        self.arity("TO_DATE", a, 1, 2)
        fmt = self.date_format(a[1] if len(a) == 2 else None, "TO_DATE")
        return self.tagged(exp.StrToTime(this=a[0], format=exp.Literal.string(fmt)), "date")

    def fn_to_char(self, a):
        self.arity("TO_CHAR", a, 1, 2)
        if self.kind(a[0]) == "date" or len(a) == 2:
            fmt = self.date_format(a[1] if len(a) == 2 else None, "TO_CHAR")
            return self.tagged(exp.TimeToStr(this=a[0], format=exp.Literal.string(fmt)), "string")
        return self.tagged(exp.Cast(this=a[0], to=exp.DataType.build("TEXT")), "string")

    def fn_add_to_date(self, a):
        self.arity("ADD_TO_DATE", a, 3, 3)
        unit = self.unit(a[1], "ADD_TO_DATE")
        return self.tagged(exp.DateAdd(this=a[0], expression=a[2], unit=exp.var(unit)), "date")

    def fn_date_diff(self, a):
        self.arity("DATE_DIFF", a, 3, 3)
        unit = self.unit(a[2], "DATE_DIFF")
        if unit not in ("DAY",):
            self.note(REVIEW, "DATE_DIFF returns fractional units in PowerCenter; SQL counts whole "
                              "unit boundaries")
        return self.tagged(exp.DateDiff(this=a[0], expression=a[1], unit=exp.var(unit)), "number")

    def fn_get_date_part(self, a):
        self.arity("GET_DATE_PART", a, 2, 2)
        return self.tagged(exp.Extract(this=exp.var(self.unit(a[1], "GET_DATE_PART")), expression=a[0]),
                           "number")

    def fn_last_day(self, a):
        self.arity("LAST_DAY", a, 1, 1)
        return self.tagged(exp.LastDay(this=a[0]), "date")

    def fn_trunc(self, a):
        self.arity("TRUNC", a, 1, 2)
        if self.kind(a[0]) == "date" or (len(a) == 2 and isinstance(a[1], exp.Literal) and a[1].is_string):
            unit = self.unit(a[1], "TRUNC") if len(a) == 2 else "DAY"
            return self.tagged(exp.TimestampTrunc(this=a[0], unit=exp.var(unit)), "date")
        if len(a) == 2:
            self.note(REVIEW, "TRUNC(number, places) isn't portable; check the target engine")
            return exp.func("TRUNC", a[0], a[1])
        if self.kind(a[0]) is None:
            self.note(REVIEW, "TRUNC on a value of unknown type was treated as a number")
        return self.toward_zero(a[0])

    def fn_round(self, a):
        self.arity("ROUND", a, 1, 2)
        if self.kind(a[0]) == "date":
            self.note(MANUAL, "ROUND on a date has no portable SQL equivalent")
            return exp.null()
        return exp.Round(this=a[0], decimals=a[1] if len(a) == 2 else None)

    # conversions ------------------------------------------------------------

    def toward_zero(self, e: exp.Expression) -> exp.Expression:
        neg = exp.LT(this=_p(e), expression=exp.Literal.number(0))
        return self.tagged(exp.Case().when(neg, exp.Ceil(this=e)).else_(exp.Floor(this=e)), "number")

    def fn_to_integer(self, a):
        self.arity("TO_INTEGER", a, 1, 2)
        truncate = len(a) == 2 and not (isinstance(a[1], exp.Literal) and a[1].this == "0") \
            and not (isinstance(a[1], exp.Boolean) and not a[1].this)
        value = self.toward_zero(a[0]) if truncate else exp.Round(this=a[0])
        return self.tagged(exp.Cast(this=value, to=exp.DataType.build("BIGINT")), "number")

    def fn_to_bigint(self, a):
        return self.fn_to_integer(a)

    def fn_to_decimal(self, a):
        self.arity("TO_DECIMAL", a, 1, 2)
        scale = a[1].this if len(a) == 2 and isinstance(a[1], exp.Literal) else "10"
        return self.tagged(exp.Cast(this=a[0], to=exp.DataType.build(f"DECIMAL(38, {scale})")), "number")

    def fn_to_float(self, a):
        self.arity("TO_FLOAT", a, 1, 1)
        return self.tagged(exp.Cast(this=a[0], to=exp.DataType.build("DOUBLE")), "number")

    # rows that PowerCenter rejects -------------------------------------------

    def fn_error(self, a):
        self.note(REVIEW, "ERROR() rejects the row in PowerCenter; here the row is kept with a NULL")
        return exp.null()

    def fn_abort(self, a):
        self.note(MANUAL, "ABORT() stops the session in PowerCenter; add a data check instead")
        return exp.null()

    # regular expressions ------------------------------------------------------

    def fn_reg_match(self, a):
        self.arity("REG_MATCH", a, 2, 2)
        pattern = a[1]
        if isinstance(pattern, exp.Literal) and pattern.is_string:
            pattern = exp.Literal.string(f"^(?:{pattern.this})$")  # REG_MATCH matches the whole string
        else:
            self.note(REVIEW, "REG_MATCH matches the whole string; anchor the computed pattern with ^...$")
        return exp.RegexpLike(this=a[0], expression=pattern)

    def fn_reg_extract(self, a):
        self.arity("REG_EXTRACT", a, 2, 3)
        group = a[2] if len(a) == 3 else exp.Literal.number(1)
        self.note(REVIEW, "REG_EXTRACT patterns use Perl syntax; check them against the target engine")
        return self.tagged(exp.RegexpExtract(this=a[0], expression=a[1], group=group), "string")

    def fn_mod(self, a):
        self.arity("MOD", a, 2, 2)
        return exp.Mod(this=_p(a[0]), expression=_p(a[1]))


_SAME_NAME: dict[str, tuple[int, int | None]] = {
    "UPPER": (1, 1), "LOWER": (1, 1), "LENGTH": (1, 1), "ABS": (1, 1), "CEIL": (1, 1), "FLOOR": (1, 1),
    "SQRT": (1, 1), "POWER": (2, 2), "EXP": (1, 1), "LN": (1, 1), "SIGN": (1, 1), "REVERSE": (1, 1),
    "CHR": (1, 1), "ASCII": (1, 1),
}


def translate(
    text: str,
    resolve: Resolver,
    params: dict[str, tuple[str, str | None]] | None = None,
) -> Translation:
    """Translate one PowerCenter expression. Raises ExpressionError if it can't be read.

    ``resolve`` maps a port name to (SQL expression, kind); ``params`` maps mapping parameters
    such as ``$$RUN_DATE`` to (default value, kind).
    """
    parser = _Parser(text, resolve, {k.upper(): v for k, v in (params or {}).items()})
    sql = parser.parse()
    while isinstance(sql, exp.Paren):
        sql = sql.this
    return Translation(sql, parser.status, [n for n in parser.notes if n], parser.functions, parser.references)
