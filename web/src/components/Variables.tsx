import { Variable } from "lucide-react";
import { useEffect, useState } from "react";

type Value = string | number | boolean;

/** The pipeline's variables, editable in place: a new value re-runs the previews and checks. */
export function Variables({ values, onChange, belowHint }: {
  values: Record<string, Value>;
  onChange: (next: Record<string, Value>) => void;
  belowHint: boolean;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  useEffect(() => { setDraft(Object.fromEntries(Object.entries(values).map(([k, v]) => [k, String(v)]))); }, [values]);
  const apply = (name: string) => {
    const text = draft[name] ?? "";
    const old = values[name];
    let next: Value = text;
    if (typeof old === "number" && text.trim() !== "" && !Number.isNaN(Number(text))) next = Number(text);
    if (typeof old === "boolean" && /^(true|false)$/i.test(text.trim())) next = text.trim().toLowerCase() === "true";
    if (next !== old) onChange({ ...values, [name]: next });
  };
  return (
    <div className={`vars glass${belowHint ? " below-hint" : ""}`} role="group" aria-label="Pipeline variables">
      <span className="vars-label" title="Variables: ${name} in the spec. Change one to re-run the previews.">
        <Variable />Variables
      </span>
      {Object.keys(values).map((name) => (
        <label key={name} className="var">
          <span>{name}</span>
          <input
            value={draft[name] ?? ""}
            spellCheck={false}
            size={Math.min(Math.max((draft[name] ?? "").length, 4), 18)}
            onChange={(e) => setDraft((d) => ({ ...d, [name]: e.target.value }))}
            onBlur={() => apply(name)}
            onKeyDown={(e) => {
              if (e.key === "Enter") apply(name);
              if (e.key === "Escape") setDraft((d) => ({ ...d, [name]: String(values[name]) }));
            }}
            aria-label={`Variable ${name}`}
          />
        </label>
      ))}
    </div>
  );
}
