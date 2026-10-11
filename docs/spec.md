# Pipeline spec reference (version 0)

A Redcliff pipeline is a YAML or JSON document. It is the single source of truth: the canvas
edits it, and code and lineage are generated from it. Editors can use
[`core/src/redcliff/pipeline.schema.json`](../core/src/redcliff/pipeline.schema.json) for
autocomplete; the Python validator adds checks a schema can't express.

```yaml
version: 0
name: orders_revenue          # required
description: optional text
nodes:                        # required, at least one
  - id: shipped               # lowercase letters, digits, underscores
    type: filter              # one of the eight types below
    inputs: [orders]          # ids of upstream nodes
    params:
      condition: status = 'shipped'
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
| `aggregate` | 1 | `group_by`: list of names, `measures`: name: SQL aggregate | group-by columns then measures |
| `union` | 2+ | `distinct` (false default) | columns of the first input, matched by position |
| `target` | 1 | `format`, `path` or `table`, `mode` (overwrite default, append) | none (writes data) |

Declaring `columns` on sources is optional but recommended: lineage and early error checks
work best when Redcliff knows each source's schema.

## What the validator checks

- Required fields, known node types, valid ids, no duplicate ids.
- Input counts per type, inputs that exist, no reading from a target, no cycles.
- Every SQL expression parses.
- It reports every problem at once, not just the first.

Column-level checks (a group-by column that doesn't exist, for example) arrive with the
lineage engine.
