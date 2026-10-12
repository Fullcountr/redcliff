"""Generate the pipeline that loads a model (M3 and M4 in docs/modeling.md).

The model is the source of truth; the load is an ordinary pipeline spec, so every compiler
(DuckDB, Spark SQL, PySpark, dbt, Lakeflow) gets it for free. Per entity:

- a source step for its ``source`` and a staging ``sql`` step that casts every attribute to its
  model type (attribute ``source`` expressions are applied here);
- Kimball dimensions: a ``scd2`` target (type-1 attributes, ignored columns, a generated surrogate
  key and an unknown member) or ``scd1``; a dimension with a date business key and no source is a
  date dimension built from the dates the facts refer to;
- Kimball facts: surrogate keys looked up point in time (the version valid on the row's date; the
  unknown member, -1, when there is none), then a ``merge`` on the fact's business key;
- Data Vault: hubs and links ``insert_new``, satellites ``insert_changed`` on an MD5 hashdiff,
  hash keys as MD5 of the upper-cased, trimmed business key parts joined with ``||``.

Staging steps carry the key expectations (not null, unique). Post-load checks (one current row
per key, no overlapping versions, facts pointing at real versions) are :func:`model_checks`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from redcliff.model import Entity, Model, validate_model
from redcliff.spec import ID_PATTERN, Pipeline, SpecError

DATE_PARTS = {"year": "YEAR(d)", "quarter": "QUARTER(d)", "month": "MONTH(d)", "day": "DAY(d)",
              "day_of_month": "DAY(d)", "day_of_year": "DAYOFYEAR(d)", "week_of_year": "WEEKOFYEAR(d)"}
DATE_KEY = "YEAR(d) * 10000 + MONTH(d) * 100 + DAY(d)"


class GenerateError(ValueError):
    pass


def _rel(entity_source: str, model_dir: Path, spec_dir: Path) -> str:
    """The entity's source path as the generated spec should name it (relative to the spec)."""
    if Path(entity_source).is_absolute():
        return entity_source
    return os.path.relpath((model_dir / entity_source).resolve(), spec_dir.resolve()).replace(os.sep, "/")


def _csv_header(path: Path) -> list[str] | None:
    """A CSV file's column names, when it exists and they're all plain names."""
    import csv

    try:
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), None)
    except OSError:
        return None
    names = [h.strip() for h in header or []]
    return names if names and all(ID_PATTERN.match(n) for n in names) and len(set(names)) == len(names) else None


def _source_node(e: Entity, model_dir: Path, spec_dir: Path) -> dict:
    """The source step for an entity. CSV columns are declared from the file's header (typed like the
    attributes they map to, else varchar; the
    staging step casts them), so lineage runs from the file's columns to the model's attributes."""
    src = e.source
    suffix = Path(src).suffix.lower()
    if suffix in (".csv", ".parquet"):
        params: dict[str, Any] = {"format": suffix[1:], "path": _rel(src, model_dir, spec_dir)}
        header = _csv_header(model_dir / src) if suffix == ".csv" else None
        if header:
            params["columns"] = {c: _raw_type(e, c) for c in header}
    else:
        params = {"format": "table", "table": src}
    return {"id": f"src_{e.id}", "type": "source", "params": params}


def _raw_type(e: Entity, column: str) -> str:
    """The type to read a file column as: the type of the attribute it maps to, else varchar."""
    for a in e.attributes:
        if a.source == column or (a.source is None and a.name == column):
            return exp.DataType.build(a.type).sql()
    return "VARCHAR"


def _table_source(node_id: str, e: Entity) -> dict:
    """A source step reading an entity's loaded table, with its columns declared from the model."""
    cols = {a.name: exp.DataType.build(a.type).sql() for a in e.attributes}
    if e.scd2:
        cols.update({"valid_from": "TIMESTAMP", "valid_to": "TIMESTAMP", "is_current": "BOOLEAN"})
    return {"id": node_id, "type": "source", "params": {"format": "table", "table": e.id, "columns": cols}}


