"""Translate IBM DataStage Transformer expressions (derivations, constraints, stage variables) into neutral SQL.

DataStage expressions are a BASIC dialect: ``If IsNull(lnk.REGION) Then "UNKNOWN" Else UpCase(lnk.REGION)``,
``Trim(lnk.FIRST_NAME) : " " : lnk.LAST_NAME``, ``DateToString(lnk.ORDER_DATE, "%yyyy-%mm")``,
``lnk.AMOUNT * FxRate`` (a job parameter), ``svTotal`` (a stage variable). This module parses that
subset and builds sqlglot expressions with the same meaning:

- ``:`` is concatenation; ``=``, ``<>``, ``#`` and ``And``/``Or``/``Not`` are SQL's comparisons and logic
- ``If ... Then ... Else`` is ``CASE``
- ``s[start, length]`` is ``SUBSTRING``
- ``NullToValue``, ``NullToZero``, ``IsNull``, ``SetNull`` handle NULLs
- date functions take DataStage formats (``%yyyy-%mm-%dd``)
- a job parameter becomes the pipeline variable ``${name}``

Where DataStage and SQL differ in an edge case the translation is marked for review, and what has no
translation raises :class:`DataStageExpressionError`, so the importer puts a placeholder in and says why.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlglot import exp

from redcliff.scorecard import CONVERTED, REVIEW, worst

# resolve(link, column) -> (expression, kind); link is None for a bare name (a stage variable).
Resolver = Callable[["str | None", str], "tuple[exp.Expression, str | None] | None"]

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<num>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)
  | (?P<param>\#[A-Za-z_][A-Za-z0-9_.$]*\#)
  | (?P<sys>@[A-Za-z_]+)
  | (?P<name>[A-Za-z_$][A-Za-z0-9_$]*)
  | (?P<op><>|><|<=|=<|>=|=>|\*\*|[-+*/^:=<>#(),.\[\]])
""", re.VERBOSE)

_KEYWORDS = {"IF", "THEN", "ELSE", "AND", "OR", "NOT"}
# ODBC SQL types, as DataStage stores them, -> (SQL type, kind)
SQL_TYPES = {
    "1": ("TEXT", "string"), "12": ("TEXT", "string"), "-1": ("TEXT", "string"), "-8": ("TEXT", "string"),
    "-9": ("TEXT", "string"), "-10": ("TEXT", "string"),
    "4": ("INT", "int"), "5": ("SMALLINT", "int"), "-6": ("TINYINT", "int"), "-5": ("BIGINT", "int"),
    "-7": ("BOOLEAN", "bool"),
    "6": ("DOUBLE", "number"), "7": ("FLOAT", "number"), "8": ("DOUBLE", "number"),
    "9": ("DATE", "date"), "10": ("TIME", "date"), "11": ("TIMESTAMP", "date"),
}
_DATE_PARTS = [("%yyyy", "%Y"), ("%yy", "%y"), ("%mmmm", "%B"), ("%mmm", "%b"), ("%mm", "%m"), ("%dd", "%d"),
               ("%ddd", "%j"), ("%hh", "%H"), ("%nn", "%M"), ("%ss", "%S"), ("%eeee", "%A"), ("%eee", "%a")]
NUMERIC = ("int", "number", "decimal")


class DataStageExpressionError(ValueError):
    """The expression can't be read or has no translation."""


@dataclass
class DataStageTranslation:
    sql: exp.Expression
    kind: str | None
    status: str = CONVERTED
    notes: list[str] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)
    variables: dict[str, str] = field(default_factory=dict)  # pipeline variables used -> default value

    def text(self) -> str:
        return self.sql.sql()


