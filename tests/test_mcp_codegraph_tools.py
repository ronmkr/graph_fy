"""Comprehensive test suite for fine-grained CodeGraph MCP tools and token savings."""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest

from graph_fy.mcp import (
    CODEGRAPH_TOOL_SPECS,
    estimate_token_savings,
    find_definitions,
    find_implementations,
    find_references,
    get_callees,
    get_callers,
)


@pytest.fixture
def sample_code_tree(tmp_path: Path):
    """Create a multi-file Python codebase for testing."""
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()

    base_file = pkg_dir / "base.py"
    base_file.write_text(
        '"""Base abstractions."""\n'
        "\n"
        "class IRepository:\n"
        '    """Abstract repository interface."""\n'
        "    def get(self, item_id: str):\n"
        "        raise NotImplementedError\n",
        encoding="utf-8",
    )

    impl_file = pkg_dir / "impl.py"
    impl_file.write_text(
        "from mypkg.base import IRepository\n"
        "\n"
        "class SqlRepository(IRepository):\n"
        '    """SQL-backed implementation."""\n'
        "    def get(self, item_id: str):\n"
        "        return f'item-{item_id}'\n"
        "\n"
        "class CachedSqlRepository(SqlRepository):\n"
        '    """Cached SQL implementation."""\n'
        "    pass\n",
        encoding="utf-8",
    )

    service_file = pkg_dir / "service.py"
    service_file.write_text(
        "from mypkg.impl import SqlRepository\n"
        "\n"
        "def helper():\n"
        "    return 42\n"
        "\n"
        "def execute_task():\n"
        '    """Execute main service task."""\n'
        "    repo = SqlRepository()\n"
        "    val = helper()\n"
        "    return repo.get('1') + str(val)\n",
        encoding="utf-8",
    )

    caller_file = pkg_dir / "main.py"
    caller_file.write_text(
        "from mypkg.service import execute_task\n"
        "\n"
        "def run_app():\n"
        "    return execute_task()\n",
        encoding="utf-8",
    )

    # Build MultiDiGraph matching the files
    G = nx.MultiDiGraph()

    # Base nodes
    G.add_node(
        "IRepository",
        label="IRepository",
        name="IRepository",
        type="class",
        file_type="code",
        source_file=str(base_file.relative_to(tmp_path)),
        start_line=3,
        end_line=6,
        docstring="Abstract repository interface.",
    )

    # Implementation nodes
    G.add_node(
        "SqlRepository",
        label="SqlRepository",
        name="SqlRepository",
        type="class",
        file_type="code",
        source_file=str(impl_file.relative_to(tmp_path)),
        source_location="L3-L6",
        docstring="SQL-backed implementation.",
    )
    G.add_node(
        "CachedSqlRepository",
        label="CachedSqlRepository",
        name="CachedSqlRepository",
        type="class",
        file_type="code",
        source_file=str(impl_file.relative_to(tmp_path)),
        source_location="L8-L10",
        docstring="Cached SQL implementation.",
    )

    # Service nodes
    G.add_node(
        "helper",
        label="helper()",
        name="helper",
        type="function",
        file_type="code",
        source_file=str(service_file.relative_to(tmp_path)),
        source_location="L3-L4",
    )
    G.add_node(
        "execute_task",
        label="execute_task()",
        name="execute_task",
        type="function",
        file_type="code",
        source_file=str(service_file.relative_to(tmp_path)),
        source_location="L6-L10",
        docstring="Execute main service task.",
    )

    # Main node
    G.add_node(
        "run_app",
        label="run_app()",
        name="run_app",
        type="function",
        file_type="code",
        source_file=str(caller_file.relative_to(tmp_path)),
        source_location="L3-L4",
    )

    # Inheritance edges (subclass -> base)
    G.add_edge("SqlRepository", "IRepository", relation="implements", confidence="EXTRACTED")
    G.add_edge("CachedSqlRepository", "SqlRepository", relation="inherits", confidence="EXTRACTED")

    # Call edges (caller -> callee)
    G.add_edge(
        "run_app",
        "execute_task",
        relation="calls",
        confidence="EXTRACTED",
        source_file=str(caller_file.relative_to(tmp_path)),
        source_location="L4",
    )
    G.add_edge(
        "execute_task",
        "helper",
        relation="calls",
        confidence="EXTRACTED",
        source_file=str(service_file.relative_to(tmp_path)),
        source_location="L9",
    )

    # Cross-file reference edges (importer -> imported symbol)
    G.add_edge(
        "SqlRepository",
        "IRepository",
        relation="imports",
        confidence="EXTRACTED",
        source_file=str(impl_file.relative_to(tmp_path)),
        source_location="L1",
    )
    G.add_edge(
        "execute_task",
        "SqlRepository",
        relation="references",
        confidence="EXTRACTED",
        source_file=str(service_file.relative_to(tmp_path)),
        source_location="L8",
    )
    G.add_edge(
        "run_app",
        "execute_task",
        relation="imports",
        confidence="EXTRACTED",
        source_file=str(caller_file.relative_to(tmp_path)),
        source_location="L1",
    )

    return {
        "root_dir": str(tmp_path),
        "graph": G,
        "base_file": str(base_file.relative_to(tmp_path)),
        "impl_file": str(impl_file.relative_to(tmp_path)),
        "service_file": str(service_file.relative_to(tmp_path)),
        "caller_file": str(caller_file.relative_to(tmp_path)),
    }