def _cast(expr: str, type_name: str) -> str:
    return f"CAST({expr} AS {exp.DataType.build(type_name).sql()})"


def _expr(a) -> str:
    return a.source or a.name


def _generated_sk(e: Entity) -> str | None:
    """The surrogate key Redcliff generates (one without a source expression)."""
    return next((a.name for a in e.attributes if a.role == "surrogate_key" and not a.source), None)


def _business_keys(e: Entity) -> list[str]:
    return [a.name for a in e.attributes if a.role == "business_key"]


def _date_column(expr: str) -> str | None:
    cols = [c.name for c in sqlglot.parse_one(expr).find_all(exp.Column)]
    return cols[0] if cols else None


def _md5(parts: list[str]) -> str:
    """The Data Vault hash: MD5 of the upper-cased, trimmed parts joined with ``||``, NULL as empty."""
    joined = " || '||' || ".join(f"COALESCE(UPPER(TRIM(CAST({p} AS VARCHAR))), '')" for p in parts)
    return f"MD5({joined})"


def model_pipeline(model: Model, model_dir: str | Path = ".", spec_dir: str | Path | None = None,
                   name: str | None = None) -> Pipeline:
    """The pipeline that loads ``model``. Paths are written relative to ``spec_dir`` (default: ``model_dir``)."""
    issues = validate_model(model)
    if issues:
        raise SpecError(issues)
    model_dir = Path(model_dir)
    spec_dir = Path(spec_dir) if spec_dir is not None else model_dir
    if model.style == "data_vault":
        nodes = _vault_nodes(model, model_dir, spec_dir)
    else:
        nodes = _kimball_nodes(model, model_dir, spec_dir)
    data: dict[str, Any] = {
        "version": 0,
        "name": name or f"load_{model.name}",
        "description": f"Loads the {model.name} model. Generated by Redcliff from the model; edit the model, not this file.",
        "variables": {"load_ts": "CURRENT_TIMESTAMP"},
        "nodes": nodes,
    }
    pipeline = Pipeline.from_dict(data)
    from redcliff.spec import validate

    problems = validate(pipeline)
    if problems:  # a generator bug, not a user error: say so plainly
        raise GenerateError("the generated pipeline has problems:\n" + "\n".join(f"- {p}" for p in problems))
    return pipeline


def _key_expect(keys: list[str]) -> list[dict]:
    out: list[dict] = [{"not_null": k} for k in keys]
    out.append({"unique": keys[0] if len(keys) == 1 else keys})
    return out


def _staging(e: Entity, skip: set[str], extra: dict[str, str] | None = None) -> dict:
    cols = [f"{_cast(_expr(a), a.type)} AS {a.name}" for a in e.attributes if a.name not in skip]
    cols += [f"{expr} AS {alias}" for alias, expr in (extra or {}).items()]
    node = {"id": f"stg_{e.id}", "type": "sql", "inputs": [f"src_{e.id}"],
            "params": {"query": f"SELECT {', '.join(cols)} FROM src_{e.id}"}}
    bks = _business_keys(e)
    if bks:
        node["expect"] = _key_expect(bks)
    return node


# --------------------------------------------------------------------------- Kimball


