# Pipeline spec reference (version 0)

A Redcliff pipeline is a YAML or JSON document. It is the single source of truth: the canvas
edits it, and code and lineage are generated from it. Editors can use
[`core/src/redcliff/pipeline.schema.json`](../core/src/redcliff/pipeline.schema.json) for
autocomplete; the Python validator adds checks a schema can't express.

```yaml
version: 0
name: orders_revenue          # required
description: optional text
variables:                    # optional: defaults, overridable per run
  status: shipped
nodes:                        # required, at least one
  - id: shipped               # lowercase letters, digits, underscores
    type: filter              # one of the eight types below
    inputs: [orders]          # ids of upstream nodes
    params:
      condition: status = '${status}'
```

## Conventions

- **Expressions are SQL strings** in a neutral dialect; Redcliff transpiles them per target.
- **Inside a join, refer to each side by its input node id**, as in
  `shipped.customer_id = customers.id`. Elsewhere, use bare column names.
- **Paths are relative to the spec file.**
- **YAML gotcha:** a bare `on:`, `yes:` or `no:` key becomes a boolean. Redcliff avoids those
  names, and the validator flags it if one slips in.

## Node types

| Type | Inputs | Params | Output columns |
| --- | --- | --- | --- |
| `source` | 0 | `format` (csv, parquet, table), `path` or `table`, optional `columns` (name: type) | the declared or discovered columns |
| `filter` | 1 | `condition` | same as input |
| `select` | 1 | `columns`: list of names to keep, in order | the listed columns |
| `derive` | 1 | `columns`: name: SQL expression | input columns plus new ones; same name replaces |
| `join` | 2 | `condition`, `how` (inner default, left, right, full) | columns of both inputs |
| `aggregate` | 1 | `group_by`: list of names, `measures`: name: SQL aggregate (optional when grouping, which is `SELECT DISTINCT`) | group-by columns then measures |
| `union` | 2+ | `distinct` (false default) | columns of the first input, matched by position |
| `sql` | 0+ | `query`: one SELECT that reads its inputs by node id | the SELECT's columns |
| `target` | 1 | `format`, `path` or `table`, `mode` (overwrite default, append, merge), optional `pre_sql` and `post_sql` (lists of statements); `mode: merge` takes `keys` and an optional `delete_when` | none (writes data) |

The `sql` node is the escape hatch for logic without a node type yet (ORDER BY, LIMIT,
subqueries in WHERE, QUALIFY). The SQL importer uses it so nothing is ever dropped silently.

A target's `pre_sql` and `post_sql` run on the target platform, before and after the write:
Spark SQL scripts and PySpark run them; the DuckDB preview script lists them in its header
instead, because DuckDB stands in for the target only locally. The PowerCenter importer fills them
from a session's Pre SQL and Post SQL.

Rules the compiler follows:

- A derive sees only its input's columns, so one derived column can't use another from the
  same node; chain two derives instead.
- When both sides of a join share a column name, the left side's column is kept.
- Without declared source columns, a derive can only add new columns, not replace one.

Declaring `columns` on sources is optional but recommended: lineage and early error checks
work best when Redcliff knows each source's schema.

## Merge targets

`mode: merge` updates the table's rows whose `keys` match an incoming row, inserts the rest, and
leaves rows that don't appear alone. `delete_when` deletes matched rows where it holds (and doesn't
insert unmatched ones):

```yaml
- id: out
  type: target
  inputs: [revenue_by_region]
  params:
    format: table
    table: revenue
    mode: merge
    keys: [region]
    delete_when: order_count = 0
```

| Output | Merge |
| --- | --- |
| DuckDB SQL | `CREATE TABLE IF NOT EXISTS` (the first run), then `MERGE INTO` |
| Spark SQL, PySpark | `MERGE INTO` / `DataFrame.mergeInto`; Spark needs a table format with row-level updates, such as Delta Lake or Iceberg |
| dbt | an incremental model with `unique_key`; `merge` strategy, or `delete+insert` on DuckDB, Postgres and Redshift. dbt's merge doesn't delete, so `delete_when` rows are left out, with a comment |
| Lakeflow | a materialized view of the incoming rows, and a job task note for the MERGE |

The incoming rows must have one row per key, as every engine's MERGE requires.

## Variables

`variables` declares values a pipeline can take per run. `${name}` anywhere in a node's params
(an expression, a path, a table name, pre/post SQL) is replaced by the value as text, the way a
template is, before the spec is checked, analyzed or compiled. Quote it where SQL needs a string:
`status = '${status}'`, `amount >= ${min_amount}`.

| Where | What happens |
| --- | --- |
| `redcliff compile / run / trace / lineage --var status=pending` | overrides the default for that run |
| Spark SQL, PySpark, DuckDB SQL, Lakeflow | the values are written into the code, listed in a header comment |
| dbt | variables stay variables: `{{ var("status") }}`, with the defaults under `vars:` in `dbt_project.yml` |
| PowerCenter import | a `$$PARAM` in an expression, or inside quotes in SQL, becomes a variable with the parameter file's value as its default |
| `redcliff diff` | changed defaults are listed, and the data comparison uses each side's values |

A variable that is used but not declared is an error; so is a name that isn't lowercase letters,
digits and underscores, or a value that isn't text, a number or true/false.

## What the validator checks

- Required fields, known node types, valid ids, no duplicate ids.
- Input counts per type, inputs that exist, no reading from a target, no cycles.
- Every SQL expression parses.
- It reports every problem at once, not just the first.

Column-level checks (an unknown or ambiguous column, a union whose inputs have different
widths) come from `redcliff.column_issues`, which needs declared source columns.
