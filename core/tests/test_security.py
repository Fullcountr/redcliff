"""Regression tests for the security audit (run-1): spec text that someone else wrote must stay data.

Each test names the finding it pins. A spec may come from a share link, a pull request, an imported
legacy file or a repository an agent opens, so none of its strings may turn into extra SQL
statements, Python, Jinja, or file access outside the folder the person allowed.
"""

from __future__ import annotations

import ast
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from redcliff.cli import main
from redcliff.compile import CompileError, compile_pyspark, compile_query, compile_script
from redcliff.dbt import _jinja_literal, compile_dbt, project_path
from redcliff.lakeflow import compile_lakeflow
from redcliff.spec import Pipeline, dump_spec, validate

ROOT = Path(__file__).parents[2]
STACKED = "range(1)) SELECT 1); COPY (SELECT 'x' AS m) TO 'pwned.csv' (FORMAT csv); SELECT COUNT(*) FROM (WITH s AS (SELECT 1 AS a"


def spec(**over) -> dict:
    data = {
        "version": 0,
        "name": "orders",
        "nodes": [
            {"id": "orders", "type": "source",
             "params": {"format": "csv", "path": "orders.csv", "columns": {"id": "int", "status": "varchar"}}},
            {"id": "shipped", "type": "filter", "inputs": ["orders"], "params": {"condition": "status = 'shipped'"}},
            {"id": "out", "type": "target", "inputs": ["shipped"], "params": {"format": "table", "table": "out_t"}},
        ],
    }
    data.update(over)
    return data


def node(data: dict, nid: str) -> dict:
    return next(n for n in data["nodes"] if n["id"] == nid)


def messages(data: dict) -> str:
    return "\n".join(str(i) for i in validate(Pipeline.from_dict(data)))


# ------------------------------------------------------------------ raw table / name strings in SQL


@pytest.mark.parametrize("table", [STACKED, "out_t; DROP TABLE x", "out_t AS SELECT 1 AS x", "a.b.c.d", "'file.csv'"])
def test_table_must_be_a_table_name(table):
    data = spec()
    node(data, "out")["params"]["table"] = table
    assert "must be a table name" in messages(data)
    source = spec()
    node(source, "orders")["params"] = {"format": "table", "table": table}
    assert "must be a table name" in messages(source)


def test_compilers_refuse_a_bad_table_even_without_validation():
    data = spec()
    node(data, "orders")["params"] = {"format": "table", "table": STACKED}
    with pytest.raises(CompileError):
        compile_query(Pipeline.from_dict(data), "orders", "duckdb")
    data = spec()
    node(data, "out")["params"]["table"] = "out_t; COPY (SELECT 1) TO 'x.csv'"
    with pytest.raises(CompileError):
        compile_script(Pipeline.from_dict(data), "duckdb")


@pytest.mark.parametrize("ok", ["out_t", "mart.daily", "cat.mart.Daily_2"])
def test_real_table_names_still_validate(ok):
    data = spec()
    node(data, "out")["params"]["table"] = ok
    assert messages(data) == ""


def test_a_name_with_a_newline_is_refused_and_never_breaks_a_comment():
    data = spec(name="orders\nCOPY (SELECT 1) TO 'x.csv';\n--")
    assert "single line" in messages(data)
    script = compile_script(Pipeline.from_dict(data), "duckdb")
    assert "\nCOPY (SELECT 1) TO 'x.csv'" not in script
    assert script.splitlines()[0].startswith("-- Redcliff pipeline: orders COPY")


# ------------------------------------------------------------------ table functions in spec SQL


@pytest.mark.parametrize("sql", [
    "(SELECT count(*) FROM read_text('/etc/hostname')) > 0",
    "id IN (SELECT 1 FROM read_csv('https://example.com/x.csv'))",
    "EXISTS (SELECT 1 FROM 'secret.csv')",
])
def test_expressions_may_not_read_files(sql):
    data = spec()
    node(data, "shipped")["params"]["condition"] = sql
    assert "read files with a source step" in messages(data)


def test_sql_steps_and_checks_may_not_read_files():
    data = spec()
    data["nodes"].insert(2, {"id": "q", "type": "sql", "inputs": ["shipped"],
                             "params": {"query": "SELECT content AS k FROM read_text('/etc/hostname')"}})
    node(data, "out")["inputs"] = ["q"]
    assert "may read only its inputs" in messages(data)
    data = spec()
    node(data, "shipped")["expect"] = [{"condition": "(SELECT count(*) FROM read_text('/etc/passwd')) > 0"}]
    assert "check the step's own columns" in messages(data)


