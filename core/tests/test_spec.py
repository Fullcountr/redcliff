"""Tests for the pipeline spec model and validator."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from redcliff import Pipeline, SpecError, json_schema, load_spec, parse_spec, validate

EXAMPLES = sorted((Path(__file__).parents[2] / "examples" / "specs").glob("*.yaml"))

BASE = {
    "version": 0,
    "name": "t",
    "nodes": [
        {"id": "src", "type": "source", "params": {"format": "csv", "path": "a.csv"}},
        {"id": "f", "type": "filter", "inputs": ["src"], "params": {"condition": "x > 1"}},
        {"id": "out", "type": "target", "inputs": ["f"], "params": {"format": "table", "table": "t_out"}},
    ],
}


def spec(**changes):
    """A copy of BASE with node params or fields patched: spec(f={"params": {...}})."""
    data = copy.deepcopy(BASE)
    for node_id, patch in changes.items():
        node = next(n for n in data["nodes"] if n["id"] == node_id)
        node.update(patch)
    return data


def messages(data) -> list[str]:
    return [str(i) for i in validate(Pipeline.from_dict(data))]


# ---------------------------------------------------------------- examples


def test_three_examples_exist():
    assert len(EXAMPLES) == 3


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_is_valid(path):
    pipeline = load_spec(path)
    assert pipeline.name == path.stem


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_matches_json_schema(path):
    import yaml

    jsonschema.validate(yaml.safe_load(path.read_text()), json_schema())


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_round_trip(path):
    pipeline = load_spec(path)
    again = parse_spec(json.dumps(pipeline.to_dict()))
    assert again == pipeline


# ---------------------------------------------------------------- structure


def test_base_is_valid():
    assert messages(BASE) == []


def test_json_text_is_accepted():
    assert parse_spec(json.dumps(BASE)).name == "t"


def test_topo_order_puts_inputs_first():
    data = copy.deepcopy(BASE)
    data["nodes"].reverse()
    order = [n.id for n in Pipeline.from_dict(data).topo_order()]
    assert order == ["src", "f", "out"]


def test_downstream():
    assert Pipeline.from_dict(BASE).downstream("src") == ["f"]


def test_cycle_is_reported():
    data = {
        "version": 0,
        "name": "loop",
        "nodes": [
            {"id": "a", "type": "filter", "inputs": ["b"], "params": {"condition": "x"}},
            {"id": "b", "type": "filter", "inputs": ["a"], "params": {"condition": "x"}},
        ],
    }
    assert any("cycle detected" in m for m in messages(data))


def test_all_issues_are_collected():
    data = spec(f={"inputs": ["missing"], "params": {"condition": "x >"}}, out={"type": "sink"})
    with pytest.raises(SpecError) as err:
        parse_spec(json.dumps(data))
    text = "\n".join(str(i) for i in err.value.issues)
    assert "input 'missing' does not exist" in text
    assert "not valid SQL" in text
    assert "unknown node type 'sink'" in text


# ---------------------------------------------------------------- one rule per case


@pytest.mark.parametrize(
    "data, expected",
    [
        ({**BASE, "version": 1}, "unsupported spec version"),
        ({**BASE, "name": ""}, "'name' is required"),
        ({**BASE, "nodes": []}, "at least one node"),
        (spec(f={"id": "Bad-Id"}), "must match"),
        (spec(f={"id": "src"}), "duplicate node id"),
        (spec(f={"inputs": []}), "filter needs exactly 1 input(s), got 0"),
        (spec(src={"inputs": ["f"]}), "source needs exactly 0 input(s)"),
        (spec(f={"params": {}}), "'condition' must be a non-empty SQL expression"),
        (spec(f={"params": {"condition": "x >"}}), "not valid SQL"),
        (spec(src={"params": {"format": "xml", "path": "a"}}), "'format' must be one of"),
        (spec(src={"params": {"format": "csv"}}), "format 'csv' needs a 'path'"),
        (spec(out={"params": {"format": "table"}}), "format 'table' needs a 'table'"),
        (spec(out={"params": {"format": "csv", "path": "o", "mode": "merge"}}), "'mode' must be one of"),
        (spec(src={"params": {"format": "csv", "path": "a", "columns": {"x": ""}}}), "column 'x' needs a type"),
    ],
)
def test_structural_rules(data, expected):
    assert any(expected in m for m in messages(data)), messages(data)


def chain(node: dict) -> dict:
    """BASE with node inserted between f and out."""
    data = copy.deepcopy(BASE)
    node = {"id": "n", "inputs": ["f"], **node}
    data["nodes"].insert(2, node)
    data["nodes"][3]["inputs"] = ["n"]
    return data


@pytest.mark.parametrize(
    "node, expected",
    [
        ({"type": "select", "params": {"columns": []}}, "non-empty list"),
        ({"type": "select", "params": {"columns": ["a", "a"]}}, "duplicate columns"),
        ({"type": "derive", "params": {"columns": {"New Col": "1"}}}, "must match"),
        ({"type": "derive", "params": {"columns": {"c": "(("}}}, "not valid SQL"),
        ({"type": "aggregate", "params": {}}, "'measures' must be a non-empty mapping"),
        ({"type": "sql", "params": {"query": "DELETE FROM f"}}, "must be a SELECT"),
        ({"type": "sql", "params": {"query": "SELECT * FROM other"}}, "reads 'other', which is not one of its inputs"),
        ({"type": "aggregate", "params": {"group_by": "a", "measures": {"n": "COUNT(*)"}}}, "must be a list"),
        ({"type": "union", "params": {}}, "union needs at least 2 input(s)"),
        ({"type": "join", "params": {"condition": "a = b"}}, "join needs exactly 2 input(s)"),
    ],
)
def test_param_rules(node, expected):
    assert any(expected in m for m in messages(chain(node))), messages(chain(node))


def test_join_how_is_checked():
    data = copy.deepcopy(BASE)
    data["nodes"].insert(1, {"id": "src2", "type": "source", "params": {"format": "csv", "path": "b.csv"}})
    data["nodes"].insert(
        2, {"id": "j", "type": "join", "inputs": ["src", "src2"], "params": {"how": "cross", "condition": "1 = 1"}}
    )
    data["nodes"][3]["inputs"] = ["j"]
    assert any("'how' must be one of" in m for m in messages(data))


def test_reading_from_a_target_is_rejected():
    data = copy.deepcopy(BASE)
    data["nodes"].append({"id": "after", "type": "filter", "inputs": ["out"], "params": {"condition": "1 = 1"}})
    assert any("cannot read from target" in m for m in messages(data))


def test_schema_rejects_unknown_fields():
    data = copy.deepcopy(BASE)
    data["nodes"][0]["colour"] = "red"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, json_schema())


def test_yaml_boolean_key_is_explained():
    data = copy.deepcopy(BASE)
    data["nodes"][1]["params"] = {True: "x > 1"}  # what YAML makes of a bare `on:`
    assert any("read as true/false" in m for m in messages(data))


def test_distinct_is_an_aggregate_without_measures():
    data = chain({"type": "aggregate", "params": {"group_by": ["region"]}})
    assert messages(data) == []


def test_sql_node_may_use_its_own_ctes():
    data = chain({"type": "sql", "params": {"query": "WITH x AS (SELECT * FROM f) SELECT * FROM x ORDER BY 1"}})
    assert messages(data) == []
