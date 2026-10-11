import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import { memo, useState } from "react";
import { MAX_COLUMNS, NODE_WIDTH } from "../layout";
import { STEP_LABELS, summarize, type Step } from "../model";
import type { Highlight } from "../lineage";

export type StepData = {
  step: Step;
  columns: string[];
  rowCount: number | null | undefined;
  issues: string[];
  highlight: Highlight | null;
  selected: boolean;
  onHoverColumn: (ref: string | null) => void;
  onPinColumn: (ref: string) => void;
};

export type StepNodeType = Node<StepData, "step">;

function rows(n: number | null | undefined): string {
  if (n === undefined) return "";
  if (n === null) return "–";
  return `${n.toLocaleString()} ${n === 1 ? "row" : "rows"}`;
}

function StepNodeView({ data }: NodeProps<StepNodeType>) {
  const { step, columns, rowCount, issues, highlight, selected } = data;
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? columns : columns.slice(0, MAX_COLUMNS);
  const involved = highlight?.nodes.has(step.id) ?? false;
  const classes = [
    "step",
    `step-${step.type}`,
    selected && "is-selected",
    issues.length > 0 && "has-issues",
    highlight && !involved && "is-dimmed",
    highlight?.rowNodes.has(step.id) && "rows-change",
  ].filter(Boolean).join(" ");

  return (
    <div className={classes} style={{ width: NODE_WIDTH }}>
      {step.type !== "source" && <Handle type="target" position={Position.Left} />}
      <div className="step-head">
        <span className="step-type">{STEP_LABELS[step.type]}</span>
        <span className="step-rows">{rows(rowCount)}</span>
      </div>
      <div className="step-id">{step.id}</div>
      <div className="step-summary" title={summarize(step)}>{summarize(step)}</div>
      {highlight?.breaks.has(step.id) && <div className="step-flag">Breaks on rename</div>}
      {highlight?.rowNodes.has(step.id) && !highlight?.breaks.has(step.id) && (
        <div className="step-flag step-flag-rows">Rows change</div>
      )}
      {issues.length > 0 && (
        <div className="step-issue" title={issues.join("\n")}>
          {issues.length === 1 ? issues[0] : `${issues.length} issues`}
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
                {expanded ? "Show fewer" : `${columns.length - MAX_COLUMNS} more`}
              </button>
            </li>
          )}
        </ul>
      )}
      {step.type !== "target" && <Handle type="source" position={Position.Right} />}
    </div>
  );
}

export const StepNode = memo(StepNodeView);
