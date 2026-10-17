"""Tests for multi-repo, multi-graph resolution, parallel queries, and entity architecture."""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest

from graph_fy.multi_repo import (
    find_workspace_graphs,
    query_parallel,
    resolve_multi_graphs,
    synthesize_entity_architecture,
)


def _make_dummy_graph(nodes: list[dict], edges: list[dict]) -> dict:
    return {
        "nodes": nodes,
        "links": edges,
        "graph": {},
    }


def test_resolve_multi_graphs_composes_two_graphs(tmp_path: Path):
    g1_path = tmp_path / "repo1_graph.json"
    g2_path = tmp_path / "repo2_graph.json"

    g1_data = _make_dummy_graph(
        nodes=[
            {"id": "node_a", "label": "ClientA", "source_file": "repo1/client.py", "community": 0},
            {"id": "node_b", "label": "SharedContract", "source_file": "repo1/types.py", "_callable_class": True, "metadata": {"namespace": "types"}},
        ],
        edges=[{"source": "node_a", "target": "node_b", "relation": "uses"}],
    )
    g2_data = _make_dummy_graph(
        nodes=[
            {"id": "node_c", "label": "ServiceB", "source_file": "repo2/service.py", "community": 0},
            {"id": "node_d", "label": "SharedContract", "source_file": "repo2/types.py", "_callable_class": True, "metadata": {"namespace": "types"}},
        ],
        edges=[{"source": "node_c", "target": "node_d", "relation": "implements"}],
    )

    g1_path.write_text(json.dumps(g1_data), encoding="utf-8")
    g2_path.write_text(json.dumps(g2_data), encoding="utf-8")

    merged = resolve_multi_graphs([g1_path, g2_path])
    assert len(merged.nodes()) == 4
    # Check that communities were offset to avoid collision
    cids = {d.get("community") for _, d in merged.nodes(data=True) if d.get("community") is not None}
    assert len(cids) >= 2


def test_find_workspace_graphs(tmp_path: Path):
    # Create sub-repos with graph_fy_out/graph.json
    repo_a = tmp_path / "service_a" / "graph_fy_out"
    repo_a.mkdir(parents=True)
    (repo_a / "graph.json").write_text("{}", encoding="utf-8")

    repo_b = tmp_path / "service_b" / "graph_fy_out"
    repo_b.mkdir(parents=True)
    (repo_b / "graph.json").write_text("{}", encoding="utf-8")

    graphs = find_workspace_graphs(tmp_path)
    assert len(graphs) == 2
    assert any("service_a" in str(p) for p in graphs)
    assert any("service_b" in str(p) for p in graphs)


def test_synthesize_entity_architecture():
    G = nx.Graph()
    G.add_node("user_model", label="User", source_file="models/user.py", source_location="L10")
    G.add_node("auth_service", label="AuthService", source_file="services/auth.py", source_location="L20")
    G.add_node("db_pool", label="DatabasePool", source_file="db/pool.py", source_location="L5")
    G.add_edge("auth_service", "user_model", relation="uses")
    G.add_edge("auth_service", "db_pool", relation="calls")

    res = synthesize_entity_architecture(["User", "AuthService", "DatabasePool"], G)
    assert len(res["entities"]) == 3
    assert "flowchart TD" in res["mermaid"]
    assert "User" in res["mermaid"]
    assert "AuthService" in res["mermaid"]
    assert res["subgraph_edges_count"] >= 2


def test_query_parallel_execution():
    queries = ["how to authenticate", "how to connect db", "error handling"]
    G = nx.Graph()
    G.add_node("n1", label="Auth")
    res = query_parallel(queries, graph=G, max_workers=3)
    assert res["count"] == 3
    assert len(res["queries"]) == 3
    assert res["concurrency"] == 3
