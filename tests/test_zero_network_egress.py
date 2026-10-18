"""Test enforcing zero network calls during default graph_fy operations (air-gapped compliance)."""
from __future__ import annotations

import http.client
import socket
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from graph_fy.extract import extract
from graph_fy.commands.build import cmd_build
from graph_fy.commands.query import cmd_query
from graph_fy.retrieval_context import task_context


def _forbidden_connect(*args, **kwargs):
    raise RuntimeError(f"Zero-network invariant violated: outbound network call blocked: {args} {kwargs}")


@pytest.fixture
def enforce_airgap(monkeypatch):
    """Monkeypatch all socket and HTTP connection points to fail closed."""
    monkeypatch.setattr(socket.socket, "connect", _forbidden_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden_connect)
    monkeypatch.setattr(socket, "create_connection", _forbidden_connect)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _forbidden_connect)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _forbidden_connect)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden_connect)
    yield


def test_zero_network_during_ast_extraction(tmp_path: Path, enforce_airgap):
    """Ensure AST extraction makes zero outbound network calls."""
    py_file = tmp_path / "module_a.py"
    py_file.write_text("def hello(name: str) -> str:\n    return f'hello {name}'\n", encoding="utf-8")
    js_file = tmp_path / "index.js"
    js_file.write_text("export function greet(name) { return `hi ${name}`; }\n", encoding="utf-8")

    # Run direct extraction under airgap
    res = extract([py_file, js_file], root=tmp_path)
    assert "nodes" in res
    assert "edges" in res
    assert len(res["nodes"]) >= 2


def test_zero_network_during_build_and_context(tmp_path: Path, enforce_airgap):
    """Ensure graph build and context generation make zero outbound network calls."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "service.py").write_text(
        "class UserService:\n"
        "    def get_user(self, uid: int):\n"
        "        return {'id': uid}\n",
        encoding="utf-8",
    )
    (src_dir / "controller.py").write_text(
        "from .service import UserService\n"
        "svc = UserService()\n"
        "def endpoint(uid):\n"
        "    return svc.get_user(uid)\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "graph_fy_out"
    code = cmd_build(["graph_fy", "build", str(src_dir), "--out", str(tmp_path)], cmd="build")
    assert code == 0
    assert (out_dir / "graph.json").is_file()

    # Pre-agent context under airgap
    ctx = task_context("UserService", graph_path=str(out_dir / "graph.json"), root_dir=str(src_dir))
    assert ctx is not None
    assert "prompt" in ctx or "nodes" in ctx or "briefing" in ctx or len(ctx) > 0

