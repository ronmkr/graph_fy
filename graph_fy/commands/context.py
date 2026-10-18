"""Handler for 'graph_fy context' and 'graph_fy prebrief'."""
from __future__ import annotations

import json
import sys


def cmd_context(argv: list[str]) -> int:
    """Execute pre-agent analysis briefing generation."""
    from graph_fy.retrieval import task_context

    task_str = ""
    budget = 1500
    root_dir = "."
    graph_paths: list[str] = []
    db_path = None
    include_diagram = "--no-diagram" not in argv
    if any(arg in argv for arg in ("--include-bodies", "--hydrate-bodies", "--full-bodies")):
        include_bodies: bool | str = True
    elif "--no-bodies" in argv or "--no-hydrate" in argv:
        include_bodies = False
    else:
        include_bodies = "auto"

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
            for p in args[i + 1].split(","):
                if p.strip():
                    graph_paths.append(p.strip())
            i += 2
        elif args[i].startswith("--graph="):
            for p in args[i].split("=", 1)[1].split(","):
                if p.strip():
                    graph_paths.append(p.strip())
            i += 1
        elif args[i] in ("--multi-repo", "--workspace") and i + 1 < len(args):
            from graph_fy.multi_repo import find_workspace_graphs
            graph_paths.extend([str(p) for p in find_workspace_graphs(args[i + 1])])
            i += 2
        elif args[i].startswith("--multi-repo=") or args[i].startswith("--workspace="):
            from graph_fy.multi_repo import find_workspace_graphs
            m_dir = args[i].split("=", 1)[1]
            graph_paths.extend([str(p) for p in find_workspace_graphs(m_dir)])
            i += 1
        elif args[i] in ("--db",) and i + 1 < len(args):
            db_path = args[i + 1]
            i += 2
        elif args[i].startswith("--db="):
            db_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--task", "-t") and i + 1 < len(args):
            task_str = args[i + 1]
            i += 2
        elif args[i].startswith("--task="):
            task_str = args[i].split("=", 1)[1]
            i += 1
        elif not args[i].startswith("-"):
            if not task_str:
                task_str = args[i]
            else:
                task_str += " " + args[i]
            i += 1
        else:
            i += 1

    if not task_str and not sys.stdin.isatty():
        task_str = sys.stdin.read().strip()
    if not task_str:
        print("Usage: graph_fy context \"<task description>\" [--root dir] [--graph file] [--budget N] [--include-bodies] [--no-diagram] [--json]", file=sys.stderr)
        return 1

    if len(graph_paths) > 1:
        from graph_fy.multi_repo import resolve_multi_graphs
        G_multi = resolve_multi_graphs(graph_paths)
        res = task_context(
            task=task_str,
            graph=G_multi,
            root_dir=root_dir,
            db_path=db_path,
            token_budget=budget,
            include_diagram=include_diagram,
            include_bodies=include_bodies,
        )
    else:
        res = task_context(
            task=task_str,
            root_dir=root_dir,
            graph_path=graph_paths[0] if graph_paths else None,
            db_path=db_path,
            token_budget=budget,
            include_diagram=include_diagram,
            include_bodies=include_bodies,
        )

    if "--json" in argv:
        print(json.dumps(res, indent=2))
    else:
        print(res.get("prompt_context", ""))
    return 0
