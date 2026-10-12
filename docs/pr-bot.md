# Pipeline diffs and the pull request bot

A text diff of a spec shows which lines moved. `redcliff diff` shows what they mean:

```text
$ redcliff diff --git origin/main --data
```

```markdown
### `examples/specs/orders_revenue.yaml`

**2 steps changed, 1 target column with new lineage, out: 2 → 3 rows**

| Step | Change |
| --- | --- |
| `shipped` (filter) | **changed**: condition: `status = 'shipped'` → `status IN ('shipped', 'pending')` |
| `revenue_by_region` (aggregate) | **changed**: measures.avg_amount `AVG(amount)` added |

Targets downstream of a change: `out`

| Column | Before | After |
| --- | --- | --- |
| `out.avg_amount` | *new* | `orders.amount` |

| Target | Rows before | Rows after | Added | Removed |
| --- | ---: | ---: | ---: | ---: |
| `out` | 2 | 3 | 1 | 0 |
```

Each report has four parts:

| Part | What it says |
| --- | --- |
| Steps | steps added, removed or changed, with every changed setting before and after |
| Columns | columns that appear in or disappear from a step |
| Lineage | target columns whose source columns changed (a new column, a dropped one, or a rewired one) |
| Data | with `--data`: every target runs on the sample data in the repository, before and after, in DuckDB; row counts, rows added and removed, and a few examples of each |

It also lists problems in the new spec (an unknown column, a cycle), which `--fail-on-issues` turns
into a failing exit code.

## Usage

| Command | Compares |
| --- | --- |
| `redcliff diff old.yaml new.yaml` | two spec files |
| `redcliff diff --git REF [PATH ...]` | every spec changed since `REF` (the working tree against `REF`); files that aren't pipeline specs are skipped |
| `--data [--db file.duckdb]` | also the rows each target returns; table sources are read from `--db` |
| `--format json`, `-o FILE` | machine-readable output, or write to a file |

With `--git`, both versions read the sample data in the working tree, so the data section shows
what the spec change does on the same rows. Specs in a pull request may read only files in the
repository: a source path outside it is reported as an issue, and the data runs in a DuckDB that
can't reach other files or the network.

## The pull request bot

Add this workflow to a repository that keeps Redcliff specs:

```yaml
name: Pipeline diff
on:
  pull_request:
    paths: ["pipelines/**"]
permissions:
  contents: read
  pull-requests: write
jobs:
  diff:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: Fullcountr/redcliff@main
```

Every pull request that touches a spec gets one comment, updated in place on each push, and the
same report in the job summary.

`@main` follows the latest code; pin a release, such as `Fullcountr/redcliff@v0.1.0`, to keep the
report stable.

| Input | Default | Does |
| --- | --- | --- |
| `base` | the pull request's base branch | the git ref to compare with |
| `paths` | the whole repository | look for specs only under these paths (space separated) |
| `data` | `true` | compare rows on the sample data |
| `comment` | `true` | post or update the pull request comment |
| `fail-on-issues` | `false` | fail the check when a changed spec has problems |
| `python-version` | `3.12` | the Python that runs Redcliff |

Pull requests from forks get a read-only token, so they can't be commented on; the report is still
in the job summary. This repository runs the bot on its own examples
([`.github/workflows/pipeline-diff.yml`](../.github/workflows/pipeline-diff.yml)).
