// Model mode: entities as cards, relationships as edges, and a panel to check, grow, export and load
// the model. New tables are added as a reviewed set of changes (core/src/redcliff/extend.py); a
// relationship can be drawn by dragging from an attribute to a dimension's key.
import dagre from "@dagrejs/dagre";
import {
  Background, BackgroundVariant, ConnectionMode, Handle, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Connection, type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import * as yaml from "js-yaml";
import {
  Check, CircleAlert, CircleCheck, CircleDot, Copy, Download, FileCode2, FilePlus2, Layers, Link2, Maximize, Minus,
  Orbit, Plus, Rows3, ScrollText, Sigma, Table2, Workflow, type LucideIcon,
} from "lucide-react";
import { memo, useEffect, useMemo, useState } from "react";
import { engine } from "../engine/engine";
import type {
  DataModel, EntityKind, JobMap, ModelAnalysis, ModelChange, ModelEntity, ModelExport, ModelExtension, Spec,
} from "../model";
import { ACCEPT } from "../files";
import { checkLink, csvHeaders, tablesInModel, type LinkCheck, type TableInfo } from "../modelData";
import { fitPadding } from "../layout";
import { highlight } from "../ui";

export type ModelTab = "overview" | "load" | "export" | "yaml";

const KIND: Record<EntityKind, { icon: LucideIcon; label: string; color: string }> = {
  dimension: { icon: Layers, label: "Dimension", color: "var(--t-source)" },
  fact: { icon: Sigma, label: "Fact", color: "var(--t-target)" },
  hub: { icon: CircleDot, label: "Hub", color: "var(--t-join)" },
  link: { icon: Link2, label: "Link", color: "var(--t-union)" },
  satellite: { icon: Orbit, label: "Satellite", color: "var(--t-derive)" },
  table: { icon: Table2, label: "Table", color: "var(--t-select)" },
};

const ROLE_MARK: Record<string, [string, string]> = {
  surrogate_key: ["SK", "Surrogate key"],
  durable_key: ["DK", "Durable key"],
  business_key: ["BK", "Business key"],
  measure: ["Σ", "Measure"],
};

const WIDTH = 248;
const HEAD = 62;
const ROW = 23;

/** The columns that identify a row: the surrogate key, else the business key (as in model.py). */
export function entityKey(e: ModelEntity): string[] {
  for (const role of ["surrogate_key", "business_key"]) {
    const names = e.attributes.filter((a) => a.role === role).map((a) => a.name);
    if (names.length) return names;
  }
  return [];
}

const isScd2 = (e: ModelEntity) => e.kind === "dimension" && (e.history ?? "scd2") === "scd2";

type EntityData = {
  entity: ModelEntity; fks: Set<string>; selected: boolean; dimmed: boolean;
  isNew?: boolean; newAttrs?: Set<string>; // in a review: what the changes add
};
type EntityNodeType = Node<EntityData, "entity">;

const EntityNode = memo(function EntityNode({ data }: NodeProps<EntityNodeType>) {
  const { entity: e, fks, selected, dimmed, isNew, newAttrs } = data;
  const kind = KIND[e.kind] ?? KIND.table;
  const key = new Set(entityKey(e));
  return (
    <div className={`entity${selected ? " is-selected" : ""}${dimmed ? " is-dimmed" : ""}${isNew ? " is-new" : ""}`}
      style={{ "--t": kind.color } as React.CSSProperties} data-testid={`entity-${e.id}`}>
      <div className="step-head">
        <span className="step-icon"><kind.icon /></span>
        <span className="step-titles">
          <span className="step-id">{e.id}{isNew && <span className="new-badge">new</span>}</span>
          <span className="step-type">
            {kind.label}
            {e.kind === "dimension" && <> · {isScd2(e) ? "SCD2" : "SCD1"}</>}
            {e.grain && <> · {e.grain}</>}
          </span>
        </span>
      </div>
      <ul className="entity-attrs">
        {e.attributes.map((a) => {
          const marks = [
            key.has(a.name) && ["PK", "Primary key"],
            fks.has(a.name) && ["FK", "Foreign key"],
            a.role && ROLE_MARK[a.role],
            a.history === "scd1" && ["SCD1", "Corrections overwrite every version"],
            a.track === false && ["skip", "Not tracked: a change doesn't make a new version"],
          ].filter(Boolean) as [string, string][];
          return (
            <li key={a.name} className={`attr${key.has(a.name) ? " is-key" : ""}${newAttrs?.has(a.name) ? " is-new" : ""}`}
              title={a.description || undefined}>
              <Handle type="source" position={Position.Right} id={`r-${a.name}`} className="attr-handle" />
              <Handle type="target" position={Position.Left} id={`l-${a.name}`} className="attr-handle" />
              <span className="attr-name">{a.name}</span>
              {marks.map(([m, t]) => <span key={m} className={`attr-mark mark-${m.toLowerCase()}`} title={t}>{m}</span>)}
              <span className="attr-type">{a.type}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
});

const nodeTypes = { entity: EntityNode };

/** Referenced entities (dimensions, hubs) on the left, the ones referencing them on the right. */
function layoutModel(model: DataModel): Record<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 90, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const e of model.entities) g.setNode(e.id, { width: WIDTH, height: HEAD + e.attributes.length * ROW + 10 });
  for (const r of model.relationships ?? []) {
    const [from, to] = [r.from.split(".")[0], r.to.split(".")[0]];
    if (g.hasNode(from) && g.hasNode(to) && from !== to) g.setEdge(to, from);
  }
  dagre.layout(g);
  return Object.fromEntries(model.entities.map((e) => {
    const p = g.node(e.id);
    return [e.id, { x: p.x - p.width / 2, y: p.y - p.height / 2 }];
  }));
}

type Props = {
  model: DataModel;
  engineReady: boolean;
  dataReady: boolean;
  tab: ModelTab;
  onTab: (t: ModelTab) => void;
  onChange: (m: DataModel, note?: string) => void;
  onOpenPipeline: (spec: Spec) => void;
  /** Why each entity and attribute is what it is, from a proposal or an extension. */
  reasons: Record<string, string[]>;
  /** The legacy job the model was proposed from, to map onto the model. */
  job?: { spec: Spec } | null;
  /** What new tables would add, waiting for the person to accept or skip each change. */
  review: { ext: ModelExtension; tables: string[] } | null;
  onReviewed: (model: DataModel | null, accepted: ModelChange[]) => void;
  /** Tables the playground has, for adding to the model. */
  tables: TableInfo[];
  onGrow: (tables: string[]) => void;
  onAddFiles: (files: FileList) => void;
  onNewModel: () => void;
};

export function ModelView(props: Props) {
  return (
    <ReactFlowProvider>
      <ModelCanvas {...props} />
    </ReactFlowProvider>
  );
}

/** The changes a set of ticked ones really brings: an unticked change takes out what needs it. */
export function effective(changes: ModelChange[], ticked: Set<string>): Set<string> {
  const byId = new Map(changes.map((c) => [c.id, c]));
  const ok = (c: ModelChange, seen = new Set<string>()): boolean =>
    ticked.has(c.id) && !seen.has(c.id) && c.needs.every((n) => !byId.has(n) || ok(byId.get(n)!, new Set([...seen, c.id])));
  return new Set(changes.filter((c) => ok(c)).map((c) => c.id));
}

function ModelCanvas(props: Props) {
  const { model, engineReady, tab, onTab, onChange, onOpenPipeline, reasons, job, review, onReviewed } = props;
  const flow = useReactFlow();
  const [jobMap, setJobMap] = useState<JobMap | null>(null);
  useEffect(() => {
    if (!job || !engineReady) { setJobMap(null); return; }
    let live = true;
    engine.modelMap(model, job.spec).then((r) => { if (live) setJobMap(r.ok ? r : null); });
    return () => { live = false; };
  }, [model, job, engineReady]);

  // A review shows the model as it would be with the ticked changes, the new parts marked.
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<DataModel | null>(null);
  useEffect(() => { setTicked(new Set(review?.ext.changes.filter((c) => c.default).map((c) => c.id) ?? [])); }, [review]);
  const accepted = useMemo(() => effective(review?.ext.changes ?? [], ticked), [review, ticked]);
  useEffect(() => {
    if (!review || !engineReady) { setPreview(null); return; }
    let live = true;
    engine.modelApply(model, review.ext.changes, [...accepted]).then((r) => { if (live && r.ok) setPreview(r.model); });
    return () => { live = false; };
  }, [model, review, accepted, engineReady]);
  const shown = review && preview ? preview : model;
  const fresh = useMemo(() => {
    const ids = new Set(model.entities.map((e) => e.id));
    const rels = new Set((model.relationships ?? []).map((r) => `${r.from}->${r.to}`));
    const attrs = new Set(model.entities.flatMap((e) => e.attributes.map((a) => `${e.id}.${a.name}`)));
    return {
      entities: new Set(shown.entities.filter((e) => !ids.has(e.id)).map((e) => e.id)),
      relationships: new Set((shown.relationships ?? []).map((r) => `${r.from}->${r.to}`).filter((k) => !rels.has(k))),
      attributes: new Set(shown.entities.filter((e) => ids.has(e.id))
        .flatMap((e) => e.attributes.map((a) => `${e.id}.${a.name}`)).filter((k) => !attrs.has(k))),
    };
  }, [model, shown]);

  const [picked, setSelected] = useState<string | null>(null);
  useEffect(() => { if (review) setSelected(null); }, [review]);  // a review shows the whole model
  const selected = picked && shown.entities.some((e) => e.id === picked) ? picked : null; // gone after an import
  const [analysis, setAnalysis] = useState<ModelAnalysis | null>(null);
  const positions = useMemo(() => layoutModel(shown), [shown]);

  useEffect(() => {
    if (!engineReady) return;
    let live = true;
    engine.modelAnalyze(model).then((r) => { if (live) setAnalysis(r.ok ? r : null); });
    return () => { live = false; };
  }, [model, engineReady]);

  useEffect(() => {
    const id = window.setTimeout(() => flow.fitView({ padding: fitPadding(), maxZoom: 1, duration: 250 }), 60);
    return () => window.clearTimeout(id);
  }, [positions, flow]);

  const related = useMemo(() => {
    if (!selected) return null;
    const out = new Set([selected]);
    for (const r of shown.relationships ?? []) {
      const [a, b] = [r.from.split(".")[0], r.to.split(".")[0]];
      if (a === selected) out.add(b);
      if (b === selected) out.add(a);
    }
    return out;
  }, [selected, shown]);

  // Drawing a relationship: drag from an attribute to another entity's key (or the other way).
  const [linking, setLinking] = useState<{ source: string; target: string; check: LinkCheck | null } | null>(null);
  const onConnect = async (c: Connection) => {
    if (review || !c.source || !c.target || c.source === c.target) return;
    const ends = [[c.source, c.sourceHandle?.slice(2)], [c.target, c.targetHandle?.slice(2)]] as [string, string | undefined][];
    const isKey = ([id, attr]: [string, string | undefined]) => {
      const e = model.entities.find((x) => x.id === id);
      return !!e && !!attr && ["dimension", "hub"].includes(e.kind) && entityKey(e).includes(attr);
    };
    const parent = isKey(ends[0]) && !isKey(ends[1]) ? ends[0] : isKey(ends[1]) ? ends[1] : null;
    const child = parent === ends[0] ? ends[1] : ends[0];
    if (!parent || !child[1]) {
      setLinking({ source: `${child[0]}.${child[1] ?? "?"}`, target: ends.find((e) => e !== child)![0],
        check: { note: "drag from an attribute to a dimension's key (or from the key to the attribute)", ok: false } });
      return;
    }
    const link = { source: `${child[0]}.${child[1]}`, target: parent[0] };
    setLinking({ ...link, check: null });
    const check = await checkLink(model, link.source, link.target);
    setLinking((l) => (l && l.source === link.source && l.target === link.target ? { ...l, check } : l));
  };

  const nodes: EntityNodeType[] = shown.entities.map((e) => ({
    id: e.id,
    type: "entity",
    position: positions[e.id] ?? { x: 0, y: 0 },
    // Known sizes, so a card shows before React Flow has measured it (it hides unmeasured nodes)
    initialWidth: WIDTH,
    initialHeight: HEAD + e.attributes.length * ROW + 10,
    draggable: true,
    data: {
      entity: e,
      fks: new Set((shown.relationships ?? []).filter((r) => r.from.startsWith(`${e.id}.`)).map((r) => r.from.split(".")[1])),
      selected: e.id === selected,
      dimmed: !!related && !related.has(e.id) && !fresh.entities.has(e.id),
      isNew: fresh.entities.has(e.id),
      newAttrs: new Set([...fresh.attributes].filter((k) => k.startsWith(`${e.id}.`)).map((k) => k.slice(e.id.length + 1))),
    },
  }));
  const edges: Edge[] = (shown.relationships ?? []).map((r) => {
    const [src, srcCol] = r.to.split(".");
    const [tgt, tgtCol] = r.from.split(".");
    const isNew = fresh.relationships.has(`${r.from}->${r.to}`);
    const lit = isNew || (!!selected && (src === selected || tgt === selected));
    return {
      id: `${r.from}->${r.to}`, source: src, sourceHandle: `r-${srcCol}`, target: tgt, targetHandle: `l-${tgtCol}`,
      type: "smoothstep", className: `rel${lit ? " is-lit" : ""}${isNew ? " is-new" : ""}`, animated: lit,
    };
  });

  return (
    <>
      <main className="canvas" data-testid="model-canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={(_, n) => { setSelected(n.id); onTab("overview"); }}
          onPaneClick={() => setSelected(null)}
          nodesConnectable={!review}
          onConnect={(c) => void onConnect(c)}
          connectionMode={ConnectionMode.Loose}
          fitView
          fitViewOptions={{ padding: fitPadding(), maxZoom: 1 }}
          minZoom={0.1}
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
        </ReactFlow>
      </main>
      {linking && (
        <div className="link-card glass" role="dialog" aria-label="Link" data-testid="link-card">
          <div className="mono">{linking.source} <span className="quiet">→</span> {linking.target}</div>
          {linking.check === null ? <p className="quiet">Checking the data…</p>
            : "note" in linking.check ? <p className={linking.check.ok ? "quiet" : "warn-text"} data-testid="link-check">{linking.check.note}</p>
              : <p data-testid="link-check" className={linking.check.found === linking.check.total ? "ok-text" : "warn-text"}>
                {linking.check.found} of {linking.check.total} {linking.check.total === 1 ? "value" : "values"} found
                {linking.check.total > linking.check.found && <> ({linking.check.total - linking.check.found} without a match
                  get the unknown member)</>}
              </p>}
          <div className="row end">
            <button className="btn sm ghost" onClick={() => setLinking(null)}>Cancel</button>
            <button className="btn sm primary" disabled={!linking.check || ("note" in linking.check && !linking.check.ok)}
              onClick={async () => {
                const r = await engine.modelLink(model, linking.source, linking.target);
                setLinking(null);
                if (r.ok) onChange(r.model, `Linked ${linking.source} to ${linking.target}`);
                else onChange(model, `Couldn't link them: ${r.error}`);
              }}>Link</button>
          </div>
        </div>
      )}
      <div className="zoom glass" role="group" aria-label="Zoom">
        <button className="btn ghost icon" onClick={() => flow.zoomIn({ duration: 150 })} aria-label="Zoom in"><Plus /></button>
        <button className="btn ghost icon" onClick={() => flow.zoomOut({ duration: 150 })} aria-label="Zoom out"><Minus /></button>
        <div className="zoom-sep" />
        <button className="btn ghost icon" onClick={() => flow.fitView({ padding: fitPadding(), maxZoom: 1, duration: 250 })}
          aria-label="Fit to screen" title="Fit to screen"><Maximize /></button>
      </div>
      <aside className="inspector" aria-label="Model">
        <div className="tabs" role="tablist">
          {([["overview", "Model", Layers], ["load", "Load", Workflow], ["export", "Export", ScrollText],
            ["yaml", "YAML", FileCode2]] as const).map(([id, label, Icon]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={`tab${tab === id ? " is-active" : ""}`}
              onClick={() => onTab(id)}>
              <Icon />{label}
            </button>
          ))}
        </div>
        <div className="panel" role="tabpanel">
          {tab === "overview" && review && (
            <ReviewSection review={review} ticked={ticked} accepted={accepted} ready={!!preview}
              onTick={(id, on) => setTicked((old) => {
                const next = new Set(old);
                const change = review.ext.changes.find((c) => c.id === id)!;
                if (on) {
                  next.add(id);
                  for (const x of change.excludes) next.delete(x);
                  // what comes with it: the references of an entity you take
                  for (const c of review.ext.changes) if (c.needs.includes(id) && c.needs.every((n) => next.has(n))) next.add(c.id);
                } else next.delete(id);
                return next;
              })}
              onAccept={() => onReviewed(preview, review.ext.changes.filter((c) => accepted.has(c.id)))}
              onDiscard={() => onReviewed(null, [])} />
          )}
          {tab === "overview" && <Overview model={shown} analysis={review ? null : analysis} engineReady={engineReady} reasons={reasons}
            selected={shown.entities.find((e) => e.id === selected) ?? null} onSelect={setSelected} reviewing={!!review} />}
          {tab === "overview" && !selected && !review && <GrowSection {...props} />}
          {tab === "overview" && !selected && !review && job && jobMap && <JobMapSection map={jobMap} name={job.spec.name} />}
          {tab === "load" && <LoadPanel model={model} valid={analysis?.valid ?? false} engineReady={engineReady}
            onOpen={onOpenPipeline} />}
          {tab === "export" && <ExportPanel model={model} valid={analysis?.valid ?? false} engineReady={engineReady} />}
          {tab === "yaml" && <YamlPanel model={model} onChange={onChange} />}
        </div>
      </aside>
    </>
  );
}

/** What new tables would add: each change with why, ticked unless it's an alternative. */
function ReviewSection({ review, ticked, accepted, ready, onTick, onAccept, onDiscard }: {
  review: { ext: ModelExtension; tables: string[] }; ticked: Set<string>; accepted: Set<string>; ready: boolean;
  onTick: (id: string, on: boolean) => void; onAccept: () => void; onDiscard: () => void;
}) {
  const { changes, notes } = review.ext;
  const icon = { entity: Plus, relationship: Link2, append: Rows3 } as const;
  return (
    <section className="review" data-testid="model-review">
      <h3>Adding {review.tables.length === 1 ? <span className="mono">{review.tables[0]}</span> : `${review.tables.length} tables`}</h3>
      {changes.length === 0 ? <p className="quiet">Nothing to add: {review.tables.length === 1 ? "it brings" : "they bring"} no
        new tables or references the model doesn't have.</p> : (
        <>
          <p className="quiet">The model keeps everything it has; these are only additions. The canvas shows the
            result, new parts marked. Untick what you don't want.</p>
          <ul className="change-list">
            {changes.filter((c) => c.needs.every((n) => ticked.has(n))).map((c) => {
              const Icon = icon[c.kind];
              const blocked = ticked.has(c.id) && !accepted.has(c.id);
              return (
                <li key={c.id} className={`${accepted.has(c.id) ? "is-on" : ""}${blocked ? " is-blocked" : ""}`} data-testid={`change-${c.id}`}>
                  <label>
                    <input type="checkbox" checked={ticked.has(c.id)} onChange={(e) => onTick(c.id, e.target.checked)} />
                    <Icon aria-hidden="true" />
                    <span className="mono">{c.label}</span>
                  </label>
                  {c.why.length > 0 && <div className="quiet why">{c.why.join("; ")}</div>}
                  {blocked && <div className="quiet why">needs {c.needs.join(", ").replace(/entity:/g, "")}</div>}
                </li>
              );
            })}
          </ul>
        </>
      )}
      {notes.length > 0 && <ul className="issue-list warn">{notes.map((n) => <li key={n}><CircleAlert />{n}</li>)}</ul>}
      <div className="row end">
        <button className="btn sm ghost" onClick={onDiscard}>Discard</button>
        <button className="btn sm primary" disabled={!ready || accepted.size === 0} onClick={onAccept} data-testid="review-accept">
          <Check />Add {accepted.size} {accepted.size === 1 ? "change" : "changes"}
        </button>
      </div>
    </section>
  );
}

/** Tables the model doesn't load yet, to add; and a fresh start. */
function GrowSection({ model, tables, onGrow, onAddFiles, onNewModel, dataReady, engineReady }: Props) {
  const inModel = useMemo(() => tablesInModel(model, tables), [model, tables]);
  const rest = tables.filter((t) => !inModel.has(t.name));
  const [picked, setPicked] = useState<Set<string>>(new Set());
  return (
    <section data-testid="model-grow">
      <h3>Grow the model</h3>
      <p className="quiet">Add tables and Redcliff proposes only what they add: new entities, and references to and
        from the ones you have. Nothing you changed is touched. You can also drag from an attribute to a dimension's key
        to link them.</p>
      {rest.length === 0 ? <p className="quiet">Every table here is in the model. Add a file to grow it.</p> : (
        <TablePicker tables={rest} picked={picked} onPick={setPicked} />
      )}
      <div className="row between">
        <label className={`btn sm ghost${dataReady ? "" : " is-disabled"}`}>
          <FilePlus2 />Add file
          <input type="file" accept={ACCEPT} multiple hidden disabled={!dataReady}
            onChange={(e) => { if (e.target.files?.length) onAddFiles(e.target.files); e.target.value = ""; }} />
        </label>
        <button className="btn sm primary" disabled={!picked.size || !dataReady || !engineReady}
          onClick={() => { onGrow([...picked]); setPicked(new Set()); }} data-testid="grow-model">
          Add {picked.size || ""} {picked.size === 1 ? "table" : "tables"} to the model
        </button>
      </div>
      <div className="row end new-model">
        <button className="btn sm ghost" onClick={onNewModel}>New model</button>
      </div>
    </section>
  );
}

/** Tables to tick: yours first (marked), then the samples, with their columns. */
export function TablePicker({ tables, picked, onPick, disabled }: {
  tables: TableInfo[]; picked: Set<string>; onPick: (next: Set<string>) => void; disabled?: Set<string>;
}) {
  return (
    <ul className="pick-list" data-testid="table-picker">
      {tables.map((t) => (
        <li key={t.name}>
          <label title={Object.entries(t.columns).map(([c, ty]) => `${c} ${ty.toLowerCase()}`).join(", ")}>
            <input type="checkbox" checked={picked.has(t.name)} disabled={disabled?.has(t.name)} onChange={(e) => {
              const next = new Set(picked);
              if (e.target.checked) next.add(t.name); else next.delete(t.name);
              onPick(next);
            }} />
            <span className="mono">{t.name}</span>
            {!t.sample && <span className="chip tiny">yours</span>}
            <span className="quiet">{Object.keys(t.columns).length} columns</span>
            {disabled?.has(t.name) && <span className="quiet">in the model</span>}
          </label>
        </li>
      ))}
    </ul>
  );
}

function Overview({ model, analysis, engineReady, reasons, selected, onSelect, reviewing }: {
  model: DataModel; analysis: ModelAnalysis | null; engineReady: boolean; reasons: Record<string, string[]>;
  selected: ModelEntity | null;
  onSelect: (id: string | null) => void;
  reviewing?: boolean;
}) {
  const counts = model.entities.reduce<Record<string, number>>((acc, e) => ({ ...acc, [e.kind]: (acc[e.kind] ?? 0) + 1 }), {});
  return (
    <>
      {selected ? (
        <section>
          <h2>{selected.id}</h2>
          <p className="quiet">
            {KIND[selected.kind]?.label ?? selected.kind}
            {selected.kind === "dimension" && ` · ${isScd2(selected) ? "SCD2: keeps every version" : "SCD1: current values only"}`}
          </p>
          {selected.source && <p className="quiet" data-testid="entity-source">Loads from <span className="mono">{selected.source}</span>
            {(selected.append ?? []).map((f) => <span key={f}> + <span className="mono">{f}</span></span>)}</p>}
          {selected.grain && <p><strong>Grain:</strong> {selected.grain}</p>}
          {selected.description && <p>{selected.description}</p>}
          {reasons[selected.id] && (
            <ul className="why-list" data-testid="model-why">
              {reasons[selected.id].map((r) => <li key={r}>{r}</li>)}
            </ul>
          )}
          <table className="attr-table">
            <thead><tr><th>Attribute</th><th>Type</th><th>Role</th></tr></thead>
            <tbody>
              {selected.attributes.map((a) => (
                <tr key={a.name} title={a.description || undefined}>
                  <td className="mono">{a.name}</td>
                  <td className="mono quiet">{a.type}</td>
                  <td>{[a.role?.replace("_", " "), a.history === "scd1" && "SCD1",
                    a.track === false && "not tracked"].filter(Boolean).join(", ")}
                    {reasons[`${selected.id}.${a.name}`] && <div className="quiet why">{reasons[`${selected.id}.${a.name}`].join("; ")}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="btn sm ghost" onClick={() => onSelect(null)}>Show the whole model</button>
        </section>
      ) : (
        <section>
          <h2>{model.name}</h2>
          <p className="quiet">
            {model.style === "data_vault" ? "Data Vault 2.0" : "Kimball"} ·{" "}
            {Object.entries(counts).map(([k, n]) => `${n} ${KIND[k as EntityKind]?.label.toLowerCase() ?? k}${n === 1 ? "" : "s"}`).join(", ")}
            {" "}· {(model.relationships ?? []).length} relationships
          </p>
          {model.description && <p>{model.description}</p>}
          {!reviewing && <p className="quiet">Click an entity for its attributes and why it is what it is. Edit the model in
            the YAML tab.</p>}
        </section>
      )}
      {!reviewing && <section>
        <h3>Checks</h3>
        {!engineReady || !analysis ? <p className="quiet">Checking once the engine has started…</p>
          : analysis.valid ? <p className="ok-line" data-testid="model-valid"><CircleCheck />The model is valid</p>
            : (
              <ul className="issue-list" data-testid="model-issues">
                {analysis.issues.map((i, n) => (
                  <li key={n}><CircleAlert />{i.node ? <><strong>{i.node}</strong>: </> : null}{i.message}</li>
                ))}
              </ul>
            )}
      </section>}
      {(model.relationships ?? []).length > 0 && (
        <section>
          <h3>Relationships</h3>
          <ul className="rel-list">
            {(model.relationships ?? []).map((r) => (
              <li key={`${r.from}->${r.to}`} className="mono">{r.from} <span className="quiet">→</span> {r.to}</li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

/** How much of what a legacy job writes the model covers, column by column; gaps first. */
function JobMapSection({ map, name }: { map: JobMap; name: string }) {
  const cols = [...map.columns].sort((a, b) => Number(a.covered) - Number(b.covered));
  return (
    <section data-testid="job-map">
      <h3>Legacy job coverage</h3>
      <p data-testid="job-map-summary"><strong>{map.covered} of {map.total}</strong> columns <span className="mono">{name}</span> writes
        ({Math.round(map.coverage * 100)}%) come from model attributes.</p>
      <div className="bar-track"><div className="bar-fill" style={{ width: `${map.coverage * 100}%` }} /></div>
      <ul className="job-map-list">
        {cols.map((c) => {
          const direct = c.inputs.filter((i) => i.kind === "direct");
          return (
            <li key={`${c.target}.${c.column}`} className={c.covered ? "" : "is-gap"}>
              <div className="mono">{c.table ?? c.target}.{c.column}</div>
              {direct.length === 0 ? <div className="quiet">computed by the job</div> : direct.map((i) => (
                <div key={`${i.table}.${i.column}`} className="mono from">
                  <span className="quiet">{i.table}.{i.column} → </span>
                  {i.attributes.length ? i.attributes.join(", ") : <span className="bad-text">not in the model</span>}
                </div>
              ))}
            </li>
          );
        })}
      </ul>
      {Object.keys(map.unmapped).length > 0 && (
        <p className="quiet">Read by the job but not in the model: {Object.entries(map.unmapped).map(([t, cs]) =>
          <span key={t}><span className="mono">{t}</span> ({cs.join(", ")}) </span>)}</p>
      )}
    </section>
  );
}

const FORMATS: { id: ModelExport; label: string; file: (name: string) => string }[] = [
  { id: "ddl", label: "DDL", file: (n) => `${n}.sql` },
  { id: "dbml", label: "DBML", file: (n) => `${n}.dbml` },
  { id: "mermaid", label: "Mermaid", file: (n) => `${n}.mmd` },
  { id: "dbt", label: "dbt", file: () => "schema.yml" },
];
const DIALECTS = ["duckdb", "snowflake", "databricks", "bigquery", "postgres", "redshift", "tsql", "oracle"];

function ExportPanel({ model, valid, engineReady }: { model: DataModel; valid: boolean; engineReady: boolean }) {
  const [fmt, setFmt] = useState<ModelExport>("ddl");
  const [dialect, setDialect] = useState("duckdb");
  const [code, setCode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!engineReady) return;
    let live = true;
    engine.modelExport(model, fmt, dialect).then((r) => {
      if (!live) return;
      setCode(r.ok ? r.code : null);
      setError(r.ok ? null : r.issues?.length ? "Fix the model's problems first (see the Model tab)." : r.error);
    });
    return () => { live = false; };
  }, [model, fmt, dialect, engineReady]);
  const file = FORMATS.find((f) => f.id === fmt)!.file(model.name.replace(/\W+/g, "_") || "model");
  return (
    <section>
      <div className="segmented small" role="group" aria-label="Format">
        {FORMATS.map((f) => <button key={f.id} aria-pressed={fmt === f.id} onClick={() => setFmt(f.id)}>{f.label}</button>)}
      </div>
      {fmt === "ddl" && (
        <div className="row between project-bar">
          <select className="file-select" value={dialect} onChange={(e) => setDialect(e.target.value)} aria-label="Dialect">
            {DIALECTS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
      )}
      {!engineReady ? <p className="quiet">Available once the engine has started.</p>
        : error ? <p className="error">{error}</p> : code !== null && (
          <>
            <div className="code-shell">
              <div className="code-head">
                <span className="code-file">{file}</span>
                <span className="row">
                  <button className="btn sm copy" onClick={() => {
                    const a = document.createElement("a");
                    a.href = URL.createObjectURL(new Blob([code], { type: "text/plain" }));
                    a.download = file;
                    a.click();
                    URL.revokeObjectURL(a.href);
                  }}><Download />Download</button>
                  <button className="btn sm copy" onClick={async () => {
                    try { await navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* blocked */ }
                  }}>{copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy"}</button>
                </span>
              </div>
              <pre className="code-block" data-testid="model-export"><code>{fmt === "ddl" ? highlight(code) : code}</code></pre>
            </div>
            {fmt === "mermaid" && <p className="quiet code-note">Paste into any Markdown that renders Mermaid, such as a GitHub README.</p>}
            {fmt === "dbml" && <p className="quiet code-note">Opens in dbdiagram.io. Roles and history travel in column notes.</p>}
          </>
        )}
      {!valid && engineReady && !error && <p className="quiet">Checking the model…</p>}
    </section>
  );
}

function YamlPanel({ model, onChange }: { model: DataModel; onChange: (m: DataModel, note?: string) => void }) {
  const [text, setText] = useState(() => yaml.dump(model, { lineWidth: 100, flowLevel: 3 }));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { setText(yaml.dump(model, { lineWidth: 100, flowLevel: 3 })); }, [model]);
  return (
    <section>
      <p className="quiet">The model is a file of its own (<code>model.yaml</code>); pipelines will reference it.</p>
      <textarea className="code-input tall" spellCheck={false} value={text} aria-label="Model YAML"
        onChange={(e) => setText(e.target.value)} />
      {error && <p className="error">{error}</p>}
      <div className="row between">
        <button className="btn sm primary" onClick={() => {
          try {
            const next = yaml.load(text) as DataModel;
            if (!next || typeof next !== "object" || !Array.isArray(next.entities)) throw new Error("a model needs a list of entities");
            setError(null);
            onChange(next, "Model updated");
          } catch (e) { setError(String((e as Error).message ?? e)); }
        }}>Apply</button>
        <button className="btn sm ghost" onClick={() => {
          const a = document.createElement("a");
          a.href = URL.createObjectURL(new Blob([text], { type: "text/yaml" }));
          a.download = "model.yaml";
          a.click();
          URL.revokeObjectURL(a.href);
        }}><Download />model.yaml</button>
      </div>
    </section>
  );
}

const SAMPLE_DDL = `CREATE TABLE dim_product (
  product_key BIGINT PRIMARY KEY,
  sku VARCHAR(20) NOT NULL,
  name VARCHAR(100),
  category VARCHAR(40)
);
CREATE TABLE fct_sales (
  sale_id BIGINT PRIMARY KEY,
  product_key BIGINT REFERENCES dim_product (product_key),
  quantity INT,
  revenue DECIMAL(12, 2)
);`;

export function ImportPanel({ engineReady, onChange }: { engineReady: boolean; onChange: (m: DataModel, note?: string) => void }) {
  const [text, setText] = useState(SAMPLE_DDL);
  const [dialect, setDialect] = useState("");
  const [name, setName] = useState("imported");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const fmt: "ddl" | "dbml" = /^\s*Table\s+[\w".]+[^\n]*\{/im.test(text) ? "dbml" : "ddl";
  return (
    <section>
      <p className="quiet">Paste <code>CREATE TABLE</code> statements or DBML. Table names give the kind
        (<code>dim_</code>, <code>fct_</code>, <code>hub_</code>, <code>lnk_</code>, <code>sat_</code>); anything Redcliff
        can't place comes back as a warning.</p>
      <textarea className="code-input medium" spellCheck={false} value={text} aria-label="DDL or DBML"
        onChange={(e) => setText(e.target.value)} />
      <div className="row between project-bar">
        <span className="chip">{fmt === "dbml" ? "DBML" : "SQL DDL"}</span>
        {fmt === "ddl" && (
          <select className="file-select" value={dialect} onChange={(e) => setDialect(e.target.value)} aria-label="Dialect">
            <option value="">any dialect</option>
            {DIALECTS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        )}
        <input className="file-select" value={name} onChange={(e) => setName(e.target.value)} aria-label="Model name" />
      </div>
      {error && <p className="error">{error}</p>}
      {warnings.length > 0 && (
        <ul className="issue-list warn" data-testid="model-import-warnings">
          {warnings.map((w) => <li key={w}><CircleAlert />{w}</li>)}
        </ul>
      )}
      <button className="btn sm primary" disabled={!engineReady || busy || !text.trim()} onClick={async () => {
        setBusy(true);
        const r = await engine.modelImport(text, fmt, dialect || null, name || "imported");
        setBusy(false);
        if (!r.ok) { setError(r.issues?.length ? r.issues.join("; ") : r.error); return; }
        setError(null);
        setWarnings(r.warnings);
        onChange(r.model, `Imported ${r.model.entities.length} tables`);
      }}>{busy ? "Importing…" : "Import"}</button>
    </section>
  );
}

function LoadPanel({ model, valid, engineReady, onOpen }: {
  model: DataModel; valid: boolean; engineReady: boolean; onOpen: (spec: Spec) => void;
}) {
  const [result, setResult] = useState<{ spec: Spec; targets: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!engineReady) return;
    let live = true;
    csvHeaders().then((headers) => engine.modelLoadPipeline(model, headers)).then((r) => {
      if (!live) return;
      setResult(r.ok ? { spec: r.spec, targets: r.targets } : null);
      setError(r.ok ? null : r.issues?.length ? r.issues.join("; ") : r.error);
    });
    return () => { live = false; };
  }, [model, engineReady]);
  const vault = model.style === "data_vault";
  return (
    <section>
      <h3>The pipeline that loads this model</h3>
      <p className="quiet">
        {vault
          ? "Hubs and links insert new keys, satellites insert a row when their hashdiff changes (MD5 hash keys), and each hub gets a current-state mart."
          : "Dimensions keep history (SCD2: type-1 columns overwrite, ignored columns don't make versions, an unknown member for missing keys); facts look up the version valid on their date."}
        {" "}It's an ordinary pipeline, so it compiles to DuckDB, Spark, PySpark, dbt and Lakeflow.
      </p>
      {!engineReady ? <p className="quiet">Available once the engine has started.</p>
        : error ? <p className="error">{valid ? error : "Fix the model's problems first (see the Model tab)."}</p>
          : result && (
            <>
              <ul className="rel-list" data-testid="model-load-targets">
                {result.targets.map((t) => <li key={t} className="mono">{t}</li>)}
              </ul>
              <div className="row between" style={{ marginTop: 12 }}>
                <span className="quiet">{result.spec.nodes.length} steps, {result.targets.length} targets</span>
                <button className="btn sm primary" onClick={() => onOpen(result.spec)}><Workflow />Open as a pipeline</button>
              </div>
              <p className="quiet code-note">From the command line: <code>redcliff model pipeline model.yaml -o load.yaml</code>,
                then <code>redcliff run load.yaml --db warehouse.duckdb</code> and <code>redcliff model test model.yaml --db warehouse.duckdb</code>.</p>
            </>
          )}
    </section>
  );
}
