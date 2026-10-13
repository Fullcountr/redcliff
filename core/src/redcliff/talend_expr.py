"""Translate Talend's Java expressions (tMap, tFilterRow, tJoin) into neutral SQL.

Talend expressions are Java: ``row1.status.equals("shipped")``, ``row2.first_name + " " + row2.last_name``,
``row1.amount == null ? 0 : row1.amount``, ``StringHandling.UPCASE(row2.region)``,
``TalendDate.formatDate("yyyy-MM", row1.order_date)``, ``context.min_amount``. This module parses that
subset of Java and builds sqlglot expressions with the same meaning:

- ``+`` on strings is concatenation (``||``)
- ``x == null`` is ``x IS NULL``; ``a.equals(b)`` is ``a = b``
- String methods (``trim``, ``toUpperCase``, ``substring``, ``contains``, ...), ``Math``, the Talend
  routines (``StringHandling``, ``TalendDate``, ``Relational``, ``Mathematical``) and BigDecimal
  arithmetic (``add``, ``multiply``, ``compareTo``) become SQL functions and operators
- integer ``/`` integer is integer division, as in Java
- ``context.name`` becomes the pipeline variable ``${name}``

Where Java and SQL differ in an edge case the translation is marked for review with a note, and
what has no translation raises :class:`TalendExpressionError`, so the importer puts a placeholder in
and says why.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlglot import exp

from redcliff.scorecard import CONVERTED, REVIEW, worst

# resolve(row, column) -> (expression, kind) for a column of an input row, or None if unknown.
Resolver = Callable[[str, str], "tuple[exp.Expression, str | None] | None"]

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<chr>'(?:[^'\\]|\\.)')
  | (?P<num>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)(?P<suffix>[LlDdFf]?)
  | (?P<name>[A-Za-z_$][A-Za-z0-9_$]*)
  | (?P<op>==|!=|<=|>=|&&|\|\||[-+*/%<>!?:(),.])
""", re.VERBOSE)

_BINARY = [("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%")]

# Talend type ids -> (SQL type, kind)
TYPES = {
    "id_String": ("TEXT", "string"), "id_Character": ("TEXT", "string"),
    "id_Integer": ("INT", "int"), "id_Short": ("SMALLINT", "int"), "id_Byte": ("TINYINT", "int"),
    "id_Long": ("BIGINT", "int"),
    "id_Double": ("DOUBLE", "number"), "id_Float": ("FLOAT", "number"),
    "id_BigDecimal": ("DECIMAL(38, 10)", "decimal"),
    "id_Date": ("TIMESTAMP", "date"), "id_Boolean": ("BOOLEAN", "bool"),
}
_JAVA_TYPES = {
    "String": ("TEXT", "string"), "Integer": ("INT", "int"), "int": ("INT", "int"), "Long": ("BIGINT", "int"),
    "long": ("BIGINT", "int"), "Short": ("SMALLINT", "int"), "short": ("SMALLINT", "int"),
    "Double": ("DOUBLE", "number"), "double": ("DOUBLE", "number"), "Float": ("FLOAT", "number"),
    "float": ("FLOAT", "number"), "BigDecimal": ("DECIMAL(38, 10)", "decimal"),
    "Boolean": ("BOOLEAN", "bool"), "boolean": ("BOOLEAN", "bool"), "Date": ("TIMESTAMP", "date"),
}
_DATE_PARTS = {
    "yyyy": "%Y", "yy": "%y", "MMMM": "%B", "MMM": "%b", "MM": "%m", "dd": "%d", "HH": "%H", "hh": "%I",
    "mm": "%M", "ss": "%S", "SSS": "%f", "a": "%p", "EEEE": "%A", "EEE": "%a", "D": "%j",
}
_UNITS = {"yyyy": "YEAR", "yy": "YEAR", "y": "YEAR", "MM": "MONTH", "M": "MONTH", "dd": "DAY", "d": "DAY",
          "DD": "DAY", "HH": "HOUR", "hh": "HOUR", "mm": "MINUTE", "ss": "SECOND", "SSS": "MILLISECOND",
          "ww": "WEEK", "YEAR": "YEAR", "MONTH": "MONTH", "DAY_OF_MONTH": "DAY", "DAY": "DAY",
          "HOUR": "HOUR", "MINUTE": "MINUTE", "SECOND": "SECOND", "DAY_OF_YEAR": "DAYOFYEAR",
          "DAY_OF_WEEK": "DAYOFWEEK", "WEEK_OF_YEAR": "WEEK"}
