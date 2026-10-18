"""Multi-repo and multi-graph resolution, parallel query execution, and whole-entity architecture synthesis.

Provides:
  - resolve_multi_graphs: Composes multiple per-repo graph.json files into a unified cross-repo graph
  - find_workspace_graphs: Discovers graph.json files across sub-repositories in a workspace
  - query_parallel: Concurrently executes multiple graph queries across worker threads
  - synthesize_entity_architecture: Extracts induced subgraphs across specified entities to generate
    Mermaid architecture diagrams, interaction matrices, and AST skeletons
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import networkx as nx
from networkx.readwrite import json_graph as _jg

from graph_fy.build import (
    distinct_repo_tags,
    prefix_graph_for_global,
)
from graph_fy.cross_repo_calls import link_cross_repo_member_calls
from graph_fy.cross_repo_types import link_shared_type_declarations
from graph_fy.security import check_graph_file_size_cap


def _load_single_graph(path: Path) -> nx.Graph:
    """Load a single graph.json file safely preserving edge directions."""
    check_graph_file_size_cap(path)
    data = json.loads(path.read_text(encoding="utf-8"))
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
    if type(G) is not nx.Graph:
        G = nx.Graph(G)
    return G


def resolve_multi_graphs(graph_paths: list[Path | str]) -> nx.Graph:
    """Compose multiple per-repo graph.json files into a single cross-repo knowledge graph.

    Nodes are disambiguated by distinct repo tags, community offsets are partitioned,
    and shared contracts (types, member calls) are linked across repo boundaries.
    """
    resolved_paths = [Path(p).resolve() for p in graph_paths if Path(p).exists()]
    if not resolved_paths:
        return nx.Graph()
    if len(resolved_paths) == 1:
        return _load_single_graph(resolved_paths[0])

    graphs = [_load_single_graph(p) for p in resolved_paths]
    repo_tags = distinct_repo_tags(resolved_paths)

    merged = nx.Graph()
    community_offset = 0
    collected_hyperedges: list[dict[str, Any]] = []

    for G, repo_tag in zip(graphs, repo_tags):
        prefixed = nx.Graph(prefix_graph_for_global(G, repo_tag, community_offset=community_offset))
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
        merged = nx.compose(merged, prefixed)

    # Link shared cross-repo type declarations & calls
    link_shared_type_declarations(merged)
    link_cross_repo_member_calls(merged)

    if collected_hyperedges:
        merged.graph["hyperedges"] = collected_hyperedges

    return merged


def find_workspace_graphs(root: Path | str) -> list[Path]:
    """Discover graph.json files across sub-repositories in a workspace."""
    root_path = Path(root).resolve()
    found: list[Path] = []
    # Direct child
    default_out = root_path / "graph_fy_out" / "graph.json"
    if default_out.exists():
        found.append(default_out)

    # Sibling child directories
    try:
        for child in sorted(root_path.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")) or child.name in ("node_modules", "dist", "build", "venv", ".venv"):
                continue
            child_out = child / "graph_fy_out" / "graph.json"
            if child_out.exists() and child_out not in found:
                found.append(child_out)
            elif (child / "graph.json").exists() and (child / "graph.json") not in found:
                found.append(child / "graph.json")
    except Exception:
        pass
    return found


def query_parallel(
    queries: list[str],
    graph: nx.Graph | None = None,
    graph_path: str | Path | None = None,
    db_path: str | Path | None = None,
    max_workers: int = 4,
    limit: int = 10,
    hops: int = 2,
    budget: int = 2000,
    use_skeleton: bool = True,
) -> dict[str, Any]:
    """Execute multiple queries concurrently across worker threads."""
    from graph_fy.retrieval import query as _retrieval_query

    clean_queries = [q.strip() for q in queries if q and q.strip()]
    if not clean_queries:
        return {"queries": [], "count": 0, "total_wall_time_s": 0.0}

    resolved_gp = Path(graph_path).resolve() if graph_path else None
    resolved_db = Path(db_path).resolve() if db_path else None
    if resolved_db is None and resolved_gp:
        if (resolved_gp.parent / "graph.db").exists():
            resolved_db = resolved_gp.parent / "graph.db"
        elif (resolved_gp.parent / "index.db").exists():
            resolved_db = resolved_gp.parent / "index.db"

    worker_count = min(max_workers, len(clean_queries), 8)
    results: list[dict[str, Any]] = []
    start_time = time.perf_counter()

    def _worker(q_text: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            hits = _retrieval_query(
                query_text=q_text,
                graph=graph,
                db_path=resolved_db,
                limit=limit,
                hops=hops,
                graph_path=resolved_gp,
                skeleton=use_skeleton,
            )
            elapsed = time.perf_counter() - t0
            hit_list = hits if isinstance(hits, list) else []
            return {
                "query": q_text,
                "hits_count": len(hit_list),
                "hits": hit_list,
                "elapsed_s": round(elapsed, 4),
                "success": True,
            }
        except Exception as exc:
            return {
                "query": q_text,
                "hits_count": 0,
                "hits": [],
                "elapsed_s": round(time.perf_counter() - t0, 4),
                "success": False,
                "error": str(exc),
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {executor.submit(_worker, q): q for q in clean_queries}
        for future in concurrent.futures.as_completed(future_map):
            results.append(future.result())

    total_wall_time = round(time.perf_counter() - start_time, 4)
    # Sort results to match original query order
    order = {q: idx for idx, q in enumerate(clean_queries)}
    results.sort(key=lambda r: order.get(r["query"], 0))

    return {
        "queries": results,
        "count": len(results),
        "total_wall_time_s": total_wall_time,
        "concurrency": worker_count,
    }


def synthesize_entity_architecture(
    entities: list[str],
    graph: nx.Graph,
    max_hops: int = 1,
) -> dict[str, Any]:
    """Extract induced architecture between specified entities and emit a unified Mermaid diagram.

    Discovers matching nodes, builds the connecting execution highway, and computes
    an entity-to-entity interaction matrix.
    """
    clean_entities = [e.strip() for e in entities if e and e.strip()]
    if not clean_entities or len(graph) == 0:
        return {"entities": [], "mermaid": "graph TD\n", "matrix": {}, "edges_count": 0}

    # 1. Match entity labels to node IDs
    matched_nodes: dict[str, list[str]] = defaultdict(list)
    for node, data in graph.nodes(data=True):
        label = str(data.get("label") or "")
        bare_label = label.lstrip(".").rstrip("()")
        for target in clean_entities:
            target_clean = target.lstrip(".").rstrip("()")
            if (
                target_clean.lower() == bare_label.lower()
                or (len(target_clean) >= 4 and target_clean.lower() in bare_label.lower())
            ):
                matched_nodes[target].append(node)

    # Pick the highest-degree representative node for each target entity
    entity_node_map: dict[str, str] = {}
    entity_metadata: list[dict[str, Any]] = []

    for entity, nodes in matched_nodes.items():
        if not nodes:
            continue
        best_node = max(nodes, key=lambda n: graph.degree(n))
        entity_node_map[entity] = best_node
        d = graph.nodes[best_node]
        entity_metadata.append({
            "entity": entity,
            "node_id": best_node,
            "label": d.get("label", best_node),
            "source_file": d.get("source_file", ""),
            "source_location": d.get("source_location", ""),
            "type": d.get("node_type", "symbol"),
            "degree": graph.degree(best_node),
            "community": d.get("community"),
            "repo": d.get("repo", "root"),
        })

    selected_nodes = set(entity_node_map.values())
    if not selected_nodes:
        return {"entities": [], "mermaid": "graph TD\n", "matrix": {}, "subgraph_nodes_count": 0, "subgraph_edges_count": 0}

    # 2. Extract connecting subgraph (direct edges + 1-hop connecting bridges)
    subgraph_nodes = set(selected_nodes)
    if max_hops >= 1:
        for n in selected_nodes:
            for neighbor in graph.neighbors(n):
                # Include neighbor if it bridges between at least two selected entities
                neighbor_links = set(graph.neighbors(neighbor)).intersection(selected_nodes)
                if len(neighbor_links) >= 2 or neighbor in selected_nodes:
                    subgraph_nodes.add(neighbor)

    induced = graph.subgraph(subgraph_nodes)

    # 3. Generate Mermaid Architectural Flow Diagram
    mermaid_lines = ["flowchart TD"]
    node_var_map: dict[str, str] = {}
    for idx, n in enumerate(induced.nodes()):
        var_id = f"node_{idx}"
        node_var_map[n] = var_id
        label = graph.nodes[n].get("label", n)
        # Highlight primary entity nodes
        if n in selected_nodes:
            mermaid_lines.append(f'    {var_id}["<b>{label}</b>"]')
        else:
            mermaid_lines.append(f'    {var_id}["{label}"]')

    # Edge tracking to avoid duplicate visual links
    seen_edges: set[tuple[str, str]] = set()
    interaction_matrix: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    direct_edges_count = 0

    for u, v, data in induced.edges(data=True):
        u_var = node_var_map.get(u)
        v_var = node_var_map.get(v)
        if not u_var or not v_var:
            continue
        edge_key = (u, v) if u <= v else (v, u)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        rel = str(data.get("relation") or "rel")
        # Visual edge styling
        mermaid_lines.append(f"    {u_var} -->|{rel}| {v_var}")
        direct_edges_count += 1

        # Check entity-to-entity relations
        u_entity = next((e for e, nid in entity_node_map.items() if nid == u), None)
        v_entity = next((e for e, nid in entity_node_map.items() if nid == v), None)
        if u_entity and v_entity:
            interaction_matrix[u_entity][v_entity].append(rel)

    mermaid_diagram = "\n".join(mermaid_lines)

    return {
        "entities": entity_metadata,
        "mermaid": mermaid_diagram,
        "matrix": {k: dict(v) for k, v in interaction_matrix.items()},
        "subgraph_nodes_count": len(induced),
        "subgraph_edges_count": direct_edges_count,
    }
