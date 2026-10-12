"""OpenLineage run events for ``redcliff run``.

A run sends START when it begins and COMPLETE (or FAIL) when it ends. Each event names the job (the
pipeline), its input datasets (the sources) and output datasets (the targets), with each dataset's
schema and, on outputs, the column lineage facet, so a catalog such as Marquez sees every run with
its column lineage. Events go to a file (one JSON object per line) or are posted to a URL.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from redcliff import __version__
from redcliff.lineage import build_graph, openlineage_facet
from redcliff.spec import Node, Pipeline, bind

PRODUCER = f"https://github.com/Fullcountr/redcliff/tree/v{__version__}"
RUN_EVENT = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
SCHEMA_FACET = "https://openlineage.io/spec/facets/1-1-1/SchemaDatasetFacet.json"
JOB_TYPE_FACET = "https://openlineage.io/spec/facets/2-0-3/JobTypeJobFacet.json"
ERROR_FACET = "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _dataset(node: Node, base_dir: Path | None, namespace: str) -> tuple[str, str]:
    """(namespace, name) for a source or target, following OpenLineage's dataset naming."""
    p = node.params
    if p.get("format") == "table":
        return namespace, p["table"]
    path = Path(p["path"])
    if base_dir is not None and not path.is_absolute():
        path = (Path(base_dir) / path).resolve()
    return "file", str(path)


def run_event(pipeline: Pipeline, event_type: str, run_id: str, base_dir: str | Path | None = None,
              namespace: str = "redcliff", error: str | None = None) -> dict[str, Any]:
    """One OpenLineage RunEvent (START, COMPLETE or FAIL) for a run of ``pipeline``."""
    pipeline = bind(pipeline)
    graph = build_graph(pipeline)
    base = Path(base_dir) if base_dir is not None else None
    written = {n.params.get("table") for n in pipeline.nodes if n.type == "target" and n.params.get("format") == "table"}

    def schema(node_id: str) -> dict[str, Any]:
        node = pipeline.node(node_id)
        types = node.params.get("columns") if isinstance(node.params.get("columns"), dict) else {}
        fields = [{"name": c, **({"type": str(types[c])} if c in types else {})} for c in graph.columns.get(node_id, [])]
        return {"_producer": PRODUCER, "_schemaURL": SCHEMA_FACET, "fields": fields}

    inputs = []
    for n in pipeline.nodes:
        if n.type != "source" or (n.params.get("format") == "table" and n.params.get("table") in written):
            continue  # a table the run writes itself is an output, not an input
        ns, name = _dataset(n, base, namespace)
        inputs.append({"namespace": ns, "name": name, "facets": {"schema": schema(n.id)} if n.id in graph.columns else {}})
    outputs = []
    for n in pipeline.nodes:
        if n.type != "target":
            continue
        ns, name = _dataset(n, base, namespace)
        facets: dict[str, Any] = {}
        if n.id in graph.columns:
            facets["schema"] = schema(n.id)
            facets["columnLineage"] = openlineage_facet(graph, n.id, namespace)
        outputs.append({"namespace": ns, "name": name, "facets": facets})
    run: dict[str, Any] = {"runId": run_id, "facets": {}}
    if error:
        run["facets"]["errorMessage"] = {"_producer": PRODUCER, "_schemaURL": ERROR_FACET,
                                         "message": error, "programmingLanguage": "SQL"}
    return {
        "eventType": event_type,
        "eventTime": _now(),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT,
        "run": run,
        "job": {"namespace": namespace, "name": pipeline.name,
                "facets": {"jobType": {"_producer": PRODUCER, "_schemaURL": JOB_TYPE_FACET,
                                       "processingType": "BATCH", "integration": "REDCLIFF", "jobType": "PIPELINE"}}},
        "inputs": inputs,
        "outputs": outputs,
    }


class Emitter:
    """Sends events to a file (JSON lines, appended) or posts them to an OpenLineage HTTP endpoint."""

    def __init__(self, target: str, api_key: str | None = None, timeout: float = 10.0):
        self.target = target
        self.api_key = api_key
        self.timeout = timeout
        self.run_id = str(uuid.uuid4())

    def send(self, event: dict[str, Any]) -> None:
        body = json.dumps(event)
        if self.target.startswith(("http://", "https://")):
            import urllib.request

            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(self.target, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - the person's own endpoint
                resp.read()
        else:
            with open(self.target, "a", encoding="utf-8") as f:
                f.write(body + "\n")
