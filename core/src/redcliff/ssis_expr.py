"""Translate SSIS expressions (Derived Column, Conditional Split, variables) into neutral SQL.

SSIS expressions look like C: ``ISNULL(Region) ? "UNKNOWN" : UPPER(TRIM(Region))``,
``(DT_WSTR, 50)OrderId + "-" + Status``, ``DATEADD("dd", 1, OrderDate)``, ``@[User::FxRate]``.
This module parses them and builds sqlglot expressions with the same meaning:

- ``+`` on strings is concatenation (``||``); NULL in, NULL out, as in SSIS
- integer ``/`` integer is integer division, as in SSIS
- casts such as ``(DT_STR, 50, 1252)`` and ``(DT_NUMERIC, 12, 2)`` become ``CAST``
- ``ISNULL(x)`` is ``x IS NULL``; ``REPLACENULL(a, b)`` is ``COALESCE(a, b)``
- ``@[User::Name]`` becomes the pipeline variable ``${name}``

Every translation reports a status (converted, review or manual) with notes, like the
Informatica translator.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlglot import exp

from redcliff.scorecard import CONVERTED, REVIEW, worst

# resolve(name) -> (expression, kind) for a column, or None if unknown.
Resolver = Callable[[str], "tuple[exp.Expression, str | None] | None"]

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<num>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)[LlUuFf]*
  | (?P<lineage>\#\{[^}]*\})
  | (?P<var>@\[[^\]]*\]|@[A-Za-z_][A-Za-z0-9_:]*)
  | (?P<bracket>\[[^\]]*\])
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|!=|<=|>=|&&|\|\||[-+*/%<>!~?:(),&|^])
""", re.VERBOSE)

# Binary operators by precedence, loosest first.
_BINARY = [("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%")]

_UNITS = {
    "yy": "YEAR", "yyyy": "YEAR", "year": "YEAR", "qq": "QUARTER", "q": "QUARTER", "quarter": "QUARTER",
    "mm": "MONTH", "m": "MONTH", "month": "MONTH", "dy": "DAYOFYEAR", "y": "DAYOFYEAR", "dayofyear": "DAYOFYEAR",
    "dd": "DAY", "d": "DAY", "day": "DAY", "wk": "WEEK", "ww": "WEEK", "week": "WEEK", "dw": "DAYOFWEEK",
    "weekday": "DAYOFWEEK", "hh": "HOUR", "hour": "HOUR", "mi": "MINUTE", "n": "MINUTE", "minute": "MINUTE",
    "ss": "SECOND", "s": "SECOND", "second": "SECOND", "ms": "MILLISECOND", "millisecond": "MILLISECOND",
}


class SSISExpressionError(ValueError):
    """The expression can't be read or has no translation."""


@dataclass
class SSISTranslation:
    sql: exp.Expression
    kind: str | None
    status: str = CONVERTED
    notes: list[str] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)
    variables: dict[str, str] = field(default_factory=dict)  # pipeline variables used -> default value

    def text(self) -> str:
        return self.sql.sql()


def ssis_type(name: str, args: list[str] | None = None) -> tuple[str, str | None]:
    """(SQL type, kind) for an SSIS type name such as DT_WSTR or wstr."""
    t = name.upper().removeprefix("DT_")
    args = args or []
    if t in ("STR", "WSTR", "TEXT", "NTEXT", "GUID"):
        return "TEXT", "string"
    if t in ("I1", "I2", "I4", "UI1", "UI2", "UI4"):
        return "INT", "int"
    if t in ("I8", "UI8"):
        return "BIGINT", "int"
    if t in ("R4", "R8"):
        return "DOUBLE", "number"
    if t == "CY":
        return "DECIMAL(19, 4)", "number"
    if t == "NUMERIC":
        p, s = (args + ["18", "0"])[:2] if len(args) >= 2 else (args[0] if args else "18", "0")
        return f"DECIMAL({p}, {s})", "number"
    if t == "DECIMAL":
        return f"DECIMAL(38, {args[0] if args else '0'})", "number"
    if t in ("DBDATE",):
        return "DATE", "date"
    if t in ("DATE", "DBTIMESTAMP", "DBTIMESTAMP2", "DBTIMESTAMPOFFSET", "FILETIME"):
        return "TIMESTAMP", "date"
    if t in ("DBTIME", "DBTIME2"):
        return "TIME", "date"
    if t == "BOOL":
        return "BOOLEAN", "bool"
    raise SSISExpressionError(f"type {name} has no SQL equivalent")


def variable_name(qualified: str) -> str:
    """The pipeline variable for an SSIS variable: User::FxRate -> fx_rate."""
    name = qualified.split("::")[-1]
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "variable"


