"""Global graph command implementation for graph_fy CLI."""
from __future__ import annotations

import sys
from pathlib import Path

from graph_fy.global_graph import (
    global_add as _global_add,
    global_list as _global_list,
    global_path as _global_path,
    global_remove as _global_remove,
)


def cmd_global(argv: list[str]) -> int:
    """Handle `graph_fy global` command."""
    subcmd = argv[2] if len(argv) > 2 else ""
    if subcmd == "add":
        args = argv[3:]
        source = None
        tag = None
        i = 0
        while i < len(args):
            if args[i] == "--as" and i + 1 < len(args):
                tag = args[i + 1]
                i += 2
            elif not source:
                source = Path(args[i])
                i += 1
            else:
                i += 1
        if not source:
            print("Usage: graph_fy global add <graph.json> [--as <repo-tag>]", file=sys.stderr)
            return 1
        if not tag:
            from graph_fy.build import distinct_repo_tags
            tag = distinct_repo_tags([source.absolute()])[0]
        try:
            result = _global_add(source, tag)
            if result["skipped"]:
                print(f"'{tag}' unchanged since last add - global graph not modified.")
            else:
                print(
                    f"Added '{tag}' to global graph: +{result['nodes_added']} nodes, "
                    f"-{result['nodes_removed']} pruned. Global: {_global_path()}"
                )
                if result.get("cross_repo_calls"):
                    print(
                        f"  resolved {result['cross_repo_calls']} "
                        f"member call(s) across repos"
                    )
            return 0
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    elif subcmd == "remove":
        if len(argv) <= 3:
            print("Usage: graph_fy global remove <repo-tag>", file=sys.stderr)
            return 1
        tag = argv[3]
        try:
            removed = _global_remove(tag)
            print(f"Removed '{tag}' from global graph ({removed} nodes pruned).")
            return 0
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    elif subcmd == "list":
        repos = _global_list()
        if not repos:
            print("Global graph is empty. Use 'graph_fy global add' to add a project.")
        else:
            print(f"Global graph: {_global_path()}")
            for tag, info in repos.items():
                print(f"  {tag}: {info.get('node_count', '?')} nodes, added {info.get('added_at', '?')[:10]}")
        return 0
    elif subcmd == "path":
        print(_global_path())
        return 0
    else:
        print("Usage: graph_fy global [add|remove|list|path]", file=sys.stderr)
        return 1
