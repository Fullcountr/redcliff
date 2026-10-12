"""Migration scorecard: how much of a legacy job Redcliff converted, and what still needs a person.

Each item is one piece of the legacy job (a PowerCenter transformation, or one SQL statement)
with a status:

- ``converted``: the generated pipeline means the same thing.
- ``review``: it runs, but differs in an edge case; the notes say which.
- ``manual``: there is no faithful translation; a placeholder stands in and the notes say why.

The scorecard also reports lineage coverage: how many target columns trace back to a source
column. Constants and placeholders don't, and the report names them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from redcliff.informatica import CONVERTED, MANUAL, REVIEW, worst
from redcliff.lineage import Ref, build_graph, trace_to_sources
from redcliff.spec import Pipeline

STATUSES = (CONVERTED, REVIEW, MANUAL)


@dataclass
class ScoreItem:
    name: str                      # e.g. EXP_CLEAN_CUSTOMER, or "statement 3"
    kind: str                      # e.g. Expression, Joiner, CREATE TABLE AS
    status: str = CONVERTED
    nodes: list[str] = field(default_factory=list)   # Redcliff node ids it became
    notes: list[str] = field(default_factory=list)
    area: str = "pipeline"         # "pipeline", or "orchestration" for workflow tasks

    def flag(self, status: str, note: str | None = None) -> None:
        self.status = worst(self.status, status)
        if note and note not in self.notes:
            self.notes.append(note)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "status": self.status, "nodes": list(self.nodes),
                "notes": list(self.notes), "area": self.area}


@dataclass
class Scorecard:
    origin: str                    # "powercenter" or "sql"
    title: str
    items: list[ScoreItem] = field(default_factory=list)
    functions: dict[str, int] = field(default_factory=dict)   # legacy function -> times used
    lineage: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {s: sum(i.status == s for i in self.items) for s in STATUSES}

    def automated(self) -> float:
        """Share of items converted with no review needed, 0 to 1."""
        return self.counts()[CONVERTED] / len(self.items) if self.items else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "title": self.title,
            "counts": self.counts(),
            "automated": round(self.automated(), 4),
            "items": [i.to_dict() for i in self.items],
            "functions": dict(sorted(self.functions.items())),
            "lineage": dict(self.lineage),
        }

    def to_markdown(self) -> str:
        c = self.counts()
        total = len(self.items)
        lines = [
            f"# Migration scorecard: {self.title}",
            "",
            f"**{c[CONVERTED]} of {total}** converted automatically ({self.automated():.0%}), "
            f"**{c[REVIEW]}** to review, **{c[MANUAL]}** to rewrite by hand.",
        ]
        orchestration = sum(i.area == "orchestration" for i in self.items)
        if orchestration:
            lines.append(f"{orchestration} of the {total} are workflow tasks, which belong in an orchestrator "
                         "rather than a pipeline.")
        if self.lineage:
            lin = self.lineage
            lines.append(f"Lineage: {lin['traced']} of {lin['columns']} target columns trace back to a source.")
        lines += ["", "| Status | Name | Type | Became | Notes |", "| --- | --- | --- | --- | --- |"]
        order = {MANUAL: 0, REVIEW: 1, CONVERTED: 2}
        for item in sorted(self.items, key=lambda i: order[i.status]):
            notes = "<br>".join(n.replace("|", "\\|") for n in item.notes) or ""
            nodes = ", ".join(f"`{n}`" for n in item.nodes) or "–"
            lines.append(f"| {item.status} | {item.name} | {item.kind} | {nodes} | {notes} |")
        if self.lineage.get("untraced"):
            lines += ["", "Target columns with no source column (constants or placeholders): "
                      + ", ".join(f"`{c}`" for c in self.lineage["untraced"])]
        if self.functions:
            lines += ["", "Legacy functions used: " + ", ".join(f"{k} ({v})" for k, v in sorted(self.functions.items()))]
        return "\n".join(lines) + "\n"


def lineage_coverage(pipeline: Pipeline) -> dict[str, Any]:
    """How many columns written to targets trace back to at least one source column."""
    graph = build_graph(pipeline)
    columns: list[str] = []
    untraced: list[str] = []
    for node in pipeline.nodes:
        if node.type != "target":
            continue
        upstream = node.inputs[0]
        for col in graph.columns.get(upstream) or []:
            label = f"{node.id}.{col}"
            columns.append(label)
            traced = trace_to_sources(graph, Ref(upstream, col))
            if not any(t == "DIRECT" for t, _ in traced.values()):
                untraced.append(label)
    return {"columns": len(columns), "traced": len(columns) - len(untraced), "untraced": untraced}
