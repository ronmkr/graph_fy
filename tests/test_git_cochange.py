"""Unit and integration tests for Git Co-Change & Temporal Churn Engine."""
from __future__ import annotations

from pathlib import Path
import networkx as nx
import pytest

from graph_fy.git_history import (
    GitCommit,
    compute_cochanges,
    compute_file_churn,
    inject_git_temporal_edges,
    parse_numstat_log,
)
from graph_fy.impact import compute_impact, format_impact_report
from graph_fy.retrieval_context import task_context


SAMPLE_NUMSTAT_LOG = """
COMMIT:c1a1|Alice|1700000000|feat: add auth service and user model
10\t2\tsrc/auth.py
25\t0\tsrc/user.py
5\t1\tsrc/config.py

COMMIT:c2b2|Bob|1700050000|fix: update auth tokens and user session
15\t5\tsrc/auth.py
20\t2\tsrc/user.py
0\t10\tdocs/auth.md

COMMIT:c3c3|Alice|1700100000|refactor: improve user password hashing and auth check
8\t1\tsrc/auth.py
12\t4\tsrc/user.py

COMMIT:c4d4|Charlie|1700150000|chore: reformat whole repo
1\t1\tsrc/auth.py
1\t1\tsrc/user.py
1\t1\tsrc/config.py
1\t1\tsrc/db.py
1\t1\tsrc/api.py
"""


def test_parse_numstat_log():
    # With max_files_per_commit=4, c4d4 (5 files) will be filtered out as bulk commit
    commits = parse_numstat_log(SAMPLE_NUMSTAT_LOG, max_files_per_commit=4)
    assert len(commits) == 3
    c1 = commits[0]
    assert c1.commit_hash == "c1a1"
    assert c1.author == "Alice"
    assert c1.timestamp == 1700000000
    assert "src/auth.py" in c1.files
    assert "src/user.py" in c1.files
    assert c1.insertions["src/auth.py"] == 10
    assert c1.deletions["src/auth.py"] == 2


def test_compute_file_churn():
    commits = parse_numstat_log(SAMPLE_NUMSTAT_LOG, max_files_per_commit=10)
    # Reference timestamp right after c4d4
    ref_ts = 1700200000
    churn_map = compute_file_churn(commits, days=30, reference_timestamp=ref_ts)

    assert "src/auth.py" in churn_map
    auth_churn = churn_map["src/auth.py"]
    assert auth_churn.commit_count == 4
    # All 4 commits happened within last 30 days
    assert auth_churn.churn_30d == (10 + 2) + (15 + 5) + (8 + 1) + (1 + 1)
    assert auth_churn.primary_authors[0] == "Alice"
    assert "Bob" in auth_churn.primary_authors


def test_compute_cochanges():
    commits = parse_numstat_log(SAMPLE_NUMSTAT_LOG, max_files_per_commit=4)
    # 3 valid commits: all 3 modified both src/auth.py and src/user.py
    cochanges = compute_cochanges(commits, min_co_commits=3, min_confidence=0.5)
    assert len(cochanges) == 1
    pair = cochanges[0]
    assert {pair.file_a, pair.file_b} == {"src/auth.py", "src/user.py"}
    assert pair.co_commits == 3
    assert pair.confidence_a_b == 1.0
    assert pair.confidence_b_a == 1.0
    assert pair.jaccard == 1.0
    assert pair.lift >= 1.0


def test_inject_git_temporal_edges_mock(monkeypatch, tmp_path: Path):
    all_nodes = [
        {"id": "file:src/auth.py", "label": "auth.py", "file_type": "file", "source_file": "src/auth.py"},
        {"id": "file:src/user.py", "label": "user.py", "file_type": "file", "source_file": "src/user.py"},
    ]
    all_edges: list[dict] = []

    # Mock extract_git_history to return parsed commits
    parsed = parse_numstat_log(SAMPLE_NUMSTAT_LOG, max_files_per_commit=4)
    monkeypatch.setattr("graph_fy.git_history.is_git_repo", lambda _: True)
    monkeypatch.setattr("graph_fy.git_history.extract_git_history", lambda _: parsed)

    injected = inject_git_temporal_edges(all_nodes, all_edges, tmp_path, min_co_commits=3, min_confidence=0.5)
    assert injected == 1
    assert len(all_edges) == 1
    edge = all_edges[0]
    assert edge["relation"] == "co_changes_with"
    assert edge["confidence"] == "INFERRED"
    assert edge["context"] == "git_cochange"
    assert edge["metadata"]["co_commits"] == 3

    # Verify node churn annotations
    auth_node = next(n for n in all_nodes if n["id"] == "file:src/auth.py")
    assert auth_node["commit_count"] == 3
    assert auth_node["primary_authors"] == ["Alice", "Bob"]


def test_impact_and_retrieval_with_hotspots(tmp_path: Path):
    G = nx.DiGraph()
    G.add_node(
        "file:src/auth.py",
        label="auth.py",
        file_type="file",
        source_file="src/auth.py",
        commit_count=25,
        churn_30d=140,
        primary_authors=["Alice", "Bob"],
    )
    G.add_node(
        "func:auth_token()",
        label="auth_token()",
        file_type="function",
        source_file="src/auth.py",
        source_location="L10",
    )
    G.add_edge("file:src/auth.py", "func:auth_token()", relation="contains")

    G.add_node(
        "file:src/user.py",
        label="user.py",
        file_type="file",
        source_file="src/user.py",
        commit_count=18,
        churn_30d=95,
        primary_authors=["Alice"],
    )
    G.add_edge("file:src/auth.py", "file:src/user.py", relation="co_changes_with", weight=0.85)

    # 1. Verify impact analysis reports hotspots
    impact = compute_impact(G, "auth.py")
    assert "hotspots" in impact
    assert len(impact["hotspots"]) >= 1
    assert impact["hotspots"][0]["id"] == "file:src/auth.py"

    report = format_impact_report(impact, terse=False)
    assert "High-Churn Hotspots" in report
    assert "140 lines churn/30d" in report

    # 2. Verify task_context incorporates precomputed co-change edges
    ctx = task_context("auth_token", graph=G, db_path=tmp_path / "index.db", root_dir=tmp_path)
    assert "historically_co_changed" in ctx
    # user.py should be discovered as historically co-changed with auth.py
    co_files = [f for f, _ in ctx["historically_co_changed"]]
    assert "src/user.py" in co_files
