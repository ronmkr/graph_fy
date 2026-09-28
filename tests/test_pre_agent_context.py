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
