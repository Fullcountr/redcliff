# Data models

Status: M1 to M5 done. Next: legacy imports mapped onto a model.
roadmap in the README tracks progress.

## The idea

Today Redcliff™ describes how data moves. A model describes what it should become. When someone adds
a file, Redcliff profiles it, proposes a model (Kimball or Data Vault 2.0, their choice), lets them
adjust it lightly on the canvas, and generates the pipelines that load it. Lineage then ends at named
business attributes (`dim_customer.segment`) instead of output columns.

## Decisions

| # | Decision | Why |
| --- | --- | --- |
| 1 | The model lives in its own `model.yaml`; pipelines reference it | One model feeds many pipelines; the model changes on a different rhythm from the loads |
| 2 | Kimball is the default style; Data Vault 2.0 is an option | Kimball fits most BI targets; DV2 earns its cost with many sources and audit needs. A DV2 model also gets Kimball marts over the vault |
| 3 | Dimensions are SCD2 by default | History not captured is lost for good; current state is always derivable from history, never the reverse |
| 4 | Type-1 attributes and ignored columns are per attribute | A typo fix should overwrite every version, not become history; audit and high-churn columns would explode versions |
| 5 | Facts carry both keys (Kimball Type 7): the version surrogate key and the durable key | "As it was" and "as it is" from one fact table; joining the current view by durable key can't double count |
| 6 | Effective dates come from a business date column when the profiler finds one, else load time, labeled "system history" | A file added once has no history yet; SCD2 records changes between loads |
| 7 | DV2 hash keys are MD5 of the upper-cased, trimmed business key parts joined with `||` | The common convention, compatible with AutomateDV; computed the same way on DuckDB, Spark and dbt |
| 8 | No licensed or employer reference models; patterns only | Clean-room rule. Model packs (YAML) can be contributed later |
| 9 | Every proposal says why ("`order_id` is unique and never null") | The person confirms; Redcliff doesn't guess silently |
| 10 | The model generates pipeline steps; generated steps are never edited by hand | Same rule as code: the spec is the source of truth |

SCD2 guardrails Redcliff generates as expectations: one current row per durable key, no overlapping
validity windows, and no fact row pointing at a missing dimension version.

## Milestones

| | Delivers |
| --- | --- |
| M1 | Model spec and validator; import from SQL DDL and DBML; export to DDL, DBML, Mermaid and dbt `schema.yml` tests; `redcliff model` commands; a read-only diagram in the playground |
| M2 | Profile files (types, nulls, cardinality, candidate keys, foreign keys by inclusion); propose a Kimball model with reasons; a date dimension |
| M3 | Generate loads: staging, SCD2 dimensions through merge targets (type-1 attributes, ignored columns, Type 7), facts with point-in-time key lookups and unknown members; golden tests on DuckDB, Spark, dbt and Lakeflow |
| M4 | Data Vault 2.0: hubs, links, satellites (hashdiff loads), MD5 keys, `load_dts` and `record_source`; Kimball marts over the vault; PIT and bridge tables later |
| M5 | Lineage and OpenLineage to model attributes; legacy imports mapped onto a model; MCP `propose_model` and `apply_model`; the PR bot reports model changes |

## The model spec (M1)

```yaml
version: 0
name: sales
style: kimball            # kimball | data_vault
entities:
  - id: dim_customer
    kind: dimension       # dimension | fact | hub | link | satellite | table
    description: One row per customer version.
    history: scd2         # dimensions: scd2 (default) | scd1
    attributes:
      - {name: customer_key, type: bigint, role: surrogate_key}
      - {name: customer_id, type: varchar, role: business_key}
      - {name: name, type: varchar, history: scd1}      # corrections overwrite every version
      - {name: segment, type: varchar}                  # tracked: a change makes a new version
      - {name: updated_at, type: timestamp, track: false}
  - id: fct_sales
    kind: fact
    grain: One row per order line.
    attributes:
      - {name: customer_key, type: bigint}
      - {name: order_id, type: varchar, role: business_key}
      - {name: amount, type: decimal(12,2), role: measure}
relationships:
  - {from: fct_sales.customer_key, to: dim_customer.customer_key}
```

An entity's `source` is where its rows come from: a data file (`.csv`, `.tsv`, `.txt`, `.parquet`,
`.json`, `.jsonl`, `.ndjson`; relative to the model) or a table. The load reads each in its format,
with the separator of delimited text found from its first line. `append` lists more files or tables with the same columns, loaded with it (a month per file,
say): the load reads each and unions them.

```yaml
  - id: fct_orders
    kind: fact
    source: data/orders.csv
    append: [data/orders_october.csv, data/orders_november.csv]
```

Rules: entity ids and attribute names match `^[a-z_][a-z0-9_]*$`; types parse as SQL types; a fact
declares its grain; a relationship joins two existing attributes of compatible types, and its
target is the entity's key (a surrogate key, or its single business key); `history` and `track`
apply to dimension attributes.

## Commands (M1)

