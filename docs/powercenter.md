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
| Update Strategy | nothing, or a `filter` that drops `DD_REJECT` rows | `DD_UPDATE` / `DD_DELETE` are **review**: they need a MERGE in the target |
| Sequence Generator | a `ROW_NUMBER()` column where it's used | **review**: numbers restart every run |
| Transaction Control | nothing | rows pass through |
| Target Definition | `select` of the connected ports + `target` | `mode: append`, since PowerCenter inserts; unconnected target columns are noted |
| Mapplet, Normalizer, Java, Stored Procedure, unconnected Lookup, anything else | a placeholder step | **manual** |

A folder export with several mappings becomes **one pipeline**. When one mapping reads a table
another one writes, Redcliff connects them, so lineage runs across the whole folder. Use
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
| `$$PARAM` | the parameter's default value | **review**: make it a pipeline variable if it changes between runs |
| `ERROR(...)` | `NULL` | **review**: PowerCenter rejects the row |
| `:LKP.name(...)`, `ABORT(...)`, `IS_DATE(...)`, unknown functions | `NULL` | **manual** |

Also translated: `ISNULL`, `IS_SPACES`, `IS_NUMBER`, `IN`, `LTRIM`/`RTRIM` (with
trim sets), `UPPER`, `LOWER`, `LENGTH`, `INSTR`, `LPAD`, `RPAD`, `REPLACESTR`, `REPLACECHR`,
`TO_DECIMAL`, `TO_FLOAT`, `ROUND`, `TRUNC`, `MOD`, `ABS`, `CEIL`, `FLOOR`, `POWER`, `SQRT`,
`SYSDATE`, `SESSSTARTTIME`, `FIRST`, `LAST`, `MEDIAN`, `STDDEV`, `VARIANCE`, `REG_EXTRACT`,
`DD_INSERT` / `DD_UPDATE` / `DD_DELETE` / `DD_REJECT`, and `--` and `//` comments.

## Not yet

- Mapplets aren't expanded; their outputs become placeholders.
- Sessions and workflows (pre/post SQL, truncate target, parameter files) aren't read; only
  mappings are.
- Rank, Normalizer, Java, Stored Procedure and SQL transformations have no step type yet.

Found a mapping that imports badly? Please open an issue with a small, **invented** export that
shows the problem. Never share an employer's or a client's real mappings.
