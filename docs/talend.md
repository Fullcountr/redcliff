# Importing Talend jobs

`redcliff import` reads a Talend Open Studio job (the `.item` file of a Standard job, from your
workspace or an exported archive) and builds a pipeline spec and a migration scorecard. From there
you get column lineage, row traces, checks, and Spark, dbt or Lakeflow code.

```text
$ redcliff import j_order_revenue.item -o pipeline.yaml --scorecard scorecard.md
imported 24 nodes (3 targets) to pipeline.yaml
warning: j_order_revenue: Redcliff builds pipelines, not schedules: recreate this run order in your orchestrator: tPrejob_1 → tMysqlConnection_1 (OnSubjobOk); tMysqlInput_1 → tSendMail_1 (OnSubjobOk); tSendMail_1 → tJava_1 (OnComponentOk); tPostjob_1 → tMysqlClose_1 (OnSubjobOk)
warning: tUniqRow_1: keeps the first row Talend reads for each key; rows have no order in SQL, so this keeps any row (the same row when duplicates are identical)
warning: tMap_1: out_orders.customer_name: Java's + writes "null" for a null value; SQL's || gives NULL
warning: tJava_1: runs Java code; rewrite it as a task in your orchestrator
scorecard: 14 of 21 converted (67%), 5 to review, 2 to rewrite by hand
```

The playground does the same: **Import → Talend job**. Nothing is uploaded.

The example, [`examples/legacy/talend/j_order_revenue.item`](../examples/legacy/talend/j_order_revenue.item),
is an invented job: orders read with a query, deduplicated (tUniqRow) and filtered (tFilterRow, with
a context variable), then a tMap with two lookups (customers, filtered to those with an email, as an
inner join whose rejects go to a file; regions as a left outer join on an upper-cased name), a Var,
and two outputs. A tReplicate feeds two tAggregateRows: revenue by region, replacing its table each
run, and activity per customer, inserted or updated on its key. A prejob, a postjob, a connection, a
tSendMail and a tJava sit around the data flow. Its pipeline returns the rows worked out by hand in
DuckDB, and the same rows on Spark, through dbt and as a Lakeflow pipeline.

## Components

Rows are followed by connection name (`row1`), as Talend's expressions name them. Step names come
from each component's label when it has one, else its unique name.

| Talend | Becomes | Notes |
| --- | --- | --- |
| tDBInput, tMysqlInput, tOracleInput, tPostgresqlInput, tMSSqlInput, tSnowflakeInput and other `t*Input` | a `source` for the table, or the query through the [SQL importer](importer.md) in the database's dialect | the query's columns map to the schema by position, as in Talend; `?` parameters are **manual** |
| tFileInputDelimited | a CSV `source` | **review** for a separator other than a comma, or more than one header line |
| tMap | per lookup: its filter, one row per key, a `join`; Var as `derive`; per output: a `filter`, a `derive` and a `select` | see below |
| tJoin | `join` on its key columns; REJECT is the main rows with no match | **review**: when the lookup has several rows for a key, tJoin keeps one |
| tFilterRow | `filter`; REJECT is the complement | conditions, operators, `$source` functions and the advanced (Java) condition, joined with AND or OR; REJECT includes rows whose condition is NULL |
| tAggregateRow | `aggregate` | sum, avg, min, max, count, count distinct; first, last and list are **review** (rows have no order); a renamed group-by column is derived first |
| tUniqRow | `aggregate` keeping one row per key | **review** when other columns could differ between duplicates; case-insensitive string keys are **review**; the DUPLICATE flow is **manual** |
| tUnite | a `select` per input + `union` | |
| tFilterColumns | `select` | |
| tConvertType | `derive` with `CAST` | **review**: a value that doesn't convert fails the query |
| tReplicate, tSortRow, tLogRow | nothing: rows pass through | sorting is dropped (tables have no order); logging is dropped |
| tDBOutput, tMysqlOutput and other `t*Output` | a `select` of the schema's columns, cast to the schema's types, + a `target` | see below |
| tFileOutputDelimited | a CSV `target` | append or overwrite; **review** for another separator or no header |
| tJavaRow and anything else in the data flow | a placeholder step with typed NULL columns | **manual** |

### tMap

| tMap | Becomes |
| --- | --- |
| main input | the rows every lookup joins to |
| lookup, *Unique match* (the default), *First* or *Last match* | one row per key, then a `join`; when the lookup's schema marks its join columns as keys, the rows are used as they are; otherwise **review**: Talend keeps the last (or first) row it loads, this keeps any |
| lookup, *All matches* | a `join` |
| lookup join keys (expressions on the left) | the join condition, each expression translated |
| lookup filter | a `filter` on the lookup before the join |
| *Inner join* | an inner `join`; with an output catching inner join rejects, a left join and a filter for each side |
| Var table | `derive`, in order; a var reading an earlier var starts a new step |
| output filter | a `filter` |
| output *Catch output reject* | the rows no other output's filter passes |
| output *Catch lookup inner join reject* | the main rows an inner join lookup found no match for |
| output expressions | a `derive` and a `select` of the output's columns; an empty expression is NULL, as in Talend |

