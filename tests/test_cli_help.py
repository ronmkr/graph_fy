"""`graph_fy --help` must list user-facing commands that dispatch_command implements.

#3140: the top-level listing in graph_fy/__main__.py is hand-maintained and
drifted from dispatch_command() — prs, provider, and 7 of 8 export formats
never appeared. Precedent: #2071 / PR #2075 (one usage line + stdout assert).
"""
from __future__ import annotations


def test_help_lists_prs_provider_and_export_formats(run_cli_in_process):
    """#3140: `graph_fy --help` must advertise prs, provider, and all export formats."""
    code, out, err = run_cli_in_process("--help")
    assert code == 0, f"--help should exit 0: {err}"
    assert "  prs" in out, "`graph_fy --help` must list prs (#3140)"
    assert "provider" in out, "`graph_fy --help` must list provider (#3140)"
    for fmt in (
        "html",
        "callflow-html",
        "obsidian",
        "wiki",
        "svg",
        "graphml",
        "neo4j",
        "falkordb",
    ):
        assert f"export {fmt}" in out, (
            f"`graph_fy --help` must list export {fmt} (#3140)"
        )
    # Silent hook internals — deliberately excluded from user-facing help.
    assert "hook-check" not in out
    assert "hook-guard" not in out
