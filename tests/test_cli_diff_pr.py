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