def _kimball_nodes(model: Model, model_dir: Path, spec_dir: Path) -> list[dict]:
    nodes: list[dict] = []
    rels = model.relationships
    dims = [e for e in model.entities if e.kind == "dimension"]
    others = [e for e in model.entities if e.kind != "dimension"]
    for e in dims:
        sk = _generated_sk(e)
        bks = _business_keys(e)
        if e.source:
            nodes.append(_source_node(e, model_dir, spec_dir))
            nodes.append(_staging(e, {sk} if sk else set()))
        else:
            nodes += _date_dimension(model, e, model_dir, spec_dir)
        params: dict[str, Any] = {"format": "table", "table": e.id, "mode": "scd2" if e.scd2 else "scd1", "keys": bks}
        sk_any = next((a.name for a in e.attributes if a.role == "surrogate_key"), None)
        if sk_any:
            params["surrogate_key"] = sk_any
            params["unknown_member"] = True
        if e.scd2:
            scd1 = [a.name for a in e.attributes if a.history == "scd1"]
            ignore = [a.name for a in e.attributes if a.track is False]
            if scd1:
                params["scd1"] = scd1
            if ignore:
                params["ignore"] = ignore
            params["as_of"] = "${load_ts}"
        nodes.append({"id": e.id, "type": "target", "inputs": [f"stg_{e.id}"], "params": params})

    for e in others:
        if not e.source:
            raise GenerateError(f"{e.id} needs a 'source' to load from")
        nodes.append(_source_node(e, model_dir, spec_dir))
        lookups = []  # (attribute, dimension entity, lookup expression)
        for a in e.attributes:
            r = next((r for r in rels if r.source == f"{e.id}.{a.name}"), None)
            if not r:
                continue
            dim = model.entity(r.target.split(".")[0])
            if dim and _generated_sk(dim) == r.target.split(".")[1]:  # a smart key (with a formula) needs no lookup
                lookups.append((a, dim, a.source or _business_keys(dim)[0]))
        skip = {a.name for a, _d, _x in lookups}
        bk_type = {a.name: dim.attribute(_business_keys(dim)[0]).type for a, dim, _x in lookups}
        nodes.append(_staging(e, skip, {f"lookup_{a.name}": _cast(x, bk_type[a.name]) for a, _d, x in lookups}))
        if lookups:
            date_attr = next((a.name for a in e.attributes if a.name not in skip
                              and exp.DataType.build(a.type).is_type(*exp.DataType.TEMPORAL_TYPES)), None)
            select, joins, lookup_inputs = [], [], []
            dim_sk = {a.name: _generated_sk(dim) for a, dim, _x in lookups}
            for a in e.attributes:
                if a.name in skip:
                    select.append(f"COALESCE(d_{a.name}.{dim_sk[a.name]}, -1) AS {a.name}")
                else:
                    select.append(f"f.{a.name}")
            for a, dim, _x in lookups:
                alias = f"d_{a.name}"
                bk = _business_keys(dim)[0]
                on = f"{alias}.{bk} = f.lookup_{a.name}"
                source = f"lookup_{a.name}"
                nodes.append(_table_source(f"lookup_{a.name}", dim))
                if dim.scd2 and date_attr:  # the version valid on the row's date (as it was)
                    # a key's first version counts from the beginning, so older rows find it (dbt snapshots
                    # start first versions when they first run)
                    names = ", ".join([*(x.name for x in dim.attributes), "valid_from", "valid_to"])
                    nodes.append({"id": f"pit_{a.name}", "type": "sql", "inputs": [source], "params": {"query": (
                        f"SELECT {names}, CASE WHEN valid_from = MIN(valid_from) OVER (PARTITION BY {bk}) "
                        f"THEN TIMESTAMP '1900-01-01 00:00:00' ELSE valid_from END AS pit_from FROM {source}")}})
                    source = f"pit_{a.name}"
                    on += (f" AND f.{date_attr} >= {alias}.pit_from"
                           f" AND ({alias}.valid_to IS NULL OR f.{date_attr} < {alias}.valid_to)")
                elif dim.scd2:
                    on += f" AND {alias}.valid_to IS NULL"
                joins.append(f"LEFT JOIN {source} AS {alias} ON {on}")
                lookup_inputs.append(source)
            nodes.append({"id": f"keyed_{e.id}", "type": "sql",
                          "inputs": [f"stg_{e.id}"] + lookup_inputs,
                          "params": {"query": f"SELECT {', '.join(select)} FROM stg_{e.id} AS f " + " ".join(joins)}})
            last = f"keyed_{e.id}"
        else:
            last = f"stg_{e.id}"
        bks = _business_keys(e)
        params = {"format": "table", "table": e.id}
        params.update({"mode": "merge", "keys": bks} if bks else {"mode": "overwrite"})
        nodes.append({"id": e.id, "type": "target", "inputs": [last], "params": params})
    seen: set[str] = set()  # the date dimension and its facts share their source steps
    return [n for n in nodes if not (n["id"] in seen or seen.add(n["id"]))]


