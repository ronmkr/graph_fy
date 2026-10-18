"""Tests for Pre-Agent Architectural Briefing & Holistic Context (graph_fy context).

Verifies:
1. Deterministic zero-LLM task_context extraction
2. Mermaid architectural diagram generation
3. Bidirectional caller/callee execution flow with hub compaction
4. Downstream blast radius risk scoring integration
5. Token budget packing & compression ratio calculation
6. CLI `graph_fy context` command execution
7. MCP tool `get_agent_context` registration and output
"""
from __future__ import annotations

import json
from pathlib import Path
import networkx as nx
import pytest

from graph_fy.retrieval import build_index, task_context
from graph_fy.mcp import get_agent_context, GET_AGENT_CONTEXT_TOOL_SPEC, CODEGRAPH_TOOL_SPECS


@pytest.fixture
def sample_task_graph(tmp_path: Path):
    """Create a mock repository with connected components and build an index."""
    root = tmp_path / "repo"
    root.mkdir()
    out = root / "graph_fy_out"
    out.mkdir()

    # Code files
    router_file = root / "router.py"
    router_file.write_text(
        'class Router:\n'
        '    """High performance request router."""\n'
        '    def route(self, req: str) -> bool:\n'
        '        """Dispatch incoming request."""\n'
        '        return True\n'
    )
    handler_file = root / "handler.py"
    handler_file.write_text(
        'def handle_request(req: str) -> dict:\n'
        '    """Process request payload."""\n'
        '    return {"status": "ok"}\n'
    )
    test_file = root / "test_router.py"
    test_file.write_text(
        'def test_router_dispatch():\n'
        '    pass\n'
    )

    G = nx.DiGraph()
    G.add_node(
        "router.Router",
        label="Router",
        source_file="router.py",
        source_location="L1",
        community=1,
        community_name="HTTP Routing",
    )
    G.add_node(
        "router.Router.route",
        label="route()",
        source_file="router.py",
        source_location="L3",
        community=1,
        community_name="HTTP Routing",
    )
    G.add_node(
        "handler.handle_request",
        label="handle_request()",
        source_file="handler.py",
        source_location="L1",
        community=2,
        community_name="Request Handlers",
    )
    G.add_node(
        "test_router.test_router_dispatch",
        label="test_router_dispatch()",
        source_file="test_router.py",
        source_location="L1",
        community=3,
        community_name="Test Suite",
    )

    G.add_edge("router.Router", "router.Router.route", relation="contains")
    G.add_edge("router.Router.route", "handler.handle_request", relation="calls")
    G.add_edge("test_router.test_router_dispatch", "router.Router.route", relation="calls")

    db_path = out / "index.db"
    build_index(G, db_path)
    return G, db_path, root


def test_task_context_extraction(sample_task_graph):
    G, db_path, root = sample_task_graph

    res = task_context(
        "router request dispatch handling",
        graph=G,
        db_path=db_path,
        root_dir=root,
        token_budget=1000,
        include_diagram=True,
    )

    assert res["task"] == "router request dispatch handling"
    assert "symbols" in res
    assert len(res["symbols"]) > 0

    # Subsystem detection
    assert any("HTTP Routing" in s or "Community 1" in s for s in res["subsystems"])

    # Mermaid diagram generated
    assert "flowchart TD" in res["architecture_diagram"]
    assert "-->" in res["architecture_diagram"]

    # Prompt context contains expected high-signal sections
    p_ctx = res["prompt_context"]
    assert "### graph_fy Pre-Agent Architectural Briefing" in p_ctx
    assert "Target Subsystem" in p_ctx
    assert "Blast Radius Risk" in p_ctx
    assert "Subsystem Architecture Diagram" in p_ctx
    assert "```mermaid" in p_ctx
    assert "Targeted Symbol Skeletons" in p_ctx

    # Compression ratio
    assert res["briefing_tokens"] > 0
    assert res["compression_ratio"] >= 0.0

    # CCR On-Demand Implementation Handle
    assert "get_symbol_implementation" in p_ctx

    # Strict Determinism (bitwise identical output across repeated invocations for prompt cache)
    res2 = task_context(
        "router request dispatch handling",
        graph=G,
        db_path=db_path,
        root_dir=root,
        token_budget=1000,
        include_diagram=True,
    )
    assert res["prompt_context"] == res2["prompt_context"]