def test_estimate_token_savings_basic(sample_code_tree):
    root = sample_code_tree["root_dir"]
    subgraph_nodes = [
        {"source_file": sample_code_tree["base_file"], "label": "IRepository"},
        {"source_file": sample_code_tree["impl_file"], "label": "SqlRepository"},
    ]

    savings = estimate_token_savings(subgraph_nodes, root_dir=root)
    assert savings["file_reads_avoided"] == 2
    assert savings["total_source_tokens"] > 0
    assert savings["subgraph_tokens"] > 0
    assert savings["tokens_saved"] >= 0
    assert savings["compression_ratio"] > 0


def test_estimate_token_savings_empty():
    savings = estimate_token_savings([], root_dir=".")
    assert savings["file_reads_avoided"] == 0
    assert savings["total_source_tokens"] == 0
    assert savings["subgraph_tokens"] == 0
    assert savings["tokens_saved"] == 0
    assert savings["compression_ratio"] == 1.0


def test_estimate_token_savings_synthetic_fallback():
    subgraph_nodes = [
        {"source_file": "nonexistent.py", "file_tokens": 1000, "label": "Dummy"},
    ]
    savings = estimate_token_savings(subgraph_nodes, root_dir=".")
    assert savings["file_reads_avoided"] == 1
    assert savings["total_source_tokens"] == 1000
    assert savings["tokens_saved"] > 0


def test_find_definitions_exact(sample_code_tree):
    G = sample_code_tree["graph"]
    root = sample_code_tree["root_dir"]

    res = find_definitions("execute_task", graph=G, root_dir=root)
    assert len(res["definitions"]) == 1
    defn = res["definitions"][0]

    assert defn["name"] == "execute_task"
    assert defn["type"] == "function"
    assert defn["source_file"] == sample_code_tree["service_file"]
    assert defn["start_line"] == 6
    assert defn["end_line"] == 10
    assert defn["docstring"] == "Execute main service task."

    assert "token_savings" in res
    assert res["tokens_saved"] >= 0
    assert res["file_reads_avoided"] == 1
    assert res["compression_ratio"] > 0


def test_find_definitions_with_rationale_edge(tmp_path: Path):
    G = nx.DiGraph()
    G.add_node(
        "fn_target",
        label="calculate_tax()",
        source_file="tax.py",
        source_location="L12-L20",
        file_type="code",
    )
    G.add_node(
        "doc_node",
        label="Calculate federal tax rate.",
        file_type="rationale",
        source_file="tax.py",
        source_location="L13",
    )
    G.add_edge("doc_node", "fn_target", relation="rationale_for")

    res = find_definitions("calculate_tax", graph=G, root_dir=str(tmp_path))
    assert len(res["definitions"]) == 1
    d = res["definitions"][0]
    assert d["name"] == "calculate_tax"
    assert d["start_line"] == 12
    assert d["end_line"] == 20
    assert d["docstring"] == "Calculate federal tax rate."