def _with_date(expr: str, date_sql: str) -> str:
    """``expr`` with its column ``d`` replaced by ``date_sql``."""
    replacement = sqlglot.parse_one(date_sql)
    tree = sqlglot.parse_one(expr).transform(
        lambda n: replacement.copy() if isinstance(n, exp.Column) and n.name == "d" and not n.table else n)
    return tree.sql()


def _date_dimension(model: Model, e: Entity, model_dir: Path, spec_dir: Path) -> list[dict]:
    """A date dimension built from the dates the facts refer to (it has no source of its own)."""
    bk = next((a for a in e.attributes if a.role == "business_key"), None)
    if bk is None or not exp.DataType.build(bk.type).is_type(*exp.DataType.TEMPORAL_TYPES):
        raise GenerateError(f"{e.id} needs a 'source' (only a date dimension, with a date business key, is built "
                            "from the facts)")
    selects, inputs = [], []
    for r in model.relationships:
        if r.target.split(".")[0] != e.id:
            continue
        fact = model.entity(r.source.split(".")[0])
        attr = fact.attribute(r.source.split(".")[1]) if fact else None
        column = _date_column(attr.source) if attr and attr.source else None
        if not (fact and fact.source and column):
            continue
        if f"src_{fact.id}" not in inputs:
            inputs.append(f"src_{fact.id}")
        selects.append((f"CAST({column} AS DATE)", f"src_{fact.id}", column))
    if not selects:
        raise GenerateError(f"{e.id}: no fact refers to it with a date (e.g. YEAR(order_date) * 10000 + ...)")
    sources = []
    for fid in inputs:
        fact = model.entity(fid[4:])
        sources.append(_source_node(fact, model_dir, spec_dir))
    branches = []
    for d, src, column in selects:
        cols = []
        for a in e.attributes:
            if a.role == "business_key":
                expr = "d"
            elif a.role == "surrogate_key":
                expr = DATE_KEY
            else:
                expr = DATE_PARTS.get(a.name, "NULL")
            cols.append(f"{_cast(_with_date(expr, d), a.type)} AS {a.name}")
        branches.append(f"SELECT DISTINCT {', '.join(cols)} FROM {src} WHERE {column} IS NOT NULL")
    query = " UNION ".join(branches)
    stg = {"id": f"stg_{e.id}", "type": "sql", "inputs": inputs, "params": {"query": query},
           "expect": _key_expect([bk.name])}
    return sources + [stg]


# --------------------------------------------------------------------------- Data Vault


