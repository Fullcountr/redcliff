"""Semantic diffs of pipelines: steps, columns, lineage and data."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import duckdb
import pytest
import yaml

from redcliff.cli import main
from redcliff.diff import data_diff, diff_pipelines
from redcliff.spec import Pipeline, dump_spec

ROOT = Path(__file__).parents[2]
SPECS = ROOT / "examples" / "specs"


def spec(name="orders_revenue") -> dict:
    return yaml.safe_load((SPECS / f"{name}.yaml").read_text())


def node(data: dict, nid: str) -> dict:
    return next(n for n in data["nodes"] if n["id"] == nid)


def changed() -> dict:
    """orders_revenue, now counting pending orders too and averaging the amount."""
    data = spec()
    node(data, "shipped")["params"]["condition"] = "status IN ('shipped', 'pending')"
    node(data, "revenue_by_region")["params"]["measures"]["avg_amount"] = "AVG(amount)"
    return data


def test_identical_specs_have_no_diff():
    diff = diff_pipelines(Pipeline.from_dict(spec()), Pipeline.from_dict(spec()))
    assert diff.empty and diff.summary() == "no change to what the pipeline does"


def test_steps_columns_lineage_and_data():
    base, head = Pipeline.from_dict(spec()), Pipeline.from_dict(changed())
    diff = diff_pipelines(base, head)
    assert [(s.id, s.change) for s in diff.steps] == [("shipped", "changed"), ("revenue_by_region", "changed")]
    assert diff.steps[0].details == ["condition: `status = 'shipped'` → `status IN ('shipped', 'pending')`"]
    assert diff.steps[1].details == ["measures.avg_amount `AVG(amount)` added"]
    assert [(c.step, c.added, c.removed) for c in diff.columns] == [("revenue_by_region", ["avg_amount"], [])]
    assert [(lc.target, lc.column, lc.change, lc.after) for lc in diff.lineage] == \
        [("out", "avg_amount", "added", ["orders.amount"])]
    assert diff.affected_targets == ["out"]

    diff.data = data_diff(base, head, duckdb.connect(), SPECS, SPECS)
    (d,) = diff.data
    assert (d.rows_before, d.rows_after, d.added, d.removed) == (2, 3, 1, 0)
    assert d.added_sample[0][0] == "East"  # the only region with a pending order and no shipped one
    md = diff.to_markdown()
    assert "**2 steps changed, 1 target column with new lineage, out: 2 → 3 rows**" in md
    assert "| `out.avg_amount` | *new* | `orders.amount` |" in md


def test_a_rewired_column_shows_its_new_sources():
    data = spec()
    node(data, "revenue_by_region")["params"]["measures"]["revenue"] = "SUM(amount) + COUNT(id)"
    del node(data, "revenue_by_region")["params"]["measures"]["order_count"]
    diff = diff_pipelines(Pipeline.from_dict(spec()), Pipeline.from_dict(data))
    by_col = {lc.column: lc for lc in diff.lineage}
    assert by_col["revenue"].change == "changed" and "customers.id" in by_col["revenue"].after
    assert by_col["order_count"].change == "removed"
    assert diff.columns[0].removed == ["order_count"]


def test_added_and_removed_steps_and_broken_specs():
    data = spec()
    data["nodes"].insert(3, {"id": "big", "type": "filter", "inputs": ["shipped"], "params": {"condition": "amount > 50"}})
    node(data, "orders_with_region")["inputs"] = ["big", "customers"]
    node(data, "orders_with_region")["params"]["condition"] = "big.customer_id = customers.id"
    diff = diff_pipelines(Pipeline.from_dict(spec()), Pipeline.from_dict(data))
    assert ("big", "added") in [(s.id, s.change) for s in diff.steps]
    assert "inputs: shipped, customers → big, customers" in next(s for s in diff.steps if s.id == "orders_with_region").details

    broken = copy.deepcopy(data)
    node(broken, "big")["params"]["condition"] = "amout > 50"
    diff = diff_pipelines(Pipeline.from_dict(spec()), Pipeline.from_dict(broken))
    assert any("amout" in i for i in diff.issues)


def test_git_mode_finds_changed_specs(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "pipelines").mkdir(parents=True)
    (repo / "data").mkdir()
    for f in ("orders.csv", "customers.csv"):
        (repo / "data" / f).write_text((ROOT / "examples" / "data" / f).read_text())
    (repo / "pipelines" / "revenue.yaml").write_text(dump_spec(spec()))
    (repo / "pipelines" / "notes.yaml").write_text("just: a config file\n")
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *a],  # noqa: E731
                                    cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-qm", "base")
    (repo / "pipelines" / "revenue.yaml").write_text(dump_spec(changed()))
    (repo / "pipelines" / "notes.yaml").write_text("just: another config\n")
    (repo / "pipelines" / "new.yaml").write_text(dump_spec(spec("customer_cleanup")))
    monkeypatch.chdir(repo)

    assert main(["diff", "--git", "HEAD", "--data"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("<!-- redcliff-diff -->\n## Redcliff")
    assert "### `pipelines/revenue.yaml`" in out and "out: 2 → 3 rows" in out
    assert "notes.yaml" not in out  # not a pipeline
    assert "new.yaml" not in out  # untracked files aren't part of a git diff

    git("add", ".")
    assert main(["diff", "--git", "HEAD", "--format", "json"]) == 0
    import json

    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"pipelines/revenue.yaml", "pipelines/new.yaml"}
    assert all(s["change"] == "added" for s in result["pipelines/new.yaml"]["steps"])


@pytest.mark.parametrize("fail", [False, True])
def test_issues_can_fail_the_check(tmp_path, capsys, fail):
    data = spec()
    node(data, "shipped")["params"]["condition"] = "stauts = 'shipped'"
    (tmp_path / "a.yaml").write_text(dump_spec(spec()))
    (tmp_path / "b.yaml").write_text(dump_spec(data))
    code = main(["diff", str(tmp_path / "a.yaml"), str(tmp_path / "b.yaml")] + (["--fail-on-issues"] if fail else []))
    assert code == (1 if fail else 0)
    assert "Issues in the new spec" in capsys.readouterr().out
