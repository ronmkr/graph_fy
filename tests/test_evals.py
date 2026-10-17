"""Tests for graph_fy evaluation framework."""
from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from graph_fy.evals import load_scenarios, run_evals


def test_evals_anti_hallucination_and_report():
    G = nx.Graph()
    G.add_node("req_dispatch", label="dispatch_request", source_file="app.py", source_location="L10")
    G.add_node("cookie_session", label="SecureCookieSession", source_file="sessions.py", source_location="L20")
    G.add_edge("req_dispatch", "cookie_session", relation="uses")

    report = run_evals(graph=G)
    assert report.total_cases >= 5
    assert "Anti-Hallucination" in report.categories
    assert report.categories["Anti-Hallucination"] == 100.0
    md = report.to_markdown()
    assert "# graph_fy Skill Evaluation Scorecard" in md
    assert "Anti-Hallucination" in md


def test_load_real_world_scenarios():
    scenarios_file = Path("evals/real_world_scenarios.json")
    assert scenarios_file.exists(), "evals/real_world_scenarios.json should exist"
    scenarios = load_scenarios(scenarios_file)
    assert len(scenarios) >= 30
    for sc in scenarios:
        assert "id" in sc
        assert "title" in sc
        assert "category" in sc
        assert "user_prompt" in sc
        assert "verification" in sc


def test_run_evals_with_scenarios_and_filtering():
    G = nx.Graph()
    G.add_node("session_node", label="session", source_file="flask/sessions.py", source_location="L10")
    G.add_node("save_session", label="save_session", source_file="flask/sessions.py", source_location="L25")
    G.add_edge("session_node", "save_session", relation="calls")

    scenarios_file = Path("evals/real_world_scenarios.json")
    report = run_evals(
        graph=G,
        scenarios_path=scenarios_file,
        limit=3,
    )
    assert report.total_cases == 3
    assert len(report.results) == 3
    assert report.latency_p50_ms >= 0.0

    # Category filter
    filtered_report = run_evals(
        graph=G,
        scenarios_path=scenarios_file,
        category_filter="Anti-Hallucination",
        limit=2,
    )
    assert filtered_report.total_cases <= 2
    for r in filtered_report.results:
        assert "Anti-Hallucination" in r.category


def test_suggest_close_symbols_and_did_you_mean():
    from graph_fy.retrieval import suggest_close_symbols, task_context
    G = nx.Graph()
    G.add_node("app::request_context", label="request_context", source_file="app.py", source_location="L100")
    G.add_node("app::before_request", label="before_request", source_file="app.py", source_location="L120")
    G.add_node("app::after_request", label="after_request", source_file="app.py", source_location="L140")

    # Direct fuzzy match
    suggestions = suggest_close_symbols(["request_contexxt"], graph=G)
    assert "request_context" in suggestions

    # Integrated task_context Did-You-Mean
    res = task_context("inspect request_contexxt", graph=G)
    assert "did_you_mean" in res
    assert "request_context" in res["did_you_mean"]
    assert "Did you mean: `request_context`?" in res["prompt_context"]


def test_run_comparative_evals():
    from graph_fy.comparative_eval import run_comparative_evals

    G = nx.Graph()
    G.add_node("session_node", label="session", source_file="flask/sessions.py", source_location="L10")
    G.add_node("save_session", label="save_session", source_file="flask/sessions.py", source_location="L25")
    G.add_edge("session_node", "save_session", relation="calls")

    scenarios_file = Path("evals/real_world_scenarios.json")
    comp_report = run_comparative_evals(
        graph=G,
        scenarios_path=scenarios_file,
        limit=4,
    )
    assert comp_report.total_scenarios == 4
    assert len(comp_report.cases) == 4
    assert comp_report.avg_token_compression_pct > 90.0
    assert comp_report.avg_turn_reduction_pct > 70.0
    assert comp_report.avg_latency_speedup_x >= 1.0
    assert comp_report.total_cost_saved_usd >= 0.0

    md = comp_report.to_markdown()
    assert "# graph_fy vs Raw LLM Comparative Evaluation Report" in md
    assert "Executive Summary Scorecard" in md
    assert "Category Performance Breakdown" in md
    assert "Detailed Case Comparisons" in md