def _vault_nodes(model: Model, model_dir: Path, spec_dir: Path) -> list[dict]:
    """Hubs, links and satellites. Each has a source; hash keys and hashdiffs are computed here.

    A hash key that references a hub (a link's or satellite's) names, as its ``source``, the column of
    this entity's source holding the hub's business key; Redcliff hashes it the hub's way, so the same
    business key gives the same hash everywhere."""
    nodes: list[dict] = []
    order = {"hub": 0, "link": 1, "satellite": 2}
    for e in sorted(model.entities, key=lambda x: order.get(x.kind, 3)):
        if e.kind not in order:
            raise GenerateError(f"{e.id}: a Data Vault model has hubs, links and satellites, not {e.kind}s")
        if not e.source:
            raise GenerateError(f"{e.id} needs a 'source' to load from")
        nodes.append(_source_node(e, model_dir, spec_dir))
        hk = next((a for a in e.attributes if a.role == "surrogate_key"), None)
        if hk is None:
            raise GenerateError(f"{e.id}: mark its hash key (role: surrogate_key)")
        exprs: dict[str, str] = {}
        hub_parts: list[str] = []  # a link's hash covers the business keys of the hubs it joins, in order
        for r in model.relationships:
            if r.source.split(".")[0] != e.id:
                continue
            col = r.source.split(".")[1]
            hub = model.entity(r.target.split(".")[0])
            attr = e.attribute(col)
            if not (attr and hub and hub.kind == "hub"):
                continue
            parts = [attr.source] if attr.source else [b.source or b.name for b in hub.attributes if b.role == "business_key"]
            exprs[col] = _md5(parts)
            hub_parts += parts
        if e.kind == "hub" and not hk.source:
            exprs[hk.name] = _md5([_expr(a) for a in e.attributes if a.role == "business_key"])
        elif e.kind == "link" and hk.name not in exprs and not hk.source:
            if not hub_parts:
                raise GenerateError(f"{e.id}: relate the link's hub hash keys to their hubs")
            exprs[hk.name] = _md5(hub_parts)
        elif e.kind == "satellite" and hk.name not in exprs:
            raise GenerateError(f"{e.id}: relate {hk.name} to its hub's (or link's) hash key")
        meta = ("load_dts", "record_source", "hashdiff")
        descriptive = [a for a in e.attributes if a.role is None and a.name not in meta and a.name not in exprs]
        if e.kind == "satellite" and e.attribute("hashdiff") and not e.attribute("hashdiff").source:
            exprs["hashdiff"] = _md5([_expr(a) for a in descriptive])
        if e.attribute("load_dts") and not e.attribute("load_dts").source:
            exprs["load_dts"] = "${load_ts}"
        if e.attribute("record_source") and not e.attribute("record_source").source:
            exprs["record_source"] = "'" + e.source.replace("'", "''") + "'"
        cols = [f"{_cast(exprs.get(a.name, _expr(a)), a.type)} AS {a.name}" for a in e.attributes]
        stg = {"id": f"stg_{e.id}", "type": "sql", "inputs": [f"src_{e.id}"],
               "params": {"query": f"SELECT DISTINCT {', '.join(cols)} FROM src_{e.id}"},
               "expect": [{"not_null": hk.name}]}
        nodes.append(stg)
        params: dict[str, Any] = {"format": "table", "table": e.id}
        if e.kind in ("hub", "link"):
            params.update({"mode": "insert_new", "keys": [hk.name]})
        else:
            if not (e.attribute("hashdiff") and e.attribute("load_dts")):
                raise GenerateError(f"{e.id}: a satellite needs 'hashdiff' and 'load_dts' attributes")
            params.update({"mode": "insert_changed", "keys": [hk.name], "compare": "hashdiff", "order_by": "load_dts"})
        nodes.append({"id": e.id, "type": "target", "inputs": [f"stg_{e.id}"], "params": params})
    nodes += _marts(model)
    return nodes


def _marts(model: Model) -> list[dict]:
    """Kimball marts over the vault: each hub with its satellites' latest rows, as ``<hub>_current``."""
    nodes: list[dict] = []
    for hub in (e for e in model.entities if e.kind == "hub"):
        hk = next(a.name for a in hub.attributes if a.role == "surrogate_key")
        sats = [model.entity(r.source.split(".")[0]) for r in model.relationships
                if r.target == f"{hub.id}.{hk}" and model.entity(r.source.split(".")[0]).kind == "satellite"]
        if not sats:
            continue
        name = hub.id[4:] if hub.id.startswith("hub_") else hub.id
        bks = [a.name for a in hub.attributes if a.role == "business_key"]
        select = [f"h.{hk}"] + [f"h.{b}" for b in bks]
        joins = []
        inputs = [f"mart_{hub.id}"]
        nodes.append(_table_source(f"mart_{hub.id}", hub))
        for i, sat in enumerate(sats):
            sat_hk = next(r.source.split(".")[1] for r in model.relationships
                          if r.source.split(".")[0] == sat.id and r.target == f"{hub.id}.{hk}")
            cols = [a.name for a in sat.attributes if a.role is None and a.name not in ("load_dts", "record_source", "hashdiff")]
            alias = f"s{i}"
            select += [f"{alias}.{c}" for c in cols if c not in bks]
            nodes.append(_table_source(f"mart_{sat.id}", sat))
            names = ", ".join(a.name for a in sat.attributes)
            nodes.append({"id": f"latest_{sat.id}", "type": "sql", "inputs": [f"mart_{sat.id}"], "params": {"query": (
                f"SELECT {names}, ROW_NUMBER() OVER (PARTITION BY {sat_hk} ORDER BY load_dts DESC) AS rn FROM mart_{sat.id}")}})
            inputs.append(f"latest_{sat.id}")
            joins.append(f"LEFT JOIN latest_{sat.id} AS {alias} ON {alias}.{sat_hk} = h.{hk} AND {alias}.rn = 1")
        nodes.append({"id": f"{name}_current_rows", "type": "sql", "inputs": inputs,
                      "params": {"query": f"SELECT {', '.join(select)} FROM mart_{hub.id} AS h " + " ".join(joins)}})
        nodes.append({"id": f"{name}_current", "type": "target", "inputs": [f"{name}_current_rows"],
                      "params": {"format": "table", "table": f"{name}_current"}})
    return nodes


