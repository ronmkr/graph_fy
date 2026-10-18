"""Path / trace command implementation for graph_fy CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import networkx as _nx
from networkx.readwrite import json_graph

from graph_fy import querylog
from graph_fy.build import edge_datas
from graph_fy.commands.hook_guard import _touch_query_stamp
from graph_fy.retrieval import get_canonical_search_graph
from graph_fy.serve import _pick_scored_endpoint, _score_nodes


def cmd_trace(
    argv: list[str],
    default_graph_path_fn: Callable[[], str],
    enforce_size_cap_fn: Callable[[Path], None],
) -> int:
    """Handle `graph_fy path` and `graph_fy trace` commands."""
    cmd = argv[1]
    raw_args = argv[2:]
    positional: list[str] = []
    graph_path = default_graph_path_fn()
    direction_flag: str | None = None
    weighted = True
    i = 0
    while i < len(raw_args):
        a = raw_args[i]
        if a == "--graph" and i + 1 < len(raw_args):
            graph_path = raw_args[i + 1]
            i += 2
        elif a.startswith("--graph="):
            graph_path = a.split("=", 1)[1]
            i += 1
        elif a in ("--root", "-r", "--dir", "-d") and i + 1 < len(raw_args):
            rdir = Path(raw_args[i + 1])
            graph_path = str(
                rdir / "graph_fy_out" / "graph.json"
                if (rdir / "graph_fy_out" / "graph.json").exists()
                else rdir / "graph.json"
            )
            i += 2
        elif a.startswith("--root=") or a.startswith("--dir="):
            rdir = Path(a.split("=", 1)[1])
            graph_path = str(
                rdir / "graph_fy_out" / "graph.json"
                if (rdir / "graph_fy_out" / "graph.json").exists()
                else rdir / "graph.json"
            )
            i += 1
        elif a == "--unweighted":
            weighted = False
            i += 1
        elif a == "--directed":
            if direction_flag == "undirected":
                print("error: --directed and --undirected are mutually exclusive", file=sys.stderr)
                return 1
            direction_flag = "directed"
            i += 1
        elif a == "--undirected":
            if direction_flag == "directed":
                print("error: --directed and --undirected are mutually exclusive", file=sys.stderr)
                return 1
            direction_flag = "undirected"
            i += 1
        elif not a.startswith("--"):
            positional.append(a)
            i += 1
        else:
            i += 1

    if len(positional) < 2:
        print(
            f'Usage: graph_fy {cmd} "<source>" "<target>" [--graph path] '
            "[--directed|--undirected] [--unweighted]",
            file=sys.stderr,
        )
        return 1
    source_label = positional[0]
    target_label = positional[1]
    undirected = direction_flag == "undirected"
    gp = Path(graph_path).resolve()
    if not gp.exists():
        print(f"error: graph file not found: {gp}", file=sys.stderr)
        return 1
    enforce_size_cap_fn(gp)
    _raw = json.loads(gp.read_text(encoding="utf-8"))
    if "links" not in _raw and "edges" in _raw:
        _raw = dict(_raw, links=_raw["edges"])
    _raw = {**_raw, "directed": True, "multigraph": True}
    try:
        G = json_graph.node_link_graph(_raw, edges="links")
    except TypeError:
        G = json_graph.node_link_graph(_raw)
    src_scored = _score_nodes(G, [t.lower() for t in source_label.split()])
    tgt_scored = _score_nodes(G, [t.lower() for t in target_label.split()])
    if not src_scored:
        print(f"No node matching '{source_label}' found.", file=sys.stderr)
        return 1
    if not tgt_scored:
        print(f"No node matching '{target_label}' found.", file=sys.stderr)
        return 1
    src_nid = _pick_scored_endpoint(G, src_scored, source_label)
    tgt_nid = _pick_scored_endpoint(G, tgt_scored, target_label)
    if src_nid == tgt_nid:
        print(
            f"'{source_label}' and '{target_label}' both resolved to the same "
            f"node '{src_nid}'. Use a more specific label or the exact node ID.",
            file=sys.stderr,
        )
        return 1
    for _name, _scored, _nid in (
        ("source", src_scored, src_nid),
        ("target", tgt_scored, tgt_nid),
    ):
        if len(_scored) >= 2 and _nid == _scored[0][1]:
            _top, _runner = _scored[0][0], _scored[1][0]
            if _top > 0 and (_top - _runner) / _top < 0.10:
                print(
                    f"warning: {_name} match was ambiguous "
                    f"(top score {_top:g}, runner-up {_runner:g})",
                    file=sys.stderr,
                )
    try:
        view = get_canonical_search_graph(G, undirected=undirected, weighted=weighted)
        weight_attr = "weight" if weighted else None
        path_nodes = _nx.shortest_path(view, src_nid, tgt_nid, weight=weight_attr)
    except (_nx.NetworkXNoPath, _nx.NodeNotFound):
        if undirected:
            print(f"No path found between '{source_label}' and '{target_label}'.")
        else:
            print(
                f"No directed path found between '{source_label}' and "
                f"'{target_label}'. Re-run with --undirected to search "
                "ignoring edge direction."
            )
        sys.exit(0)
    hops = len(path_nodes) - 1
    segments: list[str] = []
    for i in range(len(path_nodes) - 1):
        u, v = path_nodes[i], path_nodes[i + 1]
        fwd, bwd = [], []
        for a, b in ((u, v), (v, u)):
            if G.has_edge(a, b):
                for d in edge_datas(G, a, b):
                    (fwd if d.get("_src", a) == u else bwd).append(d)
        datas = fwd or bwd
        forward = bool(fwd)
        rels = sorted({d.get("relation") for d in datas if d.get("relation")})
        rel = "/".join(rels) if rels else "related"
        confs = sorted({d.get("confidence") for d in datas if d.get("confidence")})
        conf_str = f" [{'/'.join(confs)}]" if confs else ""
        if i == 0:
            segments.append(G.nodes[u].get("label", u))
        if forward:
            segments.append(f"--{rel}{conf_str}--> {G.nodes[v].get('label', v)}")
        else:
            segments.append(f"<--{rel}{conf_str}-- {G.nodes[v].get('label', v)}")
    print(f"Shortest path ({hops} hops):\n  " + " ".join(segments))
    querylog.log_query(
        kind="path",
        question=f"{source_label} -> {target_label}",
        corpus=str(gp),
        nodes_returned=hops,
    )
    _touch_query_stamp(gp)
    return 0
