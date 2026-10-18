"""Graph comparison and shrink-guard utilities for graph_fy build/watch."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _node_community_map(graph_data: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for node in graph_data.get("nodes", []):
        node_id = node.get("id")
        cid = node.get("community")
        if node_id is None or cid is None:
            continue
        try:
            out[str(node_id)] = int(cid)
        except (TypeError, ValueError):
            print(
                f"[graph_fy watch] Skipping node with invalid community id: "
                f"node_id={node_id!r} community={cid!r}",
                file=sys.stderr,
            )
            continue
    return out


def _canonical_graph_for_compare(graph_data: dict) -> dict:
    canonical = dict(graph_data)
    canonical.pop("built_at_commit", None)
    canonical["directed"] = bool(canonical.get("directed", False))
    for key in ("nodes", "links", "edges", "hyperedges"):
        if key in canonical and isinstance(canonical[key], list):
            canonical[key] = sorted(
                canonical[key],
                key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
            )
    return canonical


def _canonical_topology_for_compare(graph_data: dict) -> dict:
    canonical = dict(graph_data)
    canonical.pop("built_at_commit", None)

    nodes = canonical.get("nodes")
    if isinstance(nodes, list):
        norm_nodes = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            n = dict(node)
            n.pop("community", None)
            n.pop("community_name", None)
            n.pop("norm_label", None)
            norm_nodes.append(n)
        canonical["nodes"] = sorted(
            norm_nodes,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )

    for key in ("links", "edges"):
        items = canonical.get(key)
        if not isinstance(items, list):
            continue
        norm_edges = []
        for edge in items:
            if not isinstance(edge, dict):
                continue
            e = dict(edge)
            true_src = e.pop("_src", None)
            true_tgt = e.pop("_tgt", None)
            if true_src is not None and true_tgt is not None:
                e["source"] = true_src
                e["target"] = true_tgt
            e.pop("confidence_score", None)
            norm_edges.append(e)
        canonical[key] = sorted(
            norm_edges,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )

    hyperedges = canonical.get("hyperedges")
    if isinstance(hyperedges, list):
        canonical["hyperedges"] = sorted(
            hyperedges,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )

    return canonical


def _topology_from_graph(G) -> dict:
    from networkx.readwrite import json_graph
    try:
        data = json_graph.node_link_data(G, edges="links")
    except TypeError:
        data = json_graph.node_link_data(G)
    data["hyperedges"] = getattr(G, "graph", {}).get("hyperedges", [])
    return data


def _check_shrink(
    force: bool,
    existing_data: dict,
    new_data: dict,
    tmp: Path | None = None,
    *,
    had_explicit_deletions: bool = False,
    rebuilt_sources: set[str] | None = None,
    failed_sources: set[str] | None = None,
) -> bool:
    """Return True (ok to proceed) or False (shrink refused)."""
    if force or not existing_data:
        return True
    if had_explicit_deletions and rebuilt_sources is None:
        return True
    existing_nodes = existing_data.get("nodes", [])
    new_nodes = new_data.get("nodes", [])
    if len(new_nodes) >= len(existing_nodes):
        return True
    if rebuilt_sources is not None:
        from graph_fy.build import _norm_source_file
        new_ids = {n.get("id") for n in new_nodes}
        lost = [n for n in existing_nodes if n.get("id") not in new_ids]

        def _accounted(n: dict) -> bool:
            sf = n.get("source_file")
            if sf and failed_sources and _norm_source_file(sf) in failed_sources:
                return False
            return (not sf
                    or sf in rebuilt_sources
                    or _norm_source_file(sf) in rebuilt_sources)
        if all(_accounted(n) for n in lost):
            return True
    if tmp is not None:
        tmp.unlink(missing_ok=True)
    print(
        f"[graph_fy] WARNING: new graph has {len(new_nodes)} nodes but existing "
        f"graph.json has {len(existing_nodes)}. Refusing to overwrite — you may be "
        f"missing chunk files from a previous session. "
        f"Pass --force to override.",
        file=sys.stderr,
    )
    return False


def _report_for_compare(report_text: str) -> str:
    return re.sub(r"^- Built from commit: `[^`]+`\n?", "", report_text, flags=re.MULTILINE)


def _json_text(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
