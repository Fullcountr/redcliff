"""Data quality expectations: checks on a step's rows, declared next to the step.

    - id: shipped
      type: filter
      inputs: [orders]
      params: {condition: status = 'shipped'}
      expect:
        - not_null: customer_id
        - unique: order_id                    # or a list of columns
        - accepted_values: {column: status, values: [shipped]}
        - condition: amount >= 0              # any SQL that must hold on each row
          name: amount_is_positive            # optional
          severity: warn                      # error (the default) or warn
        - row_count: {min: 1, max: 100000}

A row fails a row-level check (``not_null``, ``accepted_values``, ``condition``) when the condition
is false; a NULL result passes, as in a SQL CHECK constraint, so ``accepted_values`` doesn't flag
NULLs (add ``not_null`` for that). ``unique`` ignores rows whose key has a NULL, like dbt's test.

The same expectations run in DuckDB (``redcliff test``, ``redcliff run``, the playground), become
dbt tests, and become Lakeflow ``@dp.expect`` decorators where they are row-level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot.errors import ParseError

KINDS = ("not_null", "unique", "accepted_values", "condition", "row_count")
SEVERITIES = ("error", "warn")
SAMPLE = 5


@dataclass
class Expectation:
    node: str
    name: str
    kind: str
    severity: str
    columns: list[str] = field(default_factory=list)
    condition: str | None = None   # neutral SQL that holds on a good row (row-level kinds)
    minimum: int | None = None     # row_count
    maximum: int | None = None

    @property
    def row_level(self) -> bool:
        return self.condition is not None

    def describe(self) -> str:
        if self.kind == "unique":
            return f"{', '.join(self.columns)} is unique"
        if self.kind == "row_count":
            bounds = [f">= {self.minimum}" if self.minimum is not None else "",
                      f"<= {self.maximum}" if self.maximum is not None else ""]
            return "row count " + " and ".join(b for b in bounds if b)
        return self.condition or ""

    def failing_sql(self, relation: str) -> str:
        """Neutral SQL returning the rows that break the expectation (none when it holds)."""
        if self.row_level:
            return f"SELECT * FROM {relation} WHERE NOT ({self.condition})"
        if self.kind == "unique":
            cols = ", ".join(self.columns)
            present = " AND ".join(f"{c} IS NOT NULL" for c in self.columns)
            return (f"SELECT {cols}, COUNT(*) AS rows_with_this_key FROM {relation} WHERE {present} "
                    f"GROUP BY {cols} HAVING COUNT(*) > 1")
        bounds = []
        if self.minimum is not None:
            bounds.append(f"COUNT(*) >= {self.minimum}")
        if self.maximum is not None:
            bounds.append(f"COUNT(*) <= {self.maximum}")
        return f"SELECT COUNT(*) AS row_count FROM {relation} HAVING NOT ({' AND '.join(bounds)})"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "check"


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def parse_expectations(node_id: str, raw: Any) -> tuple[list[Expectation], list[str]]:
    """Expectations from a node's ``expect`` list, and problems with it (as messages)."""
    out: list[Expectation] = []
    problems: list[str] = []
    if raw is None:
        return out, problems
    if not isinstance(raw, list):
        return out, ["'expect' must be a list of checks"]
    names: set[str] = set()
    for i, item in enumerate(raw):
        where = f"check #{i + 1}"
        if not isinstance(item, dict):
            problems.append(f"{where} must be a mapping such as {{not_null: column}}")
            continue
        kinds = [k for k in item if k in KINDS]
        unknown = [k for k in item if k not in KINDS and k not in ("name", "severity")]
        if len(kinds) != 1 or unknown:
            problems.append(f"{where} needs exactly one of {list(KINDS)}"
                            + (f"; unknown keys {unknown}" if unknown else ""))
            continue
        kind = kinds[0]
        value = item[kind]
        severity = item.get("severity", "error")
        if severity not in SEVERITIES:
            problems.append(f"{where}: 'severity' must be one of {list(SEVERITIES)}")
            continue
        e = Expectation(node_id, "", kind, severity)
        if kind == "not_null":
            if not isinstance(value, str):
                problems.append(f"{where}: not_null takes a column name")
                continue
            e.columns, e.condition = [value], f"{value} IS NOT NULL"
        elif kind == "unique":
            cols = [value] if isinstance(value, str) else value
            if not isinstance(cols, list) or not cols or not all(isinstance(c, str) for c in cols):
                problems.append(f"{where}: unique takes a column or a list of columns")
                continue
            e.columns = list(cols)
        elif kind == "accepted_values":
            if not isinstance(value, dict) or not isinstance(value.get("column"), str) \
                    or not isinstance(value.get("values"), list) or not value["values"]:
                problems.append(f"{where}: accepted_values takes {{column: name, values: [...]}}")
                continue
            e.columns = [value["column"]]
            e.condition = f"{value['column']} IN ({', '.join(_literal(v) for v in value['values'])})"
        elif kind == "condition":
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{where}: condition takes a SQL expression")
                continue
            try:
                sqlglot.parse_one(value)
            except ParseError as err:
                problems.append(f"{where}: condition is not valid SQL: {str(err).splitlines()[0]}")
                continue
            e.condition = value
        elif kind == "row_count":
            ok = isinstance(value, dict) and value and set(value) <= {"min", "max"} and \
                all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value.values())
            if not ok:
                problems.append(f"{where}: row_count takes {{min: n, max: n}} with whole numbers")
                continue
            e.minimum, e.maximum = value.get("min"), value.get("max")
        default = {"not_null": f"not_null_{_slug('_'.join(e.columns))}",
                   "unique": f"unique_{_slug('_'.join(e.columns))}",
                   "accepted_values": f"accepted_values_{_slug('_'.join(e.columns))}",
                   "condition": f"condition_{_slug(e.condition or '')}",
                   "row_count": "row_count"}[kind]
        e.name = item.get("name") or default
        if not isinstance(e.name, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", e.name):
            problems.append(f"{where}: 'name' must be lowercase letters, digits and underscores")
            continue
        if e.name in names:
            problems.append(f"{where}: two checks are named {e.name!r}; give one a 'name'")
            continue
        names.add(e.name)
        out.append(e)
    return out, problems


def expectations(pipeline) -> list[Expectation]:
    """Every expectation in a pipeline, in run order. Assumes a valid spec."""
    out = []
    for node in pipeline.topo_order():
        out += parse_expectations(node.id, node.expect)[0]
    return out


@dataclass
class CheckResult:
    expectation: Expectation
    failures: int
    columns: list[str] = field(default_factory=list)
    sample: list[list[Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.failures == 0 and self.error is None

    def to_dict(self) -> dict[str, Any]:
        e = self.expectation
        return {"node": e.node, "name": e.name, "kind": e.kind, "severity": e.severity,
                "describe": e.describe(), "passed": self.passed, "failures": self.failures,
                "columns": self.columns, "sample": [[None if v is None else str(v) for v in r] for r in self.sample],
                "error": self.error}


def check_queries(pipeline, node_id: str | None = None, base_dir=None) -> list[tuple[Expectation, str]]:
    """(expectation, DuckDB SQL of its failing rows) for every check, or one step's."""
    from redcliff.compile import compile_query
    from redcliff.spec import bind

    pipeline = bind(pipeline)
    out = []
    for e in expectations(pipeline):
        if node_id is not None and e.node != node_id:
            continue
        node = pipeline.node(e.node)
        rows_of = node.inputs[0] if node.type == "target" else node.id
        relation = f"({compile_query(pipeline, rows_of, 'duckdb', base_dir)}) AS checked"
        out.append((e, sqlglot.transpile(e.failing_sql(relation), write="duckdb")[0]))
    return out


def run_checks(con, pipeline, node_id: str | None = None, base_dir=None, sample: int = SAMPLE) -> list[CheckResult]:
    """Run expectations in DuckDB."""
    results = []
    for e, sql in check_queries(pipeline, node_id, base_dir):
        try:
            failures = con.sql(f"SELECT COUNT(*) FROM ({sql}) AS failing").fetchone()[0]
            rel = con.sql(f"{sql} LIMIT {sample}")
            results.append(CheckResult(e, failures, rel.columns, [list(r) for r in rel.fetchall()] if failures else []))
        except Exception as err:  # noqa: BLE001 - a broken check is reported, not fatal
            results.append(CheckResult(e, 0, error=str(err).splitlines()[0]))
    return results
