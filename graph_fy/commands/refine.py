"""Handler for 'graph_fy refine'."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_refine(argv: list[str]) -> int:
    """Execute iterative query refinement from draft code blocks."""
    from graph_fy.retrieval import refine_query

    code_draft = ""
    budget = 2000

    args = argv[2:]
    i = 0
    while i < len(args):
        if args[i] in ("--code", "-c") and i + 1 < len(args):
            code_draft = args[i + 1]
            i += 2
        elif args[i].startswith("--code="):
            code_draft = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--file", "-f") and i + 1 < len(args):
            fpath = Path(args[i + 1])
            if fpath.is_file():
                code_draft = fpath.read_text(encoding="utf-8", errors="replace")
            i += 2
        elif args[i].startswith("--file="):
            fpath = Path(args[i].split("=", 1)[1])
            if fpath.is_file():
                code_draft = fpath.read_text(encoding="utf-8", errors="replace")
            i += 1
        elif args[i] in ("--budget", "-b") and i + 1 < len(args):
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
        else:
            i += 1

    if not code_draft and not sys.stdin.isatty():
        code_draft = sys.stdin.read()
    if not code_draft:
        print("Usage: graph_fy refine --code \"<draft>\" | --file <path> [--budget N]", file=sys.stderr)
        return 1

    res = refine_query(code_draft=code_draft, token_budget=budget)
    if "--json" in argv:
        print(json.dumps(res, indent=2))
    else:
        print(res.get("prompt_context", ""))
    return 0
