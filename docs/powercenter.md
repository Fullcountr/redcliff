# Importing Informatica PowerCenter mappings

`redcliff import` reads a PowerCenter repository export (the XML you get from Designer's
*Export Objects* or `pmrep objectexport`) and builds a pipeline spec, a migration scorecard and,
from there, column lineage and Spark code.

```text
$ redcliff import m_customer_revenue.xml -o pipeline.yaml --scorecard scorecard.md
imported 19 nodes (2 targets) to pipeline.yaml
warning: AGG_REGION_MONTH: MANAGER isn't grouped or aggregated; PowerCenter returns the last row's value, ANY_VALUE() returns any row's
warning: SEQ_EXCEPTION_ID: becomes ROW_NUMBER() where it is used, so numbers restart every run; PowerCenter keeps counting across runs. Use a surrogate key strategy in the target.
scorecard: 12 of 14 converted (86%), 2 to review, 0 to rewrite by hand
scorecard written to scorecard.md
```

The playground does the same: **Import → Informatica PowerCenter**. Nothing is uploaded; the
XML is parsed in your browser tab.

Exports with **mapplets, sessions, workflows and a parameter file** import too:

```text
$ redcliff import wf_customer_revenue.xml --params wf_customer_revenue.par -o pipeline.yaml
imported 19 nodes (2 targets) to pipeline.yaml
...
scorecard: 11 of 17 converted (65%), 4 to review, 2 to rewrite by hand
```

That example ([`wf_customer_revenue.xml`](../examples/legacy/powercenter/wf_customer_revenue.xml)
and [`.par`](../examples/legacy/powercenter/wf_customer_revenue.par)) is the same job built the way
real estates are: the cleanup lives in a mapplet, the order date comes from the parameter file, a
session truncates one target and analyzes it afterwards, and a workflow runs the session between a
file check and an email. Its pipeline returns exactly the rows of the plain mapping, in DuckDB and
on Spark. Three of the 17 scorecard items are workflow tasks (the run order, a shell command, an
email), which belong in an orchestrator; without them it's the same 12 of 14.

A third example, [`wf_store_mart.xml`](../examples/legacy/powercenter/wf_store_mart.xml), turns
quarterly sales stored one column per quarter into rows with a **Normalizer**, keeps each store's two
best quarters with a **Rank** (with a tie and a missing quarter in the data), finds each region's best
store with an Aggregator and a second Rank, and runs its session inside a **worklet**. Its rows are
checked against answers worked out by hand, in DuckDB, on Spark, through dbt and as a Lakeflow pipeline.

The example above is [`examples/legacy/powercenter/m_customer_revenue.xml`](../examples/legacy/powercenter/m_customer_revenue.xml),
an invented mapping. The test suite runs its converted pipeline in DuckDB and compares the rows
with answers worked out by hand.

## The scorecard

Every instance in the mapping gets one status, with the reason:

| Status | Meaning |
| --- | --- |
| **converted** | The generated steps mean the same thing as the PowerCenter logic. |
| **review** | The steps run, but differ from PowerCenter in an edge case. The note says which. |
| **manual** | There's no faithful translation yet. A typed NULL placeholder keeps the pipeline whole, and the note says why. |

The scorecard also reports **lineage coverage**: how many target columns trace back to a source
column. Constants and placeholders don't, and the report names them. Write it as Markdown
(`--scorecard report.md`) or JSON (`--scorecard report.json`), or download it from the
playground's Migration tab.

## Transformations

| PowerCenter | Becomes | Notes |
| --- | --- | --- |
| Source Definition | `source` (table, or CSV for flat files) | column types from the definition |
| Source Qualifier | the SQL it would run, through the SQL importer | source filter, user-defined join (including `{A LEFT OUTER JOIN B ...}`), foreign-key joins, Select Distinct, and SQL overrides become real steps |
| Expression | `derive` | variable ports are inlined; a variable read before it's set (previous-row state) is **manual** |
| Filter | `filter` | |
| Aggregator | `aggregate`, plus `derive` for non-aggregate outputs | conditional aggregates like `SUM(x, cond)`; an ungrouped pass-through port is **review** (`ANY_VALUE`) |
| Joiner | `join` | normal, master outer (left), detail outer (right), full outer; clashing master columns are renamed first |
| Lookup (connected) | `source` + `join` | de-duplicated by the lookup keys for *Use Any Value*; *First/Last Value* is **review**; lookup SQL overrides and source filters are imported |
| Router | one `filter` per group that's used | the DEFAULT group is `NOT COALESCE(cond, FALSE)` for every other group, so NULLs land there, as in PowerCenter |
| Union | `select` per input group + `union` | ports matched by position |
| Sorter | nothing, or `aggregate` when Distinct | row order doesn't matter to set-based steps |
| Update Strategy | nothing, a `filter` that drops `DD_REJECT` rows, and for `DD_UPDATE` a merge target | rows flagged insert or update are merged on the target's primary key (an upsert); with no primary key, or with `DD_DELETE`, it's **review** |
| Sequence Generator | a `ROW_NUMBER()` column where it's used | **review**: numbers restart every run |
| Rank | `derive` with `RANK() OVER (PARTITION BY <group by ports> ORDER BY <rank port>)` + `filter` on it | top or bottom N per group; ties share a rank index and the next one is skipped, as PowerCenter's RANKINDEX does; NULLs rank last; ranking a string port is **review** (collations differ) |
| Normalizer | one `derive` + `select` per occurrence, then `union` | `SALES_in1`…`SALES_in4` (or `SALES1`…) feed `SALES`; `GCID_SALES` numbers the occurrence; a connected `GK_` generated key is **manual** |
| Transaction Control | nothing | rows pass through |
| Target Definition | `select` of the connected ports + `target` | `mode: append`, since PowerCenter inserts; unconnected target columns are noted |
| Mapplet | its transformations, inlined | named `MAPPLET/TRANSFORMATION` on the scorecard and `mapplet__transformation` in the spec; nested mapplets too |
| Java, Stored Procedure, SQL transformation, unconnected Lookup, anything else | a placeholder step | **manual** |