| Command | Does |
| --- | --- |
| `redcliff model validate model.yaml` | check the model (the rules above) |
| `redcliff model export model.yaml --to ddl [--dialect snowflake]` | `CREATE TABLE` statements with primary and foreign keys (Spark gets them as comments) |
| `redcliff model export model.yaml --to dbml\|mermaid\|dbt` | DBML for dbdiagram.io, a Mermaid `erDiagram`, or a dbt `schema.yml` with key and relationship tests |
| `redcliff model import tables.sql [--dialect postgres]` | a model from `CREATE TABLE` statements: columns, primary and foreign keys |
| `redcliff model propose a.csv b.csv -o model.yaml [--style data_vault]` | profile files and propose a Kimball (or Data Vault) model, with the reason for each choice |
| `redcliff model propose job.sql\|mapping.xml\|package.dtsx\|pipeline.yaml -o model.yaml` | propose a model from what a legacy job does ([below](#from-a-legacy-job)) |
| `redcliff model extend model.yaml c.csv d.csv -o model.yaml [--skip ID] [--take ID]` | add files to a model: only what they add, each change listed with why ([below](#growing-a-model)) |
| `redcliff model map model.yaml job.xml [-o map.md\|map.json]` | for every column the job writes, the model attributes behind it |
| `redcliff model pipeline model.yaml -o load.yaml` | the pipeline that loads the model |
| `redcliff model test model.yaml --db w.duckdb` | check the loaded tables |
| `redcliff model import model.dbml` | a model from DBML, including the roles and history `export --to dbml` writes into notes |

Imports take the kind from the table name (`dim_`, `fct_`, `hub_`, `lnk_`, `sat_`; anything else is
a `table`), call an integer `*_key`/`*_sk`/`*_hk` primary key a surrogate key and any other primary
key a business key, and call decimal columns, and integer columns named like quantities, measures in
facts. Whatever they can't place (an index, a many-to-many reference, a fact with no grain) comes
back as a warning, never a silent drop. Example: [`examples/models/sales.yaml`](../examples/models/sales.yaml).

## Proposing a model (M2)

`redcliff model propose orders.csv customers.csv -o model.yaml` profiles the files with DuckDB (row
counts, nulls and distinct values per column), finds candidate keys (never null, never repeated),
and tests the foreign keys the names suggest (`orders.customer_id` against `customers.id` or
`customers.customer_id`): a reference counts only when every value appears in the parent. Then:

- a table that references others and has measures is a fact; a referenced table, or one with a
  key, is a dimension;
- a dimension gets a surrogate key, its key as business key (a generic `id` becomes `customer_id`),
  SCD1 on names (a change is usually a correction) and `track: false` on audit columns (`*_at`,
  `updated_*`, `etl_*`);
- a fact gets a point-in-time surrogate key per dimension (with `source` naming the column it's
  looked up by), the original key as durable key (Type 7), measures (decimals, and integers named
  like quantities), and a date key per date column, pointing at a generated `dim_date`.

Every choice comes with its reason, printed by the CLI and shown in the playground when you click an
entity. `redcliff.propose.to_data_vault` turns the same proposal into hubs, links and satellites.

## From a legacy job

A legacy job knows things about its tables that a profile of the data can't tell you, and it knows
them without any data. `redcliff model propose m_customer_revenue.xml -o model.yaml` imports the job
(SQL, PowerCenter, SSIS, Talend, DataStage, or a pipeline spec as it is) and reads:

| In the job | Becomes |
| --- | --- |
| a join `orders.customer_id = customers.id` | a reference from orders to customers; `id` is customers' key (the side named like a key is the one side) |
| a lookup: a table deduplicated on one column (`ANY_VALUE`, `MAX`) before a join | that column is the table's key |
| a target merged on keys, with no aggregate on the way | the keys identify the source table's rows |
| a column named like its table's key (`order_id` in orders), when nothing else says | the key |
| `SUM(amount)`, `AVG(amount)` | `amount` is a measure |

Join keys are traced back through the job's steps to the source columns, so a join on a renamed or
cleaned column still counts. A join where neither side looks like a key is noted, not guessed. Then
the same rules as a proposal from data apply (facts, dimensions, SCD2, Type 7, a date dimension),
and every reason names the step it came from: *the job looks regions rows up by region (step
lkp_region_unique)*. A reference between two dimensions (customers to regions) is flagged as a
snowflake. Columns the job never reads are listed, so you can drop them or keep them on purpose.
Entity sources are the job's own tables, so the generated load reads them where they are.

`redcliff model map model.yaml job.xml` works the other way round, with any model: for every column
the job writes, the source columns behind it and the model attributes they land in, and the columns
the job reads that the model doesn't hold. A legacy table matches an entity whose source is the same
table, or a file of the same name (`orders` and `orders.csv`). The coverage line, "13 of 13 columns
the job writes (100%) come from model attributes", says whether the job's reports can be rebuilt on
the model.

## Loading a model (M3, M4)

`redcliff model pipeline model.yaml -o load.yaml` writes the pipeline that loads the model; it's an
ordinary pipeline spec using the [history targets](spec.md#history-targets), so it compiles to
every output. Per entity: a source, a staging step that casts each attribute to its type (applying
attribute `source` expressions) with key expectations, and the load:

| Entity | Load |
| --- | --- |
| dimension (SCD2) | `scd2`: type-1 attributes, ignored columns, generated surrogate key, unknown member (-1), `as_of: ${load_ts}` |
| dimension (SCD1) | `scd1` |
| date dimension (no source, a date business key) | built from the dates the facts refer to |
| fact | each generated surrogate key looked up point in time: the version valid on the fact's first date column (a key's first version counts from the beginning), -1 when there is none; then `merge` on the fact's business key |
| hub, link | `insert_new` on the hash key |
| satellite | `insert_changed` on `hashdiff` (MD5 of its descriptive columns) |
| hub with satellites | a `<name>_current` mart: the hub with each satellite's latest row |

Hash keys are MD5 of the upper-cased, trimmed business key parts joined with `||`. A link's or
satellite's hash key names, as its `source`, the column of its own source holding the hub's
business key, so the same key hashes the same everywhere.

`redcliff model test model.yaml --db warehouse.duckdb` checks the loaded tables: keys unique, one
current row per business key, no overlapping versions, and every reference resolving.

Example: [`sales.yaml`](../examples/models/sales.yaml) → [`load_sales.yaml`](../examples/models/load_sales.yaml),
and [`sales_vault.yaml`](../examples/models/sales_vault.yaml) → [`load_sales_vault.yaml`](../examples/models/load_sales_vault.yaml).

## Lineage, diffs and agents (M5)

- The generated loads declare their source columns (from the CSV header, typed like the attributes
  they feed), so column lineage runs from the files to the model's attributes, and through tables a
  load reads back (a fact's dimension lookup): `redcliff lineage load.yaml --column dim_date.year`.
- `redcliff diff` (and so the pull request bot) reports a changed model file as a model: entities,
  attributes and relationships added, removed or changed.
- The MCP server has `describe_model`, `propose_model` (profile files, or read a legacy job, and
  propose a model with reasons), `map_job` and `generate_load`; proposals go through `apply_edit`,
  like any edit.

## Growing a model

Data rarely arrives at once. `redcliff model extend model.yaml statuses.csv orders_october.csv`
(and adding files in the playground) proposes only what the new files add, and keeps every entity,
attribute and relationship the model has, renames and edits included:

- **entity**: a new entity for a new table: a dimension, a fact, or `dim_date` if new facts have
  dates and the model has no date dimension;
- **relationship**: a reference the data shows, tested both ways: a new fact's `customer_id` found
  in the dimension you have (its lookup takes the existing key's name, even after a rename), or an
  existing fact's `status` found in a new `statuses` table. The lookup attribute and the durable
  key come with the relationship, so skipping it leaves the column as it was;
- **append**: a new file with exactly the columns, in order, of one the model loads (or of another
  new file) reads as more rows of the same table, and goes into that entity's `append` list. Its
  alternative, a separate entity, is offered too, off by default.

Each change has an id (`entity:dim_status`, `relationship:fct_orders.status_key`,
`append:orders_october`) and a reason; the CLI prints them, `--skip ID` leaves one out and `--take
ID` takes one that is off (and drops its alternative). A change that needs a skipped entity goes
with it. Files the model already loads, and references between tables it already has, are left
alone: the model already says what it wants there.

A relationship can also be drawn by hand: in the playground, drag from an attribute of a fact to a
dimension's key. Redcliff first counts how many of the attribute's values the dimension has ("2 of
3 values found; 1 without a match gets the unknown member"), then adds the lookup (an attribute named
after the surrogate key, read from that column, which becomes the durable key). Facts link to
dimensions; Redcliff doesn't load dimension-to-dimension references (outriggers), and in a Data Vault
model a link is an entity of its own, added in the YAML.

## In the playground

The playground has two modes over the same tables, switched in the top bar: **Pipeline** (steps,
lineage, code) and **Model**. Model mode starts from a choice: **from your tables** (tick them and
propose a Kimball or Data Vault model), **from the open pipeline** (what a legacy job joins, looks
up and sums), **paste DDL or DBML**, or **an example**. Files you add (**Add file**, or dropped on the
page: CSV, TSV, Parquet, JSON, or Excel, where you pick the sheets) are tables both modes see: in Pipeline mode they become sources and the message offers **Model
it**; a CSV source's Step tab has **Model this data**.

With a model, entities are cards (kind, SCD1/SCD2, grain; attributes with PK, FK, SK, BK, DK, Σ and
SCD1 marks) and relationships are drawn key to key. Click an entity for its attributes, where it loads
from, and why each is what it is. **Grow the model** adds tables: what they add comes back as a list
to tick, drawn on the canvas with the new parts marked, until you add it. The **Load** tab generates
the pipeline that loads the model and opens it in Pipeline mode, where **Loads ‹model›** goes back;
**Export** writes DDL (eight dialects), DBML, Mermaid and dbt; **YAML** edits it; **Share** puts the
model in a link (the data stays with you). After importing a legacy job, its **Migration** tab has
**Model it**: a model proposed from the job, with the step behind each reason and the job's coverage,
column by column.
