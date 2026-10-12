"""The MCP server: protocol, read tools, and edits that need a proposal before they're written."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from redcliff.mcp import Server

ROOT = Path(__file__).parents[2]
SPEC = "examples/specs/orders_revenue.yaml"


@pytest.fixture
def server(tmp_path):
    for folder in ("examples/specs", "examples/data", "examples/legacy"):
        shutil.copytree(ROOT / folder, tmp_path / folder)
    return Server(tmp_path)


def call(server, name, **arguments):
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
    return reply["result"]


def ok(server, name, **arguments):
    result = call(server, name, **arguments)
    assert not result["isError"], result["content"][0]["text"]
    return result["structuredContent"]


def test_handshake_and_tool_list(server):
    init = server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "redcliff"
    assert "propose_edit" in init["instructions"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = {t["name"]: t for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert set(tools) == {"list_pipelines", "describe_pipeline", "column_lineage", "impact", "compile", "preview",
                          "trace_rows", "propose_edit", "apply_edit", "import_legacy"}
    assert tools["apply_edit"]["annotations"]["readOnlyHint"] is False
    assert all(t["annotations"]["readOnlyHint"] for n, t in tools.items() if n != "apply_edit")
    assert server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601


def test_read_tools(server):
    specs = {p["spec"] for p in ok(server, "list_pipelines")["pipelines"]}
    assert {SPEC, "examples/specs/customer_cleanup.yaml"} <= specs
    described = ok(server, "describe_pipeline", spec=SPEC)
    shipped = next(s for s in described["steps"] if s["id"] == "shipped")
    assert shipped["does"] == "keeps rows where status = 'shipped'"
    sources = ok(server, "column_lineage", spec=SPEC, column="revenue_by_region.revenue")["sources"]
    assert {"column": "orders.amount", "kind": "direct", "how": "aggregation"} in sources
    assert "revenue_by_region" in ok(server, "impact", spec=SPEC, column="orders.status")["rows"]
    assert "INSERT OVERWRITE" in ok(server, "compile", spec=SPEC, to="spark")["code"]
    assert "dbt_project.yml" in ok(server, "compile", spec=SPEC, to="dbt")["files"]
    rows = ok(server, "preview", spec=SPEC, step="out")
    assert rows["columns"] == ["region", "revenue", "order_count"] and len(rows["rows"]) == 2
    traced = ok(server, "trace_rows", spec=SPEC, step="out", where="region = 'South'")["rows"]
    assert [o["order_id"] for o in traced[0]["sources"]["orders"]] == [1, 4, 8]


def test_an_edit_is_proposed_then_applied(server):
    path = server.root / SPEC
    before = path.read_text()
    prop = ok(server, "propose_edit", spec=SPEC, operations=[
        {"op": "update_step", "id": "shipped", "params": {"condition": "status IN ('shipped', 'pending')"}},
        {"op": "update_step", "id": "revenue_by_region", "params": {"measures": {"avg_amount": "AVG(amount)"}}},
    ])
    assert path.read_text() == before  # nothing written yet
    assert prop["summary"] == ("2 steps changed, 1 target column with new lineage, out: 2 → 3 rows")
    assert "-      condition: status = 'shipped'" in prop["diff"]
    assert "+        avg_amount: AVG(amount)" in prop["diff"]
    text = call(server, "propose_edit", spec=SPEC, operations=[{"op": "set_description", "description": "x"}])
    assert "```diff" in text["content"][0]["text"]

    assert ok(server, "apply_edit", proposal_id=prop["proposal_id"]) == {"written": SPEC}
    after = yaml.safe_load(path.read_text())
    assert after["nodes"][2]["params"]["condition"] == "status IN ('shipped', 'pending')"
    assert call(server, "apply_edit", proposal_id=prop["proposal_id"])["isError"]  # used up


def test_stale_and_broken_proposals_are_refused(server):
    prop = ok(server, "propose_edit", spec=SPEC, operations=[{"op": "set_description", "description": "Revenue"}])
    (server.root / SPEC).write_text((server.root / SPEC).read_text() + "\n")
    assert "changed since the proposal" in call(server, "apply_edit", proposal_id=prop["proposal_id"])["content"][0]["text"]

    bad = ok(server, "propose_edit", spec=SPEC, operations=[
        {"op": "update_step", "id": "shipped", "params": {"condition": "stauts = 'shipped'"}}])
    assert not bad["can_apply"] and any("stauts" in i for i in bad["issues"])
    refused = call(server, "apply_edit", proposal_id=bad["proposal_id"])
    assert refused["isError"] and "problems" in refused["content"][0]["text"]

    assert "no step named 'nope'" in call(server, "propose_edit", spec=SPEC, operations=[
        {"op": "remove_step", "id": "nope"}])["content"][0]["text"]
    assert "outside the workspace" in call(server, "describe_pipeline", spec="../../etc/passwd")["content"][0]["text"]


def test_add_and_remove_steps(server):
    prop = ok(server, "propose_edit", spec=SPEC, operations=[
        {"op": "add_step", "after": "shipped", "feeds": ["orders_with_region"],
         "step": {"id": "big", "type": "filter", "inputs": ["shipped"], "params": {"condition": "amount > 50"}}},
        {"op": "update_step", "id": "orders_with_region", "params": {"condition": "big.customer_id = customers.id"}},
    ])
    assert prop["can_apply"], prop["issues"]
    assert "big" in prop["report"] and "out: 2 → 2 rows" not in prop["summary"]
    ok(server, "apply_edit", proposal_id=prop["proposal_id"])
    ids = [n["id"] for n in yaml.safe_load((server.root / SPEC).read_text())["nodes"]]
    assert ids.index("big") == ids.index("shipped") + 1


def test_import_legacy_proposes_a_new_spec(server):
    prop = ok(server, "import_legacy", source="examples/legacy/powercenter/m_customer_revenue.xml",
              out="pipelines/customer_revenue.yaml")
    assert prop["new_file"] and "12 of 14" in prop["scorecard"].replace("**", "") or "86%" in prop["scorecard"]
    assert not (server.root / "pipelines").exists()
    ok(server, "apply_edit", proposal_id=prop["proposal_id"])
    assert ok(server, "describe_pipeline", spec="pipelines/customer_revenue.yaml")["issues"] == []


def test_stdio(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "describe_pipeline", "arguments": {"spec": SPEC}}},
    ]
    done = subprocess.run([sys.executable, "-m", "redcliff.cli", "mcp", "--root", str(tmp_path)],
                          input="".join(json.dumps(m) + "\n" for m in messages) + "not json\n",
                          capture_output=True, text=True, timeout=60)
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[0]["result"]["protocolVersion"] == "2025-03-26"
    assert replies[1]["result"]["structuredContent"]["name"] == "orders_revenue"
    assert replies[2]["error"]["code"] == -32700


def test_edits_keep_the_files_layout():
    from redcliff.mcp import apply_operations
    from redcliff.spec import edit_spec_text

    text = (ROOT / SPEC).read_text().replace("  - id: shipped\n", "  # only what left the warehouse\n  - id: shipped\n")
    data = yaml.safe_load(text)
    after = apply_operations(data, [{"op": "update_step", "id": "out", "params": {"mode": "append"}}])
    new = edit_spec_text(text, after)
    assert yaml.safe_load(new) == after
    changed = [line for line in new.splitlines() if line not in text.splitlines()]
    assert changed == ["      mode: append"]  # the comment, flow mappings and blank lines all stay
    renamed = edit_spec_text(text, {**after, "name": "revenue"})
    assert renamed.startswith("version: 0\nname: revenue\n") and yaml.safe_load(renamed)["name"] == "revenue"
