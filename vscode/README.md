# Redcliff for VS Code

See a [Redcliff](https://github.com/Fullcountr/redcliff) pipeline spec as a canvas next to its YAML.
The YAML stays the source of truth: edit it and the canvas follows as you type.

- **Canvas**: every step with its columns, laid out from sources to targets.
- **Column lineage**: hover a column to see where it comes from, back to the sources.
- **Blast radius**: click a column to see which values and rows change, and which steps break if
  you rename it.
- **Jump to the YAML**: click a step, or an issue, and the editor shows its lines.
- **Problems in the editor**: unknown columns and broken references are underlined on the step
  that has them, as you type.
- **Generated code**: Spark SQL, PySpark or DuckDB SQL for the whole pipeline or one step.
- Follows your light, dark or high-contrast theme.

## Getting started

1. Install Redcliff, which does the analysis: `pip install redcliff` (0.3.0 or later).
2. Open a pipeline spec (YAML with `version:` and `nodes:`), for example
   [`examples/specs/orders_revenue.yaml`](https://github.com/Fullcountr/redcliff/blob/main/examples/specs/orders_revenue.yaml).
3. Click the canvas icon in the editor's title bar, or run **Redcliff: Show Pipeline Canvas**.

Don't have a spec yet? `redcliff import` turns legacy SQL, Informatica PowerCenter, SSIS, Talend
and DataStage jobs into one.

## Settings

| Setting | Default | Does |
| --- | --- | --- |
| `redcliff.command` | `redcliff` | How to run Redcliff: a full path, or for example `python3 -m redcliff.cli` in a virtual environment |
| `redcliff.diagnostics` | `true` | Show a spec's problems in the editor |

## How it works

The extension runs `redcliff bridge`, a Python process that answers in JSON lines with the same
analysis the [browser playground](https://fullcountr.github.io/redcliff/) runs. Nothing leaves your
machine. The **Redcliff** output channel logs the bridge.

Apache 2.0.