class _Parser:
    def __init__(self, text: str, resolve: Resolver, variables: dict[str, tuple[str, str | None]],
                 package: str | None):
        self.toks = self.tokenize(text)
        self.i = 0
        self.resolve = resolve
        self.vars = {k.upper(): v for k, v in variables.items()}  # User::Name -> (value, kind)
        self.package = package
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
                raise SSISExpressionError(f"unexpected character {text[pos]!r}")
            pos = m.end()
            if m.lastgroup != "ws":
                out.append((m.lastgroup, m.group(m.lastgroup)))
        return out

    def note(self, status: str, message: str) -> None:
        self.status = worst(self.status, status)
        if message not in self.notes:
            self.notes.append(message)

    # ---------------------------------------------------------------- token helpers

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise SSISExpressionError(f"expected {value!r}, got the end" if value else "unexpected end")
        if value is not None and tok[1] != value:
            raise SSISExpressionError(f"expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def at(self, *values: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] == "op" and tok[1] in values

    # ---------------------------------------------------------------- grammar

    def parse(self) -> tuple[exp.Expression, str | None]:
        e = self.conditional()
        if self.peek() is not None:
            raise SSISExpressionError(f"unexpected {self.peek()[1]!r}")
        return e

    def conditional(self) -> tuple[exp.Expression, str | None]:
        cond = self.binary(0)
        if not self.at("?"):
            return cond
        self.take("?")
        then = self.conditional()
        self.take(":")
        other = self.conditional()
        kind = then[1] if then[1] == other[1] else (then[1] or other[1])
        return exp.Case(ifs=[exp.If(this=_bool(cond[0]), true=then[0])], default=other[0]), kind

    def binary(self, level: int) -> tuple[exp.Expression, str | None]:
        if level == len(_BINARY):
            return self.unary()
        left = self.binary(level + 1)
        while self.at(*_BINARY[level]):
            op = self.take()[1]
            right = self.binary(level + 1)
            left = self.combine(op, left, right)
        return left

    def combine(self, op, left, right) -> tuple[exp.Expression, str | None]:
        (a, ka), (b, kb) = left, right
        if op == "||":
            return exp.Or(this=_p(_bool(a)), expression=_p(_bool(b))), "bool"
        if op == "&&":
            return exp.And(this=_p(_bool(a)), expression=_p(_bool(b))), "bool"
        if op in ("|", "^", "&"):
            cls = {"|": exp.BitwiseOr, "^": exp.BitwiseXor, "&": exp.BitwiseAnd}[op]
            return cls(this=_p(a), expression=_p(b)), "int"
        if op in ("==", "!=", "<", ">", "<=", ">="):
            cls = {"==": exp.EQ, "!=": exp.NEQ, "<": exp.LT, ">": exp.GT, "<=": exp.LTE, ">=": exp.GTE}[op]
            return cls(this=_p(a), expression=_p(b)), "bool"
        if op == "+":
            if "string" in (ka, kb):
                return exp.DPipe(this=_p(a), expression=_p(b), safe=False), "string"
            if ka == "date" or kb == "date":
                raise SSISExpressionError("adding to a date with + has no SQL equivalent; use DATEADD")
            return exp.Add(this=_p(a), expression=_p(b)), _num(ka, kb)
        if op == "-":
            return exp.Sub(this=_p(a), expression=_p(b)), _num(ka, kb)
        if op == "*":
            return exp.Mul(this=_p(a), expression=_p(b)), _num(ka, kb)
        if op == "/":
            if ka == kb == "int":
                # SSIS divides integers as integers, truncating toward zero. (a - a % b) / b is exact
                # in every engine, so the cast never rounds.
                exact = exp.Div(this=exp.Paren(this=exp.Sub(this=_p(a), expression=exp.Mod(this=_p(a), expression=_p(b)))),
                                expression=_p(b))
                return exp.Cast(this=exact, to=exp.DataType.build("BIGINT")), "int"
            return exp.Div(this=_p(a), expression=_p(b)), "number"
        if op == "%":
            return exp.Mod(this=_p(a), expression=_p(b)), "int"
        raise SSISExpressionError(f"operator {op} has no translation")

    def unary(self) -> tuple[exp.Expression, str | None]:
        if self.at("!"):
            self.take()
            e, _ = self.unary()
            return exp.Not(this=_p(_bool(e))), "bool"
        if self.at("-"):
            self.take()
            e, k = self.unary()
            return exp.Neg(this=_p(e)), k
        if self.at("~"):
            self.take()
            e, k = self.unary()
            return exp.BitwiseNot(this=_p(e)), k
        if self.at("(") and self.peek(1) and self.peek(1)[0] == "name" and self.peek(1)[1].upper().startswith("DT_"):
            return self.cast()
        return self.primary()

    def cast(self) -> tuple[exp.Expression, str | None]:
        self.take("(")
        name = self.take()[1]
        args = []
        while self.at(","):
            self.take(",")
            args.append(self.take()[1])
        self.take(")")
        sql_type, kind = ssis_type(name, args)
        e, k = self.unary()
        if kind == "string" and k == "date":
            self.note(REVIEW, f"({name}) of a date: SSIS writes 'yyyy-mm-dd hh:mm:ss[.nnn]'; check the engine's format")
        return exp.Cast(this=_p(e), to=exp.DataType.build(sql_type)), kind

    def primary(self) -> tuple[exp.Expression, str | None]:
        kind, value = self.take()
        if kind == "op" and value == "(":
            e = self.conditional()
            self.take(")")
            return exp.Paren(this=e[0]), e[1]
        if kind == "str":
            return exp.Literal.string(_unescape(value[1:-1])), "string"
        if kind == "num":
            text = re.sub(r"[LlUuFf]+$", "", value)
            return exp.Literal.number(text), "int" if re.fullmatch(r"\d+", text) else "number"
        if kind == "var":
            return self.variable(value)
        if kind == "lineage":
            raise SSISExpressionError("lineage references (#{...}); use the friendly expression")
        if kind in ("bracket", "name"):
            name = value[1:-1] if kind == "bracket" else value
            if kind == "name" and self.at("("):
                return self.function(name)
            upper = name.upper()
            if kind == "name" and upper in ("TRUE", "FALSE"):
                return exp.Boolean(this=upper == "TRUE"), "bool"
            resolved = self.resolve(name)
            if resolved is None:
                raise SSISExpressionError(f"unknown column {name}")
            e, k = resolved
            return e.copy(), k
        raise SSISExpressionError(f"unexpected {value!r}")

    def variable(self, token: str) -> tuple[exp.Expression, str | None]:
        name = token[2:-1] if token.startswith("@[") else token[1:]
        qualified = name if "::" in name else f"User::{name}"
        ns, _, short = qualified.partition("::")
        if ns.upper() == "SYSTEM":
            if short.upper() in ("STARTTIME", "CONTAINERSTARTTIME", "CREATIONDATE"):
                self.note(REVIEW, f"@[System::{short}] is when the package started; CURRENT_TIMESTAMP is when "
                                  "the query runs")
                return exp.CurrentTimestamp(), "date"
            if short.upper() == "PACKAGENAME" and self.package:
                return exp.Literal.string(self.package), "string"
            raise SSISExpressionError(f"system variable {short} has no pipeline equivalent")
        found = self.vars.get(qualified.upper())
        if found is None:
            raise SSISExpressionError(f"variable {qualified} isn't defined in the package")
        value, kind = found
        var = variable_name(qualified)
        self.used[var] = value
        text = f"${{{var}}}"
        if kind in ("int", "number"):
            return exp.Var(this=text), kind
        if kind == "bool":
            return exp.Var(this=text), "bool"
        if kind == "date":
            return exp.Cast(this=exp.Literal.string(text), to=exp.DataType.build("TIMESTAMP")), "date"
        return exp.Literal.string(text), "string"

    def args(self) -> list[tuple[exp.Expression, str | None]]:
        self.take("(")
        out = []
        if not self.at(")"):
            out.append(self.conditional())
            while self.at(","):
                self.take(",")
                out.append(self.conditional())
        self.take(")")
        return out

    def function(self, name: str) -> tuple[exp.Expression, str | None]:
        fn = name.upper()
        self.functions.add(fn)
        if fn == "NULL":  # NULL(DT_WSTR, 50)
            self.take("(")
            tname = self.take()[1]
            targs = []
            while self.at(","):
                self.take(",")
                targs.append(self.take()[1])
            self.take(")")
            sql_type, kind = ssis_type(tname, targs)
            return exp.Cast(this=exp.null(), to=exp.DataType.build(sql_type)), kind
        a = self.args()
        e = [x[0] for x in a]
        k = [x[1] for x in a]

        def need(lo: int, hi: int | None = None) -> None:
            hi = lo if hi is None else hi
            if not lo <= len(a) <= hi:
                raise SSISExpressionError(f"{fn} takes {lo if lo == hi else f'{lo} to {hi}'} argument(s), got {len(a)}")

        def unit(i: int) -> str:
            if not isinstance(e[i], exp.Literal) or not e[i].is_string:
                raise SSISExpressionError(f"{fn} needs a literal date part such as \"dd\"")
            part = e[i].this.lower()
            if part not in _UNITS:
                raise SSISExpressionError(f"{fn}: unknown date part {part!r}")
            return _UNITS[part]

        if fn == "ISNULL":
            need(1)
            return exp.Is(this=_p(e[0]), expression=exp.null()), "bool"
        if fn == "REPLACENULL":
            need(2)
            return exp.Coalesce(this=e[0], expressions=[e[1]]), k[0] or k[1]
        if fn in ("UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM"):
            need(1)
            cls = {"UPPER": exp.Upper, "LOWER": exp.Lower}.get(fn)
            if cls:
                return cls(this=e[0]), "string"
            side = {"LTRIM": "LEADING", "RTRIM": "TRAILING"}.get(fn)
            # SSIS trims spaces only, like SQL's TRIM.
            return (exp.Trim(this=e[0], position=side) if side else exp.Trim(this=e[0])), "string"
        if fn == "LEN":
            need(1)
            return exp.Length(this=e[0]), "int"
        if fn == "SUBSTRING":
            need(3)
            return exp.Substring(this=e[0], start=e[1], length=e[2]), "string"
        if fn in ("LEFT", "RIGHT"):
            need(2)
            return (exp.Left if fn == "LEFT" else exp.Right)(this=e[0], expression=e[1]), "string"
        if fn == "REPLACE":
            need(3)
            return exp.func("REPLACE", *e), "string"
        if fn == "REPLICATE":
            need(2)
            return exp.Repeat(this=e[0], times=e[1]), "string"
        if fn == "REVERSE":
            need(1)
            return exp.func("REVERSE", e[0]), "string"
        if fn == "FINDSTRING":
            need(3)
            if not (isinstance(e[2], exp.Literal) and e[2].this == "1"):
                raise SSISExpressionError("FINDSTRING for an occurrence other than 1 has no translation")
            return exp.StrPosition(this=e[0], substr=e[1]), "int"
        if fn in ("GETDATE", "GETUTCDATE"):
            need(0)
            if fn == "GETUTCDATE":
                self.note(REVIEW, "GETUTCDATE() is UTC; CURRENT_TIMESTAMP is in the engine's time zone")
            return exp.CurrentTimestamp(), "date"
        if fn == "DATEADD":
            need(3)
            return exp.DateAdd(this=e[2], expression=e[1], unit=exp.var(unit(0))), "date"
        if fn == "DATEDIFF":
            need(3)
            return exp.DateDiff(this=e[2], expression=e[1], unit=exp.var(unit(0))), "int"
        if fn == "DATEPART":
            need(2)
            return exp.Extract(this=exp.var(unit(0)), expression=e[1]), "int"
        if fn in ("YEAR", "MONTH", "DAY"):
            need(1)
            return {"YEAR": exp.Year, "MONTH": exp.Month, "DAY": exp.Day}[fn](this=e[0]), "int"
        if fn in ("ABS", "CEILING", "FLOOR", "SQRT", "SIGN", "EXP", "LN"):
            need(1)
            cls = {"ABS": exp.Abs, "CEILING": exp.Ceil, "FLOOR": exp.Floor, "SQRT": exp.Sqrt, "SIGN": exp.Sign,
                   "EXP": exp.Exp, "LN": exp.Ln}[fn]
            return cls(this=e[0]), k[0] if fn in ("ABS", "SIGN") else "number"
        if fn == "LOG":
            need(1)
            return exp.Log(this=exp.Literal.number(10), expression=e[0]), "number"
        if fn == "POWER":
            need(2)
            return exp.Pow(this=e[0], expression=e[1]), "number"
        if fn == "ROUND":
            need(2)
            return exp.Round(this=e[0], decimals=e[1]), k[0]
        if fn == "CODEPOINT":
            need(1)
            return exp.func("ASCII", e[0]), "int"
        raise SSISExpressionError(f"{fn}() has no translation")


def _p(e: exp.Expression) -> exp.Expression:
    return exp.Paren(this=e) if isinstance(e, (exp.Binary, exp.Case, exp.Not)) and not isinstance(e, exp.Paren) else e


def _bool(e: exp.Expression) -> exp.Expression:
    return e


def _num(a: str | None, b: str | None) -> str:
    return "int" if a == b == "int" else "number"


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), text)


def translate_ssis(text: str, resolve: Resolver, variables: dict[str, tuple[str, str | None]] | None = None,
                   package: str | None = None) -> SSISTranslation:
    """Translate one SSIS expression. Raises SSISExpressionError if it can't be read or translated.

    ``variables`` maps ``User::Name`` to (default value, kind); each use becomes ``${name}``.
    """
    parser = _Parser(text, resolve, variables or {}, package)
    sql, kind = parser.parse()
    while isinstance(sql, exp.Paren):
        sql = sql.this
    return SSISTranslation(sql, kind, parser.status, parser.notes, parser.functions, parser.used)
