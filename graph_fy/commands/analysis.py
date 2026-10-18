"""Analysis command handlers (affected, impact, flow, god-nodes) for graph_fy."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_affected(argv: list[str], default_graph_path_fn, find_graph_path_fn) -> int:
    """Execute 'graph_fy affected' query."""
    if len(argv) < 3:
        print("Usage: graph_fy affected \"<node-or-label>\" [--relation R] [--depth N] [--graph path]", file=sys.stderr)
        return 1

    from graph_fy.affected import DEFAULT_AFFECTED_RELATIONS, format_affected, load_graph

    query = argv[2]
    graph_path = default_graph_path_fn()
    depth = 2
    relations: list[str] = []
    args = argv[3:]
    i = 0
    while i < len(args):
        if args[i] in ("--graph", "-g") and i + 1 < len(args):
            graph_path = args[i + 1]
            i += 2
        elif args[i].startswith("--graph="):
            graph_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--root", "-r", "--dir", "-d") and i + 1 < len(args):
            graph_path = str(find_graph_path_fn(args[i + 1]))
            i += 2
        elif args[i].startswith(("--root=", "--dir=")):
            graph_path = str(find_graph_path_fn(args[i].split("=", 1)[1]))
            i += 1
        elif args[i] == "--depth" and i + 1 < len(args):
            try:
                depth = int(args[i + 1])
            except ValueError:
                print("error: --depth must be an integer", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--depth="):
            try:
                depth = int(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --depth must be an integer", file=sys.stderr)
                return 1
            i += 1
        elif args[i] == "--relation" and i + 1 < len(args):
            relations.append(args[i + 1])
            i += 2
        elif args[i].startswith("--relation="):
            relations.append(args[i].split("=", 1)[1])
            i += 1
        else:
            i += 1

    gp = Path(graph_path).resolve()
    if not gp.exists():
        print(f"error: graph file not found: {gp}", file=sys.stderr)
        return 1
    if not gp.suffix == ".json":
        print("error: graph file must be a .json file", file=sys.stderr)
        return 1

    try:
        graph = load_graph(gp)
    except Exception as exc:
        print(f"error: could not load graph: {exc}", file=sys.stderr)
        return 1

    from graph_fy.paths import graph_fy_OUT_NAME
    graph_root = gp.parent.parent if gp.parent.name == graph_fy_OUT_NAME else gp.parent
    print(
        format_affected(
            graph,
            query,
            relations=relations or DEFAULT_AFFECTED_RELATIONS,
            depth=depth,
            root=graph_root,
        )
    )
    return 0


def cmd_impact(argv: list[str], default_graph_path_fn, find_graph_path_fn) -> int:
    """Execute 'graph_fy impact' and 'graph_fy blast' query."""
    if len(argv) < 3:
        print("Usage: graph_fy impact <target> [--depth N] [--json] [--terse] [--graph path]", file=sys.stderr)
        return 1

    from graph_fy.affected import load_graph
    from graph_fy.impact import compute_impact, format_impact_report

    target = argv[2]
    graph_path = default_graph_path_fn()
    depth = 10
    as_json = False
    terse = False
    args = argv[3:]
    i = 0
    while i < len(args):
        if args[i] in ("--graph", "-g") and i + 1 < len(args):
            graph_path = args[i + 1]
            i += 2
        elif args[i].startswith("--graph="):
            graph_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--root", "-r", "--dir", "-d") and i + 1 < len(args):
            graph_path = str(find_graph_path_fn(args[i + 1]))
            i += 2
        elif args[i].startswith(("--root=", "--dir=")):
            graph_path = str(find_graph_path_fn(args[i].split("=", 1)[1]))
            i += 1
        elif args[i] in ("--depth", "-d") and i + 1 < len(args):
            try:
                depth = int(args[i + 1])
            except ValueError:
                print("error: --depth must be an integer", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--depth="):
            try:
                depth = int(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --depth must be an integer", file=sys.stderr)
                return 1
            i += 1
        elif args[i] == "--json":
            as_json = True
            i += 1
        elif args[i] in ("--terse", "-t"):
            terse = True
            i += 1
        else:
            i += 1

    gp = Path(graph_path).resolve()
    if not gp.exists():
        print(f"error: graph file not found: {gp}", file=sys.stderr)
        return 1
    if not gp.suffix == ".json":
        print("error: graph file must be a .json file", file=sys.stderr)
        return 1

    try:
        graph = load_graph(gp)
    except Exception as exc:
        print(f"error: could not load graph: {exc}", file=sys.stderr)
        return 1

    from graph_fy.paths import graph_fy_OUT_NAME
    graph_root = gp.parent.parent if gp.parent.name == graph_fy_OUT_NAME else gp.parent
    impact_data = compute_impact(graph, target, max_depth=depth, root=graph_root)
    if as_json:
        print(json.dumps(impact_data, indent=2))
    else:
        print(format_impact_report(impact_data, terse=terse))
    return 0


def cmd_flow(argv: list[str], default_graph_path_fn, find_graph_path_fn) -> int:
    """Execute 'graph_fy flow' query."""
    if len(argv) < 3:
        print("Usage: graph_fy flow <entry_point> [--sink <sink>] [--json] [--graph <path>] [--max-depth N]", file=sys.stderr)
        return 1

    from graph_fy.affected import load_graph
    from graph_fy.flow import format_flow_diagram, trace_execution_flow

    entry_point = argv[2]
    sink: str | None = None
    as_json = False
    graph_path = default_graph_path_fn()
    max_depth = 15
    args = argv[3:]
    i = 0
    while i < len(args):
        if args[i] == "--sink" and i + 1 < len(args):
            sink = args[i + 1]
            i += 2
        elif args[i].startswith("--sink="):
            sink = args[i].split("=", 1)[1]
            i += 1
        elif args[i] == "--json":
            as_json = True
            i += 1
        elif args[i] in ("--graph", "-g") and i + 1 < len(args):
            graph_path = args[i + 1]
            i += 2
        elif args[i].startswith("--graph="):
            graph_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--root", "-r", "--dir", "-d") and i + 1 < len(args):
            graph_path = str(find_graph_path_fn(args[i + 1]))
            i += 2
        elif args[i].startswith(("--root=", "--dir=")):
            graph_path = str(find_graph_path_fn(args[i].split("=", 1)[1]))
            i += 1
        elif args[i] in ("--max-depth", "--depth") and i + 1 < len(args):
            try:
                max_depth = int(args[i + 1])
            except ValueError:
                print("error: --max-depth must be an integer", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--max-depth=") or args[i].startswith("--depth="):
            try:
                max_depth = int(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --max-depth must be an integer", file=sys.stderr)
                return 1
            i += 1
        else:
            i += 1

    gp = Path(graph_path).resolve()
    if not gp.exists():
        print(f"error: graph file not found: {gp}", file=sys.stderr)
        return 1
    if not gp.suffix == ".json":
        print("error: graph file must be a .json file", file=sys.stderr)
        return 1

    try:
        graph = load_graph(gp)
    except Exception as exc:
        print(f"error: could not load graph: {exc}", file=sys.stderr)
        return 1

    flow_data = trace_execution_flow(graph, entry_point=entry_point, sink=sink, max_depth=max_depth)
    if as_json:
        print(json.dumps(flow_data, indent=2))
    else:
        print(format_flow_diagram(flow_data))
    return 0


def cmd_god_nodes(argv: list[str], default_graph_path_fn, find_graph_path_fn) -> int:
    """Execute 'graph_fy god-nodes' query."""
    from graph_fy.affected import load_graph
    from graph_fy.analyze import god_nodes as _god_nodes
    from graph_fy.security import sanitize_label as _sanitize_label

    graph_path = default_graph_path_fn()
    top_n = 10
    gn_exclude_hubs: float | None = None
    as_json = "--json" in argv
    args = argv[2:]
    i = 0
    while i < len(args):
        if args[i] in ("--graph", "-g") and i + 1 < len(args):
            graph_path = args[i + 1]
            i += 2
        elif args[i].startswith("--graph="):
            graph_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--root", "-r", "--dir", "-d") and i + 1 < len(args):
            graph_path = str(find_graph_path_fn(args[i + 1]))
            i += 2
        elif args[i].startswith(("--root=", "--dir=")):
            graph_path = str(find_graph_path_fn(args[i].split("=", 1)[1]))
            i += 1
        elif args[i] == "--top" and i + 1 < len(args):
            try:
                top_n = int(args[i + 1])
            except ValueError:
                print("error: --top must be an integer", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--top="):
            try:
                top_n = int(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --top must be an integer", file=sys.stderr)
                return 1
            i += 1
        elif args[i] == "--exclude-hubs" and i + 1 < len(args):
            try:
                gn_exclude_hubs = float(args[i + 1])
            except ValueError:
                print("error: --exclude-hubs must be a number (percentile 0-100)", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--exclude-hubs="):
            try:
                gn_exclude_hubs = float(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --exclude-hubs must be a number (percentile 0-100)", file=sys.stderr)
                return 1
            i += 1
        else:
            i += 1

    gp = Path(graph_path).resolve()
    if not gp.exists():
        print(f"error: graph file not found: {gp}", file=sys.stderr)
        return 1
    if not gp.suffix == ".json":
        print("error: graph file must be a .json file", file=sys.stderr)
        return 1

    try:
        G = load_graph(gp)
    except Exception as exc:
        print(f"error: could not load graph: {exc}", file=sys.stderr)
        return 1

    gods = _god_nodes(G, top_n=top_n, exclude_hubs_percentile=gn_exclude_hubs)
    if as_json:
        print(json.dumps(gods, indent=2))
    else:
        print("God nodes (most connected):")
        for rank, n in enumerate(gods, 1):
            print(f"  {rank}. {_sanitize_label(str(n['label']))} - {n['degree']} edges")
    return 0


def format_graph_stats(G, communities, active_graph_path: str | Path | None = None) -> str:
    """Format graph stats summary matching CLI and MCP format."""
    confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
    total = len(confs) or 1
    lines = [
        f"Nodes: {G.number_of_nodes()}",
        f"Edges: {G.number_of_edges()}",
        f"Communities: {len(communities)}",
        f"EXTRACTED: {round(confs.count('EXTRACTED')/total*100)}%",
        f"INFERRED: {round(confs.count('INFERRED')/total*100)}%",
        f"AMBIGUOUS: {round(confs.count('AMBIGUOUS')/total*100)}%",
    ]
    built_commit = G.graph.get("built_at_commit")
    if built_commit:
        short_commit = built_commit[:8]
        if active_graph_path is not None:
            p = Path(active_graph_path).resolve()
            repo_root = p.parent.parent if p.parent.name == "graph_fy_out" else p.parent
            from graph_fy.export import _git_head
            head_commit = _git_head(repo_root)
            if head_commit:
                if head_commit.startswith(built_commit) or built_commit.startswith(head_commit):
                    lines.append(f"Built at commit: {short_commit} (current HEAD)")
                else:
                    lines.append(f"Built at commit: {short_commit} (stale - current HEAD is {head_commit[:8]})")
            else:
                lines.append(f"Built at commit: {short_commit}")
        else:
            lines.append(f"Built at commit: {short_commit}")
    return "\n".join(lines)


def cmd_stats(argv: list[str], default_graph_path_fn, find_graph_path_fn) -> int:
    """Execute 'graph_fy stats' command."""
    graph_path = default_graph_path_fn()
    as_json = False
    args = argv[2:]
    i = 0
    while i < len(args):
        if args[i] in ("--graph", "-g") and i + 1 < len(args):
            graph_path = args[i + 1]
            i += 2
        elif args[i].startswith("--graph="):
            graph_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--root", "-r", "--dir", "-d") and i + 1 < len(args):
            graph_path = str(find_graph_path_fn(args[i + 1]))
            i += 2
        elif args[i].startswith(("--root=", "--dir=")):
            graph_path = str(find_graph_path_fn(args[i].split("=", 1)[1]))
            i += 1
        elif args[i] == "--json":
            as_json = True
            i += 1
        elif not args[i].startswith("-"):
            graph_path = str(find_graph_path_fn(args[i]))
            i += 1
        else:
            i += 1

    gp = Path(graph_path).resolve()
    if not gp.exists():
        print(f"error: graph file not found: {gp}", file=sys.stderr)
        return 1

    from graph_fy.serve import _load_graph, _communities_from_graph
    try:
        G = _load_graph(str(gp))
        communities = _communities_from_graph(G)
    except Exception as exc:
        print(f"error: could not load graph: {exc}", file=sys.stderr)
        return 1

    if as_json:
        confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
        total = len(confs) or 1
        built_commit = G.graph.get("built_at_commit")
        repo_root = gp.parent.parent if gp.parent.name == "graph_fy_out" else gp.parent
        from graph_fy.export import _git_head
        head_commit = _git_head(repo_root) if built_commit else None
        is_stale = None
        if built_commit and head_commit:
            is_stale = not (head_commit.startswith(built_commit) or built_commit.startswith(head_commit))
        out = {
            "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(),
            "communities": len(communities),
            "confidence": {
                "extracted_pct": round(confs.count("EXTRACTED") / total * 100),
                "inferred_pct": round(confs.count("INFERRED") / total * 100),
                "ambiguous_pct": round(confs.count("AMBIGUOUS") / total * 100),
            },
            "built_at_commit": built_commit,
            "head_commit": head_commit,
            "is_stale": is_stale,
        }
        print(json.dumps(out, indent=2))
    else:
        print(format_graph_stats(G, communities, gp))
    return 0

