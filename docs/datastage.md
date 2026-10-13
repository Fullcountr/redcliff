# Importing IBM DataStage jobs

`redcliff import` reads a DataStage export, either the `.dsx` text file or the same export as XML,
and builds a pipeline spec and a migration scorecard from its parallel jobs. From there you get
column lineage, row traces, checks, and Spark, dbt or Lakeflow code.

```text
$ redcliff import px_order_revenue.dsx -o pipeline.yaml --scorecard scorecard.md
imported 32 nodes (3 targets) to pipeline.yaml
warning: rmd_orders: keeps the first row of each key in sort order; rows have no order in SQL, so this keeps any row (the same row when duplicates are identical)
warning: xfm_prepare: lnk_prepared.CUSTOMER_NAME: Trim also turns runs of spaces inside the string into one space; TRIM only trims the ends
warning: before-job subroutine: runs a shell command before the job: /opt/etl/bin/check_landing.sh; move it to your orchestrator
warning: after-job subroutine: sends an email after the job; use your orchestrator's notifications
scorecard: 14 of 18 converted (78%), 2 to review, 2 to rewrite by hand
```

The playground does the same: **Import → IBM DataStage**. Nothing is uploaded.

> **How it was built.** The `.dsx` format is IBM's and isn't fully published. This importer follows
> its public structure (jobs, records, stages, pins, links, columns, Transformer derivations and
> constraints, the connectors' XML properties) and is tested on invented exports. Where a stage
> stores options it doesn't recognise, it puts a placeholder in and says so on the scorecard,
> rather than guessing. If an export of yours imports wrongly, the scorecard notes (not the file)
> are what to report.

The example, [`examples/legacy/datastage/px_order_revenue.dsx`](../examples/legacy/datastage/px_order_revenue.dsx),
is an invented parallel job: Oracle connectors reading orders, customers (with a WHERE in the
SELECT) and regions; Remove Duplicates; a Filter with a reject link and a job parameter; a Lookup
whose reject link writes unknown customers to a file; a Transformer with a stage variable, string,
date and conditional derivations; a Copy that renames a key; a left outer Join; a second
Transformer routing rows with a constraint; two Aggregators; and two Oracle targets, one replacing
its table and one inserting then updating on its key. A before-job ExecSH and an after-job
DSSendMail sit around it. Its pipeline returns the rows worked out by hand in DuckDB (twice, for the
insert-then-update), and the same rows on Spark, through dbt and as a Lakeflow pipeline.

## Stages

Step names come from stage and link names. Columns follow the links; an output link without
columns of its own carries its input's.

| DataStage | Becomes | Notes |
| --- | --- | --- |
| Oracle, DB2, ODBC, Teradata, Snowflake, Netezza and other connectors (and the older enterprise stages), reading | a `source` for the table, or the SELECT through the [SQL importer](importer.md) in the database's dialect | the SELECT's columns map to the link's by position; columns it filters on but doesn't select are declared too; `?` and `ORCHESTRATE.` parameters are **manual** |
| the same, writing | a `select` of the link's columns, cast to the link's types, + a `target` | see below |
| Sequential File | a CSV `source` or `target` | **review** for another delimiter or no header line; append or overwrite |
| Transformer | stage variables as `derive` (a variable reading an earlier one starts a new step); per output link, its constraint as a `filter`, the derivations as `derive`, and a `select` | the otherwise/reject link gets the rows no other constraint passes; a stage variable that carries a value from the previous row is **manual** |
| Filter | a `filter` per where clause, to its output link | rows can go to several outputs, as in DataStage; with *output rows only once*, a row takes the first clause it passes; the reject link gets the rest, including NULL results |
| Lookup | per reference link: one row per key, then a `join` on the key derivations | if not found: *continue* is a left join, *drop* an inner join, *reject* sends the stream row to the reject link, *fail* is **review** (the job would stop); when the reference link's key columns are its keys, rows are used as they are, otherwise **review** (DataStage returns the first match) |
| Join | `join` on the key columns | inner, left outer, right outer, full outer (**review**: key columns come from the left); a non-key column on both inputs comes from the left |
| Aggregator | `aggregate` | count rows, sum, mean, min, max, range, non-missing and missing counts; standard deviation and variance are the sample ones (**review**) |
| Remove Duplicates | `aggregate` keeping one row per key | **review** when other columns could differ between duplicates |
| Funnel | a `select` per input + `union` | the funnel's order is dropped |
| Copy | `select` of each output link's columns, renamed by derivation | |
| Sort, Peek | nothing: rows pass through | a sort that removes duplicates becomes Remove Duplicates |
| anything else (Pivot, Change Capture, Surrogate Key, Modify, ...) | a placeholder step with typed NULL columns | **manual** |

### Targets

| Write mode, table action | Target |
| --- | --- |
| Insert; Replace or Truncate | `mode: overwrite` |
| Insert; Append or Create | `mode: append` |
| Insert then update, Update then insert | `mode: merge` on the link's key columns |
| Update | `mode: merge` (**review**: a merge also inserts the rows that don't exist) |
| Delete and others | **manual** |

## Expressions

Transformer and Lookup derivations, constraints and stage variables are parsed and rebuilt as
neutral SQL, then transpiled per target:

| DataStage | SQL | Why it's not a plain copy |
| --- | --- | --- |
| `a : " " : b` | `a \|\| ' ' \|\| b` | **review** when a number is joined: DataStage formats it its own way |
| `If c Then a Else b` | `CASE WHEN c THEN a ELSE b END` | |
| `=`, `<>`, `#`, `And`, `Or`, `Not` | `=`, `<>`, `AND`, `OR`, `NOT` | |
| `s[2, 3]` | `SUBSTRING(s, 2, 3)` | `s[n]` (the last n characters) is **manual** |
| `qty / 4` | `qty / 4` | DataStage divides as decimals, so no integer division |
| `IsNull`, `IsNotNull`, `NullToValue`, `NullToZero`, `NullToEmpty`, `SetNull()` | `IS NULL`, `COALESCE`, `NULL` | |
| `Trim(s)` | `TRIM(s)` | **review**: Trim also turns runs of inner spaces into one |
| `TrimLeadingTrailing`, `TrimF`, `TrimB`, `UpCase`, `DownCase`, `Len`, `Left`, `Right`, `Index(s, x, 1)`, `Field`, `Ereplace`, `Str`, `Space` | string functions | |
| `DateToString(d, "%yyyy-%mm")`, `StringToDate`, `StringToTimestamp`, `TimestampToString` | date formatting and parsing | DataStage formats are converted |
| `YearFromDate`, `MonthFromDate`, `MonthDayFromDate`, `DaysSinceFromDate`, `DateFromDaysSince` | `EXTRACT`, date arithmetic | |
| `StringToDecimal`, `AsInteger`, `AsDouble`, `DecimalToString` | `CAST` | **review** for `DecimalToString`: DataStage pads with zeros by default |
| `CurrentDate()`, `CurrentTimestamp()`, `@DATE` | `CURRENT_DATE`, `CURRENT_TIMESTAMP` | **review**: when the query runs |
| `@INROWNUM`, `@OUTROWNUM` | `ROW_NUMBER()` | **review**: per partition and in arrival order in DataStage |
| `Abs`, `Mod`, `Int`, `Sqrt`, `Exp`, `Ln`, `^` | the same | |
| a job parameter (`MinAmount`, `#MinAmount#`, `#ps_sales.Region#`) | the [pipeline variable](spec.md#variables) `${min_amount}`, defaulting to the parameter's default | |

Filter where clauses are SQL already and are read as SQL; `#Param#` inside them, and in file names
and table names, becomes a pipeline variable too.

## Around the jobs

| DataStage | Becomes |
| --- | --- |
| several parallel jobs in one export | one pipeline; steps on the scorecard are named per job |
| before-job and after-job subroutines (ExecSH, ExecDOS, DSSendMail, DSWaitForFile, your own) | **manual**, with the command: move it to your orchestrator |
| sequence jobs | **review**: they run other jobs in order; recreate that in your orchestrator |
| server and mainframe jobs | **manual**: only parallel jobs are imported |
| job parameters and parameter sets | pipeline variables, where expressions, file names or SQL use them |