def test_find_definitions_not_found(sample_code_tree):
    G = sample_code_tree["graph"]
    root = sample_code_tree["root_dir"]

    res = find_definitions("NonExistentSymbol", graph=G, root_dir=root)
    assert res["definitions"] == []
    assert res["tokens_saved"] == 0
    assert res["file_reads_avoided"] == 0


def test_get_callers(sample_code_tree):
    G = sample_code_tree["graph"]
    root = sample_code_tree["root_dir"]

    res = get_callers("execute_task", graph=G, root_dir=root)
    assert len(res["callers"]) == 1
    caller = res["callers"][0]
    assert caller["id"] == "run_app"
    assert caller["source_file"] == sample_code_tree["caller_file"]
    assert caller["call_site"] == "L4"
    assert caller["call_line"] == 4

    assert "tokens_saved" in res
    assert res["file_reads_avoided"] >= 1


def test_get_callees(sample_code_tree):
    G = sample_code_tree["graph"]
    root = sample_code_tree["root_dir"]

    res = get_callees("execute_task", graph=G, root_dir=root)
    assert len(res["callees"]) == 1
    callee = res["callees"][0]
    assert callee["id"] == "helper"
    assert callee["source_file"] == sample_code_tree["service_file"]

    assert "tokens_saved" in res
    assert res["file_reads_avoided"] >= 1


def test_find_references(sample_code_tree):
    G = sample_code_tree["graph"]
    root = sample_code_tree["root_dir"]

    res = find_references("IRepository", graph=G, root_dir=root)
    assert len(res["references"]) >= 1

    refs_by_src = {r["id"]: r for r in res["references"]}
    assert "SqlRepository" in refs_by_src
    ref = refs_by_src["SqlRepository"]
    assert ref["relation"] == "imports"
    assert ref["source_file"] == sample_code_tree["impl_file"]

    assert res["file_reads_avoided"] >= 1


def test_find_references_excludes_same_file():
    G = nx.DiGraph()
    G.add_node("target_sym", label="target_sym", source_file="a.py")
    G.add_node("internal_caller", label="internal_caller", source_file="a.py")
    G.add_node("external_caller", label="external_caller", source_file="b.py")

    G.add_edge("internal_caller", "target_sym", relation="calls", source_file="a.py")
    G.add_edge("external_caller", "target_sym", relation="calls", source_file="b.py")

    res = find_references("target_sym", graph=G)
    ref_ids = [r["id"] for r in res["references"]]
    assert "external_caller" in ref_ids
    assert "internal_caller" not in ref_ids


def test_find_implementations(sample_code_tree):
    G = sample_code_tree["graph"]
    root = sample_code_tree["root_dir"]

    res = find_implementations("IRepository", graph=G, root_dir=root)
    impl_ids = [i["id"] for i in res["implementations"]]

    # Both direct and transitive implementations
    assert "SqlRepository" in impl_ids
    assert "CachedSqlRepository" in impl_ids

    assert res["file_reads_avoided"] >= 1
    assert "tokens_saved" in res


def test_codegraph_tool_specs():
    assert len(CODEGRAPH_TOOL_SPECS) == 11
    names = {spec["name"] for spec in CODEGRAPH_TOOL_SPECS}
    expected = {
        "find_definitions",
        "get_callers",
        "get_callees",
        "find_references",
        "find_implementations",
        "get_code_skeleton",
        "get_symbol_implementation",
        "get_diff_context",
        "refine_context",
        "get_agent_context",
        "get_test_selection",
    }
    assert names == expected


