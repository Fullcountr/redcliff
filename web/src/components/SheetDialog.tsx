// Which sheets of an Excel workbook to add, each as a table, and whether their first row is the header.
import { FileSpreadsheet, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Sheet } from "../engine/excel";

export type Workbook = { file: string; sheets: Sheet[] };

type Props = {
  books: Workbook[];
  onAdd: (picked: { file: string; sheet: Sheet }[], header: boolean) => void;
  onClose: () => void;
};

export function SheetDialog({ books, onAdd, onClose }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const key = (file: string, sheet: string) => `${file}\u0000${sheet}`;
  const [picked, setPicked] = useState<Set<string>>(
    () => new Set(books.flatMap((b) => b.sheets.map((s) => key(b.file, s.name)))));
  const [header, setHeader] = useState(true);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);
  const count = picked.size;
  return (
    <dialog ref={ref} className="dialog sheet-dialog" onClose={onClose} aria-labelledby="sheets-title" data-testid="sheet-dialog">
      <form method="dialog" onSubmit={(e) => {
        e.preventDefault();
        onAdd(books.flatMap((b) => b.sheets.filter((s) => picked.has(key(b.file, s.name))).map((sheet) => ({ file: b.file, sheet }))), header);
      }}>
        <div className="dialog-head">
          <div>
            <h2 id="sheets-title">Add sheets</h2>
            <p>Each sheet you pick becomes a table. Pipelines and models read it by its table name; for
              production, land the sheet as CSV or Parquet, which Spark reads directly.</p>
          </div>
          <button type="button" className="btn ghost icon" onClick={onClose} aria-label="Close"><X /></button>
        </div>
        <div className="dialog-body">
          {books.map((b) => (
            <section key={b.file} className="sheet-book">
              <h3><FileSpreadsheet aria-hidden="true" />{b.file}</h3>
              <ul className="pick-list">
                {b.sheets.map((s) => (
                  <li key={s.name}>
                    <label>
                      <input type="checkbox" checked={picked.has(key(b.file, s.name))} onChange={(e) => {
                        const next = new Set(picked);
                        if (e.target.checked) next.add(key(b.file, s.name)); else next.delete(key(b.file, s.name));
                        setPicked(next);
                      }} />
                      <span className="mono">{s.name}</span>
                      <span className="quiet">{Math.max(0, s.rows.length - (header ? 1 : 0))} rows</span>
                      <span className="quiet sheet-head" title={s.rows[0]?.join(", ")}>{s.rows[0]?.slice(0, 5).join(", ")}
                        {(s.rows[0]?.length ?? 0) > 5 ? ", …" : ""}</span>
                    </label>
                  </li>
                ))}
              </ul>
            </section>
          ))}
          <label className="check-line">
            <input type="checkbox" checked={header} onChange={(e) => setHeader(e.target.checked)} />
            The first row holds the column names
          </label>
        </div>
        <div className="dialog-foot">
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn primary" disabled={!count} data-testid="add-sheets">
            Add {count} {count === 1 ? "sheet" : "sheets"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
