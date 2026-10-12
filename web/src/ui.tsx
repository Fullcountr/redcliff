// Small shared UI pieces: step icons, a dropdown menu, the theme switch and a code highlighter.
import {
  ArrowDownToLine, Columns3, Database, Filter, GitMerge, Layers, Sigma, SquareCode, SquareFunction,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { StepType } from "./model";

export const STEP_ICONS: Record<StepType, LucideIcon> = {
  source: Database,
  filter: Filter,
  select: Columns3,
  derive: SquareFunction,
  join: GitMerge,
  aggregate: Sigma,
  union: Layers,
  sql: SquareCode,
  target: ArrowDownToLine,
};

export function StepIcon({ type }: { type: StepType }) {
  const Icon = STEP_ICONS[type];
  return <span className="step-icon" aria-hidden="true"><Icon /></span>;
}

export function BrandMark({ size = 20 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size * 1.2} height={size} viewBox="0 0 22 18" aria-hidden="true">
      <rect x="0" y="0" width="22" height="4" rx="1.5" className="s1" />
      <rect x="3" y="7" width="19" height="4" rx="1.5" className="s2" />
      <rect x="7" y="14" width="15" height="4" rx="1.5" className="s3" />
    </svg>
  );
}

// ------------------------------------------------------------------ menu

export function Menu({ button, children, label }: {
  button: (props: { open: boolean; toggle: () => void }) => ReactNode;
  children: (close: () => void) => ReactNode;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("pointerdown", onDown); window.removeEventListener("keydown", onKey); };
  }, [open]);
  return (
    <div className="menu-wrap" ref={ref}>
      {button({ open, toggle: () => setOpen((o) => !o) })}
      {open && <div className="menu" role="menu" aria-label={label}>{children(close)}</div>}
    </div>
  );
}

export function MenuItem({ icon: Icon, title, sub, checked, onClick }: {
  icon: LucideIcon; title: string; sub?: string; checked?: boolean; onClick: () => void;
}) {
  return (
    <button type="button" role="menuitem" className="menu-item" onClick={onClick}>
      <span className="mi-icon"><Icon /></span>
      <span className="mi-text">
        <span className="mi-title">{title}</span>
        {sub && <span className="mi-sub">{sub}</span>}
      </span>
      {checked && <svg className="mi-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M20 6 9 17l-5-5" /></svg>}
    </button>
  );
}

// ------------------------------------------------------------------ theme

export type Theme = "system" | "light" | "dark";

function storedTheme(): Theme {
  try {
    const t = localStorage.getItem("redcliff-theme");
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

export function useTheme(): [Theme, (t: Theme) => void, boolean] {
  const [theme, setTheme] = useState<Theme>(storedTheme);
  const [systemDark, setSystemDark] = useState(() => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    const on = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mq?.addEventListener("change", on);
    return () => mq?.removeEventListener("change", on);
  }, []);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try { localStorage.setItem("redcliff-theme", theme); } catch { /* storage unavailable */ }
  }, [theme]);
  const dark = theme === "dark" || (theme === "system" && systemDark);
  return [theme, setTheme, dark];
}

// ------------------------------------------------------------------ code highlighting

const SQL_WORDS = new Set((
  "select from where group by order having join left right full inner outer cross on as and or not in is null " +
  "case when then else end with insert into overwrite table create replace view distinct union all limit " +
  "over partition values cast true false between like copy to format header delimiter csv parquet " +
  "import def return if for in lambda none"
).split(" "));

const TOKEN = /(--[^\n]*|#[^\n]*)|('(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.)*")|\b(\d+(?:\.\d+)?)\b|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?/g;

/** Light syntax colouring for SQL and Python; plain text stays readable without it. */
export function highlight(code: string, python = false): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let k = 0;
  for (const m of code.matchAll(TOKEN)) {
    const [text, comment, str, num, word, paren] = m;
    const at = m.index ?? 0;
    if (at > last) out.push(code.slice(last, at));
    last = at + text.length;
    if (comment && (python ? comment.startsWith("#") : comment.startsWith("--"))) {
      out.push(<span key={k++} className="tok-com">{comment}</span>);
    } else if (comment) {
      out.push(comment);
    } else if (str) {
      out.push(<span key={k++} className="tok-str">{str}</span>);
    } else if (num) {
      out.push(<span key={k++} className="tok-num">{num}</span>);
    } else if (word) {
      const lower = word.toLowerCase();
      if (paren) {
        out.push(<span key={k++} className={SQL_WORDS.has(lower) && !python ? "tok-kw" : "tok-fn"}>{word}</span>, paren);
      } else if (SQL_WORDS.has(lower) && (python ? word === lower : true)) {
        out.push(<span key={k++} className="tok-kw">{word}</span>);
      } else {
        out.push(word);
      }
    }
  }
  if (last < code.length) out.push(code.slice(last));
  return out;
}
