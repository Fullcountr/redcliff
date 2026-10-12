"""Pipeline variables: ${name} in params, defaults in the spec, overrides per run."""

from pathlib import Path

import duckdb
import pytest
import yaml

from redcliff.cli import main
from redcliff.compile import compile_pyspark, compile_query, compile_script
from redcliff.dbt import compile_dbt
from redcliff.lakeflow import compile_lakeflow
from redcliff.lineage import Ref, blast_radius, build_graph
from redcliff.spec import Pipeline, SpecError, bind, dump_spec, validate

ROOT = Path(__file__).parents[2]
SPECS = ROOT / "examples" / "specs"


def spec(**variables) -> dict:
    data = yaml.safe_load((SPECS / "orders_revenue.yaml").read_text())
    data["variables"] = variables or {"status": "shipped", "min_amount": 50}
    shipped = next(n for n in data["nodes"] if n["id"] == "shipped")
    shipped["params"]["condition"] = "status = '${status}' AND amount >= ${min_amount}"
    return data


def rows(pipeline):
    return sorted(duckdb.sql(compile_query(pipeline, "revenue_by_region", "duckdb", SPECS)).fetchall())


def test_defaults_and_overrides():
    p = Pipeline.from_dict(spec())
    assert validate(p) == []
    assert [r[0] for r in rows(p)] == ["North", "South"]
    assert bind(p).node("shipped").params["condition"] == "status = 'shipped' AND amount >= 50"
    p.variables["min_amount"] = 150
    assert rows(p)[0][:2] == ("South", 200)  # only order 4 is shipped and >= 150
    assert bind(p, {"status": "pending", "min_amount": 0}).node("shipped").params["condition"] == \
        "status = 'pending' AND amount >= 0"


def test_problems_are_reported():
    data = spec()
    del data["variables"]["status"]
    assert any("uses ${status}, which has no value" in str(i) for i in validate(Pipeline.from_dict(data)))
    data["variables"] = {"Bad-Name": 1, "ok": [1]}
    issues = [str(i) for i in validate(Pipeline.from_dict(data))]
    assert any("variable name 'Bad-Name'" in i for i in issues) and any("'ok' needs a text" in i for i in issues)
    with pytest.raises(SpecError):
        bind(Pipeline.from_dict(data))


def test_lineage_and_round_trip():
    p = Pipeline.from_dict(spec())
    graph = build_graph(p)
    assert graph.issues == []
    assert "revenue_by_region" in blast_radius(graph, Ref("orders", "status")).rows  # the filter still names it
    again = Pipeline.from_dict(yaml.safe_load(dump_spec(p)))
    assert again.variables == {"status": "shipped", "min_amount": 50}
    assert "${status}" in again.node("shipped").params["condition"]


def test_generated_code_bakes_values_and_says_so():
    p = Pipeline.from_dict(spec())
    script = compile_script(p, "spark")
    assert "--   min_amount = 50" in script and "amount >= 50" in script and "${" not in script
    assert "#   status = 'shipped'" in compile_pyspark(p)
    lake = compile_lakeflow(p)["transformations/orders_revenue.py"]
    assert "  status = 'shipped'" in lake and "${" not in lake


def test_dbt_keeps_variables_as_vars():
    files = compile_dbt(Pipeline.from_dict(spec()), "duckdb", SPECS)
    project = yaml.safe_load(files["dbt_project.yml"])
    assert project["vars"] == {"status": "shipped", "min_amount": 50}
    shipped = files["models/int_shipped.sql"]
    assert "status = '{{ var(\"status\") }}'" in shipped and "amount >= {{ var(\"min_amount\") }}" in shipped


def test_cli_var_overrides(tmp_path, capsys):
    path = tmp_path / "p.yaml"
    path.write_text(dump_spec(spec()).replace("../data/", str(ROOT / "examples" / "data") + "/"))
    assert main(["compile", str(path), "--to", "duckdb", "--node", "shipped", "--var", "min_amount=100"]) == 0
    assert "amount >= 100" in capsys.readouterr().out
    assert main(["run", str(path), "--node", "revenue_by_region", "--var", "status=pending", "--var", "min_amount=0"]) == 0
    assert "East | 15.25 | 1" in capsys.readouterr().out
