"""Tests for case-only community relabel sweeping and report wikilink slug consistency (Issue #76)."""
from __future__ import annotations

from pathlib import Path
import networkx as nx

from graph_fy.report import _safe_community_name
from graph_fy.export import _obsidian_safe_stem
from graph_fy.wiki import to_wiki


def _graph() -> nx.Graph:
    G = nx.Graph()
    G.add_node("a", label="a", file_type="code")
    G.add_node("b", label="b", file_type="code")
    G.add_edge("a", "b", relation="calls")
    return G


def test_safe_community_name_matches_obsidian_safe_stem():
    labels = [
        "auth.service",
        "report.qmd",
        ".hidden",
        "!!!",
        "A / B : C",
        "Very " * 50,
    ]
    for label in labels:
        assert _safe_community_name(label) == _obsidian_safe_stem(label)


def test_case_only_relabel_keeps_the_new_content_not_the_stale_twin(tmp_path: Path):
    g = _graph()
    to_wiki(g, {0: ["a", "b"]}, tmp_path, community_labels={0: "groups"})
    to_wiki(g, {0: ["a", "b"]}, tmp_path, community_labels={0: "Groups"})

    articles = [p for p in tmp_path.glob("*.md") if p.name != "index.md"]
    assert len(articles) == 1, [p.name for p in articles]
    body = articles[0].read_text(encoding="utf-8")
    assert "# Groups" in body, body


def test_a_genuinely_absent_article_is_still_swept(tmp_path: Path):
    g = _graph()
    to_wiki(g, {0: ["a"], 1: ["b"]}, tmp_path, community_labels={0: "one", 1: "two"})
    # Re-run with only community 0
    to_wiki(g, {0: ["a", "b"]}, tmp_path, community_labels={0: "one"})

    names = {p.name for p in tmp_path.glob("*.md")}
    assert "one.md" in names
    assert "two.md" not in names
