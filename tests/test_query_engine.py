"""Tests for GraphQueryEngine (unified query, retrieval, and impact kernel)."""
from pathlib import Path
import networkx as nx
import pytest

from graph_fy.query_engine import GraphQueryEngine, get_query_engine


def test_get_query_engine_singleton():
    engine1 = get_query_engine()
    engine2 = get_query_engine()
    assert engine1 is engine2
    assert isinstance(engine1, GraphQueryEngine)


def test_resolve_seed_and_targets():
    G = nx.DiGraph()
    G.add_node("file:app.py", label="app.py", source_file="app.py", source_location="L1")
    G.add_node("func:start_server()", label="start_server()", source_file="app.py", source_location="L10")
    G.add_node("func:stop_server()", label="stop_server()", source_file="app.py", source_location="L25")
    G.add_edge("file:app.py", "func:start_server()", relation="contains")
    G.add_edge("file:app.py", "func:stop_server()", relation="contains")

    engine = get_query_engine()
    seed = engine.resolve_seed(G, "start_server")
    assert seed == "func:start_server()"

    targets = engine.resolve_targets(G, "app.py")
    assert "file:app.py" in targets


def test_callers_and_callees():
    G = nx.DiGraph()
    G.add_node("caller_fn()", label="caller_fn()")
    G.add_node("target_fn()", label="target_fn()")
    G.add_node("callee_fn()", label="callee_fn()")
    G.add_edge("caller_fn()", "target_fn()", relation="calls")
    G.add_edge("target_fn()", "callee_fn()", relation="calls")

    engine = get_query_engine()
    callers, callees = engine.get_callers_and_callees(G, "target_fn()")
    assert callers == ["caller_fn()"]
    assert callees == ["callee_fn()"]


def test_find_affected_and_compute_impact():
    G = nx.DiGraph()
    G.add_node("lib:db", label="db", file_type="code", source_file="db.py")
    G.add_node("service:user", label="user", file_type="code", source_file="user.py")
    G.add_node("test:test_user", label="test_user", file_type="code", source_file="tests/test_user.py")
    G.add_edge("service:user", "lib:db", relation="calls")
    G.add_edge("test:test_user", "service:user", relation="calls")

    engine = get_query_engine()
    affected = engine.find_affected(G, "lib:db", depth=2)
    affected_ids = {h.node_id for h in affected}
    assert "service:user" in affected_ids
    assert "test:test_user" in affected_ids

    impact = engine.compute_impact(G, "db")
    assert impact["downstream_count"] >= 2
    assert any(t.get("id") == "test:test_user" for t in impact["affected_tests"])

    report_terse = engine.format_impact_report(impact, terse=True)
    assert "Impact for db:" in report_terse

    report_full = engine.format_impact_report(impact, terse=False)
    assert "PREDICTIVE IMPACT ANALYSIS" in report_full
    assert "Risk Tier:" in report_full


def test_skeletonize_file_and_code(tmp_path: Path):
    py_file = tmp_path / "sample.py"
    py_file.write_text(
        "def compute(x: int) -> int:\n"
        '    """Compute square of integer."""\n'
        "    y = x * x\n"
        "    return y\n",
        encoding="utf-8",
    )

    engine = get_query_engine()
    skeleton = engine.skeletonize_file(py_file)
    assert "def compute(x: int) -> int:" in skeleton
    assert '"""Compute square of integer."""' in skeleton
    assert "y = x * x" not in skeleton

    snippet = engine.get_symbol_code(py_file, line_number=1, skeletonize=False)
    assert "def compute" in snippet
    assert "y = x * x" in snippet
