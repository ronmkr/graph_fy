"""tests/test_agent_prime.py — Tests for graph_fy prime codebase priming header (#103)."""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest

from graph_fy.commands.prime import (
    PRIME_END,
    PRIME_START,
    cmd_prime,
    generate_prime_header,
    inject_prime_header,
)


def _build_priming_graph() -> nx.DiGraph:
    G = nx.DiGraph()
    # Context 1: Auth
    for i in range(15):
        G.add_node(f"auth_{i}", label=f"AuthService_{i}", community=1, file_type="code", source_file="src/auth.py")
    # Context 2: Storage
    for i in range(12):
        G.add_node(f"store_{i}", label=f"StorageEngine_{i}", community=2, file_type="code", source_file="src/storage.py")

    # Entry point
    G.add_node("cli_main", label="dispatch_command()", source_file="src/cli.py", file_type="code")
    # High-degree hub
    G.add_node("hub_extract", label="extract()", source_file="src/extract.py", file_type="code")
    for i in range(15):
        G.add_edge(f"auth_{i}", "hub_extract", relation="calls")
    for i in range(12):
        G.add_edge(f"store_{i}", "hub_extract", relation="calls")
    return G


def test_generate_prime_header_token_cap():
    G = _build_priming_graph()
    header = generate_prime_header(G)

    assert PRIME_START in header
    assert PRIME_END in header
    assert "### graph_fy Prime Briefing" in header
    assert "- **Bounded Contexts**:" in header
    assert "Context 1" in header
    assert "- **Key Entry Points**:" in header
    assert "`dispatch_command()`" in header
    assert "- **High-Risk Hubs**:" in header
    assert "`extract()`" in header
    assert "graph_fy context" in header

    # Ensure token cap (strictly within 100-300 tokens)
    word_count = len(header.split())
    approx_tokens = int(word_count * 1.3)
    assert 50 <= approx_tokens <= 250


def test_inject_prime_header(tmp_path: Path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# My Project\n\nSome guidelines here.\n", encoding="utf-8")

    header_v1 = f"{PRIME_START}\nHeader V1\n{PRIME_END}"
    inject_prime_header(target, header_v1)

    content_v1 = target.read_text(encoding="utf-8")
    assert content_v1.startswith(f"{PRIME_START}\nHeader V1\n{PRIME_END}\n\n# My Project")

    # Inject update (replaces in-place without duplicating)
    header_v2 = f"{PRIME_START}\nHeader V2 Updated\n{PRIME_END}"
    inject_prime_header(target, header_v2)

    content_v2 = target.read_text(encoding="utf-8")
    assert "Header V2 Updated" in content_v2
    assert "Header V1" not in content_v2
    assert "# My Project" in content_v2


def test_cmd_prime_cli(tmp_path: Path, monkeypatch, capsys):
    G = _build_priming_graph()
    from networkx.readwrite import json_graph

    graph_file = tmp_path / "graph.json"
    data = json_graph.node_link_data(G)
    graph_file.write_text(json.dumps(data), encoding="utf-8")

    # 1. JSON output
    rc = cmd_prime(["graph_fy", "prime", "--graph", str(graph_file), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    loaded = json.loads(out)
    assert "header" in loaded
    assert loaded["approx_tokens"] > 0

    # 2. Target agents sync
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("# Agent Rules\n", encoding="utf-8")
    rc2 = cmd_prime(["graph_fy", "prime", "--graph", str(graph_file), "--output", str(agents_file)])
    assert rc2 == 0
    assert PRIME_START in agents_file.read_text(encoding="utf-8")

    # 3. Target claude and target all
    claude_file = tmp_path / "CLAUDE.md"
    claude_file.write_text("# Claude Rules\n", encoding="utf-8")
    rc3 = cmd_prime(["graph_fy", "prime", "--graph", str(graph_file), "--root", str(tmp_path), "--target", "claude"])
    assert rc3 == 0
    assert PRIME_START in claude_file.read_text(encoding="utf-8")

    rc4 = cmd_prime(["graph_fy", "prime", "--graph", str(graph_file), "--root", str(tmp_path), "--target", "all"])
    assert rc4 == 0
    assert "### graph_fy Prime Briefing" in agents_file.read_text(encoding="utf-8")
    assert "### graph_fy Prime Briefing" in claude_file.read_text(encoding="utf-8")
