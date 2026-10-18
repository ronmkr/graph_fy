"""Handler for 'graph_fy diff'."""
from __future__ import annotations

import json
import sys


def cmd_diff(argv: list[str]) -> int:
    """Execute git diff-scoped retrieval and blast radius assessment."""
    from graph_fy.retrieval import diff_context

    base_ref = "HEAD"
    budget = 2000
    root_dir = "."
    graph_path = None
    fmt = "default"

    args = argv[2:]
    i = 0
    while i < len(args):
        if args[i] in ("--budget", "-b") and i + 1 < len(args):
            try:
                budget = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif args[i].startswith("--budget="):
            try:
                budget = int(args[i].split("=", 1)[1])
            except ValueError:
                pass
            i += 1
        elif args[i] in ("--root", "-r", "--dir", "-d") and i + 1 < len(args):
            root_dir = args[i + 1]
            i += 2
        elif args[i].startswith("--root=") or args[i].startswith("--dir="):
            root_dir = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--graph", "-g") and i + 1 < len(args):
            graph_path = args[i + 1]
            i += 2
        elif args[i].startswith("--graph="):
            graph_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--github-pr", "--pr"):
            fmt = "github-pr"
            i += 1
        elif args[i].startswith("--format="):
            fmt = args[i].split("=", 1)[1]
            i += 1
        elif not args[i].startswith("-"):
            base_ref = args[i]
            i += 1
        else:
            i += 1

    res = diff_context(base_ref=base_ref, root_dir=root_dir, token_budget=budget, graph_path=graph_path, format=fmt)
    if "--json" in argv:
        print(json.dumps(res, indent=2))
    else:
        print(res.get("prompt_context", ""))
    return 0
