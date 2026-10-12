# The MCP server: AI agents that edit pipelines, with a diff you accept first

`redcliff mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server. An agent
connected to it can read your pipelines, explain them, trace columns and rows, generate code, and
propose changes. A change is never written until you've seen what it does and said yes.

```text
pip install "./core[cli]"          # from a checkout of this repository
redcliff mcp --root path/to/repo   # speaks MCP over stdio
```

It uses only the Python standard library to talk MCP, and reads and writes only spec files under
`--root`. `--db warehouse.duckdb` gives previews and traces a DuckDB file holding table sources.

## Connecting a client

Claude Code:

```text
claude mcp add redcliff -- redcliff mcp --root .
```

Claude Desktop, Cursor and other clients take the same command in their JSON config:

```json
{
  "mcpServers": {
    "redcliff": { "command": "redcliff", "args": ["mcp", "--root", "/path/to/repo"] }
  }
}
```

## Tools

| Tool | Does | Writes? |
| --- | --- | --- |
| `list_pipelines` | every spec under the root, with its steps and targets | no |
| `describe_pipeline` | steps in run order, what each does in one line, output columns, problems | no |
| `column_lineage` | the source columns behind a column, direct or indirect | no |
| `impact` | a column's blast radius: values that change, rows that change, steps a rename breaks | no |
| `compile` | DuckDB or Spark SQL, PySpark, or a whole dbt or Lakeflow project | no |
| `preview` | a step's first rows, run in DuckDB | no |
| `trace_rows` | rows of a step with the source rows behind each one | no |
| `propose_edit` | validates a change and returns what it does, plus a text diff | no |
| `import_legacy` | turns a SQL script or PowerCenter export into a proposed new spec, with its scorecard | no |
| `apply_edit` | writes an accepted proposal | **yes** |

## Edits are proposals

`propose_edit` takes a spec and either a complete new spec or a list of operations:

```json
[
  {"op": "update_step", "id": "shipped", "params": {"condition": "status IN ('shipped', 'pending')"}},
  {"op": "add_step", "after": "shipped", "feeds": ["orders_with_region"],
   "step": {"id": "big", "type": "filter", "inputs": ["shipped"], "params": {"condition": "amount > 50"}}},
  {"op": "remove_step", "id": "old_step", "rewire_to": "shipped"},
  {"op": "rename_step", "id": "big", "new_id": "large_orders"}
]
```

It returns the same report as [`redcliff diff`](pr-bot.md) (steps, columns, lineage of target
columns, rows before and after on sample data) and a unified diff of the file:

```diff
@@ -20,7 +20,7 @@
     type: filter
     inputs: [orders]
     params:
-      condition: status = 'shipped'
+      condition: status IN ('shipped','pending')
```

The agent shows you both; `apply_edit` with the proposal id writes the file. Steps the edit
doesn't touch keep their exact text, comments included. `apply_edit` refuses when:

- the file changed after the proposal (propose again);
- the new spec has problems, such as an unknown column or a cycle, unless `force` is set;
- the proposal was already applied.

## Safety

- Paths outside `--root` are refused.
- The only tool that writes is `apply_edit`, and it's marked as such (`readOnlyHint: false`), so
  clients that ask before a write will ask.
- Generated code is never written: the spec is the source of truth, and code is generated from it.
