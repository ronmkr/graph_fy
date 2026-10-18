"""Merge graphs command implementation for graph_fy CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _enforce_graph_size_cap_or_exit(gp: Path) -> None:
    from graph_fy.security import check_graph_file_size_cap
    try:
        check_graph_file_size_cap(gp)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_merge_graphs(argv: list[str], default_out_dir: str | Path) -> int:
    """Handle `graph_fy merge-graphs` command."""
    args = argv[2:]
    graph_paths: list[Path] = []
    out_path = Path(default_out_dir) / "merged-graph.json"
    i = 0
    while i < len(args):
        if args[i] == "--out" and i + 1 < len(args):
            out_path = Path(args[i + 1])
            i += 2
        else:
            graph_paths.append(Path(args[i]))
            i += 1
    if len(graph_paths) < 2:
        print(
            "Usage: graph_fy merge-graphs <graph1.json> <graph2.json> [...] [--out merged.json]",
            file=sys.stderr,
        )
        return 1

    import networkx as _nx
    from networkx.readwrite import json_graph as _jg
    from graph_fy.build import prefix_graph_for_global as _prefix, distinct_repo_tags as _repo_tags

    graphs = []
    for gp in graph_paths:
        if not gp.exists():
            print(f"error: not found: {gp}", file=sys.stderr)
            return 1
        _enforce_graph_size_cap_or_exit(gp)
        data = json.loads(gp.read_text(encoding="utf-8"))
        if "links" not in data and "edges" in data:
            data = dict(data, links=data["edges"])
        data = dict(
            data,
            links=[
                {
                    **link,
                    "_src": link.get("_src", link.get("source")),
                    "_tgt": link.get("_tgt", link.get("target")),
                }
                for link in data.get("links", [])
            ],
        )
        try:
            G = _jg.node_link_graph(data, edges="links")
        except TypeError:
            G = _jg.node_link_graph(data)
        if "hyperedges" not in G.graph and isinstance(data.get("hyperedges"), list):
            G.graph["hyperedges"] = data["hyperedges"]
        graphs.append(G)

    def _to_simple(g: _nx.Graph) -> _nx.Graph:
        if type(g) is not _nx.Graph:
            return _nx.Graph(g)
        return g

    repo_tags = _repo_tags(graph_paths)
    naive_tags = [gp.parent.parent.name for gp in graph_paths]
    if len(set(naive_tags)) != len(naive_tags):
        print(f"  note: repo dir names collide; using distinct tags: {', '.join(repo_tags)}")
    merged = _nx.Graph()
    collected_hyperedges: list = []
    community_offset = 0
    for G, repo_tag in zip(graphs, repo_tags):
        prefixed = _to_simple(_prefix(G, repo_tag, community_offset=community_offset))
        hes = prefixed.graph.get("hyperedges")
        if isinstance(hes, list):
            collected_hyperedges.extend(h for h in hes if isinstance(h, dict))
        cids = [
            d["community"]
            for _, d in prefixed.nodes(data=True)
            if isinstance(d.get("community"), int)
        ]
        if cids:
            community_offset = max(community_offset, max(cids) + 1)
        merged = _nx.compose(merged, prefixed)

    from graph_fy.cross_repo_types import link_shared_type_declarations as _link_shared
    shared_links = _link_shared(merged)
    if shared_links:
        print(f"  linked {shared_links} type declaration(s) shared across repos")

    from graph_fy.cross_repo_calls import link_cross_repo_member_calls as _link_calls
    call_links = _link_calls(merged)
    if call_links:
        print(f"  resolved {call_links} member call(s) across repos")

    merged.graph.pop("hyperedges", None)
    if collected_hyperedges:
        from graph_fy.export import attach_hyperedges as _attach
        _attach(merged, collected_hyperedges)
    try:
        out_data = _jg.node_link_data(merged, edges="links")
    except TypeError:
        out_data = _jg.node_link_data(merged)

    for link in out_data.get("links", []):
        tsrc = link.pop("_src", None)
        ttgt = link.pop("_tgt", None)
        if tsrc is not None and ttgt is not None:
            link["source"] = tsrc
            link["target"] = ttgt

    out_data["hyperedges"] = merged.graph.get("hyperedges", [])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from graph_fy.paths import write_json_atomic as _wja
    _wja(out_path, out_data, indent=2)
    print(f"Merged {len(graphs)} graphs -> {merged.number_of_nodes()} nodes, {merged.number_of_edges()} edges")
    print(f"Written to: {out_path}")
    return 0


def cmd_merge_driver(argv: list[str]) -> int:
    """Handle `graph_fy merge-driver` command."""
    if len(argv) < 5:
        print("Usage: graph_fy merge-driver <base> <current> <other>", file=sys.stderr)
        return 1
    _base_path, _current_path, _other_path = argv[2], argv[3], argv[4]
    _MERGE_MAX_BYTES = 50 * 1024 * 1024
    _MERGE_MAX_NODES = 100_000
    import networkx as _nx
    from networkx.readwrite import json_graph as _jg

    def _load_graph(p: str):
        path_obj = Path(p)
        try:
            size = path_obj.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"cannot stat {p}: {exc}") from exc
        if size > _MERGE_MAX_BYTES:
            raise RuntimeError(
                f"graph.json {p} is {size} bytes, exceeds {_MERGE_MAX_BYTES}-byte cap"
            )
        data = json.loads(path_obj.read_text(encoding="utf-8"))
        from graph_fy.paths import load_node_link_graph as _lnlg
        return _lnlg(data), data

    try:
        G_cur, _ = _load_graph(_current_path)
        G_oth, _ = _load_graph(_other_path)
    except Exception as exc:
        print(f"[graph_fy merge-driver] error loading graphs: {exc}", file=sys.stderr)
        return 1
    merged = _nx.compose(G_cur, G_oth)
    if merged.number_of_nodes() > _MERGE_MAX_NODES:
        print(
            f"[graph_fy merge-driver] merged graph has {merged.number_of_nodes()} nodes, "
            f"exceeds {_MERGE_MAX_NODES}-node cap; aborting merge.",
            file=sys.stderr,
        )
        return 1
    try:
        out_data = _jg.node_link_data(merged, edges="links")
    except TypeError:
        out_data = _jg.node_link_data(merged)
    from graph_fy.paths import write_json_atomic
    write_json_atomic(_current_path, out_data, indent=2)
    return 0

