# Importing SSIS packages

`redcliff import` reads a SQL Server Integration Services package (the `.dtsx` file, SSIS 2012 or
later) and builds a pipeline spec and a migration scorecard. From there you get column lineage,
row traces, checks, and Spark, dbt or Lakeflow code.

```text
$ redcliff import LoadRegionRevenue.dtsx -o pipeline.yaml --scorecard scorecard.md
imported 16 nodes (2 targets) to pipeline.yaml
warning: LoadRegionRevenue: Redcliff builds pipelines, not schedules: recreate this run order in your orchestrator: SQL Truncate Region Revenue → DFT Load Region Revenue → SQL Refresh Stats → Send Done Mail
warning: LoadRegionRevenue/SQL Refresh Stats: runs SQL on the database, which belongs in your orchestrator: EXEC dbo.usp_refresh_region_stats
warning: LoadRegionRevenue/Send Done Mail: sends an email; use your orchestrator's notifications
scorecard: 14 of 17 converted (82%), 2 to review, 1 to rewrite by hand
```

The playground does the same: **Import → SSIS package**. Nothing is uploaded.

The example, [`examples/legacy/ssis/LoadRegionRevenue.dtsx`](../examples/legacy/ssis/LoadRegionRevenue.dtsx),
is an invented package: a conditional split on order status, derived columns (one replacing a
column in place, one using a package variable), two sorts and a merge join, a lookup, an aggregate,
a row count, two OLE DB destinations, and Execute SQL and Send Mail tasks around the data flow.
Its pipeline returns the rows worked out by hand in DuckDB, and the same rows on Spark, through dbt
and as a Lakeflow pipeline.

## Data flow components

Columns are followed by lineage id, as SSIS does: synchronous components pass their input's
columns on, and asynchronous ones (Sort, Merge Join, Union All, Aggregate) map new ids to them.

| SSIS | Becomes | Notes |
| --- | --- | --- |
| OLE DB / ADO.NET Source | a `source` for a table, or the SQL command through the [SQL importer](importer.md) (T-SQL) | the output columns declare the source's columns; a table or query from a variable is read at import (**review**); `?` parameters are **manual** |
| Flat File Source | a CSV `source` | **review**: check the delimiter, header and code page |
| Derived Column | `derive` | a replaced column keeps its name; new columns are added |
| Data Conversion | `derive` with `CAST` | |
| Conditional Split | one `filter` per output in use | a row takes the **first** output whose condition is true, as in SSIS (each filter excludes the earlier conditions); a NULL condition counts as false |
| Multicast, Row Count | nothing: rows pass through | the row count's variable is for the control flow |
| Sort | nothing, or a distinct `aggregate` when it removes duplicates | its keys are remembered for a Merge Join |
| Merge Join | `join` on the inputs' sort keys | inner, left or full; NULL keys match each other when *treat nulls as equal* is on, as it is by default |
| Lookup | the reference query, one row per key, and a left `join` | match and no-match outputs become filters; with the default *fail component* behaviour it's **review**: SSIS fails the package when a row has no match |
| Union All | `select` per input + `union` | columns mapped by the union's output columns |
| Aggregate | `aggregate` | group by, sum, avg, min, max, count, count all, count distinct |
| OLE DB / ADO.NET / SQL Server Destination | `select` of the mapped columns + `target` | `mode: append`, or `overwrite` when an Execute SQL task truncates the table first |
| Flat File Destination | a CSV `target` | **review**, as for the source |
| Script Component and anything else | a placeholder step with typed NULL columns | **manual** |

## Expressions

SSIS expressions are parsed and rebuilt as neutral SQL, then transpiled per target:

| SSIS | SQL | Why it's not a plain copy |
| --- | --- | --- |
| `"a" + b` (strings) | `'a' \|\| b` | `+` concatenates strings in SSIS; NULL in, NULL out in both |
| `qty / packs` (integers) | an exact integer division | SSIS divides integers as integers, truncating toward zero |
| `ISNULL(x) ? a : b` | `CASE WHEN x IS NULL THEN a ELSE b END` | |
| `REPLACENULL(a, b)` | `COALESCE(a, b)` | |
| `(DT_WSTR, 50)x`, `(DT_NUMERIC, 12, 2)x`, `(DT_DBTIMESTAMP)x` | `CAST` | |
| `NULL(DT_WSTR, 50)` | `CAST(NULL AS TEXT)` | |
| `DATEADD("dd", 1, d)`, `DATEDIFF("mm", a, b)`, `DATEPART("yyyy", d)` | date arithmetic | |
| `@[User::FxRate]` | the [pipeline variable](spec.md#variables) `${fx_rate}`, defaulting to the package's value | |
| `@[System::StartTime]`, `GETUTCDATE()` | `CURRENT_TIMESTAMP` | **review**: when the query runs, not when the package started; time zone |

Also translated: `UPPER`, `LOWER`, `TRIM`, `LTRIM`, `RTRIM`, `LEN`, `SUBSTRING`, `LEFT`, `RIGHT`,
`REPLACE`, `REPLICATE`, `REVERSE`, `FINDSTRING` (first occurrence), `GETDATE`, `YEAR`, `MONTH`,
`DAY`, `ABS`, `CEILING`, `FLOOR`, `ROUND`, `POWER`, `SQRT`, `SIGN`, `EXP`, `LN`, `LOG`, `CODEPOINT`,
`@[System::PackageName]`, and the `==`, `!=`, `&&`, `||`, `!`, `? :` and bitwise operators.

## The control flow

| SSIS | Becomes |
| --- | --- |
| Precedence constraints | the order data flows are imported in; the run order is noted (**review**: recreate it in your orchestrator) |
| Sequence container | its tasks, inlined |
| For / Foreach Loop container | its data flows are imported; the loop is **manual** |
| Execute SQL Task: `TRUNCATE TABLE t` | the destination writing `t` overwrites (converted) |
| Execute SQL Task: anything else | **review**, with the SQL in the note |
| Execute Package Task | **review**: import that package too |
| Send Mail, Script, Execute Process, File System tasks | **manual** |
| Package variables (`User::`) | pipeline variables, where expressions use them |

A package with several data flows becomes one pipeline. When one flow reads a table an earlier one
writes, they connect, so lineage runs across the package.

## Not yet

- The SSIS 2005/2008 package format (open and save the package in SQL Server Data Tools first).
- Old-style component class GUIDs, Slowly Changing Dimension, Fuzzy Lookup, Pivot and Unpivot,
  CDC components, and project-level connection managers and parameters.
- Error outputs other than a lookup's no-match rows.

Found a package that imports badly? Please open an issue with a small, **invented** package that
shows the problem. Never share an employer's or a client's real packages.