def sql_type(code: str | int | None, precision: str | int | None = None, scale: str | int | None = None
             ) -> tuple[str, str | None]:
    """(SQL type, kind) for a DataStage column's SqlType code (ODBC), precision and scale."""
    code = str(code if code is not None else "12").strip()
    if code in ("2", "3"):
        try:
            p, s = int(precision or 0), int(scale or 0)
        except ValueError:
            p, s = 0, 0
        return (f"DECIMAL({p}, {s})" if 0 < p <= 38 and 0 <= s <= p else "DECIMAL(38, 10)"), "decimal"
    if code in SQL_TYPES:
        return SQL_TYPES[code]
    raise DataStageExpressionError(f"SQL type {code} has no equivalent")


def ds_format(fmt: str) -> str:
    """A DataStage date format (``%yyyy-%mm-%dd``) as a strftime format (``%Y-%m-%d``)."""
    out, i = [], 0
    while i < len(fmt):
        if fmt[i] == "%":
            for ds, py in sorted(_DATE_PARTS, key=lambda p: -len(p[0])):
                if fmt.startswith(ds, i):
                    out.append(py)
                    i += len(ds)
                    break
            else:
                raise DataStageExpressionError(f"date format part {fmt[i:i + 5]!r} in {fmt!r} has no SQL equivalent")
            continue
        out.append(fmt[i])
        i += 1
    return "".join(out)


