"""Unit tests for sub-second on-demand AST search and single-file skeleton extraction (Issue #50)."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from graph_fy.fast_query import (
    _scan_files_python,
    _scan_files_rg,
    fast_ast_grep,
    format_fast_hits,
    skeletonize_single_file,
)
from graph_fy.commands.skeleton import cmd_skeleton
from graph_fy.commands.query import cmd_query
from graph_fy.mcp import fast_ast_search, get_code_skeleton


@pytest.fixture
def sample_workspace(tmp_path: Path) -> Path:
    """Create a multi-language source workspace for testing."""
    py_file = tmp_path / "auth_service.py"
    py_file.write_text(
        '"""Authentication service module."""\n\n'
        "class AuthService:\n"
        '    """Handles user credential validation."""\n\n'
        "    def authenticate(self, username: str, token: str) -> bool:\n"
        '        """Verify token validity."""\n'
        "        if not username or not token:\n"
        "            return False\n"
        "        return token.startswith('valid_')\n\n"
        "def hash_password(plain: str) -> str:\n"
        '    """Compute password hash."""\n'
        "    return f'hash_{plain}'\n",
        encoding="utf-8",
    )

    ts_file = tmp_path / "api_client.ts"
    ts_file.write_text(
        "export interface UserProfile {\n"
        "  id: string;\n"
        "  email: string;\n"
        "}\n\n"
        "export async function fetchProfile(userId: string): Promise<UserProfile> {\n"
        "  const res = await fetch(`/api/users/${userId}`);\n"
        "  return res.json();\n"
        "}\n",
        encoding="utf-8",
    )

    rust_file = tmp_path / "dispatcher.rs"
    rust_file.write_text(
        "pub struct Dispatcher {\n"
        "    workers: usize,\n"
        "}\n\n"
        "impl Dispatcher {\n"
        "    pub fn new(workers: usize) -> Self {\n"
        "        Dispatcher { workers }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    return tmp_path


def test_fast_ast_grep_exact_match(sample_workspace: Path):
    hits = fast_ast_grep("hash_password", target=sample_workspace)
    assert len(hits) >= 1
    top = hits[0]
    assert top["symbol"] == "hash_password"
    assert top["kind"] == "function"
    assert top["match_type"] == "exact_definition"
    assert "def hash_password" in top["signature"]
    assert "Compute password hash." in top["docstring"]
    assert "return f'hash_{plain}'" in top["code"]


def test_fast_ast_grep_class_method(sample_workspace: Path):
    hits = fast_ast_grep("authenticate", target=sample_workspace)
    assert len(hits) >= 1
    top = hits[0]
    assert top["symbol"] == "authenticate"
    assert top["kind"] in ("function", "method")
    assert "def authenticate" in top["signature"]
    assert "Verify token validity." in top["docstring"]


def test_fast_ast_grep_typescript_interface_and_function(sample_workspace: Path):
    hits = fast_ast_grep("fetchProfile", target=sample_workspace)
    assert len(hits) >= 1
    top = hits[0]
    assert top["symbol"] == "fetchProfile"
    assert top["kind"] == "function"
    assert "fetchProfile" in top["signature"]


def test_fast_ast_grep_rust_struct(sample_workspace: Path):
    hits = fast_ast_grep("Dispatcher", target=sample_workspace)
    assert len(hits) >= 1
    top = hits[0]
    assert top["symbol"] == "Dispatcher"
    assert top["kind"] == "struct"


def test_scan_files_python_fallback(sample_workspace: Path):
    matches = _scan_files_python("hash_password", sample_workspace)
    assert len(matches) >= 1
    fpath, lno, text = matches[0]
    assert fpath.name == "auth_service.py"
    assert "def hash_password" in text


def test_skeletonize_single_file_whole(sample_workspace: Path):
    res = skeletonize_single_file(sample_workspace / "auth_service.py")
    assert res["file"].endswith("auth_service.py")
    assert "def authenticate" in res["skeleton"]
    assert "..." in res["skeleton"]
    assert "return token.startswith" not in res["skeleton"]


def test_skeletonize_single_file_symbol(sample_workspace: Path):
    res = skeletonize_single_file(sample_workspace / "auth_service.py", symbol_name="hash_password")
    assert res["symbol"] == "hash_password"
    assert res["kind"] == "function"
    assert "def hash_password(plain: str) -> str:" in res["skeleton"]
    assert "..." in res["skeleton"]


def test_skeletonize_single_file_symbol_not_found(sample_workspace: Path):
    res = skeletonize_single_file(sample_workspace / "auth_service.py", symbol_name="nonexistent_function")
    assert "error" in res
    assert "not found" in res["error"]


def test_cmd_skeleton_cli(sample_workspace: Path, capsys):
    rc = cmd_skeleton(["graph_fy", "skeleton", str(sample_workspace / "auth_service.py"), "--symbol", "hash_password"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "def hash_password" in captured.out
    assert "..." in captured.out


def test_cmd_skeleton_cli_json(sample_workspace: Path, capsys):
    rc = cmd_skeleton(["graph_fy", "skeleton", str(sample_workspace / "auth_service.py"), "--symbol", "AuthService", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["symbol"] == "AuthService"
    assert data["kind"] == "class"


def test_cmd_query_fast_grep_flag(sample_workspace: Path, capsys):
    rc = cmd_query(
        ["graph_fy", "query", "hash_password", "--fast-grep", str(sample_workspace)],
        default_graph_path_fn=lambda: str(sample_workspace / "graph_fy_out" / "graph.json"),
        find_graph_path_fn=lambda p: Path(p),
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "Fast AST Search" in captured.out
    assert "hash_password" in captured.out
    assert "auth_service.py" in captured.out


def test_cmd_query_unindexed_repo_fallback(sample_workspace: Path, capsys):
    # Missing graph returns 1 and gives tip to run --fast-grep
    rc = cmd_query(
        ["graph_fy", "query", "hash_password", "--graph", str(sample_workspace / "nonexistent.json")],
        default_graph_path_fn=lambda: str(sample_workspace / "nonexistent.json"),
        find_graph_path_fn=lambda p: Path(p),
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "graph file not found" in captured.err
    assert "--fast-grep" in captured.err


def test_mcp_fast_ast_search_and_get_code_skeleton(sample_workspace: Path):
    search_res = fast_ast_search("authenticate", target=str(sample_workspace))
    assert search_res["count"] >= 1
    assert search_res["hits"][0]["symbol"] == "authenticate"

    skel_res = get_code_skeleton(str(sample_workspace / "auth_service.py"), symbol="hash_password")
    assert skel_res["symbol"] == "hash_password"
    assert "def hash_password" in skel_res["skeleton"]
