"""Lineage tests: each compares against a column map worked out by hand."""

from pathlib import Path

import pytest

from redcliff import Pipeline, load_spec
from redcliff.lineage import (
    Ref,
    analyze_expression,
    blast_radius,
    build_graph,
    column_issues,
    dataset_dependencies,
    lineage_report,
    openlineage_facet,
    trace_to_sources,
)

SPECS = Path(__file__).parents[2] / "examples" / "specs"


def sources(graph, node, col):
    return {str(k): v for k, v in trace_to_sources(graph, Ref(node, col)).items()}


@pytest.fixture(scope="module")
def revenue():
    return build_graph(load_spec(SPECS / "orders_revenue.yaml"))


@pytest.fixture(scope="module")
def cleanup():
    return build_graph(load_spec(SPECS / "customer_cleanup.yaml"))


@pytest.fixture(scope="module")
def events():
    return build_graph(load_spec(SPECS / "daily_events.yaml"))


# ---------------------------------------------------------------- expressions


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("amount", [("amount", "DIRECT", "IDENTITY")]),
        ("amount * 1.1", [("amount", "DIRECT", "TRANSFORMATION")]),
        ("SUM(amount)", [("amount", "DIRECT", "AGGREGATION")]),
        ("COUNT(*)", []),
        ("'web'", []),
        (
            "CASE WHEN status = 'x' THEN SUM(amount) ELSE 0 END",
            [("status", "INDIRECT", "CONDITIONAL"), ("amount", "DIRECT", "AGGREGATION")],
        ),
        (
            "SUM(CASE WHEN status = 'x' THEN amount END)",
            [("status", "INDIRECT", "CONDITIONAL"), ("amount", "DIRECT", "AGGREGATION")],
        ),
        (
            "ROW_NUMBER() OVER (PARTITION BY region ORDER BY amount DESC)",
            [("region", "INDIRECT", "WINDOW"), ("amount", "INDIRECT", "WINDOW")],
        ),
    ],
)
def test_analyze_expression(sql, expected):
    got = [(u.column, u.type, u.subtype) for u in analyze_expression(sql)]
    assert sorted(got) == sorted(expected)


# ---------------------------------------------------------------- expected column maps


def test_revenue_column_map(revenue):
    assert revenue.issues == []
    assert sources(revenue, "out", "region") == {"customers.region": ("DIRECT", "IDENTITY")}
    assert sources(revenue, "out", "revenue") == {"orders.amount": ("DIRECT", "AGGREGATION")}
    assert sources(revenue, "out", "order_count") == {}


def test_revenue_row_dependencies(revenue):
    deps = {str(k): v for k, v in dataset_dependencies(revenue, "out").items()}
    assert deps == {
        "orders.status": {"FILTER"},
        "orders.customer_id": {"JOIN"},
        "customers.id": {"JOIN"},
        "customers.region": {"GROUP_BY"},
    }


def test_cleanup_column_map(cleanup):
    assert sources(cleanup, "out", "id") == {"customers.id": ("DIRECT", "IDENTITY")}
    assert sources(cleanup, "out", "email") == {"customers.email": ("DIRECT", "TRANSFORMATION")}
    assert sources(cleanup, "out", "full_name") == {
        "customers.first_name": ("DIRECT", "TRANSFORMATION"),
        "customers.last_name": ("DIRECT", "TRANSFORMATION"),
    }
    # The filter reads the cleaned email, which traces back to the source email.
    assert {str(k): v for k, v in dataset_dependencies(cleanup, "out").items()} == {
        "customers.email": {"FILTER"}
    }


def test_events_column_map_through_union(events):
    assert sources(events, "out", "users") == {
        "web_events.user_id": ("DIRECT", "AGGREGATION"),
        "app_events.user_id": ("DIRECT", "AGGREGATION"),
    }
    assert sources(events, "out", "event_day") == {
        "web_events.event_time": ("DIRECT", "TRANSFORMATION"),
        "app_events.event_time": ("DIRECT", "TRANSFORMATION"),
    }
    assert sources(events, "out", "channel") == {}  # a literal per branch


# ---------------------------------------------------------------- blast radius


def test_blast_radius_of_a_filter_column(revenue):
    impact = blast_radius(revenue, Ref("orders", "status")).to_dict()
    assert impact == {
        "column": "orders.status",
        "values": ["orders_with_region.status", "shipped.status"],
        "rows": ["orders_with_region", "out", "revenue_by_region", "shipped"],
        "breaks": ["shipped"],
    }


def test_blast_radius_of_a_group_by_column(revenue):
    impact = blast_radius(revenue, Ref("customers", "region")).to_dict()
    assert impact["values"] == ["orders_with_region.region", "out.region", "revenue_by_region.region"]
    assert impact["rows"] == ["out", "revenue_by_region"]
    assert impact["breaks"] == ["revenue_by_region"]