# --------------------------------------------------------------------------- post-load checks


def model_checks(model: Model) -> list[tuple[str, str, str]]:
    """(entity, check, SQL returning the failing rows) to run against the loaded tables."""
    out = []
    for e in model.entities:
        sk = next((a.name for a in e.attributes if a.role == "surrogate_key"), None)
        bks = _business_keys(e)
        if sk and e.kind != "satellite":
            out.append((e.id, f"{sk} is unique", f"SELECT {sk}, COUNT(*) AS n FROM {e.id} GROUP BY {sk} HAVING COUNT(*) > 1"))
        if e.kind == "dimension" and bks:
            keys = ", ".join(bks)
            if e.scd2:
                out.append((e.id, f"one current row per {keys}",
                            f"SELECT {keys}, COUNT(*) AS n FROM {e.id} WHERE valid_to IS NULL AND {bks[0]} IS NOT NULL "
                            f"GROUP BY {keys} HAVING COUNT(*) > 1"))
                on = " AND ".join(f"a.{k} = b.{k}" for k in bks)
                out.append((e.id, "no overlapping versions",
                            f"SELECT a.{bks[0]}, a.{sk}, b.{sk} AS overlaps FROM {e.id} AS a JOIN {e.id} AS b ON {on} "
                            f"AND a.{sk} < b.{sk} AND a.valid_from < COALESCE(b.valid_to, TIMESTAMP '9999-12-31') "
                            f"AND b.valid_from < COALESCE(a.valid_to, TIMESTAMP '9999-12-31')"))
            else:
                out.append((e.id, f"{keys} is unique", f"SELECT {keys}, COUNT(*) AS n FROM {e.id} "
                                                       f"WHERE {bks[0]} IS NOT NULL GROUP BY {keys} HAVING COUNT(*) > 1"))
        if e.kind == "hub" and bks:
            keys = ", ".join(bks)
            out.append((e.id, f"{keys} is unique",
                        f"SELECT {keys}, COUNT(*) AS n FROM {e.id} GROUP BY {keys} HAVING COUNT(*) > 1"))
    for r in model.relationships:
        src_e, src_c = r.source.split(".")
        tgt_e, tgt_c = r.target.split(".")
        out.append((src_e, f"every {src_c} is in {tgt_e}",
                    f"SELECT f.{src_c} FROM {src_e} AS f LEFT JOIN {tgt_e} AS d ON f.{src_c} = d.{tgt_c} "
                    f"WHERE f.{src_c} IS NOT NULL AND d.{tgt_c} IS NULL"))
    return out


def run_model_checks(con, model: Model, sample: int = 5) -> list[dict]:
    """Run :func:`model_checks`; each result says whether it passed, with a few failing rows."""
    results = []
    for entity, name, sql in model_checks(model):
        try:
            rel = con.sql(sql)
            rows = rel.fetchmany(sample)
            failing = con.sql(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0] if rows else 0
            results.append({"entity": entity, "check": name, "passed": not rows, "failing": failing,
                            "sample": [list(r) for r in rows], "error": None})
        except Exception as err:  # noqa: BLE001 - a missing table is a failed check, not a crash
            results.append({"entity": entity, "check": name, "passed": False, "failing": None, "sample": [],
                            "error": str(err).splitlines()[0]})
    return results