def param_name(name: str) -> str:
    """The pipeline variable for a job parameter: MinAmount -> min_amount, ps_sales.Region -> ps_sales_region."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.strip("#"))
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "parameter"


def _p(e: exp.Expression) -> exp.Expression:
    wrap = isinstance(e, (exp.Binary, exp.Not, exp.Case, exp.Connector)) and not isinstance(e, exp.Dot)
    return exp.Paren(this=e) if wrap else e


_LOGIC = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is, exp.Not, exp.And, exp.Or)


def _pc(e: exp.Expression) -> exp.Expression:
    return exp.Paren(this=e) if isinstance(e, _LOGIC) else e


def _num(a: str | None, b: str | None) -> str:
    if "decimal" in (a, b):
        return "decimal"
    return "int" if a == b == "int" else "number"


class _Parser:
    def __init__(self, text: str, resolve: Resolver, params: dict[str, tuple[str, str | None]]):
        self.toks = self.tokenize(text)
        self.i = 0
        self.resolve = resolve
        self.params = {k.upper(): (k, v) for k, v in params.items()}
        self.status = CONVERTED
        self.notes: list[str] = []
        self.functions: set[str] = set()
        self.used: dict[str, str] = {}

    @staticmethod
    def tokenize(text: str) -> list[tuple[str, str]]:
        out, pos = [], 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m:
                raise DataStageExpressionError(f"unexpected character {text[pos]!r}")
            pos = m.end()
            if m.lastgroup != "ws":
                out.append((m.lastgroup, m.group(m.lastgroup)))
        return out

    def note(self, status: str, message: str) -> None:
        self.status = worst(self.status, status)
        if message not in self.notes:
            self.notes.append(message)

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise DataStageExpressionError("the expression ends too soon")
        if value is not None and tok[1].upper() != value.upper():
            raise DataStageExpressionError(f"expected {value!r}, found {tok[1]!r}")
        self.i += 1
        return tok

    def at(self, *values: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] in ("op", "name") and tok[1].upper() in {v.upper() for v in values}

    # ---------------------------------------------------------------- grammar

    def parse(self) -> tuple[exp.Expression, str | None]:
        e = self.expr()
        if self.peek() is not None:
            raise DataStageExpressionError(f"unexpected {self.peek()[1]!r}")
        return e

    def expr(self) -> tuple[exp.Expression, str | None]:
        if self.at("If"):
            self.take()
            cond, _k = self.expr()
            self.take("Then")
            then = self.expr()
            self.take("Else")
            other = self.expr()
            kind = then[1] if then[1] == other[1] else (then[1] or other[1])
            if then[1] in NUMERIC and other[1] in NUMERIC:
                kind = _num(then[1], other[1])
            return exp.Case(ifs=[exp.If(this=cond, true=then[0])], default=other[0]), kind
        return self.disjunction()

    def disjunction(self) -> tuple[exp.Expression, str | None]:
        left = self.conjunction()
        while self.at("Or"):
            self.take()
            right = self.conjunction()
            left = exp.Or(this=left[0], expression=right[0]), "bool"
        return left

    def conjunction(self) -> tuple[exp.Expression, str | None]:
        left = self.negation()
        while self.at("And"):
            self.take()
            right = self.negation()
            wrap = lambda x: exp.Paren(this=x) if isinstance(x, exp.Or) else x  # noqa: E731
            left = exp.And(this=wrap(left[0]), expression=wrap(right[0])), "bool"
        return left

    def negation(self) -> tuple[exp.Expression, str | None]:
        if self.at("Not") and not (self.peek(1) and self.peek(1)[1] == "("):
            self.take()
            e, _k = self.negation()
            return exp.Not(this=_p(e)), "bool"
        return self.comparison()

    def comparison(self) -> tuple[exp.Expression, str | None]:
        left = self.concat()
        ops = {"=": exp.EQ, "<>": exp.NEQ, "#": exp.NEQ, "><": exp.NEQ, "<": exp.LT, ">": exp.GT, "<=": exp.LTE,
               "=<": exp.LTE, ">=": exp.GTE, "=>": exp.GTE}
        if self.peek() and self.peek()[0] == "op" and self.peek()[1] in ops:
            op = self.take()[1]
            right = self.concat()
            return ops[op](this=_pc(left[0]), expression=_pc(right[0])), "bool"
        return left

    def concat(self) -> tuple[exp.Expression, str | None]:
        left = self.additive()
        while self.at(":"):
            self.take()
            right = self.additive()
            parts = [x if k == "string" else exp.Cast(this=_p(x), to=exp.DataType.build("TEXT"))
                     for x, k in (left, right)]
            if any(k in NUMERIC for _x, k in (left, right)):
                self.note(REVIEW, "a number joined with ':' is written the way DataStage formats it (decimals keep "
                                  "their scale, with leading zeros by default); CAST AS TEXT writes it plainly")
            first = parts[0] if isinstance(parts[0], exp.DPipe) else _p(parts[0])
            left = exp.DPipe(this=first, expression=_p(parts[1]), safe=False), "string"
        return left

    def additive(self) -> tuple[exp.Expression, str | None]:
        left = self.term()
        while self.at("+", "-"):
            op = self.take()[1]
            right = self.term()
            if "date" in (left[1], right[1]):
                raise DataStageExpressionError("date arithmetic with + or - has no translation; use DateFromDaysSince")
            cls = exp.Add if op == "+" else exp.Sub
            left = cls(this=_p(left[0]), expression=_p(right[0])), _num(left[1], right[1])
        return left

    def term(self) -> tuple[exp.Expression, str | None]:
        left = self.power()
        while self.at("*", "/"):
            op = self.take()[1]
            right = self.power()
            if op == "*":
                left = exp.Mul(this=_p(left[0]), expression=_p(right[0])), _num(left[1], right[1])
            else:  # DataStage divides as decimals or floats, never truncating
                left = exp.Div(this=_p(left[0]), expression=_p(right[0])), \
                    "decimal" if "decimal" in (left[1], right[1]) else "number"
        return left

    def power(self) -> tuple[exp.Expression, str | None]:
        base = self.unary()
        if self.at("^", "**"):
            self.take()
            exponent = self.unary()
            return exp.Pow(this=_p(base[0]), expression=_p(exponent[0])), "number"
        return base

    def unary(self) -> tuple[exp.Expression, str | None]:
        if self.at("-"):
            self.take()
            e, k = self.unary()
            return exp.Neg(this=_p(e)), k
        if self.at("+"):
            self.take()
            return self.unary()
        return self.substring(self.primary())

    def substring(self, value: tuple[exp.Expression, str | None]) -> tuple[exp.Expression, str | None]:
        while self.at("["):
            self.take("[")
            first = self.expr()
            if not self.at(","):
                raise DataStageExpressionError("s[n] (the last n characters) has no translation; use Right(s, n)")
            self.take(",")
            length = self.expr()
            self.take("]")
            self.functions.add("[start, length]")
            value = exp.Substring(this=value[0], start=first[0], length=length[0]), "string"
        return value

    def primary(self) -> tuple[exp.Expression, str | None]:
        kind, value = self.take()
        if kind == "op" and value == "(":
            e = self.expr()
            self.take(")")
            return exp.Paren(this=e[0]), e[1]
        if kind == "str":
            return exp.Literal.string(value[1:-1]), "string"
        if kind == "num":
            return exp.Literal.number(value), "int" if re.fullmatch(r"\d+", value) else "number"
        if kind == "param":
            return self.parameter(value.strip("#"))
        if kind == "sys":
            return self.system(value.upper())
        if kind != "name" or value.upper() in _KEYWORDS - {"NOT"}:
            raise DataStageExpressionError(f"unexpected {value!r}")
        if self.at("("):
            return self.function(value, self.args())
        if self.at(".") and self.peek(1) and self.peek(1)[0] == "name":
            self.take(".")
            col = self.take()[1]
            found = self.resolve(value, col)
            if found is None:
                if value.upper() in self.params or value.lower().startswith("ps_"):  # a parameter set's value
                    return self.parameter(f"{value}.{col}")
                raise DataStageExpressionError(f"unknown column {value}.{col}")
            return found[0].copy(), found[1]
        found = self.resolve(None, value)
        if found is not None:
            return found[0].copy(), found[1]
        if value.upper() in self.params:
            return self.parameter(value)
        raise DataStageExpressionError(f"unknown name {value}")

    def parameter(self, name: str) -> tuple[exp.Expression, str | None]:
        hit = self.params.get(name.upper())
        if hit is None:
            raise DataStageExpressionError(f"job parameter {name} isn't defined in the job")
        original, (value, kind) = hit
        var = param_name(original)
        self.used[var] = value
        text = f"${{{var}}}"
        if kind in NUMERIC or kind == "bool":
            return exp.Var(this=text), kind
        if kind == "date":
            return exp.Cast(this=exp.Literal.string(text), to=exp.DataType.build("DATE")), "date"
        return exp.Literal.string(text), "string"

    def system(self, name: str) -> tuple[exp.Expression, str | None]:
        if name in ("@INROWNUM", "@OUTROWNUM"):
            self.note(REVIEW, f"{name} numbers rows per partition in the order they arrive; ROW_NUMBER() needs an "
                              "order and runs over all rows")
            return exp.Window(this=exp.RowNumber()), "int"
        if name in ("@DATE", "@TIME"):
            self.note(REVIEW, f"{name} is when the job started; CURRENT_DATE is when the query runs")
            return (exp.CurrentDate() if name == "@DATE" else exp.CurrentTimestamp()), "date"
        if name == "@NULL":
            return exp.null(), None
        if name in ("@TRUE", "@FALSE"):
            return exp.Boolean(this=name == "@TRUE"), "bool"
        raise DataStageExpressionError(f"system variable {name} has no translation")

    def args(self) -> list[tuple[exp.Expression, str | None]]:
        self.take("(")
        out = []
        if not self.at(")"):
            out.append(self.expr())
            while self.at(","):
                self.take(",")
                out.append(self.expr())
        self.take(")")
        return out

    def function(self, name: str, args) -> tuple[exp.Expression, str | None]:
        fn = name.upper()
        self.functions.add(name)
        a = [e for e, _k in args]
        k = [kind for _e, kind in args]
        n = len(a)

        def need(*counts: int) -> None:
            if n not in counts:
                raise DataStageExpressionError(f"{name} with {n} arguments has no translation")

        def lit(i: int) -> str:
            if not (isinstance(a[i], exp.Literal) and a[i].is_string):
                raise DataStageExpressionError(f"{name} needs a literal format")
            return a[i].this

        if fn == "TRIM":
            need(1)
            self.note(REVIEW, "Trim also turns runs of spaces inside the string into one space; TRIM only trims the ends")
            return exp.Trim(this=a[0]), "string"
        if fn in ("TRIMLEADINGTRAILING",):
            need(1)
            return exp.Trim(this=a[0]), "string"
        if fn in ("TRIMF", "TRIMB"):
            need(1)
            return exp.Trim(this=a[0], position="LEADING" if fn == "TRIMF" else "TRAILING"), "string"
        if fn in ("UPCASE", "DOWNCASE"):
            need(1)
            return (exp.Upper if fn == "UPCASE" else exp.Lower)(this=a[0]), "string"
        if fn == "LEN":
            need(1)
            return exp.Length(this=a[0]), "int"
        if fn in ("LEFT", "RIGHT"):
            need(2)
            return (exp.Left if fn == "LEFT" else exp.Right)(this=a[0], expression=a[1]), "string"
        if fn == "INDEX":
            need(3)
            if not (isinstance(a[2], exp.Literal) and a[2].this == "1"):
                raise DataStageExpressionError("Index for an occurrence other than the first has no translation")
            return exp.StrPosition(this=a[0], substr=a[1]), "int"
        if fn == "FIELD":
            need(3)
            return exp.SplitPart(this=a[0], delimiter=a[1], part_index=a[2]), "string"
        if fn in ("EREPLACE", "CHANGE"):
            need(3)
            return exp.Anonymous(this="REPLACE", expressions=a), "string"
        if fn == "STR":
            need(2)
            return exp.Repeat(this=a[0], times=a[1]), "string"
        if fn == "SPACE":
            need(1)
            return exp.Repeat(this=exp.Literal.string(" "), times=a[0]), "string"
        if fn == "ISNULL":
            need(1)
            return exp.Is(this=_pc(a[0]), expression=exp.null()), "bool"
        if fn == "ISNOTNULL":
            need(1)
            return exp.Not(this=exp.Is(this=_pc(a[0]), expression=exp.null())), "bool"
        if fn == "NOT":
            need(1)
            return exp.Not(this=_p(a[0])), "bool"
        if fn == "NULLTOVALUE":
            need(2)
            return exp.Coalesce(this=a[0], expressions=[a[1]]), k[0] or k[1]
        if fn in ("NULLTOZERO", "NULLTOEMPTY"):
            need(1)
            fill = exp.Literal.number(0) if fn == "NULLTOZERO" else exp.Literal.string("")
            return exp.Coalesce(this=a[0], expressions=[fill]), k[0]
        if fn == "SETNULL":
            need(0)
            return exp.null(), None
        if fn in ("STRINGTODECIMAL", "ASDOUBLE", "ASFLOAT", "ASINTEGER", "STRINGTOINTEGER"):
            need(1, 2)
            if fn in ("ASINTEGER", "STRINGTOINTEGER"):
                return exp.Cast(this=exp.Trunc(this=exp.Cast(this=_p(a[0]), to=exp.DataType.build("DOUBLE"))),
                                to=exp.DataType.build("BIGINT")), "int"
            t = ("DECIMAL(38, 10)", "decimal") if fn == "STRINGTODECIMAL" else ("DOUBLE", "number")
            return exp.Cast(this=_p(a[0]), to=exp.DataType.build(t[0])), t[1]
        if fn in ("DECIMALTOSTRING", "NUMTOSTRING"):
            need(1, 2)
            self.note(REVIEW, f"{name} pads with leading zeros and a sign by default; CAST AS TEXT writes the number plainly")
            return exp.Cast(this=_p(a[0]), to=exp.DataType.build("TEXT")), "string"
        if fn == "DATETOSTRING":
            need(1, 2)
            fmt = ds_format(lit(1)) if n == 2 else "%Y-%m-%d"
            return exp.TimeToStr(this=a[0], format=exp.Literal.string(fmt)), "string"
        if fn == "TIMESTAMPTOSTRING":
            need(1, 2)
            fmt = ds_format(lit(1)) if n == 2 else "%Y-%m-%d %H:%M:%S"
            return exp.TimeToStr(this=a[0], format=exp.Literal.string(fmt)), "string"
        if fn == "STRINGTODATE":
            need(1, 2)
            fmt = ds_format(lit(1)) if n == 2 else "%Y-%m-%d"
            return exp.StrToDate(this=a[0], format=exp.Literal.string(fmt)), "date"
        if fn == "STRINGTOTIMESTAMP":
            need(1, 2)
            fmt = ds_format(lit(1)) if n == 2 else "%Y-%m-%d %H:%M:%S"
            return exp.StrToTime(this=a[0], format=exp.Literal.string(fmt)), "date"
        if fn in ("CURRENTDATE", "CURRENTTIMESTAMP", "CURRENTTIME"):
            need(0)
            self.note(REVIEW, f"{name}() is when the row is processed; the SQL value is when the query runs")
            return (exp.CurrentDate() if fn == "CURRENTDATE" else exp.CurrentTimestamp()), "date"
        parts = {"YEARFROMDATE": "YEAR", "MONTHFROMDATE": "MONTH", "MONTHDAYFROMDATE": "DAY",
                 "YEARDAYFROMDATE": "DAYOFYEAR", "WEEKDAYFROMDATE": "DAYOFWEEK", "HOURSFROMTIME": "HOUR",
                 "MINUTESFROMTIME": "MINUTE", "SECONDSFROMTIME": "SECOND"}
        if fn in parts:
            need(1)
            if fn == "WEEKDAYFROMDATE":
                self.note(REVIEW, "WeekdayFromDate counts from Sunday = 0 by default; check the engine's DAYOFWEEK")
            return exp.Extract(this=exp.var(parts[fn]), expression=a[0]), "int"
        if fn == "DAYSSINCEFROMDATE":
            need(2)
            return exp.DateDiff(this=a[0], expression=a[1], unit=exp.var("DAY")), "int"
        if fn == "DATEFROMDAYSSINCE":
            need(2)
            return exp.DateAdd(this=a[1], expression=a[0], unit=exp.var("DAY")), "date"
        if fn == "ABS":
            need(1)
            return exp.Abs(this=a[0]), k[0]
        if fn == "MOD":
            need(2)
            return exp.Mod(this=_p(a[0]), expression=_p(a[1])), _num(k[0], k[1])
        if fn == "INT":
            need(1)
            return exp.Cast(this=exp.Trunc(this=a[0]), to=exp.DataType.build("BIGINT")), "int"
        if fn in ("SQRT", "EXP", "LN"):
            need(1)
            cls = {"SQRT": exp.Sqrt, "EXP": exp.Exp, "LN": exp.Ln}[fn]
            return cls(this=a[0]), "number"
        if fn in ("CEIL", "FLOOR"):
            need(1)
            return (exp.Ceil if fn == "CEIL" else exp.Floor)(this=a[0]), "number"
        raise DataStageExpressionError(f"{name}() has no translation")


def translate_datastage(text: str, resolve: Resolver, params: dict[str, tuple[str, str | None]] | None = None
                        ) -> DataStageTranslation:
    """Translate one DataStage Transformer expression."""
    p = _Parser(text, resolve, params or {})
    e, kind = p.parse()
    return DataStageTranslation(e, kind, p.status, p.notes, p.functions, p.used)


def substitute_params(text: str, params: dict[str, tuple[str, str | None]]) -> tuple[str, dict[str, str]]:
    """``#Param#`` in a property (a file name, a where clause) as ``${param}``; returns (text, variables used)."""
    used: dict[str, str] = {}
    known = {k.upper(): k for k in params}

    def sub(m: re.Match) -> str:
        name = m.group(1)
        original = known.get(name.upper())
        if original is None:
            raise DataStageExpressionError(f"job parameter {name} isn't defined in the job")
        var = param_name(original)
        used[var] = params[original][0]
        return f"${{{var}}}"

    return re.sub(r"#([A-Za-z_][A-Za-z0-9_.$]*)#", sub, text or ""), used
