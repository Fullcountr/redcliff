"""Precompute what the playground shows for each example, so it's interactive before the engine loads.

For every example spec: the analysis (columns and lineage), each step's row count on the sample
data, and the blast radius of every column. All of it comes from the same engine the browser
runs, so it matches what Pyodide and DuckDB-Wasm compute later.

    python precompute.py SAMPLES_DIR OUT_JSON   (with core/src on PYTHONPATH, sqlglot and duckdb installed)
"""

import json
import os
import sys
from pathlib import Path

import duckdb

from redcliff import web


def call(fn, *args):
    result = json.loads(fn(*args))
    if not result.pop("ok"):
        raise RuntimeError(result.get("error"))
    return result


def main(samples_dir: str, out: str) -> None:
    samples = Path(samples_dir)
    specs = json.loads((samples / "samples.json").read_text())["specs"]
    os.chdir(samples)  # previews read CSV sources by file name
    con = duckdb.connect()
    for csv in samples.glob("*.csv"):
        con.execute(f"CREATE TABLE {csv.stem} AS SELECT * FROM read_csv('{csv.name}', header = true)")

    result = {}
    for name, spec in specs.items():
        text = json.dumps(spec)
        analysis = call(web.analyze, text)
        counts = {}
        for node_id in analysis.get("order", []):
            step = next(n for n in spec["nodes"] if n["id"] == node_id)
            if step["type"] == "target":
                continue
            sql = call(web.preview_query, text, node_id)["sql"]
            counts[node_id] = con.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0]
        impacts = {}
        for node_id, columns in analysis["columns"].items():
            for column in columns:
                impacts[f"{node_id}.{column}"] = call(web.impact, text, node_id, column)
        result[name] = {"analysis": analysis, "counts": counts, "impacts": impacts}
    Path(out).write_text(json.dumps(result, separators=(",", ":")))
    print(f"precomputed {len(result)} examples")


if __name__ == "__main__":
    main(*sys.argv[1:3])
