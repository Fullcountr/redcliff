"""M5: model diffs (redcliff diff and the PR bot) and the MCP model tools."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from redcliff.cli import main
from redcliff.diff import diff_models
from redcliff.model import Model, load_model, parse_model

ROOT = Path(__file__).parents[2]
SALES = ROOT / "examples" / "models" / "sales.yaml"


def changed_model() -> Model:
    data = load_model(SALES).to_dict()
    dim = data["entities"][0]
    dim["attributes"] = [a for a in dim["attributes"] if a["name"] != "email"]
    next(a for a in dim["attributes"] if a["name"] == "region")["history"] = "scd1"
    dim["attributes"].append({"name": "segment", "type": "varchar"})
    data["entities"][2]["grain"] = "one order line"
    data["entities"].append({"id": "dim_store", "kind": "dimension", "attributes": [
        {"name": "store_key", "type": "bigint", "role": "surrogate_key"}, {"name": "store_id", "type": "bigint", "role": "business_key"}]})
    data["relationships"] = data["relationships"][:1]
    return Model.from_dict(data)


def test_model_diff():
    d = diff_models(load_model(SALES), changed_model())
    entities = dict(d.entities)
    assert entities["dim_store"].startswith("**added**") and entities["fct_orders"].startswith("**changed**: grain")
    changes = {(e, a): c for e, a, c in d.attributes}
    assert changes[("dim_customer", "segment")] == "**added**: varchar"
    assert changes[("dim_customer", "email")] == "**removed**"
    assert "history: `null` → `scd1`" in changes[("dim_customer", "region")]
    assert d.relationships == ["**removed**: `fct_orders.order_date_key` → `dim_date.date_key`"]
    md = d.to_markdown("model", 3)
    assert "(data model)" in md and "| `dim_customer.segment` | **added**: varchar |" in md
    assert diff_models(load_model(SALES), load_model(SALES)).empty


def test_diff_reports_models_in_git_mode(tmp_path, monkeypatch, capsys):
    if not shutil.which("git"):
        pytest.skip("git isn't installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *a],  # noqa: E731
                                    cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (repo / "model.yaml").write_text(SALES.read_text())
    git("add", ".")
    git("commit", "-qm", "base")
    from redcliff.model import dump_model

    (repo / "model.yaml").write_text(dump_model(changed_model()))
    monkeypatch.chdir(repo)
    assert main(["diff", "--git", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "## Redcliff: what this change does to the pipelines and models" in out
    assert "### `model.yaml` (data model)" in out and "| `dim_store` | **added**: dimension" in out


def test_mcp_model_tools(tmp_path):
    pytest.importorskip("duckdb")
    from redcliff.mcp import Server

    for f in ("orders.csv", "customers.csv"):
        (tmp_path / "data").mkdir(exist_ok=True)
        shutil.copy(ROOT / "examples" / "data" / f, tmp_path / "data" / f)
    server = Server(tmp_path)

    def call(tool, **args):
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        result = reply["result"]
        assert not result["isError"], result["content"][0]["text"]
        return result["structuredContent"]

    tools = {t["name"]: t for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert all(tools[t]["annotations"]["readOnlyHint"] for t in ("propose_model", "generate_load", "describe_model"))

    proposal = call("propose_model", files=["data/orders.csv", "data/customers.csv"], out="models/sales.yaml", name="sales")
    assert proposal["can_apply"] and "fct_orders" in proposal["report"] and "dim_customer" in proposal["reasons"]
    assert not (tmp_path / "models" / "sales.yaml").exists()  # only a proposal
    call("apply_edit", proposal_id=proposal["proposal_id"])
    model = parse_model((tmp_path / "models" / "sales.yaml").read_text())
    assert model.entity("dim_customer").source == "../data/customers.csv"

    described = call("describe_model", model="models/sales.yaml")
    assert described["issues"] == [] and {e["id"] for e in described["entities"]} == {"dim_customer", "dim_date", "fct_orders"}

    load = call("generate_load", model="models/sales.yaml", out="pipelines/load_sales.yaml")
    assert load["can_apply"]
    call("apply_edit", proposal_id=load["proposal_id"])
    assert "path: ../data/customers.csv" in (tmp_path / "pipelines" / "load_sales.yaml").read_text()

    vault = call("propose_model", files=["data/orders.csv", "data/customers.csv"], out="models/vault.yaml", style="data_vault")
    assert "hub_customer" in vault["report"]

    outside = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "propose_model", "arguments": {"files": ["../x.csv"], "out": "m.yaml"}}})
    assert outside["result"]["isError"]  # paths stay in the workspace
    assert json.loads(json.dumps(described))  # results are plain JSON
