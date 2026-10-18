"""Tree HTML command implementation for graph_fy CLI."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional as _Opt

from graph_fy.tree_html import DEFAULT_MAX_CHILDREN, write_tree_html


def cmd_tree(
    argv: list[str],
    graph_fy_out: str | Path,
    enforce_size_cap_fn: Callable[[Path], None],
) -> int:
    """Handle `graph_fy tree` command."""
    graph_path = Path(graph_fy_out) / "graph.json"
    output_path: "_Opt[Path]" = None
    root: "_Opt[str]" = None
    max_children = DEFAULT_MAX_CHILDREN
    top_k_edges = 0
    project_label: "_Opt[str]" = None
    args = argv[2:]
    i_arg = 0
    while i_arg < len(args):
        a = args[i_arg]
        if a == "--graph" and i_arg + 1 < len(args):
            graph_path = Path(args[i_arg + 1])
            i_arg += 2
        elif a == "--output" and i_arg + 1 < len(args):
            output_path = Path(args[i_arg + 1])
            i_arg += 2
        elif a == "--root" and i_arg + 1 < len(args):
            root = args[i_arg + 1]
            i_arg += 2
        elif a == "--max-children" and i_arg + 1 < len(args):
            max_children = int(args[i_arg + 1])
            i_arg += 2
        elif a == "--top-k-edges" and i_arg + 1 < len(args):
            top_k_edges = int(args[i_arg + 1])
            i_arg += 2
        elif a == "--label" and i_arg + 1 < len(args):
            project_label = args[i_arg + 1]
            i_arg += 2
        elif a in ("-h", "--help"):
            print("Usage: graph_fy tree [--graph PATH] [--output HTML]")
            print("  --graph PATH         path to graph.json (default graph_fy_out/graph.json)")
            print("  --output HTML        output path (default graph_fy_out/GRAPH_TREE.html)")
            print("  --root PATH          filesystem root (default: longest common dir of all source_files)")
            print("  --max-children N     cap visible children per node (default 200)")
            print("  --top-k-edges N      pre-compute top-K outbound edges per symbol (default 12)")
            print("  --label NAME         project label shown in the page header")
            return 0
        else:
            i_arg += 1

    if not graph_path.is_file():
        print(f"error: graph.json not found at {graph_path}", file=sys.stderr)
        return 1

    enforce_size_cap_fn(graph_path)
    if output_path is None:
        output_path = graph_path.parent / "GRAPH_TREE.html"

    try:
        out = write_tree_html(
            graph_path=graph_path,
            output_path=output_path,
            root=root,
            max_children=max_children,
            top_k_edges=top_k_edges,
            project_label=project_label,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    size_kb = out.stat().st_size / 1024
    print(f"wrote {out} ({size_kb:.1f} KB)")
    print(f"open with: xdg-open {out}  (or file://{out.resolve()})")
    return 0