def test_blast_radius_of_a_join_key_follows_the_renamed_copy(revenue):
    impact = blast_radius(revenue, Ref("orders", "customer_id"))
    # The join names shipped.customer_id, a pass-through copy of orders.customer_id.
    assert impact.breaks == {"orders_with_region"}
    assert "revenue_by_region" in impact.rows


def test_blast_radius_of_a_column_used_twice(cleanup):
    impact = blast_radius(cleanup, Ref("customers", "email"))
    assert impact.breaks == {"cleaned"}  # renaming the source breaks the derive that reads it
    assert Ref("out", "email") in impact.values
    assert impact.rows == {"reachable", "contact_list", "out"}


def test_unused_column_has_no_impact(cleanup):
    impact = blast_radius(cleanup, Ref("customers", "signup_date"))
    assert impact.breaks == set() and impact.rows == set()
    assert Ref("contact_list", "signup_date") not in impact.values


# ---------------------------------------------------------------- column checks


def tiny(*nodes, columns=True):
    src = {"format": "csv", "path": "a.csv"}
    if columns:
        src["columns"] = {"id": "int", "amount": "double", "status": "text"}
    other = {"format": "csv", "path": "b.csv", "columns": {"id": "int", "label": "text"}}
    return Pipeline.from_dict({"version": 0, "name": "t", "nodes": [
        {"id": "a", "type": "source", "params": src},
        {"id": "b", "type": "source", "params": other},
        *nodes,
    ]})


@pytest.mark.parametrize(
    "node, expected",
    [
        ({"id": "f", "type": "filter", "inputs": ["a"], "params": {"condition": "stats = 'x'"}},
         "unknown column 'stats'; available: amount, id, status"),
        ({"id": "s", "type": "select", "inputs": ["a"], "params": {"columns": ["id", "nope"]}},
         "unknown column 'nope'"),
        ({"id": "g", "type": "aggregate", "inputs": ["a"],
          "params": {"group_by": ["region"], "measures": {"n": "COUNT(*)"}}},
         "unknown column 'region'"),
        ({"id": "j", "type": "join", "inputs": ["a", "b"], "params": {"condition": "id = id"}},
         "column 'id' is ambiguous"),
        ({"id": "j", "type": "join", "inputs": ["a", "b"], "params": {"condition": "a.id = c.id"}},
         "'c' is not an input"),
        ({"id": "u", "type": "union", "inputs": ["a", "b"]}, "has 2 columns but 'a' has 3"),
    ],
)
def test_column_issues(node, expected):
    msgs = [str(i) for i in column_issues(tiny(node))]
    assert any(expected in m for m in msgs), msgs


def test_examples_have_no_column_issues():
    for path in SPECS.glob("*.yaml"):
        assert column_issues(load_spec(path)) == [], path.name


def test_undeclared_source_is_reported_once():
    p = tiny({"id": "f", "type": "filter", "inputs": ["a"], "params": {"condition": "x > 1"}},
             {"id": "f2", "type": "filter", "inputs": ["f"], "params": {"condition": "x > 2"}}, columns=False)
    msgs = [str(i) for i in column_issues(p)]
    assert msgs == ["a: declare 'columns' on this source to get lineage through it"]


def test_derive_case_and_window_lineage():
    p = tiny({"id": "d", "type": "derive", "inputs": ["a"], "params": {"columns": {
        "big_amount": "CASE WHEN status = 'paid' THEN amount ELSE 0 END",
        "rank_in_status": "ROW_NUMBER() OVER (PARTITION BY status ORDER BY amount DESC)",
    }}})
    g = build_graph(p)
    assert sources(g, "d", "big_amount") == {
        "a.status": ("INDIRECT", "CONDITIONAL"),
        "a.amount": ("DIRECT", "TRANSFORMATION"),
    }
    assert sources(g, "d", "rank_in_status") == {
        "a.status": ("INDIRECT", "WINDOW"),
        "a.amount": ("INDIRECT", "WINDOW"),
    }


# ---------------------------------------------------------------- export


def test_openlineage_facet(revenue):
    facet = openlineage_facet(revenue, "out")
    assert facet["_schemaURL"].endswith("1-2-0/ColumnLineageDatasetFacet.json")
    assert set(facet["fields"]) == {"region", "revenue", "order_count"}
    revenue_inputs = facet["fields"]["revenue"]["inputFields"]
    assert revenue_inputs == [{
        "namespace": "redcliff",
        "name": "../data/orders.csv",
        "field": "amount",
        "transformations": [{"type": "DIRECT", "subtype": "AGGREGATION", "description": "", "masking": False}],
    }]
    reasons = {(d["field"], d["transformations"][0]["subtype"]) for d in facet["dataset"]}
    assert reasons == {("status", "FILTER"), ("customer_id", "JOIN"), ("id", "JOIN"), ("region", "GROUP_BY")}


def test_lineage_report_is_json_ready(revenue):
    import json

    report = lineage_report(revenue.pipeline)
    json.dumps(report)
    assert report["issues"] == []
    assert {"from": "orders.amount", "to": "shipped.amount", "type": "DIRECT", "subtype": "IDENTITY"} in report["edges"]
