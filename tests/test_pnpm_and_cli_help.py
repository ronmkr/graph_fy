"""Tests for PNPM workspace comment handling, glob resilience, query stopwords, and CLI help (Issue #77)."""
from __future__ import annotations

from pathlib import Path
import pytest

from graph_fy.resolvers.javascript import _pnpm_workspace_globs, _load_workspace_packages
from graph_fy.serve_search import _QUERY_STOPWORDS
from graph_fy.commands.query import cmd_query, cmd_explain


def test_pnpm_workspace_globs_strips_comments(tmp_path: Path):
    workspace_yaml = tmp_path / "pnpm-workspace.yaml"
    workspace_yaml.write_text(
        "packages:\n"
        "  - 'packages/*' # inline comment after single quote\n"
        '  - "apps/*"   # inline comment after double quote\n'
        "  - services/* # unquoted with comment\n"
        "  - '!ignored/*'\n",
        encoding="utf-8",
    )
    globs = _pnpm_workspace_globs(workspace_yaml)
    assert globs == ["packages/*", "apps/*", "services/*"]


def test_load_workspace_packages_skips_invalid_globs(tmp_path: Path, capsys):
    workspace_yaml = tmp_path / "pnpm-workspace.yaml"
    workspace_yaml.write_text(
        "packages:\n"
        "  - '/**/invalid'\n"
        "  - 'valid/*'\n",
        encoding="utf-8",
    )
    (tmp_path / "valid" / "pkg1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "valid" / "pkg1" / "package.json").write_text('{"name": "@test/pkg1"}', encoding="utf-8")

    pkgs = _load_workspace_packages(tmp_path)
    assert "@test/pkg1" in pkgs


def test_query_stopwords_includes_get_variants():
    for verb in ("get", "gets", "got", "getting"):
        assert verb in _QUERY_STOPWORDS


def test_cmd_query_lone_help_prints_usage(capsys):
    rc = cmd_query(["graph_fy", "query", "--help"], lambda: "graph.json", lambda _: Path("graph.json"))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith('Usage: graph_fy query "<text>"')

    rc_short = cmd_query(["graph_fy", "query", "-h"], lambda: "graph.json", lambda _: Path("graph.json"))
    assert rc_short == 0
    out_short = capsys.readouterr().out
    assert out_short.startswith('Usage: graph_fy query "<text>"')


def test_cmd_explain_lone_help_prints_usage(capsys):
    rc = cmd_explain(["graph_fy", "explain", "--help"], lambda: "graph.json", lambda _: Path("graph.json"))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith('Usage: graph_fy explain "<node>"')

    rc_short = cmd_explain(["graph_fy", "explain", "-h"], lambda: "graph.json", lambda _: Path("graph.json"))
    assert rc_short == 0
    out_short = capsys.readouterr().out
    assert out_short.startswith('Usage: graph_fy explain "<node>"')
