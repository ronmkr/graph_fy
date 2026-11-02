"""Tests for diff_context and --github-pr formatting."""
from __future__ import annotations

from pathlib import Path
import networkx as nx
import pytest

from graph_fy.retrieval import diff_context


def test_diff_context_github_pr_format(tmp_path: Path):
    G = nx.Graph()
    G.add_node("app::route", label="register_route", source_file="app.py", source_location="L10")
    G.add_node("app::handler", label="handle_request", source_file="app.py", source_location="L30")
    G.add_node("tests::test_app", label="test_route", source_file="tests/test_app.py", source_location="L5")
    G.add_edge("app::handler", "app::route", relation="calls")
    G.add_edge("tests::test_app", "app::route", relation="tests")

    res = diff_context(graph=G, root_dir=tmp_path, format="github-pr")
    assert "prompt_context" in res
    ctx = res["prompt_context"]
    assert "## 🔍 graph_fy AST Blast Radius Review" in ctx
    assert "| **Risk Tier** |" in ctx
    assert "| **Modified Files** |" in ctx
    assert "| **Directly Touched Symbols** |" in ctx
    assert "| **Impacted Callers (1-Hop)** |" in ctx
    assert "| **Affected Test Targets** |" in ctx


def test_diff_context_markdown_format(tmp_path: Path):
    """Verify Issue #104 consolidation: --format markdown produces structured review table."""
    G = nx.Graph()
    G.add_node("svc::auth", label="AuthService", source_file="svc.py", source_location="L10")
    res = diff_context(graph=G, root_dir=tmp_path, format="markdown")
    assert "prompt_context" in res
    assert "## 🔍 graph_fy AST Blast Radius Review" in res["prompt_context"]


def test_cmd_diff_fail_on_breaking(tmp_path: Path, monkeypatch):
    """Verify Issue #104 consolidation: --fail-on-breaking returns exit code 2 when high risk detected."""
    from graph_fy.commands.diff import cmd_diff

    # Case 1: LOW risk -> returns 0
    monkeypatch.setattr(
        "graph_fy.retrieval.diff_context",
        lambda **kwargs: {"risk_tier": "LOW", "risk_score": 0.1, "prompt_context": "ok"},
    )
    rc_low = cmd_diff(["graph_fy", "diff", "--fail-on-breaking"])
    assert rc_low == 0

    # Case 2: HIGH / CRITICAL risk -> returns 2
    monkeypatch.setattr(
        "graph_fy.retrieval.diff_context",
        lambda **kwargs: {"risk_tier": "CRITICAL", "risk_score": 0.85, "prompt_context": "breaking"},
    )
    rc_high = cmd_diff(["graph_fy", "diff", "--fail-on-breaking"])
    assert rc_high == 2

