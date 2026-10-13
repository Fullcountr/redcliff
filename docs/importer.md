# SQL importer

`redcliff.import_sql(sql, dialect=None, name="imported", schema=None)` turns SQL into a
validated pipeline. It reads any dialect sqlglot knows (Teradata, Oracle, SQL Server,
Snowflake, Spark, DuckDB and more).

```python
from redcliff import import_sql, compile_query

result = import_sql(open("legacy.sql").read(), dialect="teradata",
                    schema={"sales.orders": {"order_id": "int", "amount": "decimal(10,2)"}})
print(result.warnings)                     # what was kept as raw SQL, and why
print(compile_query(result.pipeline, result.outputs[-1], "spark"))
```

## What maps to nodes

| SQL | Becomes |
| --- | --- |
| A table in FROM or JOIN | `source` (format `table`) |
| `JOIN ... ON` / `USING`, LEFT, RIGHT, FULL, CROSS | `join` |
| `WHERE`, `HAVING` | `filter` |
| `GROUP BY` and aggregates, `GROUP BY 1`, grouping by an expression | `derive` (if needed) then `aggregate` |
| Computed or renamed columns | `derive` then `select` |
| `SELECT DISTINCT` | `aggregate` with no measures |
| `UNION`, `UNION ALL` | `union` |
| CTEs and subqueries in FROM | their own chain of nodes, named after the CTE or alias |
| `CREATE TABLE AS` read by a later statement | an intermediate step |
| `CREATE TABLE AS` read by nothing, `INSERT INTO ... SELECT` | `target` |
| Teradata BTEQ dot-commands (`.LOGON`, `.IF ERRORCODE`) | skipped, with a warning |

## What is kept as a `sql` step, with a warning

ORDER BY and LIMIT, subqueries outside FROM, QUALIFY, recursive CTEs, INTERSECT and EXCEPT,
ROLLUP/CUBE/GROUPING SETS, NATURAL joins and table functions. Statements with no pipeline
equivalent (UPDATE, DELETE, DROP) are skipped with a warning.

## Join column clashes

The `join` node keeps the left side's column when both sides share a name. When the SQL
references the right side's copy (`a.event_time` while `w.event_time` also exists), the
importer first adds a `derive` that renames it (`a_event_time`), so no value is lost.
Pass `schema` so this check is exact; without it, Redcliff™ warns that clashes can't be checked.

## How it's tested

Each test runs the original SQL in DuckDB, imports it, compiles the pipeline back to DuckDB
SQL, runs that, and requires the same rows. Row order is compared only when the original has
a top-level ORDER BY.
