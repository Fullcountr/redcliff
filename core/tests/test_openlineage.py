"""OpenLineage run events from `redcliff run`: START and COMPLETE (or FAIL), with column lineage."""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from redcliff.cli import main
from redcliff.openlineage import run_event
from redcliff.spec import load_spec

ROOT = Path(__file__).parents[2]
pytest.importorskip("duckdb")


@pytest.fixture
def work(tmp_path):
    for folder in ("specs", "data"):
        shutil.copytree(ROOT / "examples" / folder, tmp_path / folder)
    return tmp_path


def test_events_to_a_file(work):
    events = work / "events.jsonl"
    assert main(["run", str(work / "specs" / "orders_revenue.yaml"), "--openlineage", str(events)]) == 0
    start, complete = (json.loads(line) for line in events.read_text().splitlines())
    assert (start["eventType"], complete["eventType"]) == ("START", "COMPLETE")
    assert start["run"]["runId"] == complete["run"]["runId"]
    assert start["job"] == complete["job"] and start["job"]["name"] == "orders_revenue"
    assert start["schemaURL"].endswith("#/$defs/RunEvent") and start["eventTime"].endswith("Z")
    inputs = {i["name"] for i in complete["inputs"]}
    assert {str(work / "data" / "orders.csv"), str(work / "data" / "customers.csv")} <= inputs
    out = complete["outputs"][0]
    assert out["namespace"] == "file" and out["name"].endswith("revenue_by_region.parquet")
    revenue = out["facets"]["columnLineage"]["fields"]["revenue"]["inputFields"]
    assert {(f["field"], f["transformations"][0]["subtype"]) for f in revenue} == {("amount", "AGGREGATION")}
    assert [f["name"] for f in out["facets"]["schema"]["fields"]][:1] == ["region"]


def test_events_posted_to_a_url(work):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(201)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/lineage"
        import os

        os.environ["OPENLINEAGE_API_KEY"] = "dummy-key"
        try:
            assert main(["run", str(work / "specs" / "orders_revenue.yaml"), "--openlineage", url, "--namespace", "dev"]) == 0
        finally:
            del os.environ["OPENLINEAGE_API_KEY"]
    finally:
        server.shutdown()
    assert [e["eventType"] for _a, e in received] == ["START", "COMPLETE"]
    assert received[0][0] == "Bearer dummy-key" and received[0][1]["job"]["namespace"] == "dev"


def test_a_failed_run_sends_fail_and_lineage_never_breaks_a_run(work, capsys):
    events = work / "events.jsonl"
    spec = load_spec(work / "specs" / "orders_revenue.yaml")
    event = run_event(spec, "FAIL", "r1", work / "specs", error="boom")
    assert event["run"]["facets"]["errorMessage"]["message"] == "boom"
    (work / "data" / "orders.csv").write_text("not,the,right,columns\n1,2,3,4\n")
    assert main(["run", str(work / "specs" / "orders_revenue.yaml"), "--skip-checks", "--openlineage", str(events)]) == 1
    assert [json.loads(line)["eventType"] for line in events.read_text().splitlines()] == ["START", "FAIL"]
    # an unreachable endpoint only warns
    shutil.copy(ROOT / "examples" / "data" / "orders.csv", work / "data" / "orders.csv")
    assert main(["run", str(work / "specs" / "orders_revenue.yaml"), "--openlineage", "http://127.0.0.1:9/nowhere"]) == 0
    assert "couldn't send the OpenLineage START event" in capsys.readouterr().err