NUMERIC = ("int", "number", "decimal")


class TalendExpressionError(ValueError):
    """The expression can't be read or has no translation."""


@dataclass
class TalendTranslation:
    sql: exp.Expression
    kind: str | None
    status: str = CONVERTED
    notes: list[str] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)
    variables: dict[str, str] = field(default_factory=dict)  # pipeline variables used -> default value

    def text(self) -> str:
        return self.sql.sql()


def talend_type(type_id: str, length: str | int | None = None, precision: str | int | None = None,
                pattern: str | None = None) -> tuple[str, str | None]:
    """(SQL type, kind) for a Talend column type such as id_BigDecimal with its length and precision."""
    if type_id == "id_BigDecimal":
        try:
            p, s = int(length or 0), int(precision or 0)
        except ValueError:
            p, s = 0, 0
        if 0 < p <= 38 and 0 <= s <= p:
            return f"DECIMAL({p}, {s})", "decimal"
        return "DECIMAL(38, 10)", "decimal"
    if type_id == "id_Date":
        fmt = (pattern or "").strip('"')
        if fmt and not re.search(r"[HhmsSa]", fmt):
            return "DATE", "date"
        return "TIMESTAMP", "date"
    if type_id in TYPES:
        return TYPES[type_id]
    raise TalendExpressionError(f"type {type_id} has no SQL equivalent")


