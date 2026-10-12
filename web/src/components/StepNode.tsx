import { Handle, Position, useStore, type NodeProps, type Node } from "@xyflow/react";
import { CircleAlert, Rows3, TriangleAlert, Unlink, Wrench } from "lucide-react";
import { memo, useState } from "react";
import { MAX_COLUMNS, NODE_WIDTH } from "../layout";
import { STEP_LABELS, summarize, type ScoreStatus, type Step } from "../model";
import type { Highlight } from "../lineage";
import { StepIcon } from "../ui";

export type StepData = {
  step: Step;
  columns: string[];
  rowCount: number | null | undefined;
  issues: string[];
  highlight: Highlight | null;
  selected: boolean;
  score: ScoreStatus | null; // worst scorecard status of the legacy piece this step came from
  scored: ScoreStatus | null; // set while that scorecard item is picked in the inspector
  onHoverColumn: (ref: string | null) => void;
  onPinColumn: (ref: string) => void;
};

export type StepNodeType = Node<StepData, "step">;

function rows(n: number | null | undefined): string {
  if (n === undefined) return "";
  if (n === null) return "–";
  return `${n.toLocaleString()} ${n === 1 ? "row" : "rows"}`;
}

const SCORE_COLOR: Record<ScoreStatus, string> = { converted: "var(--ok)", review: "var(--warn)", manual: "var(--bad)" };

/** Below this zoom, cards trade their column lists for a large, readable title. */
export const FAR_ZOOM = 0.5;

function StepNodeView({ data }: NodeProps<StepNodeType>) {
  const { step, columns, rowCount, issues, highlight, selected, score, scored } = data;
  const [expanded, setExpanded] = useState(false);
  // Bucketed so panning and small zoom steps don't re-render every card.
  const zoom = useStore((s) => (s.transform[2] < FAR_ZOOM ? Math.round(s.transform[2] * 40) / 40 : 1));
  const far = zoom < FAR_ZOOM;
  const shown = expanded ? columns : columns.slice(0, MAX_COLUMNS);
  const involved = highlight?.nodes.has(step.id) ?? false;
  const classes = [
    "step",
    `step-${step.type}`,
    selected && "is-selected",
    issues.length > 0 && "has-issues",
    highlight && !involved && "is-dimmed",
    highlight?.rowNodes.has(step.id) && "rows-change",
    highlight?.mode === "impact" && "impact-mode",
    scored && "is-scored",
    far && "is-far",
  ].filter(Boolean).join(" ");
  const summary = summarize(step);
  const breaks = highlight?.breaks.has(step.id);
  const rowsChange = highlight?.rowNodes.has(step.id) && !breaks;

  return (
    <div className={classes} style={{ width: NODE_WIDTH, ...(scored ? { ["--score" as string]: SCORE_COLOR[scored] } : {}) }}>
      {step.type !== "source" && <Handle type="target" position={Position.Left} />}
      <div className="step-head">
        <StepIcon type={step.type} />
        <div className="step-titles">
          <span className="step-id" title={step.id}>{step.id}</span>
          <span className="step-type">{STEP_LABELS[step.type]}</span>
        </div>
        <span className="step-rows">{rows(rowCount)}</span>
      </div>
      <div className="step-summary" title={summary}>{summary}</div>
      {(breaks || rowsChange || (score && score !== "converted")) && (
        <div className="step-badges">
          {breaks && <span className="badge breaks"><Unlink />Breaks on rename</span>}
          {rowsChange && <span className="badge rows"><Rows3 />Rows change</span>}
          {score === "review" && <span className="badge review"><TriangleAlert />Review</span>}
          {score === "manual" && <span className="badge manual"><Wrench />Rewrite by hand</span>}
        </div>
      )}
      {issues.length > 0 && (
        <div className="step-issue" title={issues.join("\n")}>
          <CircleAlert />
          <span>{issues.length === 1 ? issues[0] : `${issues.length} issues`}</span>
        </div>
      )}
      {columns.length > 0 && (
        <ul className="step-columns nodrag">
          {shown.map((c) => {
            const ref = `${step.id}.${c}`;
            const lit = highlight?.columns.has(ref);
            const isFocus = highlight?.focus === ref;
            return (
              <li key={c}>
                <button
                  type="button"
                  className={`col${lit ? " is-lit" : ""}${isFocus ? " is-focus" : ""}`}
                  onMouseEnter={() => data.onHoverColumn(ref)}
                  onMouseLeave={() => data.onHoverColumn(null)}
                  onFocus={() => data.onHoverColumn(ref)}
                  onBlur={() => data.onHoverColumn(null)}
                  onClick={(e) => {
                    e.stopPropagation();
                    data.onPinColumn(ref);
                  }}
                  aria-label={`${c}: show lineage`}
                >
                  {c}
                </button>
              </li>
            );
          })}
          {columns.length > MAX_COLUMNS && (
            <li>
              <button type="button" className="col col-more" onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}>
                {expanded ? "Show fewer" : `${columns.length - MAX_COLUMNS} more columns`}
              </button>
            </li>
          )}
        </ul>
      )}
      {far && (
        <div className="step-far" style={{ ["--z" as string]: zoom }} aria-hidden="true">
          <span className="far-meta">
            <StepIcon type={step.type} />
            {STEP_LABELS[step.type]}{rowCount != null ? ` · ${rows(rowCount)}` : ""}
            {score === "review" && <span className="far-flag review">Review</span>}
            {score === "manual" && <span className="far-flag manual">Rewrite</span>}
          </span>
          <span className="far-id">
            {step.id.split("_").map((part, i) => <span key={i}>{i ? <>_<wbr /></> : null}{part}</span>)}
          </span>
        </div>
      )}
      {step.type !== "target" && <Handle type="source" position={Position.Right} />}
    </div>
  );
}

export const StepNode = memo(StepNodeView);
