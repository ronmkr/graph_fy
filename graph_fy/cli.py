"""graph_fy command dispatch — every non-install subcommand.

Extracted verbatim from __main__.main(); __main__ now calls dispatch_command(cmd)
after the install/platform dispatch. Kept out of __main__ to shrink the CLI entry
module. The path-redirect (`graph_fy <path>` -> extract) re-enters via a lazy
import of main to avoid a cli<->__main__ import cycle.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from graph_fy.commands.build import (
    _handle_unverified_semantic_shrink,
    _prune_graph_json_sources,
    _stale_graph_sources,
    _stamped_manifest_files,
    _zero_node_stamped_code_sources,
    _zero_node_stamped_semantic_sources,
)
from graph_fy.commands.clone import _clone_repo
from graph_fy.commands.hook_guard import (
    _GEMINI_NUDGE_TEXT,
    _HOOK_SOURCE_EXTS,
    _READ_DENY,
    _READ_NUDGE,
    _READ_NUDGE_STALE,
    _SEARCH_NUDGE,
    _bash_invokes_search,
    _hook_strict_enabled,
    _is_cwd_relative,
    _mark_session_denied,
    _query_stamp_fresh,
    _run_hook_guard,
    _target_is_indexed,
    _touch_query_stamp,
)
from graph_fy.paths import graph_fy_OUT as _graph_fy_OUT


def _default_graph_path() -> str:
    from graph_fy.paths import default_graph_json
    return default_graph_json()


def _find_graph_path_for_target(target: str | Path | None = None) -> Path:
    if target:
        p = Path(target)
        if p.is_file() and p.suffix == ".json":
            return p
        if p.is_dir():
            for cand in (p / "graph_fy_out" / "graph.json", p / "graphify_out" / "graph.json", p / "graph.json"):
                if cand.is_file():
                    return cand
            return p / "graph_fy_out" / "graph.json"
    return Path(_default_graph_path())


class _StageTimer:
    """Print per-stage wall-clock timings to stderr when --timing is set (#1490).

    Monotonic (perf_counter), diagnostic-only: emits ``[graph_fy timing] <stage>:
    N.Ns`` after each stage and a final total. Off by default, so normal output is
    byte-identical and machine-read stdout is untouched.
    """

    def __init__(self, enabled: bool) -> None:
        import time as _time
        self._now = _time.perf_counter
        self.enabled = enabled
        self.start = self._now()
        self._last = self.start

    def mark(self, stage: str) -> None:
        now = self._now()
        if self.enabled:
            print(f"[graph_fy timing] {stage}: {now - self._last:.1f}s", file=sys.stderr)
        self._last = now

    def total(self) -> None:
        if self.enabled:
            print(f"[graph_fy timing] total: {self._now() - self.start:.1f}s", file=sys.stderr)


def _enforce_graph_size_cap_or_exit(gp: Path) -> None:
    """Reject oversized graph files before parsing (CLI exit-on-fail flavor)."""
    from graph_fy.security import check_graph_file_size_cap
    try:
        check_graph_file_size_cap(gp)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def _reenter_main() -> None:
    from graph_fy.__main__ import main
    main()


def dispatch_command(cmd: str) -> None:
    if cmd == "describe":
        from graph_fy.describe_arch import cli_describe
        cli_describe(sys.argv[2:])
    elif cmd == "provider":
        print("[graph_fy] LLM providers are disabled in this fully local, zero-model build.", file=sys.stderr)
        sys.exit(0)
    elif cmd == "prs":
        from graph_fy.prs import cmd_prs
        cmd_prs(sys.argv[2:])
    elif cmd == "hook":
        from graph_fy.hooks import (
            install as hook_install,
            status as hook_status,
            uninstall as hook_uninstall,
        )
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ""
        if subcmd == "install":
            print(hook_install(Path(".")))
        elif subcmd == "uninstall":
            print(hook_uninstall(Path(".")))
        elif subcmd == "status":
            print(hook_status(Path(".")))
        else:
            print("Usage: graph_fy hook [install|uninstall|status]", file=sys.stderr)
            sys.exit(1)
    elif cmd == "diff":
        from graph_fy.commands.diff import cmd_diff
        sys.exit(cmd_diff(sys.argv))
    elif cmd == "refine":
        from graph_fy.commands.refine import cmd_refine
        sys.exit(cmd_refine(sys.argv))
    elif cmd in ("context", "prebrief"):
        from graph_fy.commands.context import cmd_context
        sys.exit(cmd_context(sys.argv))
    elif cmd == "query":
        from graph_fy.commands.query import cmd_query
        rc = cmd_query(sys.argv, _default_graph_path, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "affected":
        from graph_fy.commands.analysis import cmd_affected
        rc = cmd_affected(sys.argv, _default_graph_path, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd in ("impact", "blast"):
        from graph_fy.commands.analysis import cmd_impact
        rc = cmd_impact(sys.argv, _default_graph_path, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "flow":
        from graph_fy.commands.analysis import cmd_flow
        rc = cmd_flow(sys.argv, _default_graph_path, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd in ("god-nodes", "god_nodes"):
        from graph_fy.commands.analysis import cmd_god_nodes
        rc = cmd_god_nodes(sys.argv, _default_graph_path, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd in ("stats", "graph-stats", "graph_stats"):
        from graph_fy.commands.analysis import cmd_stats
        rc = cmd_stats(sys.argv, _default_graph_path, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "save-result":
        from graph_fy.commands.reflect import cmd_save_result
        rc = cmd_save_result(sys.argv, _graph_fy_OUT)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "reflect":
        from graph_fy.commands.reflect import cmd_reflect
        rc = cmd_reflect(sys.argv, _graph_fy_OUT)
        if rc != 0:
            sys.exit(rc)
    elif cmd in ("path", "trace"):
        from graph_fy.commands.trace import cmd_trace
        rc = cmd_trace(sys.argv, _default_graph_path, _enforce_graph_size_cap_or_exit)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "explain":
        from graph_fy.commands.query import cmd_explain
        rc = cmd_explain(sys.argv, _default_graph_path, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "report":
        report_path = Path("graph_fy_out/GRAPH_REPORT.md")
        if len(sys.argv) > 2 and not sys.argv[2].startswith("-"):
            arg_p = Path(sys.argv[2])
            if arg_p.is_dir():
                cand = arg_p / "graph_fy_out" / "GRAPH_REPORT.md"
                report_path = cand if cand.exists() else arg_p / "GRAPH_REPORT.md"
            else:
                report_path = arg_p
        if not report_path.exists():
            print(f"Error: report file not found at {report_path}. Run `graph_fy build` first.", file=sys.stderr)
            sys.exit(1)
    elif cmd == "lint" and not (len(sys.argv) > 2 and sys.argv[2] == "multigraph"):
        from graph_fy.commands.lint import cmd_lint
        rc = cmd_lint(sys.argv, _default_graph_path, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd in ("diagnose", "lint"):
        from graph_fy.commands.lint import cmd_diagnose
        rc = cmd_diagnose(sys.argv, _default_graph_path)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "add":
        from graph_fy.commands.ingest import cmd_add
        rc = cmd_add(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "watch":
        from graph_fy.commands.watch import cmd_watch
        rc = cmd_watch(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif cmd in ("cluster-only", "label"):
        from graph_fy.commands.cluster import cmd_cluster
        rc = cmd_cluster(sys.argv, cmd, _graph_fy_OUT, _StageTimer)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "update":
        from graph_fy.commands.watch import cmd_update
        rc = cmd_update(sys.argv, _graph_fy_OUT)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "hook-check":
        from graph_fy.commands.hook_guard import cmd_hook_check
        rc = cmd_hook_check()
        if rc != 0:
            sys.exit(rc)
    elif cmd == "hook-guard":
        from graph_fy.commands.hook_guard import cmd_hook_guard
        rc = cmd_hook_guard(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "check-update":
        from graph_fy.commands.watch import cmd_check_update
        rc = cmd_check_update(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "tree":
        from graph_fy.commands.tree import cmd_tree
        rc = cmd_tree(sys.argv, _graph_fy_OUT, _enforce_graph_size_cap_or_exit)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "merge-driver":
        from graph_fy.commands.merge import cmd_merge_driver
        rc = cmd_merge_driver(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "merge-graphs":
        from graph_fy.commands.merge import cmd_merge_graphs
        rc = cmd_merge_graphs(sys.argv, _graph_fy_OUT)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "clone":
        from graph_fy.commands.clone import cmd_clone
        rc = cmd_clone(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "export":
        from graph_fy.commands.export import cmd_export
        rc = cmd_export(sys.argv, _graph_fy_OUT, _find_graph_path_for_target)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "benchmark":
        from graph_fy.benchmark import print_benchmark, run_benchmark
        graph_path = _find_graph_path_for_target(sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else None)
        if not graph_path.exists():
            print(f"error: graph file not found at {graph_path}. Run `graph_fy build` first.", file=sys.stderr)
            sys.exit(1)
        _enforce_graph_size_cap_or_exit(graph_path)
        corpus_words = None
        detect_path = graph_path.parent / ".graph_fy_detect.json"
        if detect_path.exists():
            try:
                detect_data = json.loads(detect_path.read_text(encoding="utf-8"))
                corpus_words = detect_data.get("total_words")
            except Exception:
                pass
        result = run_benchmark(graph_path, corpus_words=corpus_words)
        print_benchmark(result)
    elif cmd in ("arch-entities", "arch_entities", "entities-arch"):
        from graph_fy.commands.arch_entities import cmd_arch_entities
        rc = cmd_arch_entities(sys.argv, _default_graph_path)
        if rc != 0:
            sys.exit(rc)
    elif cmd in ("eval", "evals"):
        from graph_fy.commands.eval import cmd_eval
        rc = cmd_eval(sys.argv, _default_graph_path)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "global":
        from graph_fy.commands.global_cmd import cmd_global
        rc = cmd_global(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif cmd in ("extract", "build"):
        from graph_fy.commands.build import cmd_build
        rc = cmd_build(sys.argv, cmd, _graph_fy_OUT, _StageTimer)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "cache-check":
        from graph_fy.commands.chunk_merge import cmd_cache_check
        rc = cmd_cache_check(sys.argv, _graph_fy_OUT)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "merge-chunks":
        from graph_fy.commands.chunk_merge import cmd_merge_chunks
        rc = cmd_merge_chunks(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif cmd == "merge-semantic":
        from graph_fy.commands.chunk_merge import cmd_merge_semantic
        rc = cmd_merge_semantic(sys.argv)
        if rc != 0:
            sys.exit(rc)
    elif Path(cmd).exists() or cmd in (".", "..") or cmd.startswith(("./", "../", "/", "~")):
        sys.argv.insert(2, sys.argv[1])
        sys.argv[1] = "extract"
        _reenter_main()
    else:
        print(f"error: unknown command '{cmd}'", file=sys.stderr)
        print("Run 'graph_fy --help' for usage.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _reenter_main()
