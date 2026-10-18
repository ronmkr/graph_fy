"""Add/ingest command implementation for graph_fy CLI."""
from __future__ import annotations

import sys
from pathlib import Path

from graph_fy.ingest import ingest as _ingest


def cmd_add(argv: list[str]) -> int:
    """Handle `graph_fy add` command."""
    if len(argv) < 3:
        print(
            "Usage: graph_fy add <url> [--author Name] [--contributor Name] [--dir ./raw]",
            file=sys.stderr,
        )
        return 1

    url = argv[2]
    author: str | None = None
    contributor: str | None = None
    target_dir = Path("raw")
    args = argv[3:]
    i = 0
    while i < len(args):
        if args[i] == "--author" and i + 1 < len(args):
            author = args[i + 1]
            i += 2
        elif args[i] == "--contributor" and i + 1 < len(args):
            contributor = args[i + 1]
            i += 2
        elif args[i] == "--dir" and i + 1 < len(args):
            target_dir = Path(args[i + 1])
            i += 2
        else:
            i += 1
    try:
        saved = _ingest(url, target_dir, author=author, contributor=contributor)
        print(f"Saved to {saved}")
        print("Run /graph_fy --update in your AI assistant to update the graph.")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
