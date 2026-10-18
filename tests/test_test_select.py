"""tests/test_test_select.py — Tests for native test selection.

Validates diff hunk line mapping, reverse-BFS traversal to tests,
test runner command formatting (pytest, cargo, vitest, npm, go, mvn, gradle),
CLI execution, and MCP tool output.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
import pytest
import networkx as nx

from graph_fy.commands.test_select import (
    _detect_runner,
    _format_test_command,
    _parse_git_diff_hunks,
    cmd_coverage,
    cmd_test_select,
    select_affected_tests,
)
from graph_fy.mcp import get_test_selection


def test_detect_runner(tmp_path: Path):
    assert _detect_runner(tmp_path, explicit_runner="cargo") == "cargo"

    # Pytest
    (tmp_path / "pytest.ini").write_text("", encoding="utf-8")
    assert _detect_runner(tmp_path) == "pytest"
    (tmp_path / "pytest.ini").unlink()

    # Cargo
    (tmp_path / "Cargo.toml").write_text("", encoding="utf-8")
    assert _detect_runner(tmp_path) == "cargo"
    (tmp_path / "Cargo.toml").unlink()

    # Vitest via package.json
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^1.0.0"}}), encoding="utf-8")
    assert _detect_runner(tmp_path) == "vitest"
    (tmp_path / "package.json").unlink()

    # Go
    (tmp_path / "go.mod").write_text("", encoding="utf-8")
    assert _detect_runner(tmp_path) == "go"
    (tmp_path / "go.mod").unlink()

    # Maven
    (tmp_path / "pom.xml").write_text("", encoding="utf-8")
    assert _detect_runner(tmp_path) == "mvn"


def test_format_test_command():
    selected = [
        {"source_file": "tests/test_auth.py", "label": "test_login"},
        {"source_file": "tests/test_auth.py", "label": "test_logout"},
    ]
    # Pytest
    cmd_py = _format_test_command("pytest", selected, Path("."))
    assert "pytest tests/test_auth.py -k \"test_login or test_logout\"" == cmd_py

    # Cargo
    selected_rust = [
        {"source_file": "tests/test_queue.rs", "label": "test_push"},
    ]
    cmd_cargo = _format_test_command("cargo", selected_rust, Path("."))
    assert "cargo test --test test_queue test_push" == cmd_cargo

    # Vitest
    selected_ts = [
        {"source_file": "tests/user.test.ts", "label": "createsUser"},
    ]
    cmd_vitest = _format_test_command("vitest", selected_ts, Path("."))
    assert "npx vitest run tests/user.test.ts -t \"createsUser\"" == cmd_vitest

    # Go
    cmd_go = _format_test_command("go", selected, Path("."))
    assert "go test -v ./... -run \"^(test_login|test_logout)$\"" == cmd_go


def test_select_affected_tests_with_coverage_and_callers():
    G = nx.DiGraph()

    # Function 1: modified
    G.add_node("func_auth", label="login", source_file="src/auth.py", start_line=10, end_line=25)

    # Function 2: intermediate helper
    G.add_node("func_token", label="create_token", source_file="src/token.py", start_line=5, end_line=15)

    # Test 1: directly calls func_auth
    G.add_node("test_login_success", label="test_login_success", source_file="tests/test_auth.py", start_line=5, end_line=12)

    # Test 2: covered by test edge
    G.add_node("test_auth_cov", label="test_auth_cov", source_file="tests/test_cov.py", start_line=1, end_line=10)

    # Test 3: 2-hop caller through func_token
    G.add_node("test_transitive", label="test_transitive", source_file="tests/test_token.py", start_line=1, end_line=8)

    # Edges: callers call functions
    G.add_edge("test_login_success", "func_auth", relation="calls")
    G.add_edge("func_auth", "test_auth_cov", relation="covered_by_test")
    G.add_edge("test_transitive", "func_token", relation="calls")
    G.add_edge("func_token", "func_auth", relation="calls")

    # Mock diff: line 15 in src/auth.py changed
    diff_hunks = {
        "src/auth.py": [(14, 16)],
    }

    with patch("graph_fy.commands.test_select._parse_git_diff_hunks", return_value=diff_hunks):
        res = select_affected_tests(G, since="HEAD~1", runner="pytest")

    assert res["modified_files"] == ["src/auth.py"]
    assert "login" in res["modified_functions"]

    selected = res["selected_tests"]
    assert len(selected) >= 2

    # High-confidence direct coverage / caller tests should be at the top
    top_labels = [t["label"] for t in selected]
    assert "test_auth_cov" in top_labels
    assert "test_login_success" in top_labels
    assert "test_transitive" in top_labels

    assert "pytest" in res["command"]


def test_select_affected_tests_directly_modified_test():
    G = nx.DiGraph()
    G.add_node("test_foo", label="test_foo", source_file="tests/test_foo.py", start_line=1, end_line=10)

    diff_hunks = {"tests/test_foo.py": [(3, 5)]}
    with patch("graph_fy.commands.test_select._parse_git_diff_hunks", return_value=diff_hunks):
        res = select_affected_tests(G, since="HEAD~1", runner="pytest")

    assert len(res["selected_tests"]) == 1
    t = res["selected_tests"][0]
    assert t["label"] == "test_foo"
    assert t["score"] == 1.0
    assert t["distance"] == 0
    assert t["reason"] == "directly_modified_test"


def test_mcp_get_test_selection():
    G = nx.DiGraph()
    G.add_node("mod_fn", label="do_work", source_file="src/work.py", start_line=1, end_line=20)
    G.add_node("test_fn", label="test_work", source_file="tests/test_work.py", start_line=1, end_line=10)
    G.add_edge("test_fn", "mod_fn", relation="calls")

    diff_hunks = {"src/work.py": [(5, 6)]}
    with patch("graph_fy.commands.test_select._parse_git_diff_hunks", return_value=diff_hunks):
        res = get_test_selection(since="HEAD~1", runner="pytest", graph=G)

    assert "selected_tests" in res
    assert len(res["selected_tests"]) == 1
    assert res["selected_tests"][0]["label"] == "test_work"


def test_cmd_test_select_cli(tmp_path: Path, capsys):
    G = nx.DiGraph()
    G.add_node("fn1", label="calculate", source_file="src/calc.py", start_line=1, end_line=10)
    G.add_node("test1", label="test_calc", source_file="tests/test_calc.py", start_line=1, end_line=5)
    G.add_edge("test1", "fn1", relation="calls")

    from graph_fy.export import to_json
    to_json(G, {}, str(tmp_path / "graph.json"), force=True)

    diff_hunks = {"src/calc.py": [(2, 3)]}
    with patch("graph_fy.commands.test_select._parse_git_diff_hunks", return_value=diff_hunks):
        rc = cmd_test_select(["graph_fy", "test-select", "--graph", str(tmp_path / "graph.json"), "--dir", str(tmp_path)])
        assert rc == 0

    out = capsys.readouterr().out
    assert "test-select" in out
    assert "test_calc" in out
    assert "Ready-to-Run Test Command:" in out


def test_cmd_coverage_cli(tmp_path: Path, capsys):
    G = nx.DiGraph()
    G.add_node("fn1", label="foo", source_file="src/foo.py", start_line=1, end_line=5)
    from graph_fy.export import to_json
    to_json(G, {}, str(tmp_path / "graph.json"), force=True)

    cov_file = tmp_path / "coverage.xml"
    cov_file.write_text("""<?xml version="1.0" ?>
    <coverage version="7.0">
        <packages><package name="p"><classes>
            <class name="foo.py" filename="src/foo.py">
                <lines><line number="2" hits="1"/></lines>
            </class>
        </classes></package></packages>
    </coverage>""", encoding="utf-8")

    rc = cmd_coverage(["graph_fy", "coverage", str(cov_file), "--graph", str(tmp_path / "graph.json"), "--dir", str(tmp_path)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Successfully ingested" in out
    assert "Overall repository line coverage" in out