def test_serve_handlers_dispatch(sample_code_tree, monkeypatch):
    """Test dispatching via serve._handlers dictionary."""
    from graph_fy.serve import _build_server

    # Check that _build_server can construct the tool list
    # If mcp package is missing in the testing env, skip or mock
    pytest.importorskip("mcp")

    graph_path = Path(sample_code_tree["root_dir"]) / "graph_fy_out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    from networkx.readwrite import json_graph

    graph_path.write_text(
        json.dumps(json_graph.node_link_data(sample_code_tree["graph"], edges="links")),
        encoding="utf-8",
    )

    server = _build_server(str(graph_path))
    assert server is not None


def test_serve_handlers_with_mock(sample_code_tree, monkeypatch):
    """Test CodeGraph tool handlers registered in _build_server using mocked MCP."""
    import asyncio
    import sys
    from unittest.mock import MagicMock

    mock_mcp = MagicMock()
    mock_server_mod = MagicMock()
    mock_types_mod = MagicMock()

    class DummyTool:
        def __init__(self, name, description="", inputSchema=None):
            self.name = name
            self.description = description
            self.inputSchema = inputSchema or {}

    class DummyTextContent:
        def __init__(self, type="text", text=""):
            self.type = type
            self.text = text

    mock_types_mod.Tool = DummyTool
    mock_types_mod.TextContent = DummyTextContent
    mock_mcp.types = mock_types_mod

    class DummyServer:
        def __init__(self, name="graph_fy", **kwargs):
            self.name = name
            self._tools_fn = None
            self._call_tool_fn = None

        def list_tools(self):
            def decorator(fn):
                self._tools_fn = fn
                return fn

            return decorator

        def call_tool(self):
            def decorator(fn):
                self._call_tool_fn = fn
                return fn

            return decorator

        def list_resources(self):
            return lambda fn: fn

        def read_resource(self):
            return lambda fn: fn

    mock_server_mod.Server = DummyServer

    monkeypatch.setitem(sys.modules, "mcp", mock_mcp)
    monkeypatch.setitem(sys.modules, "mcp.server", mock_server_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", mock_types_mod)

    from graph_fy.serve import _build_server

    graph_path = Path(sample_code_tree["root_dir"]) / "graph_fy_out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    from networkx.readwrite import json_graph

    graph_path.write_text(
        json.dumps(json_graph.node_link_data(sample_code_tree["graph"], edges="links")),
        encoding="utf-8",
    )

    server = _build_server(str(graph_path))
    assert server is not None
    assert server._tools_fn is not None
    assert server._call_tool_fn is not None

    tools = asyncio.run(server._tools_fn())
    tool_names = {t.name for t in tools}
    assert "find_definitions" in tool_names
    assert "get_callers" in tool_names
    assert "get_callees" in tool_names
    assert "find_references" in tool_names
    assert "find_implementations" in tool_names

    # Test executing call_tool for find_definitions
    res = asyncio.run(server._call_tool_fn("find_definitions", {"query": "execute_task"}))
    assert len(res) == 1
    parsed = json.loads(res[0].text)
    assert len(parsed["definitions"]) == 1
    assert parsed["definitions"][0]["name"] == "execute_task"
    assert "tokens_saved" in parsed

    # Test executing call_tool for get_callers
    res_callers = asyncio.run(server._call_tool_fn("get_callers", {"symbol": "execute_task"}))
    assert len(res_callers) == 1
    parsed_callers = json.loads(res_callers[0].text)
    assert len(parsed_callers["callers"]) == 1
    assert parsed_callers["callers"][0]["id"] == "run_app"

    # Test executing call_tool for get_callees
    res_callees = asyncio.run(server._call_tool_fn("get_callees", {"symbol": "execute_task"}))
    assert len(res_callees) == 1
    parsed_callees = json.loads(res_callees[0].text)
    assert len(parsed_callees["callees"]) == 1
    assert parsed_callees["callees"][0]["id"] == "helper"

    # Test executing call_tool for find_references
    res_refs = asyncio.run(server._call_tool_fn("find_references", {"symbol": "IRepository"}))
    assert len(res_refs) == 1
    parsed_refs = json.loads(res_refs[0].text)
    assert len(parsed_refs["references"]) >= 1

    # Test executing call_tool for find_implementations
    res_impls = asyncio.run(server._call_tool_fn("find_implementations", {"symbol": "IRepository"}))
    assert len(res_impls) == 1
    parsed_impls = json.loads(res_impls[0].text)
    assert len(parsed_impls["implementations"]) == 2



def test_graph_fy_root_exports():
    """Verify lazy-loaded re-exports on graph_fy package root."""
    import graph_fy

    assert callable(graph_fy.find_definitions)
    assert callable(graph_fy.get_callers)
    assert callable(graph_fy.get_callees)
    assert callable(graph_fy.find_references)
    assert callable(graph_fy.find_implementations)
    assert callable(graph_fy.estimate_token_savings)


def test_find_definitions_variations(sample_code_tree):
    G = sample_code_tree["graph"]
    root = sample_code_tree["root_dir"]

    # Case-insensitive
    res_upper = find_definitions("EXECUTE_TASK", graph=G, root_dir=root)
    assert len(res_upper["definitions"]) == 1

    # Trailing parentheses in query
    res_parens = find_definitions("execute_task()", graph=G, root_dir=root)
    assert len(res_parens["definitions"]) == 1
    assert res_parens["definitions"][0]["name"] == "execute_task"

    # Single line source_location
    G.add_node(
        "single_line_fn",
        label="single_line_fn",
        source_file=sample_code_tree["service_file"],
        source_location="L42",
        file_type="code",
    )
    res_single = find_definitions("single_line_fn", graph=G, root_dir=root)
    assert len(res_single["definitions"]) == 1
    assert res_single["definitions"][0]["start_line"] == 42
    assert res_single["definitions"][0]["end_line"] == 42


def test_callers_callees_not_found(sample_code_tree):
    G = sample_code_tree["graph"]
    root = sample_code_tree["root_dir"]

    callers = get_callers("NonExistent", graph=G, root_dir=root)
    assert callers["callers"] == []
    assert callers["tokens_saved"] == 0

    callees = get_callees("NonExistent", graph=G, root_dir=root)
    assert callees["callees"] == []
    assert callees["tokens_saved"] == 0


def test_find_implementations_deep_hierarchy():
    G = nx.DiGraph()
    G.add_node("Animal", label="Animal", type="class", source_file="a.py")
    G.add_node("Mammal", label="Mammal", type="class", source_file="m.py")
    G.add_node("Canine", label="Canine", type="class", source_file="c.py")
    G.add_node("Dog", label="Dog", type="class", source_file="d.py")

    G.add_edge("Mammal", "Animal", relation="inherits")
    G.add_edge("Canine", "Mammal", relation="extends")
    G.add_edge("Dog", "Canine", relation="implements")

    res = find_implementations("Animal", graph=G)
    ids = [impl["id"] for impl in res["implementations"]]
    assert ids == ["Mammal", "Canine", "Dog"]


def test_cli_query_verbose(tmp_path: Path, monkeypatch, capsys):
    """Test graph_fy query --verbose flag outputs token savings."""
    import sys
    from networkx.readwrite import json_graph
    from graph_fy.__main__ import main

    file_a = tmp_path / "hello.py"
    file_a.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    out_dir = tmp_path / "graph_fy_out"
    out_dir.mkdir()
    graph_path = out_dir / "graph.json"

    G = nx.Graph()
    G.add_node("hello_fn", label="hello()", source_file="hello.py", source_location="L1", community=0)
    graph_path.write_text(json.dumps(json_graph.node_link_data(G, edges="links")), encoding="utf-8")

    test_args = [
        "graph_fy",
        "query",
        "hello",
        "--graph",
        str(graph_path),
        "--text",
        "--verbose",
    ]
    monkeypatch.setattr(sys, "argv", test_args)
    monkeypatch.chdir(tmp_path)

    main()
    captured = capsys.readouterr()
    assert "[verbose] Token savings:" in captured.out
    assert "tokens saved" in captured.out
    assert "compression" in captured.out
