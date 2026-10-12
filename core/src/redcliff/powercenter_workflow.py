"""The parts of a PowerCenter export around a mapping: mapplets, sessions, workflows, parameter files.

- **Mapplets** are inlined: their transformations join the mapping under a ``MAPPLET__`` prefix,
  and the mapplet's Input and Output transformations become pass-through steps wired to the
  mapping's connectors. Nested mapplets are inlined too.
- **Sessions** override transformation attributes (SQL overrides, source filters, lookup
  overrides), set target options (truncate, pre/post SQL) and name a parameter file.
- **Workflows** give the run order of sessions and hold other tasks (commands, emails,
  decisions) that belong to an orchestrator, not a pipeline. **Worklets** are expanded in place:
  their tasks join the run order where the worklet runs, nested worklets too.
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


MAX_MAPPLET_INSTANCES = 10_000  # per mapping, after flattening: real exports stay far below this


def flatten_mapplets(mapping: ET.Element, mapplets: dict[str, ET.Element], display: dict[str, str],
                     transforms: dict[str, ET.Element], depth: int = 0, _chain: tuple[str, ...] = (),
                     _budget: list[int] | None = None) -> tuple[ET.Element, list[str]]:
    """A copy of ``mapping`` with every mapplet instance replaced by the mapplet's contents.

    Returns the copy and the names of mapplets that couldn't be found (left in place, so the
    converter marks them manual). ``display`` maps flattened instance names to readable ones
    such as ``MPLT_CLEAN/EXP_TRIM``.

    A mapplet that contains itself (directly or through others), or a mapping that would flatten
    to more than MAX_MAPPLET_INSTANCES instances, is left in place too, so a small export can't
    expand exponentially.
    """
    budget = [MAX_MAPPLET_INSTANCES] if _budget is None else _budget
    flat = copy.deepcopy(mapping)
    missing: list[str] = []
    for inst in list(flat.findall("INSTANCE")):
        if not is_mapplet(inst):
            continue
        outer = (inst.get("NAME") or "").upper()
        mplt_name = (inst.get("TRANSFORMATION_NAME") or outer).upper()
        mplt = mapplets.get(mplt_name)
        if mplt is None or depth > 8 or mplt_name in _chain or budget[0] <= 0:
            missing.append(mplt_name)
            continue
        inner, more = flatten_mapplets(mplt, mapplets, display, transforms, depth + 1, _chain + (mplt_name,), budget)
        missing += more
        budget[0] -= len(inner.findall("INSTANCE"))
        if budget[0] < 0:
            missing.append(mplt_name)
            continue
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
    worklet: str | None = None  # the worklet instance it runs in, for [folder.WF:wf.WT:wt.ST:s]


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
    labels: dict[str, str] = field(default_factory=dict)  # task key -> name as written, worklet included


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


_DETAIL_ATTRS = ("email user name", "email subject", "decision name", "decision condition", "event wait file")


def _flow(el: ET.Element, wname: str, ctx: dict, sessions: dict[str, Session], key: str = "", label: str = "",
          worklet: str | None = None, depth: int = 0, labels: dict[str, str] | None = None,
          ) -> tuple[list[str], dict[str, Task], dict[str, str], list[str]]:
    """Run order, tasks, sessions and link conditions of a workflow or worklet, worklets expanded.

    Task keys inside a worklet are prefixed with the worklet instance (``WKL_LOAD/S_LOAD``).
    """
    for s_el in el.findall("SESSION"):
        s = _session(s_el, wname)
        s.worklet = worklet
        sessions[s.name] = s
    local_worklets = {(w.get("NAME") or "").upper(): w for w in el.findall("WORKLET")}
    tasks_el = {(t.get("NAME") or "").upper(): t for t in el.findall("TASK")}
    names: list[str] = []
    tasks: dict[str, Task] = {}
    session_of: dict[str, str] = {}
    expanded: dict[str, list[str]] = {}
    conditions: list[str] = []
    for ti in el.findall("TASKINSTANCE"):
        name = (ti.get("NAME") or "").upper()
        task = (ti.get("TASKNAME") or name).upper()
        ttype = ti.get("TASKTYPE") or "Task"
        shown = f"{label}{ti.get('NAME') or name}"
        names.append(name)
        if labels is not None:
            labels[key + name] = shown
        if ttype.lower() == "session":
            session_of[key + name] = task
            if task in sessions and sessions[task].workflow is None:
                sessions[task].workflow = wname
            if task in sessions and worklet and sessions[task].worklet is None:
                sessions[task].worklet = worklet
            continue
        if ttype.lower() == "worklet":
            wel = local_worklets.get(task)
            if wel is None:
                wel = ctx["worklets"].get(task)
            if wel is not None and depth < 8:
                order, inner, inner_sessions, inner_conds = _flow(
                    wel, wname, ctx, sessions, f"{key}{name}/", f"{shown}/", name, depth + 1, labels)
                expanded[name] = order
                tasks.update(inner)
                session_of.update(inner_sessions)
                conditions += inner_conds
                continue
            tasks[key + name] = Task(shown, ttype, [f"worklet {ti.get('TASKNAME') or name} isn't in the export"], shown)
            continue
        found = tasks_el.get(task)
        if found is None:  # an element without children is falsy, so no `or`
            found = ctx["tasks"].get(task)
        detail = []
        if found is not None:
            detail = [f"{v.get('NAME')}: {v.get('VALUE')}" for v in found.findall("VALUEPAIR") if v.get("VALUE")]
            detail += [f"{a.get('NAME')}: {a.get('VALUE')}" for a in found.findall("ATTRIBUTE")
                       if (a.get("NAME") or "").lower() in _DETAIL_ATTRS and a.get("VALUE")]
        tasks[key + name] = Task(shown, ttype, detail, shown)
    links = [((k.get("FROMTASK") or "").upper(), (k.get("TOTASK") or "").upper()) for k in el.findall("WORKFLOWLINK")]
    conditions = [f"{label}{k.get('FROMTASK')} → {label}{k.get('TOTASK')} if {k.get('CONDITION')}"
                  for k in el.findall("WORKFLOWLINK") if (k.get("CONDITION") or "").strip()] + conditions
    order: list[str] = []
    for n in _run_order(names, links):
        order += expanded.get(n, [key + n])
    return order, tasks, session_of, conditions


def parse_folder(folder: ET.Element) -> tuple[dict[str, Session], list[Workflow]]:
    """Sessions (reusable and inside workflows) and workflows of one folder, worklets expanded."""
    fname = (folder.get("NAME") or "").upper()
    sessions = {s.name: s for s in (_session(el, None) for el in folder.findall("SESSION"))}
    ctx = {"tasks": {(t.get("NAME") or "").upper(): t for t in folder.findall("TASK")},
           "worklets": {(w.get("NAME") or "").upper(): w for w in folder.findall("WORKLET")}}
    workflows: list[Workflow] = []
    for wf in folder.findall("WORKFLOW"):
        wname = (wf.get("NAME") or "").upper()
        labels: dict[str, str] = {}
        order, tasks, session_of, conditions = _flow(wf, wname, ctx, sessions, labels=labels)
        workflows.append(Workflow(wname, wf.get("NAME") or wname, fname, order, tasks, session_of, conditions, labels))
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
                   session: str | None, worklet: str | None = None) -> dict[str, str]:
    """Parameter values for one session; more specific sections win, as in PowerCenter."""
    folder, workflow, session = folder.upper(), (workflow or "").upper(), (session or "").upper()
    worklet = (worklet or "").upper()

    def rank(header: str) -> int:
        if header == "GLOBAL":
            return 1
        parts = header.split(".")
        if parts[0] not in (folder, "") and len(parts) > 1:
            return 0
        rest = [p for p in parts[1:]] if len(parts) > 1 else parts
        wf = next((p[3:] for p in rest if p.startswith("WF:")), None)
        st = next((p[3:] for p in rest if p.startswith("ST:")), None)
        wt = [p[3:] for p in rest if p.startswith("WT:")]
        if wt and (not worklet or wt[-1] != worklet):
            return 0
        if wf and st:
            return (5 if wt else 4) if wf == workflow and st == session else 0
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


def _in_quotes(text: str, at: int) -> bool:
    """Is position ``at`` inside a single-quoted SQL string? ('' escapes count twice, so parity works.)"""
    return text.count("'", 0, at) % 2 == 1


def substitute_as_variables(text: str, params: dict[str, str], variable) -> tuple[str, dict[str, str], list[str], list[str]]:
    """Like :func:`substitute`, but a parameter inside a quoted string becomes ``${variable}``.

    PowerCenter expands parameters as text, and so does a pipeline variable, so a quoted use keeps
    its meaning and can change per run. An unquoted use (a number, a column list) is expanded to
    its value here, because the SQL has to parse. Returns (text, variables used -> value, names
    expanded to their value, missing names).
    """
    used: dict[str, str] = {}
    baked: list[str] = []
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        name = m.group(0).upper()
        if name not in params:
            missing.append(name)
            return m.group(0)
        if _in_quotes(text, m.start()):
            var = variable(name)
            used[var] = params[name]
            return f"${{{var}}}"
        baked.append(name)
        return params[name]

    return _PARAM.sub(repl, text), used, baked, missing
