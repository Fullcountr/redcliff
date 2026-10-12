"""The parts of a PowerCenter export around a mapping: mapplets, sessions, workflows, parameter files.

- **Mapplets** are inlined: their transformations join the mapping under a ``MAPPLET__`` prefix,
  and the mapplet's Input and Output transformations become pass-through steps wired to the
  mapping's connectors. Nested mapplets are inlined too.
- **Sessions** override transformation attributes (SQL overrides, source filters, lookup
  overrides), set target options (truncate, pre/post SQL) and name a parameter file.
- **Workflows** give the run order of sessions and hold other tasks (commands, emails,
  decisions) that belong to an orchestrator, not a pipeline.
- **Parameter files** set ``$$PARAMS`` per session, workflow or globally.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- mapplets

_IO_TYPES = ("input transformation", "output transformation")


def is_mapplet(inst: ET.Element) -> bool:
    return ((inst.get("TRANSFORMATION_TYPE") or "").lower() == "mapplet"
            or (inst.get("TYPE") or "").upper() == "MAPPLET")


def _ports_of(defn: ET.Element | None) -> list[str]:
    return [(f.get("NAME") or "").upper() for f in defn.findall("TRANSFORMFIELD")] if defn is not None else []


def flatten_mapplets(mapping: ET.Element, mapplets: dict[str, ET.Element], display: dict[str, str],
                     transforms: dict[str, ET.Element], depth: int = 0) -> tuple[ET.Element, list[str]]:
    """A copy of ``mapping`` with every mapplet instance replaced by the mapplet's contents.

    Returns the copy and the names of mapplets that couldn't be found (left in place, so the
    converter marks them manual). ``display`` maps flattened instance names to readable ones
    such as ``MPLT_CLEAN/EXP_TRIM``.
    """
    flat = copy.deepcopy(mapping)
    missing: list[str] = []
    for inst in list(flat.findall("INSTANCE")):
        if not is_mapplet(inst):
            continue
        outer = (inst.get("NAME") or "").upper()
        mplt_name = (inst.get("TRANSFORMATION_NAME") or outer).upper()
        mplt = mapplets.get(mplt_name)
        if mplt is None or depth > 8:
            missing.append(mplt_name)
            continue
        inner, more = flatten_mapplets(mplt, mapplets, display, transforms, depth + 1)
        missing += more
        prefix = f"{outer}__"
        local = {(t.get("NAME") or "").upper() for t in inner.findall("TRANSFORMATION")}

        for t in inner.findall("TRANSFORMATION"):
            if (t.get("TYPE") or "").lower() == "mapplet":
                continue  # the mapplet's own interface description, not a step
            t = copy.deepcopy(t)
            t.set("NAME", prefix + (t.get("NAME") or "").upper())
            flat.append(t)

        inputs: dict[str, str] = {}   # port -> flattened Input transformation instance
        outputs: dict[str, str] = {}  # port -> flattened Output transformation instance
        for i in inner.findall("INSTANCE"):
            i = copy.deepcopy(i)
            name = (i.get("NAME") or "").upper()
            tname = (i.get("TRANSFORMATION_NAME") or name).upper()
            new = prefix + name
            i.set("NAME", new)
            kind = (i.get("TYPE") or "").upper()
            if kind not in ("SOURCE", "TARGET") and tname in local:
                i.set("TRANSFORMATION_NAME", prefix + tname)
                i.set("REUSABLE", "NO")
            for a in i.findall("ASSOCIATED_SOURCE_INSTANCE"):
                a.set("NAME", prefix + (a.get("NAME") or "").upper())
            ttype = (i.get("TRANSFORMATION_TYPE") or "").lower()
            if ttype in _IO_TYPES:
                defn = next((t for t in inner.findall("TRANSFORMATION")
                             if (t.get("NAME") or "").upper() == tname), None)
                if defn is None:  # elements without children are falsy, so no `or` here
                    defn = transforms.get(tname)
                for port in _ports_of(defn):
                    (inputs if ttype == "input transformation" else outputs).setdefault(port, new)
            display[new] = f"{display.get(outer, outer)}/{display.get(name, name)}"
            flat.append(i)

        for c in inner.findall("CONNECTOR"):
            c = copy.deepcopy(c)
            c.set("FROMINSTANCE", prefix + (c.get("FROMINSTANCE") or "").upper())
            c.set("TOINSTANCE", prefix + (c.get("TOINSTANCE") or "").upper())
            flat.append(c)

        # Rewire the mapping's connectors to and from the mapplet onto its Input/Output steps.
        for c in flat.findall("CONNECTOR"):
            if (c.get("TOINSTANCE") or "").upper() == outer:
                port = (c.get("TOFIELD") or "").upper()
                if port in inputs:
                    c.set("TOINSTANCE", inputs[port])
            if (c.get("FROMINSTANCE") or "").upper() == outer:
                port = (c.get("FROMFIELD") or "").upper()
                if port in outputs:
                    c.set("FROMINSTANCE", outputs[port])
        flat.remove(inst)
    return flat, missing


# --------------------------------------------------------------------------- sessions


@dataclass
class Session:
    name: str      # upper case, for matching
    mapping: str
    label: str = ""  # as written
    attrs: dict[str, dict[str, str]] = field(default_factory=dict)  # instance -> attribute -> value
    param_file: str | None = None
    workflow: str | None = None


def _session(el: ET.Element, workflow: str | None) -> Session:
    s = Session((el.get("NAME") or "").upper(), (el.get("MAPPINGNAME") or "").upper(), el.get("NAME") or "",
                workflow=workflow)
    for holder in [*el.findall("SESSTRANSFORMATIONINST"), *el.findall("SESSIONEXTENSION")]:
        inst = (holder.get("SINSTANCENAME") or holder.get("TRANSFORMATIONNAME") or "").upper()
        for a in holder.findall("ATTRIBUTE"):
            value = a.get("VALUE") or ""
            if inst and value.strip():
                s.attrs.setdefault(inst, {})[(a.get("NAME") or "").upper()] = value
    for a in el.findall("ATTRIBUTE"):
        if (a.get("NAME") or "").upper() == "PARAMETER FILENAME" and (a.get("VALUE") or "").strip():
            s.param_file = a.get("VALUE")
    return s


# --------------------------------------------------------------------------- workflows


@dataclass
class Task:
    name: str
    type: str
    detail: list[str] = field(default_factory=list)
    label: str = ""


@dataclass
class Workflow:
    name: str
    label: str
    folder: str
    order: list[str]                                   # task instance names, in run order
    tasks: dict[str, Task]
    sessions: dict[str, str]                           # task instance -> session name
    conditions: list[str] = field(default_factory=list)


def _run_order(names: list[str], links: list[tuple[str, str]]) -> list[str]:
    preds: dict[str, set[str]] = {n: set() for n in names}
    for a, b in links:
        if a in preds and b in preds:
            preds[b].add(a)
    order: list[str] = []
    pending = list(names)
    while pending:
        ready = [n for n in pending if preds[n] <= set(order)] or pending[:1]  # a cycle: keep document order
        order.append(ready[0])
        pending.remove(ready[0])
    return order


def parse_folder(folder: ET.Element) -> tuple[dict[str, Session], list[Workflow]]:
    """Sessions (reusable and inside workflows) and workflows of one folder."""
    fname = (folder.get("NAME") or "").upper()
    sessions = {s.name: s for s in (_session(el, None) for el in folder.findall("SESSION"))}
    reusable_tasks = {(t.get("NAME") or "").upper(): t for t in folder.findall("TASK")}
    workflows: list[Workflow] = []
    for wf in folder.findall("WORKFLOW"):
        wname = (wf.get("NAME") or "").upper()
        for el in wf.findall("SESSION"):
            s = _session(el, wname)
            sessions[s.name] = s
        tasks_el = {(t.get("NAME") or "").upper(): t for t in wf.findall("TASK")}
        names: list[str] = []
        tasks: dict[str, Task] = {}
        session_of: dict[str, str] = {}
        for ti in wf.findall("TASKINSTANCE"):
            name = (ti.get("NAME") or "").upper()
            task = (ti.get("TASKNAME") or name).upper()
            ttype = ti.get("TASKTYPE") or "Task"
            names.append(name)
            if ttype.lower() == "session":
                session_of[name] = task
                if task in sessions and sessions[task].workflow is None:
                    sessions[task].workflow = wname
                continue
            el = tasks_el.get(task)
            if el is None:  # an element without children is falsy, so no `or`
                el = reusable_tasks.get(task)
            detail = []
            if el is not None:
                detail = [f"{v.get('NAME')}: {v.get('VALUE')}" for v in el.findall("VALUEPAIR") if v.get("VALUE")]
                detail += [f"{a.get('NAME')}: {a.get('VALUE')}" for a in el.findall("ATTRIBUTE")
                           if (a.get("NAME") or "").lower() in ("email user name", "email subject", "decision name",
                                                                  "decision condition", "event wait file")
                           and a.get("VALUE")]
            tasks[name] = Task(name, ttype, detail, ti.get("NAME") or name)
        links = [((l.get("FROMTASK") or "").upper(), (l.get("TOTASK") or "").upper()) for l in wf.findall("WORKFLOWLINK")]
        conditions = [f"{l.get('FROMTASK')} → {l.get('TOTASK')} if {l.get('CONDITION')}"
                      for l in wf.findall("WORKFLOWLINK") if (l.get("CONDITION") or "").strip()]
        workflows.append(Workflow(wname, wf.get("NAME") or wname, fname, _run_order(names, links), tasks, session_of,
                                  conditions))
    return sessions, workflows


# --------------------------------------------------------------------------- parameter files


def parse_param_file(text: str) -> list[tuple[str, dict[str, str]]]:
    """Sections of a PowerCenter parameter file, in order: (header, {name: value})."""
    sections: list[tuple[str, dict[str, str]]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//", "--")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {}
            sections.append((line[1:-1].strip().upper(), current))
        elif "=" in line and current is not None:
            name, value = line.split("=", 1)
            current[name.strip().upper()] = value.strip()
    return sections


def resolve_params(sections: list[tuple[str, dict[str, str]]], folder: str, workflow: str | None,
                   session: str | None) -> dict[str, str]:
    """Parameter values for one session; more specific sections win, as in PowerCenter."""
    folder, workflow, session = folder.upper(), (workflow or "").upper(), (session or "").upper()

    def rank(header: str) -> int:
        if header == "GLOBAL":
            return 1
        parts = header.split(".")
        if parts[0] not in (folder, "") and len(parts) > 1:
            return 0
        rest = [p for p in parts[1:]] if len(parts) > 1 else parts
        wf = next((p[3:] for p in rest if p.startswith("WF:")), None)
        st = next((p[3:] for p in rest if p.startswith("ST:")), None)
        if wf and st:
            return 4 if wf == workflow and st == session else 0
        if wf:
            return 2 if wf == workflow else 0
        name = rest[-1] if rest else ""
        return 3 if session and name == session else 0

    merged: dict[str, str] = {}
    for _, values in sorted(((rank(h), v) for h, v in sections if rank(h) > 0), key=lambda x: x[0]):
        merged.update(values)
    return merged


_PARAM = re.compile(r"\$\$[A-Za-z_][A-Za-z0-9_]*")


def substitute(text: str, params: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """PowerCenter expands $$PARAMS in SQL text before running it. Returns (text, used, missing)."""
    used, missing = [], []

    def repl(m: re.Match) -> str:
        name = m.group(0).upper()
        if name in params:
            used.append(name)
            return params[name]
        missing.append(name)
        return m.group(0)

    return _PARAM.sub(repl, text), used, missing