### Outputs

| TABLE_ACTION, DATA_ACTION | Target |
| --- | --- |
| DROP_CREATE, DROP_IF_EXISTS_AND_CREATE, CLEAR or TRUNCATE; INSERT | `mode: overwrite` |
| NONE, CREATE or CREATE_IF_NOT_EXISTS; INSERT | `mode: append` (`overwrite` when a tDBRow truncates the table first) |
| INSERT_OR_UPDATE, UPDATE_OR_INSERT | `mode: merge` on the schema's key columns |
| UPDATE | `mode: merge` (**review**: a merge also inserts the rows that don't exist) |
| INSERT_IGNORE, INSERT_IF_NOT_EXIST | `mode: insert_new` on the key columns |
| DELETE and others | **manual** |

## Expressions

Talend expressions are Java. They are parsed and rebuilt as neutral SQL, then transpiled per target:

| Java | SQL | Why it's not a plain copy |
| --- | --- | --- |
| `row1.status.equals("shipped")`, `!a.equals(b)` | `status = 'shipped'`, `a <> b` | |
| `x == null`, `x != null` | `x IS NULL`, `NOT x IS NULL` | |
| `a + " " + b` (strings) | `a \|\| ' ' \|\| b` | **review**: Java writes `"null"` for a null value; SQL gives NULL |
| `s == "x"` (strings) | `s = 'x'` | **review**: `==` compares references in Java; this compares values, which is what was meant |
| `qty / packs` (integers) | an exact integer division | Java divides integers as integers |
| `c ? a : b` | `CASE WHEN c THEN a ELSE b END` | |
| `amount.multiply(x)`, `.add`, `.subtract`, `.divide`, `.setScale(n)` | `*`, `+`, `-`, `/`, `ROUND` | BigDecimal arithmetic |
| `amount.compareTo(x) > 0` | `amount > x` | |
| `new BigDecimal(x)`, `(Integer) x`, `Integer.parseInt(s)`, `String.valueOf(x)` | `CAST` | |
| `.trim()`, `.toUpperCase()`, `.toLowerCase()`, `.length()`, `.substring(a, b)`, `.startsWith`, `.endsWith`, `.contains`, `.indexOf`, `.replace`, `.isEmpty()` | string functions | `substring` is 0-based in Java, 1-based in SQL |
| `StringHandling.UPCASE`, `DOWNCASE`, `TRIM`, `LTRIM`, `RTRIM`, `LEN`, `LEFT`, `RIGHT`, `INDEX`, `CHANGE`, `SPACE`, `IS_ALPHA` | string functions | |
| `TalendDate.formatDate("yyyy-MM", d)`, `parseDate`, `addDate`, `diffDate`, `getPartOfDate` | date functions | Java date patterns are converted |
| `TalendDate.getCurrentDate()` | `CURRENT_TIMESTAMP` | **review**: when the query runs, not when the job started |
| `Relational.ISNULL`, `Relational.NOT`, `Math.abs`, `floor`, `ceil`, `max`, `min`, `pow`, `sqrt` | the same | |
| `Math.round(x)` | `CAST(ROUND(x) AS BIGINT)` | **review**: Java rounds halves up, SQL away from zero |
| `Numeric.sequence(...)` | `ROW_NUMBER()` | **review**: restarts every run |
| `context.minAmount` | the [pipeline variable](spec.md#variables) `${min_amount}`, defaulting to the context's value | |
| `globalMap.get(...)`, other methods | none | **manual**, with the reason |

Parameters written as Java strings (`"orders"`, `context.outDir + "/rejects.csv"`) are read the same
way, so a path built from a context variable becomes `${out_dir}/rejects.csv`.

## Around the data flow

| Talend | Becomes |
| --- | --- |
| OnSubjobOk, OnComponentOk, Run if and other triggers | the run order is noted (**review**: recreate it in your orchestrator) |
| tPrejob, tPostjob | **review**: run before or after the job in your orchestrator |
| tDBConnection, tMysqlConnection, commits, closes, rollbacks | nothing (converted): a pipeline doesn't hold a connection |
| tDBRow, tMysqlRow: `TRUNCATE TABLE t` | the output writing `t` overwrites (converted) |
| tDBRow: anything else | **review**, with the SQL in the note |
| tRunJob | **review**: import that job too |
| tSendMail, tJava, tJavaFlex, loops, file tasks | **manual** |
| context variables | pipeline variables, where expressions or parameters use them |

## Limits

Big Data and Spark jobs, joblets, ELT components (tELT*), routines you wrote, and tJavaRow code are
not translated; each shows up on the scorecard. Only the default context is read.