def test_trace_where_may_not_read_files(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from redcliff.trace import trace_rows

    (tmp_path / "orders.csv").write_text("id,status\n1,shipped\n")
    with pytest.raises(CompileError):
        trace_rows(duckdb.connect(), Pipeline.from_dict(spec()), "shipped",
                   "(SELECT count(*) FROM read_text('/etc/hostname')) > 0", tmp_path)


# ------------------------------------------------------------------ generated Python


def top_level_calls(code: str) -> list[str]:
    tree = ast.parse(code)
    return [ast.unparse(s) for s in tree.body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)]


BREAKOUT = '"""\nopen("MARKER.txt", "w").write("x")\n"""'


def test_a_name_cannot_close_the_pyspark_or_lakeflow_docstring():
    pipeline = Pipeline.from_dict(spec(name="orders" + BREAKOUT))
    lakeflow = next(v for k, v in compile_lakeflow(pipeline).items() if k.endswith(".py"))
    for code in (compile_pyspark(pipeline), lakeflow):
        assert not any("MARKER" in c for c in top_level_calls(code))
    assert '"""' in pipeline.name and "single line" in messages(spec(name="orders" + BREAKOUT))


def test_merge_delete_when_cannot_close_the_lakeflow_docstring():
    data = spec()
    node(data, "out")["params"].update({"mode": "merge", "keys": ["id"],
                                        "delete_when": "status = '" + BREAKOUT.replace("'", "''") + "'"})
    pipeline = Pipeline.from_dict(data)
    assert messages(data) == ""
    code = next(v for k, v in compile_lakeflow(pipeline).items() if k.endswith(".py"))
    assert not any("MARKER" in c for c in top_level_calls(code))


# ------------------------------------------------------------------ dbt Jinja


def test_jinja_in_spec_text_is_literal_in_dbt():
    data = spec()
    node(data, "shipped")["params"]["condition"] = "status = '{{ env_var(\"SECRET\") }}' OR status = '{% if 1 %}x{% endif %}'"
    node(data, "shipped")["expect"] = [{"condition": "status <> '{{ env_var(\"SECRET\") }}'"}]
    files = compile_dbt(Pipeline.from_dict(data), "duckdb")
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()
    env.globals.update(ref=lambda *a: "REF", source=lambda *a: "SRC", config=lambda **k: "",
                       env_var=lambda *a: pytest.fail("spec text was rendered as Jinja"))
    tests = [p for p in files if p.startswith("tests/shipped__")]
    assert tests
    for path in ["models/int_shipped.sql", *tests]:
        rendered = env.from_string(files[path]).render()
        assert "{{ env_var(\"SECRET\") }}" in rendered, path


def test_jinja_literal_round_trips():
    jinja2 = pytest.importorskip("jinja2")
    text = "a {{ b }} {% c %} {# d #} }} %}"
    assert jinja2.Environment().from_string(_jinja_literal(text)).render() == text


# ------------------------------------------------------------------ dbt project files stay in -o


def test_dbt_files_stay_inside_the_output_folder(tmp_path):
    data = spec()
    node(data, "out")["params"] = {"format": "csv", "path": "../../../escaped/out.csv"}
    files = compile_dbt(Pipeline.from_dict(data), "duckdb")
    assert not any(".." in Path(k).parts for k in files)
    with pytest.raises(ValueError):
        project_path(tmp_path / "proj", "../escaped/.gitkeep")
    with pytest.raises(ValueError):
        project_path(tmp_path / "proj", "/etc/x")


# ------------------------------------------------------------------ MCP server


@pytest.fixture
def workspace(tmp_path):
    pytest.importorskip("duckdb")
    from redcliff.mcp import Server

    root = tmp_path / "ws"
    root.mkdir()
    (root / "orders.csv").write_text("id,status\n1,shipped\n")
    (tmp_path / "SECRET.csv").write_text("id,status\n9,DUMMY_SECRET\n")
    return Server(root), root, tmp_path


def call(server, name, **arguments):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})["result"]


def test_mcp_read_only_tools_cannot_write_files(workspace):
    import json

    server, root, outside = workspace
    data = spec()
    node(data, "orders")["params"] = {"format": "table", "table": STACKED, "columns": {"a": "int"}}
    node(data, "shipped")["params"]["condition"] = "a = 1"
    result = call(server, "propose_edit", spec="new.json", new_spec=json.dumps(data))
    assert not list(root.rglob("pwned.csv")) and not list(outside.glob("pwned.csv"))
    assert result["isError"] or not result["structuredContent"]["can_apply"]


def test_mcp_refuses_spec_paths_outside_the_root(workspace):
    server, root, _outside = workspace
    data = spec()
    node(data, "orders")["params"]["path"] = "../SECRET.csv"
    (root / "leak.yaml").write_text(dump_spec(data))
    for tool, args in (("preview", {"step": "orders"}), ("compile", {"to": "dbt"}), ("run_checks", {})):
        result = call(server, tool, spec="leak.yaml", **args)
        assert result["isError"], tool
        assert "DUMMY_SECRET" not in result["content"][0]["text"]


