"""Keep a spec from someone else inside a folder.

The MCP server (``--root``) and ``redcliff diff --data`` (the pull request bot) run specs that the
person running them didn't write. Two checks keep those specs to the files of the workspace:

- :func:`check_paths` refuses a spec whose sources or targets point outside the folder;
- :func:`lock_duckdb` limits a DuckDB connection to the folder for good, so SQL that slipped past
  validation can't read or write other files or reach the network either.
"""

from __future__ import annotations

from pathlib import Path

from redcliff.spec import Pipeline, bind


class OutsideRoot(ValueError):
    """A spec names a file outside the folder it may use."""


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_paths(pipeline: Pipeline, spec_dir: str | Path, root: str | Path) -> None:
    """Raise :class:`OutsideRoot` if a file source or target resolves outside ``root``.

    Paths are relative to ``spec_dir``, the spec's folder, as everywhere else in Redcliff."""
    root = Path(root).resolve()
    for node in bind(pipeline).nodes:
        raw = node.params.get("path") if node.type in ("source", "target") else None
        if not isinstance(raw, str) or node.params.get("format") == "table":
            continue
        resolved = (Path(spec_dir) / raw).resolve()
        if not _inside(resolved, root):
            raise OutsideRoot(f"{node.id}: {raw} is outside {root}")


def lock_duckdb(con, *roots: str | Path):
    """Allow ``con`` file access only under ``roots``, with no network, and lock the setting."""
    folders = [str(Path(r).resolve()).rstrip("/") + "/" for r in roots]
    con.execute("SET allowed_directories = ?", [folders])
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con