def java_format(pattern: str) -> str:
    """A Java date pattern (``yyyy-MM-dd HH:mm``) as a strftime format (``%Y-%m-%d %H:%M``)."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "'":
            j = pattern.find("'", i + 1)
            if j < 0:
                raise TalendExpressionError(f"unclosed quote in date pattern {pattern!r}")
            out.append(pattern[i + 1:j].replace("%", "%%") or "'")
            i = j + 1
            continue
        if ch.isalpha():
            j = i
            while j < len(pattern) and pattern[j] == ch:
                j += 1
            run = pattern[i:j]
            if run not in _DATE_PARTS:
                raise TalendExpressionError(f"date pattern part {run!r} in {pattern!r} has no SQL equivalent")
            out.append(_DATE_PARTS[run])
            i = j
            continue
        out.append("%%" if ch == "%" else ch)
        i += 1
    return "".join(out)


def _p(e: exp.Expression) -> exp.Expression:
    wrap = isinstance(e, (exp.Binary, exp.Not, exp.Case, exp.Connector)) and not isinstance(e, exp.Dot)
    return exp.Paren(this=e) if wrap else e


_LOGIC = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is, exp.Not, exp.And, exp.Or, exp.Like)


def _pc(e: exp.Expression) -> exp.Expression:
    """Parentheses for an operand of a comparison: only around other conditions."""
    return exp.Paren(this=e) if isinstance(e, _LOGIC) else e


def _num(a: str | None, b: str | None) -> str:
    if "decimal" in (a, b):
        return "decimal"
    return "int" if a == b == "int" else "number"


def _cmp(e: exp.Expression) -> bool:
    return isinstance(e, exp.Anonymous) and e.name == "__COMPARE__"


class _Parser:
    def __init__(self, text: str, resolve: Resolver, rows: set[str], context: dict[str, tuple[str, str | None]]):
        self.toks = self.tokenize(text)
        self.i = 0
        self.resolve = resolve
        self.rows = rows
        self.context = context  # name -> (default value, kind)
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
                raise TalendExpressionError(f"unexpected character {text[pos]!r}")
            pos = m.end()
            kind = m.lastgroup if m.lastgroup != "suffix" else "num"
            if kind == "ws":
                continue
            if m.group("num") is not None:
                out.append(("num", m.group("num") + (m.group("suffix") or "")))
            else:
                out.append((kind, m.group(kind)))
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
            raise TalendExpressionError("the expression ends too soon")
        if value is not None and tok[1] != value:
            raise TalendExpressionError(f"expected {value!r}, found {tok[1]!r}")
        self.i += 1
        return tok

    def at(self, *values: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] == "op" and tok[1] in values

    # ---------------------------------------------------------------- grammar

    def parse(self) -> tuple[exp.Expression, str | None]:
        e = self.conditional()
        if self.peek() is not None:
            raise TalendExpressionError(f"unexpected {self.peek()[1]!r}")
        if _cmp(e[0]):
            raise TalendExpressionError("compareTo outside a comparison")
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
        if then[1] in NUMERIC and other[1] in NUMERIC:
            kind = _num(then[1], other[1])
        return exp.Case(ifs=[exp.If(this=cond[0], true=then[0])], default=other[0]), kind

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
            return exp.Or(this=a, expression=b), "bool"
        if op == "&&":
            wrap = lambda x: exp.Paren(this=x) if isinstance(x, exp.Or) else x  # noqa: E731
            return exp.And(this=wrap(a), expression=wrap(b)), "bool"
        if op in ("==", "!="):
            for x, y in ((a, b), (b, a)):
                if isinstance(y, exp.Null):
                    e = exp.Is(this=_pc(x), expression=exp.null())
                    return (e if op == "==" else exp.Not(this=e)), "bool"
            if "string" in (ka, kb):
                self.note(REVIEW, "== on strings compares references in Java; this compares the values, which is "
                                  "almost always what was meant")
        if op in ("==", "!=", "<", ">", "<=", ">="):
            cls = {"==": exp.EQ, "!=": exp.NEQ, "<": exp.LT, ">": exp.GT, "<=": exp.LTE, ">=": exp.GTE}[op]
            if _cmp(a) and isinstance(b, exp.Literal) and b.this == "0":  # x.compareTo(y) > 0  ->  x > y
                return cls(this=_pc(a.expressions[0]), expression=_pc(a.expressions[1])), "bool"
            if _cmp(a) or _cmp(b):
                raise TalendExpressionError("compareTo is only translated when compared with 0")
            return cls(this=_pc(a), expression=_pc(b)), "bool"
        if op == "+":
            if "string" in (ka, kb):
                self.note(REVIEW, 'Java\'s + writes "null" for a null value; SQL\'s || gives NULL')
                parts = [x if k == "string" else exp.Cast(this=_p(x), to=exp.DataType.build("TEXT"))
                         for x, k in ((a, ka), (b, kb))]
                left = parts[0] if isinstance(parts[0], exp.DPipe) else _p(parts[0])
                return exp.DPipe(this=left, expression=_p(parts[1]), safe=False), "string"
            if "date" in (ka, kb):
                raise TalendExpressionError("adding to a date with + has no translation; use TalendDate.addDate")
            return exp.Add(this=_p(a), expression=_p(b)), _num(ka, kb)
        if op == "-":
            return exp.Sub(this=_p(a), expression=_p(b)), _num(ka, kb)
        if op == "*":
            return exp.Mul(this=_p(a), expression=_p(b)), _num(ka, kb)
        if op == "/":
            if ka == kb == "int":
                exact = exp.Div(this=exp.Paren(this=exp.Sub(this=_p(a), expression=exp.Mod(this=_p(a), expression=_p(b)))),
                                expression=_p(b))
                return exp.Cast(this=exact, to=exp.DataType.build("BIGINT")), "int"
            return exp.Div(this=_p(a), expression=_p(b)), _num(ka, kb) if "decimal" in (ka, kb) else "number"
        if op == "%":
            return exp.Mod(this=_p(a), expression=_p(b)), _num(ka, kb)
        raise TalendExpressionError(f"operator {op} has no translation")

    def unary(self) -> tuple[exp.Expression, str | None]:
        if self.at("!"):
            self.take()
            e, _ = self.unary()
            if isinstance(e, exp.EQ):  # !a.equals(b)
                return exp.NEQ(this=e.this, expression=e.expression), "bool"
            return exp.Not(this=_p(e)), "bool"
        if self.at("-"):
            self.take()
            e, k = self.unary()
            return exp.Neg(this=_p(e)), k
        if self.at("+"):
            self.take()
            return self.unary()
        nxt, after = self.peek(1), self.peek(2)
        operand = self.peek(3)
        if (self.at("(") and nxt and nxt[0] == "name" and nxt[1] in _JAVA_TYPES and after and after[1] == ")"
                and operand is not None and (operand[0] in ("name", "str", "num", "chr") or operand[1] == "(")):
            self.take("(")
            name = self.take()[1]
            self.take(")")
            sql_type, kind = _JAVA_TYPES[name]
            e, _k = self.unary()
            return exp.Cast(this=_p(e), to=exp.DataType.build(sql_type)), kind
        return self.postfix(self.primary())

    def postfix(self, value: tuple[exp.Expression, str | None]) -> tuple[exp.Expression, str | None]:
        while self.at("."):
            self.take(".")
            name = self.take()[1]
            if not self.at("("):
                raise TalendExpressionError(f"field .{name} has no translation")
            value = self.method(value, name, self.args())
        return value

    def primary(self) -> tuple[exp.Expression, str | None]:
        kind, value = self.take()
        if kind == "op" and value == "(":
            e = self.conditional()
            self.take(")")
            return exp.Paren(this=e[0]), e[1]
        if kind == "str":
            return exp.Literal.string(_unescape(value[1:-1])), "string"
        if kind == "chr":
            return exp.Literal.string(_unescape(value[1:-1])), "string"
        if kind == "num":
            suffix = value[-1] if value[-1] in "LlDdFf" else ""
            text = value[:-1] if suffix else value
            if suffix in "DdFf" and suffix:
                return exp.Literal.number(text), "number"
            return exp.Literal.number(text), "int" if re.fullmatch(r"\d+", text) else "number"
        if kind != "name":
            raise TalendExpressionError(f"unexpected {value!r}")
        if value == "null":
            return exp.null(), None
        if value in ("true", "false"):
            return exp.Boolean(this=value == "true"), "bool"
        if value == "new":
            return self.new()
        path = [value]
        while self.at(".") and self.peek(1) and self.peek(1)[0] == "name" and not (self.peek(2) and self.peek(2)[1] == "("):
            self.take(".")
            path.append(self.take()[1])
        if path[0] == "context" and len(path) == 2:
            return self.variable(path[1])
        if path[0] in self.rows and len(path) == 2:
            found = self.resolve(path[0], path[1])
            if found is None:
                raise TalendExpressionError(f"unknown column {path[0]}.{path[1]}")
            return found[0].copy(), found[1]
        if self.at("(") and len(path) == 1:
            raise TalendExpressionError(f"method {value}() has no translation")
        if self.at(".") and self.peek(1) and self.peek(1)[0] == "name":  # Class.method(...)
            self.take(".")
            fn = self.take()[1]
            cls = path[-1]
            return self.static(cls, fn, self.args())
        if path[0] in ("globalMap",):
            raise TalendExpressionError("globalMap values are set at run time; pass the value as a context variable")
        raise TalendExpressionError(f"unknown name {'.'.join(path)}")

    def variable(self, name: str) -> tuple[exp.Expression, str | None]:
        if name not in self.context:
            raise TalendExpressionError(f"context variable {name} isn't defined in the job")
        value, kind = self.context[name]
        var = _var(name)
        self.used[var] = value
        text = f"${{{var}}}"
        if kind in NUMERIC or kind == "bool":
            return exp.Var(this=text), kind
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

    def new(self) -> tuple[exp.Expression, str | None]:
        parts = [self.take()[1]]
        while self.at("."):
            self.take(".")
            parts.append(self.take()[1])
        cls = parts[-1]
        args = self.args()
        self.functions.add(f"new {cls}")
        if cls == "BigDecimal" and len(args) == 1 and isinstance(args[0][0], exp.Literal):
            value = args[0][0].this
            if re.fullmatch(r"-?\d+(\.\d+)?", value):
                return exp.Literal.number(value), "decimal"
        if cls in _JAVA_TYPES and len(args) == 1:
            sql_type, kind = _JAVA_TYPES[cls]
            return exp.Cast(this=_p(args[0][0]), to=exp.DataType.build(sql_type)), kind
        raise TalendExpressionError(f"new {cls}(...) has no translation")

    # ---------------------------------------------------------------- methods and routines

    def method(self, recv, name: str, args) -> tuple[exp.Expression, str | None]:
        x, kx = recv
        a = [e for e, _k in args]
        self.functions.add(f".{name}()")
        n = len(a)

        def need(count: int) -> None:
            if n != count:
                raise TalendExpressionError(f".{name}() with {n} arguments has no translation")

        if name == "equals":
            need(1)
            return exp.EQ(this=_p(x), expression=_p(a[0])), "bool"
        if name == "equalsIgnoreCase":
            need(1)
            return exp.EQ(this=exp.Upper(this=x), expression=exp.Upper(this=a[0])), "bool"
        if name == "trim":
            need(0)
            return exp.Trim(this=x), "string"
        if name in ("toUpperCase", "toLowerCase"):
            need(0)
            return (exp.Upper if name == "toUpperCase" else exp.Lower)(this=x), "string"
        if name == "length":
            need(0)
            return exp.Length(this=x), "int"
        if name == "isEmpty":
            need(0)
            return exp.EQ(this=exp.Length(this=x), expression=exp.Literal.number(0)), "bool"
        if name == "substring":
            if n == 1:
                return exp.Substring(this=x, start=_plus1(a[0])), "string"
            need(2)
            length = _fold(exp.Sub(this=_p(a[1]), expression=_p(a[0])))
            return exp.Substring(this=x, start=_plus1(a[0]), length=length), "string"
        if name in ("contains", "startsWith", "endsWith"):
            need(1)
            if name == "contains":
                return exp.GT(this=exp.StrPosition(this=x, substr=a[0]), expression=exp.Literal.number(0)), "bool"
            pattern = (exp.DPipe(this=a[0], expression=exp.Literal.string("%"), safe=False) if name == "startsWith"
                       else exp.DPipe(this=exp.Literal.string("%"), expression=a[0], safe=False))
            if isinstance(a[0], exp.Literal) and a[0].is_string and not re.search(r"[%_]", a[0].this):
                pattern = exp.Literal.string(a[0].this + "%" if name == "startsWith" else "%" + a[0].this)
            return exp.Like(this=x, expression=pattern), "bool"
        if name == "indexOf":
            need(1)
            return exp.Sub(this=exp.StrPosition(this=x, substr=a[0]), expression=exp.Literal.number(1)), "int"
        if name in ("replace", "replaceAll"):
            need(2)
            if name == "replaceAll":
                return exp.RegexpReplace(this=x, expression=a[0], replacement=a[1]), "string"
            return exp.Anonymous(this="REPLACE", expressions=[x, a[0], a[1]]), "string"
        if name == "toString":
            need(0)
            return exp.Cast(this=_p(x), to=exp.DataType.build("TEXT")), "string"
        if name in ("intValue", "longValue", "doubleValue", "floatValue"):
            need(0)
            t = {"intValue": ("INT", "int"), "longValue": ("BIGINT", "int"), "doubleValue": ("DOUBLE", "number"),
                 "floatValue": ("FLOAT", "number")}[name]
            return exp.Cast(this=_p(x), to=exp.DataType.build(t[0])), t[1]
        if name in ("add", "subtract", "multiply"):
            need(1)
            cls = {"add": exp.Add, "subtract": exp.Sub, "multiply": exp.Mul}[name]
            return cls(this=_p(x), expression=_p(a[0])), "decimal"
        if name == "divide":
            if n not in (1, 2, 3):
                need(1)
            if n == 3 or (n == 2 and args[1][1] == "int"):
                return exp.Round(this=exp.Div(this=_p(x), expression=_p(a[0])), decimals=a[1]), "decimal"
            return exp.Div(this=_p(x), expression=_p(a[0])), "decimal"
        if name == "negate":
            need(0)
            return exp.Neg(this=_p(x)), kx
        if name == "abs":
            need(0)
            return exp.Abs(this=x), kx
        if name == "setScale":
            if n not in (1, 2):
                need(1)
            if n == 2:
                self.note(REVIEW, "setScale's rounding mode is approximated by ROUND (half up)")
            return exp.Round(this=x, decimals=a[0]), "decimal"
        if name == "compareTo":
            need(1)
            return exp.Anonymous(this="__COMPARE__", expressions=[x, a[0]]), "int"
        raise TalendExpressionError(f".{name}() has no translation")

    def static(self, cls: str, fn: str, args) -> tuple[exp.Expression, str | None]:
        a = [e for e, _k in args]
        k = [kind for _e, kind in args]
        n = len(a)
        self.functions.add(f"{cls}.{fn}")
        key = f"{cls}.{fn}"

        def need(count: int) -> None:
            if n != count:
                raise TalendExpressionError(f"{key} with {n} arguments has no translation")

        def lit(i: int) -> str:
            if not (isinstance(a[i], exp.Literal) and a[i].is_string):
                raise TalendExpressionError(f"{key} needs a literal pattern")
            return a[i].this

        if cls == "StringHandling":
            simple = {"UPCASE": exp.Upper, "DOWNCASE": exp.Lower, "TRIM": exp.Trim, "LEN": exp.Length}
            if fn in simple:
                need(1)
                return simple[fn](this=a[0]), "int" if fn == "LEN" else "string"
            if fn in ("LTRIM", "RTRIM"):
                need(1)
                return exp.Trim(this=a[0], position="LEADING" if fn == "LTRIM" else "TRAILING"), "string"
            if fn in ("LEFT", "RIGHT"):
                need(2)
                return (exp.Left if fn == "LEFT" else exp.Right)(this=a[0], expression=a[1]), "string"
            if fn == "INDEX":
                need(2)
                return exp.Sub(this=exp.StrPosition(this=a[0], substr=a[1]), expression=exp.Literal.number(1)), "int"
            if fn == "CHANGE":
                need(3)
                return exp.Anonymous(this="REPLACE", expressions=a), "string"
            if fn == "SPACE":
                need(1)
                return exp.Anonymous(this="REPEAT", expressions=[exp.Literal.string(" "), a[0]]), "string"
            if fn == "IS_ALPHA":
                need(1)
                return exp.RegexpLike(this=a[0], expression=exp.Literal.string("^[A-Za-z]+$")), "bool"
        if cls == "Relational":
            if fn == "ISNULL":
                need(1)
                return exp.Is(this=_p(a[0]), expression=exp.null()), "bool"
            if fn == "NOT":
                need(1)
                return exp.Not(this=_p(a[0])), "bool"
        if cls in ("Math", "Mathematical"):
            upper = fn.upper()
            if upper == "ABS":
                need(1)
                return exp.Abs(this=a[0]), k[0]
            if upper in ("FLOOR", "CEIL"):
                need(1)
                return (exp.Floor if upper == "FLOOR" else exp.Ceil)(this=a[0]), "number"
            if fn == "round":
                need(1)
                self.note(REVIEW, "Math.round rounds halves up (toward positive infinity); SQL ROUND rounds "
                                  "halves away from zero, so negative halves differ")
                return exp.Cast(this=exp.Round(this=a[0]), to=exp.DataType.build("BIGINT")), "int"
            if upper in ("MAX", "MIN"):
                need(2)
                cls_ = exp.Greatest if upper == "MAX" else exp.Least
                return cls_(this=a[0], expressions=[a[1]]), _num(k[0], k[1])
            if upper == "POW":
                need(2)
                return exp.Pow(this=a[0], expression=a[1]), "number"
            if upper == "SQRT":
                need(1)
                return exp.Sqrt(this=a[0]), "number"
        if cls == "String" and fn == "valueOf":
            need(1)
            return exp.Cast(this=_p(a[0]), to=exp.DataType.build("TEXT")), "string"
        if cls in ("Integer", "Long", "Double", "Float", "BigDecimal") and fn in (
                "parseInt", "parseLong", "parseDouble", "parseFloat", "valueOf"):
            need(1)
            sql_type, kind = _JAVA_TYPES[cls]
            return exp.Cast(this=_p(a[0]), to=exp.DataType.build(sql_type)), kind
        if cls == "TalendDate":
            if fn == "getCurrentDate" or fn == "getDate" and n == 0:
                self.note(REVIEW, f"{key}() is when the job runs; CURRENT_TIMESTAMP is when the query runs")
                return exp.CurrentTimestamp(), "date"
            if fn == "formatDate":
                need(2)
                return exp.TimeToStr(this=a[1], format=exp.Literal.string(java_format(lit(0)))), "string"
            if fn == "parseDate":
                need(2)
                return exp.StrToTime(this=a[1], format=exp.Literal.string(java_format(lit(0)))), "date"
            if fn == "addDate":
                need(3)
                unit = _UNITS.get(lit(2))
                if unit is None:
                    raise TalendExpressionError(f"{key} unit {lit(2)!r} has no translation")
                return exp.DateAdd(this=a[0], expression=a[1], unit=exp.var(unit)), "date"
            if fn == "diffDate":
                unit = _UNITS.get(lit(2)) if n == 3 else "DAY"
                if n not in (2, 3) or unit is None:
                    raise TalendExpressionError(f"{key} has no translation for these arguments")
                return exp.DateDiff(this=a[0], expression=a[1], unit=exp.var(unit)), "int"
            if fn == "getPartOfDate":
                need(2)
                unit = _UNITS.get(lit(0))
                if unit is None:
                    raise TalendExpressionError(f"{key} part {lit(0)!r} has no translation")
                return exp.Extract(this=exp.var(unit), expression=a[1]), "int"
            if fn == "isDate":
                need(2)
                return exp.Not(this=exp.Is(this=exp.Anonymous(this="TRY_STRPTIME", expressions=[
                    a[0], exp.Literal.string(java_format(lit(1)))]), expression=exp.null())), "bool"
        if cls == "Numeric" and fn == "sequence":
            self.note(REVIEW, "Numeric.sequence numbers rows in the order they arrive; ROW_NUMBER() needs an order "
                              "and restarts every run")
            start = a[1] if n >= 2 else exp.Literal.number(1)
            win = exp.Window(this=exp.RowNumber(), order=None)
            return exp.Add(this=exp.Sub(this=_p(start), expression=exp.Literal.number(1)), expression=win), "int"
        raise TalendExpressionError(f"{key}() has no translation")


def _fold(e: exp.Expression) -> exp.Expression:
    """Integer literal arithmetic done now: SUBSTRING(x, 1, 3 - 0) reads better as SUBSTRING(x, 1, 3)."""
    a, b = e.this, e.expression
    if all(isinstance(x, exp.Literal) and not x.is_string and re.fullmatch(r"\d+", x.this) for x in (a, b)):
        value = int(a.this) - int(b.this) if isinstance(e, exp.Sub) else int(a.this) + int(b.this)
        if value >= 0:
            return exp.Literal.number(value)
    return e


def _plus1(e: exp.Expression) -> exp.Expression:
    if isinstance(e, exp.Literal) and not e.is_string and re.fullmatch(r"\d+", e.this):
        return exp.Literal.number(int(e.this) + 1)
    return exp.Add(this=_p(e), expression=exp.Literal.number(1))


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), text)


def _var(name: str) -> str:
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "variable"


def variable_name(context_name: str) -> str:
    """The pipeline variable for a Talend context variable: minAmount -> min_amount."""
    return _var(context_name)


def translate_talend(text: str, resolve: Resolver, rows: set[str] | list[str],
                     context: dict[str, tuple[str, str | None]] | None = None) -> TalendTranslation:
    """Translate one Talend (Java) expression. ``rows`` are the input row names (``row1``)."""
    p = _Parser(text, resolve, set(rows), context or {})
    e, kind = p.parse()
    return TalendTranslation(e, kind, p.status, p.notes, p.functions, p.used)


def java_string(text: str, context: dict[str, tuple[str, str | None]] | None = None) -> tuple[str, dict[str, str]]:
    """A parameter written as a Java string expression (``"orders"``, ``context.dir + "/orders.csv"``) as plain
    text, with context variables as ``${name}``; returns (text, variables used)."""
    text = (text or "").strip()
    if not text:
        return "", {}
    if not text.startswith('"') and "context." not in text:
        return text, {}
    p = _Parser(text, lambda _r, _c: None, set(), context or {})
    e, _kind = p.parse()

    def flat(x: exp.Expression) -> str:
        if isinstance(x, exp.Paren):
            return flat(x.this)
        if isinstance(x, exp.DPipe):
            return flat(x.this) + flat(x.expression)
        if isinstance(x, exp.Cast):
            return flat(x.this)
        if isinstance(x, exp.Literal):
            return x.this
        if isinstance(x, exp.Var):
            return x.this
        raise TalendExpressionError(f"{text!r} isn't a plain string")

    return flat(e), p.used