def test_mcp_duckdb_is_locked_to_the_root(workspace):
    import duckdb

    server, root, outside = workspace
    con = server.duck()
    assert con.sql(f"SELECT count(*) FROM read_csv_auto('{root / 'orders.csv'}')").fetchone() == (1,)
    for sql in (f"SELECT * FROM read_csv_auto('{outside / 'SECRET.csv'}')",
                f"COPY (SELECT 1) TO '{outside / 'x.csv'}'", "SET enable_external_access = true"):
        with pytest.raises(duckdb.Error):
            con.sql(sql)


# ------------------------------------------------------------------ redcliff diff (the PR bot)


def git_repo(path: Path):
    path.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                       cwd=path, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    return git


@pytest.mark.parametrize("name", ["p/new revenue.yaml", "p/réveil.yaml"])
def test_diff_sees_specs_whose_names_need_quoting(tmp_path, monkeypatch, capsys, name):
    repo = tmp_path / "repo"
    git = git_repo(repo)
    (repo / "p").mkdir()
    (repo / "p" / "orders.csv").write_text("id,status\n1,shipped\n")
    git("add", ".")
    git("commit", "-qm", "base")
    data = spec()
    node(data, "shipped")["params"]["condition"] = "no_such_column = 1"
    (repo / name).write_text(dump_spec(data))
    git("add", ".")
    monkeypatch.chdir(repo)
    assert main(["diff", "--git", "HEAD", "--fail-on-issues"]) == 1
    out = capsys.readouterr().out
    assert "No pipeline specs changed." not in out and "no_such_column" in out


def test_diff_data_reads_only_repository_files(tmp_path, monkeypatch, capsys):
    pytest.importorskip("duckdb")
    repo = tmp_path / "repo"
    git = git_repo(repo)
    (tmp_path / "SECRET.csv").write_text("id,status\n9,DUMMY_SECRET\n")
    (repo / "orders.csv").write_text("id,status\n1,shipped\n")
    (repo / "p.yaml").write_text(dump_spec(spec()))
    git("add", ".")
    git("commit", "-qm", "base")
    data = spec()
    node(data, "orders")["params"]["path"] = "../SECRET.csv"
    (repo / "p.yaml").write_text(dump_spec(data))
    monkeypatch.chdir(repo)
    assert main(["diff", "--git", "HEAD", "--data", "--fail-on-issues"]) == 1
    out = capsys.readouterr().out
    assert "DUMMY_SECRET" not in out and "outside" in out


# ------------------------------------------------------------------ PowerCenter mapplets


def test_a_self_referencing_mapplet_does_not_expand_exponentially():
    from redcliff.powercenter_workflow import flatten_mapplets

    inst = '<INSTANCE NAME="J{i}" TRANSFORMATION_NAME="M" TRANSFORMATION_TYPE="Mapplet" TYPE="MAPPLET"/>'
    mapplet = ET.fromstring('<MAPPLET NAME="M">' + "".join(inst.format(i=i) for i in range(6)) + "</MAPPLET>")
    mapping = ET.fromstring('<MAPPING NAME="m"><INSTANCE NAME="X" TRANSFORMATION_NAME="M" '
                            'TRANSFORMATION_TYPE="Mapplet" TYPE="MAPPLET"/></MAPPING>')
    start = time.monotonic()
    flat, missing = flatten_mapplets(mapping, {"M": mapplet}, {}, {})
    assert time.monotonic() - start < 5
    assert "M" in missing and len(flat.findall("INSTANCE")) <= 10_000


def test_a_wide_chain_of_mapplets_hits_the_budget():
    from redcliff.powercenter_workflow import MAX_MAPPLET_INSTANCES, flatten_mapplets

    def mplt(name, child):
        kids = "".join(f'<INSTANCE NAME="{child}{i}" TRANSFORMATION_NAME="{child}" TRANSFORMATION_TYPE="Mapplet" '
                       'TYPE="MAPPLET"/>' for i in range(5)) if child else '<INSTANCE NAME="E" TRANSFORMATION_NAME="E"/>'
        return ET.fromstring(f'<MAPPLET NAME="{name}">{kids}</MAPPLET>')
    names = [f"M{i}" for i in range(9)]
    mapplets = {n: mplt(n, names[i + 1] if i + 1 < len(names) else None) for i, n in enumerate(names)}
    mapping = ET.fromstring('<MAPPING NAME="m"><INSTANCE NAME="X" TRANSFORMATION_NAME="M0" '
                            'TRANSFORMATION_TYPE="Mapplet" TYPE="MAPPLET"/></MAPPING>')
    start = time.monotonic()
    flat, missing = flatten_mapplets(mapping, mapplets, {}, {})
    assert time.monotonic() - start < 10
    assert missing and len(flat.findall("INSTANCE")) <= 2 * MAX_MAPPLET_INSTANCES
