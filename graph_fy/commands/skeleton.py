"""Single-file AST skeleton command for graph_fy CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from graph_fy.fast_query import skeletonize_single_file


def cmd_skeleton(argv: list[str]) -> int:
    """Handle `graph_fy skeleton <file>` subcommand."""
    if len(argv) < 3:
        print(
            "Usage: graph_fy skeleton <file> [--symbol <name>] [--budget <N>] [--json]",
            file=sys.stderr,
        )
        return 1

    target_file = argv[2]
    if target_file.startswith("-"):
        print("error: first argument must be a file path", file=sys.stderr)
        return 1

    symbol_name: str | None = None
    token_budget: int | None = None
    as_json = "--json" in argv

    args = argv[3:]
    i = 0
    while i < len(args):
        if args[i] == "--symbol" and i + 1 < len(args):
            symbol_name = args[i + 1]
            i += 2
        elif args[i].startswith("--symbol="):
            symbol_name = args[i].split("=", 1)[1]
            i += 1
        elif args[i] == "--budget" and i + 1 < len(args):
            try:
                token_budget = int(args[i + 1])
            except ValueError:
                print("error: --budget must be an integer", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--budget="):
            try:
                token_budget = int(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --budget must be an integer", file=sys.stderr)
                return 1
            i += 1
        elif args[i] == "--json":
            as_json = True
            i += 1
        else:
            i += 1

    res = skeletonize_single_file(target_file, symbol_name=symbol_name, token_budget=token_budget)

    if as_json:
        print(json.dumps(res, indent=2))
        return 0

    if "error" in res and not res.get("skeleton"):
        print(f"error: {res['error']}", file=sys.stderr)
        return 1

    print(res["skeleton"])
    return 0
