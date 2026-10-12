# Data models

Status: M1 to M5 done. Next: legacy imports mapped onto a model.
roadmap in the README tracks progress.

## The idea

Today Redcliff describes how data moves. A model describes what it should become. When someone adds
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
| `redcliff model propose a.csv b.csv -o model.yaml` | profile files and propose a Kimball model, with the reason for each choice |
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
- The MCP server has `describe_model`, `propose_model` (profile files, propose a model with reasons)
  and `generate_load`; their results are proposals that `apply_edit` writes, like any edit.

## In the playground

**Examples → Data models → sales** opens the model view: entities as cards (kind, SCD1/SCD2, grain;
attributes with PK, FK, SK, BK, Σ and SCD1 marks) and relationships drawn key to key. Click an entity
for its attributes. The panel checks the model, exports DDL (in eight dialects), DBML, Mermaid and dbt,
edits the YAML, and imports DDL or DBML (**Import → Data model**). **Import → Data model → From
your data** profiles the loaded tables and proposes a model; the **Load** tab generates its pipeline
and opens it on the canvas. **Pipelines** goes back to the canvas.