## Sessions, workflows and parameter files

| PowerCenter | Becomes |
| --- | --- |
| Session attribute overrides (SQL Query, Source Filter, User Defined Join, lookup overrides, ...) | applied over the mapping, and noted on the transformation |
| Truncate target table option | the target's `mode: overwrite` (otherwise `append`) |
| Target Pre SQL / Post SQL | the target's `pre_sql` / `post_sql`, translated from the target database's dialect; Spark SQL and PySpark run them |
| Source qualifier Pre SQL / Post SQL | **review**: it runs on the source database, which Redcliff doesn't touch |
| Parameter file | `$$PARAMS` resolved per session (`[folder.WF:wf.WT:worklet.ST:session]` beats `[folder.WF:wf.ST:session]` beats `[session]` beats `[folder.WF:wf]` beats `[Global]`). A parameter used in an expression, or inside quotes in SQL text, becomes a [pipeline variable](spec.md#variables) (`$$MIN_ORDER_DATE` → `${min_order_date}`) with that value as its default. One used unquoted in SQL (a number, a list of columns) is written in as its value, and is **review** |
| Workflow | sets the order of its sessions' mappings; the run order and link conditions are noted (**review**: recreate them in your orchestrator) |
| Worklet | expanded in place: its sessions and tasks join the run order where it runs (`wklt_load/s_m_store_sales`), nested worklets too; `[folder.WF:wf.WT:worklet.ST:session]` parameter sections apply |
| Command, Email, Timer, Event Wait/Raise, Control tasks | **manual**, with the command, address or condition in the note |
| Decision, Assignment tasks | **review** |

A mapping run by several sessions gets the first session's overrides (in workflow order), with a
note. A `$$PARAM` used in SQL with no value anywhere makes that transformation **manual**: pass the
parameter file with `--params` (or in the playground's import dialog).

A folder export with several mappings becomes **one pipeline**. When one mapping reads a table
another one writes, Redcliff™ connects them, so lineage runs across the whole folder. Use
`--mapping NAME` (repeatable) to import only some mappings.

## The expression language

PowerCenter expressions are parsed and rebuilt as neutral SQL, then transpiled by sqlglot for
each target. Where SQL behaves differently, the translation keeps PowerCenter's behaviour:

| PowerCenter | SQL | Why it's not a plain copy |
| --- | --- | --- |
| `a \|\| b`, `CONCAT(a, b)` | `CONCAT(COALESCE(a, ''), COALESCE(b, ''))` | PowerCenter treats NULL as an empty string here; SQL returns NULL |
| `IIF(c, a)` | `CASE WHEN c THEN a ELSE 0 END` (or `''`) | the missing ELSE defaults by type |
| `DECODE(TRUE, c1, r1, ..., d)` | `CASE WHEN c1 THEN r1 ... ELSE d END` | |
| `SUBSTR(s, 0, n)` | `SUBSTRING(s, 1, n)` | a start of 0 means 1 |
| `TO_INTEGER(x)` | `CAST(ROUND(x) AS BIGINT)` | it rounds unless the truncate flag is set |
| `REG_MATCH(s, p)` | `REGEXP_LIKE(s, '^(?:p)$')` | it matches the whole string |
| `TO_DATE(s, 'MM/DD/YYYY')`, `TO_CHAR(d, 'YYYY-MM')` | `STR_TO_TIME`, `TIME_TO_STR` with converted formats | defaults to `MM/DD/YYYY HH24:MI:SS`, as PowerCenter does |
| `ADD_TO_DATE`, `DATE_DIFF`, `GET_DATE_PART`, `TRUNC(date, 'MM')`, `LAST_DAY` | date arithmetic | `DATE_DIFF` in units other than days is **review** (PowerCenter returns fractions) |
| `SUM(x, cond)`, `COUNT(x, cond)` | `SUM(CASE WHEN cond THEN x END)` | |
| `$$PARAM` | a pipeline variable, `${param}`, defaulting to the parameter's value | |
| `ERROR(...)` | `NULL` | **review**: PowerCenter rejects the row |
| `:LKP.name(...)`, `ABORT(...)`, `IS_DATE(...)`, unknown functions | `NULL` | **manual** |

Also translated: `ISNULL`, `IS_SPACES`, `IS_NUMBER`, `IN` (with its case-insensitive flag), `LTRIM`/`RTRIM` (with
trim sets), `UPPER`, `LOWER`, `LENGTH`, `INSTR`, `LPAD`, `RPAD`, `REPLACESTR`, `REPLACECHR`,
`TO_DECIMAL`, `TO_FLOAT`, `ROUND`, `TRUNC`, `MOD`, `ABS`, `CEIL`, `FLOOR`, `POWER`, `SQRT`,
`SYSDATE`, `SESSSTARTTIME`, `FIRST`, `LAST`, `MEDIAN`, `STDDEV`, `VARIANCE`, `REG_EXTRACT`,
`DD_INSERT` / `DD_UPDATE` / `DD_DELETE` / `DD_REJECT`, and `--` and `//` comments.

## Not yet

- Reusable session configurations (config objects) aren't read.
- Connection objects and `$PM...` built-in variables are ignored.
- Java, Stored Procedure and SQL transformations have no step type yet, nor do Rank ports that
  compute values or Normalizers with nested occurs (VSAM sources).

Found a mapping that imports badly? Please open an issue with a small, **invented** export that
shows the problem. Never share an employer's or a client's real mappings.