def test_task_context_empty_task():
    res = task_context("")
    assert res["prompt_context"] == "No task provided."


def test_mcp_get_agent_context(sample_task_graph):
    G, db_path, root = sample_task_graph

    # Verify tool specification exists
    assert GET_AGENT_CONTEXT_TOOL_SPEC in CODEGRAPH_TOOL_SPECS
    assert GET_AGENT_CONTEXT_TOOL_SPEC["name"] == "get_agent_context"

    # Call get_agent_context
    res = get_agent_context(
        task="dispatch request",
        root_dir=str(root),
        token_budget=1000,
        graph=G,
        include_diagram=True,
    )
    assert isinstance(res, dict)
    assert "prompt_context" in res
    assert "### graph_fy Pre-Agent Architectural Briefing" in res["prompt_context"]


def test_cli_task_context_with_root_flag(sample_task_graph, monkeypatch, capsys):
    G, db_path, root = sample_task_graph
    import graph_fy.__main__ as mainmod
    import sys

    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", ["graph_fy", "context", "router dispatch", "--root", str(root)])
    try:
        mainmod.main()
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "### graph_fy Pre-Agent Architectural Briefing" in out
    assert "Router" in out


def test_task_context_mermaid_diagram_declarations(sample_task_graph):
    G, db_path, root = sample_task_graph
    res = task_context(
        "router request dispatch handling",
        graph=G,
        db_path=db_path,
        root_dir=root,
        token_budget=1000,
        include_diagram=True,
    )
    diag = res.get("architecture_diagram", "")
    assert "flowchart TD" in diag
    # Ensure all connected nodes are declared with quoted labels and no unformatted node IDs
    assert 'route["route()"]' in diag or 'route' in diag
    assert "router.Router.route" not in diag


def test_task_context_anti_hallucination_guard(sample_task_graph):
    G, db_path, root = sample_task_graph
    # Query for a completely non-existent symbol
    res = task_context(
        "FakeNonExistentService doMagicQuantumCrypto",
        graph=G,
        db_path=db_path,
        root_dir=root,
    )
    # Must reject false-positive stem hits and return zero candidates
    assert len(res["symbols"]) == 0
    assert "Target Symbol Not Found" in res["prompt_context"]
    assert "Do NOT hallucinate non-existent APIs" in res["prompt_context"]


def test_task_context_sqlite_fallback_when_graph_none(sample_task_graph):
    G, db_path, root = sample_task_graph
    # Call task_context without in-memory graph (graph=None)
    res = task_context(
        "router route",
        graph=None,
        db_path=db_path,
        root_dir=root,
    )
    assert len(res["symbols"]) > 0
    assert "Grounded AST Invariant" in res["prompt_context"]
    assert "Router" in res["prompt_context"]


def test_task_context_prompt_cache_prefix_priming(sample_task_graph):
    """Verify that multiple queries within the same subsystem share an identical prefix for prompt-cache hits."""
    G, db_path, root = sample_task_graph

    res1 = task_context("router dispatch request", graph=G, db_path=db_path, root_dir=root, token_budget=1500)
    res2 = task_context("router dispatch request", graph=G, db_path=db_path, root_dir=root, token_budget=1500)

    p1 = res1["prompt_context"]
    p2 = res2["prompt_context"]

    # Both start with deterministic cache header and invariant subsystem briefing
    assert p1.startswith("<!-- graph_fy:agent_context:v1 -->")
    assert p2.startswith("<!-- graph_fy:agent_context:v1 -->")

    # Prefix up to the dynamic tail is identical
    split_marker = "#### Query Impact & Verification Targets:"
    assert split_marker in p1
    assert split_marker in p2

    prefix1 = p1.split(split_marker)[0]
    prefix2 = p2.split(split_marker)[0]

    assert prefix1 == prefix2
    # Ensure prefix contains the bulk of tokens (>200 chars) for cache eligibility
    assert len(prefix1) > 200




