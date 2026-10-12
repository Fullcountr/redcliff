# Data models

Status: design agreed, M1 in progress. This page is the source of truth for the decisions; the
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
| `redcliff model import model.dbml` | a model from DBML, including the roles and history `export --to dbml` writes into notes |

Imports take the kind from the table name (`dim_`, `fct_`, `hub_`, `lnk_`, `sat_`; anything else is
a `table`), call an integer `*_key`/`*_sk`/`*_hk` primary key a surrogate key and any other primary
key a business key, and call decimal columns, and integer columns named like quantities, measures in
facts. Whatever they can't place (an index, a many-to-many reference, a fact with no grain) comes
back as a warning, never a silent drop. Example: [`examples/models/sales.yaml`](../examples/models/sales.yaml).
