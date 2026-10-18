"""Fully local lexical + graph-augmented retrieval layer using SQLite FTS5 (BM25) and NetworkX.

No LLM calls, no embedding models, zero external network requests.
Provides:
  - build_index(G, db_path): builds SQLite FTS5 index and precomputes static graph metrics (PageRank, degree)
  - query(query_text, graph, db_path, limit, hops): 4-step GraphRAG retrieval (Seed -> Expand -> Re-rank -> Return)
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import networkx as nx
from rapidfuzz import distance, process

from graph_fy.paths import graph_fy_OUT, out_path


from graph_fy.retrieval_index import (
    build_index,
    compute_pagerank,
    expand_subtokens,
    fuzzy_match_term,
    fuzzy_match_tokens,
    get_default_index_path,
    get_indexed_terms,
    max_allowed_distance,
    split_subtokens,
    _clean_fts_query,
    _extract_terms_from_text,
    _is_relational_graph_db,
    _prepare_node_document,
)
from graph_fy.retrieval_format import (
    format_compact_path,
    format_query_results,
    format_rag_prompt,
    retrieve,
    _format_source_location,
)
from graph_fy.retrieval_context import (
    diff_context,
    refine_query,
    suggest_close_symbols,
    task_context,
    _KEYWORDS_AND_BUILTINS,
)


def _find_callers_and_callees(G: nx.Graph, nid: Any) -> tuple[list[str], list[str]]:
    """Find immediate callers (inbound calls) and callees (outbound calls) for a node."""
    callers: list[str] = []
    callees: list[str] = []

    def _get_edges(u: Any, v: Any) -> list[dict]:
        edata = G.get_edge_data(u, v) or {}
        if isinstance(edata, dict):
            if any(isinstance(k, int) for k in edata.keys()):
                return [val for val in edata.values() if isinstance(val, dict)]
            return [edata]
        return []

    # 1. Directed graph: predecessors (callers) and successors (callees)
    if hasattr(G, "predecessors"):
        for pred in G.predecessors(nid):
            for ed in _get_edges(pred, nid):
                rel = str(ed.get("relation", "")).lower()
                if rel in ("calls", "call", "invokes"):
                    lbl = str(G.nodes[pred].get("label") or pred)
                    if lbl not in callers:
                        callers.append(lbl)
    if hasattr(G, "successors"):
        for succ in G.successors(nid):
            for ed in _get_edges(nid, succ):
                rel = str(ed.get("relation", "")).lower()
                if rel in ("calls", "call", "invokes"):
                    lbl = str(G.nodes[succ].get("label") or succ)
                    if lbl not in callees:
                        callees.append(lbl)

    # 2. Undirected graph: inspect neighbors and edge source/target
    if hasattr(G, "neighbors") and not G.is_directed():
        for nb in G.neighbors(nid):
            for ed in _get_edges(nid, nb):
                rel = str(ed.get("relation", "")).lower()
                if rel in ("calls", "call", "invokes"):
                    lbl = str(G.nodes[nb].get("label") or nb)
                    src = ed.get("source")
                    tgt = ed.get("target")
                    if src == nb or tgt == nid:
                        if lbl not in callers:
                            callers.append(lbl)
                    elif src == nid or tgt == nb:
                        if lbl not in callees:
                            callees.append(lbl)
                    else:
                        if lbl not in callers:
                            callers.append(lbl)

    return callers, callees


def query(
    query_text: str,
    graph: nx.Graph | None = None,
    db_path: Path | str | None = None,
    *,
    limit: int = 10,
    hops: int = 2,
    graph_path: Path | str | None = None,
    synonyms: dict[str, str] | None = None,
    fuzzy: bool = True,
    fuzzy_min_results: int = 1,
    terse: bool = False,
    caveman: bool = False,
    skeleton: bool = False,
    boundary_pruning: bool = True,
    root: Path | str | None = None,
) -> list[dict[str, Any]] | str:
    """Execute 4-step GraphRAG retrieval over the SQLite FTS5 index and graph.

    Steps:
      1. Seed: FTS5 BM25 search (with optional typo-tolerant fuzzy expansion) -> top matching seed nodes
      2. Expand: traverse graph outward up to `hops` from each seed
      3. Re-rank: combined score = BM25 + proximity discount + centrality bonus
      4. Return: ranked list of { node_id, score, text, source_path, connected_via }
    """
    if not query_text or not query_text.strip():
        return []

    # Resolve database path relative to root
    eff_root = Path(root) if root is not None else Path(".")
    if db_path is not None:
        idx_path = Path(db_path)
    elif graph_path is not None and (Path(graph_path).parent / "graph.db").exists():
        idx_path = Path(graph_path).parent / "graph.db"
    elif graph_path is not None and (Path(graph_path).parent / "index.db").exists():
        idx_path = Path(graph_path).parent / "index.db"
    elif (eff_root / "graph_fy_out" / "graph.db").exists():
        idx_path = eff_root / "graph_fy_out" / "graph.db"
    elif (eff_root / "graph_fy_out" / "index.db").exists():
        idx_path = eff_root / "graph_fy_out" / "index.db"
    elif (eff_root / "graph.db").exists():
        idx_path = eff_root / "graph.db"
    elif (eff_root / "index.db").exists():
        idx_path = eff_root / "index.db"
    else:
        idx_path = get_default_index_path(eff_root)

    if synonyms is None:
        try:
            from graph_fy.labeling import load_aliases_config
            root_cand = idx_path.parent.parent if idx_path.parent.name in ("graph_fy_out", "graph_fy_out") else eff_root
            synonyms = load_aliases_config(root_cand)
        except Exception:
            synonyms = None

    # Load graph if not provided
    if graph is None:
        if graph_path is not None:
            gp = Path(graph_path)
        elif (eff_root / "graph_fy_out" / "graph.json").exists():
            gp = eff_root / "graph_fy_out" / "graph.json"
        elif (eff_root / "graph.json").exists():
            gp = eff_root / "graph.json"
        else:
            gp = out_path("graph.json")

        if gp.exists():
            try:
                from graph_fy.paths import load_node_link_graph
                graph = load_node_link_graph(gp)
            except Exception:
                graph = nx.Graph()
        elif idx_path.exists() and _is_relational_graph_db(idx_path):
            try:
                from graph_fy.storage_sqlite import SqliteGraphStore
                with SqliteGraphStore(idx_path) as store:
                    graph = store.to_networkx()
            except Exception:
                graph = nx.Graph()
        else:
            graph = nx.Graph()

    # If FTS index does not exist or has not been populated, build it now
    needs_build = not idx_path.exists()
    if not needs_build and _is_relational_graph_db(idx_path):
        try:
            with sqlite3.connect(str(idx_path), timeout=2.0) as _chk:
                _c = _chk.cursor()
                _c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts';")
                if not _c.fetchone():
                    needs_build = True
                else:
                    _c.execute("SELECT 1 FROM nodes_fts LIMIT 1;")
                    if not _c.fetchone():
                        needs_build = True
        except Exception:
            pass

    if needs_build:
        if len(graph) == 0:
            return []
        build_index(graph, idx_path)

    conn = sqlite3.connect(str(idx_path), timeout=30.0)
    try:
        cur = conn.cursor()
        seed_limit = max(limit * 2, 20)

        vocab: set[str] | None = None
        fuzzy_expansions: dict[str, list[str]] | None = None

        if fuzzy:
            vocab = get_indexed_terms(conn)
            raw_q_tokens = [t.strip("-") for t in re.findall(r"[A-Za-z0-9_\-]+", query_text) if t.strip("-")]
            q_tokens: list[str] = []
            for qt in raw_q_tokens:
                q_tokens.extend(split_subtokens(qt))
            q_tokens = list(dict.fromkeys(q_tokens))
            token_expansions = fuzzy_match_tokens(q_tokens, vocab)
            if token_expansions:
                fuzzy_expansions = token_expansions

        clean_q = _clean_fts_query(query_text, synonyms=synonyms, fuzzy_expansions=fuzzy_expansions)
        if not clean_q:
            return []

        # Step 1: Seed step via BM25 with column weights:
        # title=5.0, headings=3.0, tags=4.0, body=1.0
        # In FTS5, bm25() returns negative values (lower = more relevant);
        # we negate it so higher positive score = more relevant.
        seed_rows = []
        try:
            cur.execute(
                """
                SELECT node_id, (-1.0 * bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0)) AS bm25_score
                FROM nodes_fts
                WHERE nodes_fts MATCH ?
                ORDER BY bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0) ASC
                LIMIT ?;
                """,
                (clean_q, seed_limit),
            )
            seed_rows = cur.fetchall()
        except sqlite3.OperationalError:
            raw_words = [t.strip("-") for t in re.findall(r"[A-Za-z0-9_\-]+", query_text) if t.strip("-")]
            if raw_words:
                safe_fallback_q = " OR ".join(f'"{w}"' for w in raw_words)
                try:
                    cur.execute(
                        """
                        SELECT node_id, (-1.0 * bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0)) AS bm25_score
                        FROM nodes_fts
                        WHERE nodes_fts MATCH ?
                        ORDER BY bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0) ASC
                        LIMIT ?;
                        """,
                        (safe_fallback_q, seed_limit),
                    )
                    seed_rows = cur.fetchall()
                except Exception:
                    seed_rows = []

        # If search term yields 0 or very few BM25 results, and fuzzy matching is enabled,
        # perform broader fuzzy expansion if not already applied
        if fuzzy and len(seed_rows) < fuzzy_min_results and not fuzzy_expansions:
            if vocab is None:
                vocab = get_indexed_terms(conn)
            raw_q_tokens = [t.strip("-") for t in re.findall(r"[A-Za-z0-9_\-]+", query_text) if t.strip("-")]
            q_tokens = []
            for qt in raw_q_tokens:
                q_tokens.extend(split_subtokens(qt))
            q_tokens = list(dict.fromkeys(q_tokens))
            broader_expansions = {}
            for tok in q_tokens:
                tok_clean = tok.lower()
                matches = fuzzy_match_term(tok_clean, vocab)
                if matches and matches != [tok_clean]:
                    broader_expansions[tok_clean] = matches
            if broader_expansions:
                fuzzy_clean_q = _clean_fts_query(query_text, synonyms=synonyms, fuzzy_expansions=broader_expansions)
                if fuzzy_clean_q and fuzzy_clean_q != clean_q:
                    cur.execute(
                        """
                        SELECT node_id, (-1.0 * bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0)) AS bm25_score
                        FROM nodes_fts
                        WHERE nodes_fts MATCH ?
                        ORDER BY bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0) ASC
                        LIMIT ?;
                        """,
                        (fuzzy_clean_q, seed_limit),
                    )
                    fuzzy_rows = cur.fetchall()
                    if fuzzy_rows:
                        seed_rows = fuzzy_rows

        if not seed_rows:
            return []

        # Map seed nodes to their initial BM25 scores (normalized to 0-10 scale)
        max_raw = max((float(score) for _, score in seed_rows), default=1.0)
        if max_raw <= 0:
            max_raw = 1.0
        seed_scores: dict[str, float] = {}
        for nid, score in seed_rows:
            raw_val = max(float(score), 0.0)
            seed_scores[nid] = (raw_val / max_raw) * 10.0

        # Step 2: Expand step (1 to `hops` hops)
        # We find paths from seed nodes to neighbors in the graph
        # visited: nid -> {"dist": int, "seed": str, "path": list[dict]}
        visited: dict[str, dict[str, Any]] = {}

        for seed_id in seed_scores:
            if seed_id not in visited:
                visited[seed_id] = {
                    "dist": 0,
                    "seed": seed_id,
                    "path": [],
                }

        if hops > 0 and len(graph) > 0:
            queue = [(seed_id, 0) for seed_id in list(seed_scores.keys()) if seed_id in graph]
            idx = 0
            max_candidates = max(limit * 30, 250)
            while idx < len(queue) and len(visited) < max_candidates:
                curr, dist = queue[idx]
                idx += 1
                if dist >= hops:
                    continue

                curr_seed = visited[curr]["seed"]
                curr_path = visited[curr]["path"]

                for neighbor in graph.neighbors(curr):
                    if len(visited) >= max_candidates:
                        break
                    n_str = str(neighbor)
                    new_dist = dist + 1

                    # Dynamic Ego-Graph Boundary Pruning (RepoGraph ICLR 2025)
                    if boundary_pruning and dist >= 1:
                        ed = graph.get_edge_data(curr, neighbor) or {}
                        if isinstance(ed, dict) and 0 in ed:
                            ed = ed[0]
                        weight = float(ed.get("weight", 1.0))
                        if weight < 0.2:
                            continue
                        if len(visited) > max(limit * 2, 20):
                            deg = graph.degree(neighbor)
                            if deg <= 1:
                                continue

                    if n_str not in visited or visited[n_str]["dist"] > new_dist:
                        # Get edge relation if available
                        edge_data = graph.get_edge_data(curr, neighbor) or {}
                        if isinstance(edge_data, dict) and 0 in edge_data:
                            edge_data = edge_data[0]
                        relation = edge_data.get("relation") or "connected"
                        edge_step = {
                            "from": curr,
                            "relation": relation,
                            "to": n_str,
                        }
                        visited[n_str] = {
                            "dist": new_dist,
                            "seed": curr_seed,
                            "path": curr_path + [edge_step],
                        }
                        queue.append((n_str, new_dist))

        # Retrieve metadata & content for all candidate nodes
        all_candidate_ids = list(visited.keys())
        if not all_candidate_ids:
            return []

        # Chunk candidate lookups for SQLite parameter limit safety
        meta_dict: dict[str, tuple] = {}
        chunk_size = 500
        for i in range(0, len(all_candidate_ids), chunk_size):
            chunk = all_candidate_ids[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(
                f"""
                SELECT node_id, label, source_path, source_location, pagerank, degree, content
                FROM node_metadata
                WHERE node_id IN ({placeholders});
                """,  # nosec B608
                chunk,
            )
            for row in cur.fetchall():
                meta_dict[row[0]] = row

        # Step 3: Re-rank step using BM25, proximity discount, and Personalized PageRank (PPR)
        # Compute Personalized PageRank seeded by BM25 match scores over candidate subgraph
        ppr_scores: dict[str, float] = {}
        if graph is not None and len(all_candidate_ids) > 1:
            try:
                sub_nodes = set(all_candidate_ids)
                subG = graph.subgraph([n for n in sub_nodes if n in graph])
                if len(subG) > 0:
                    pers_map = {n: seed_scores.get(n, 0.1) for n in subG.nodes()}
                    ppr_scores = compute_pagerank(subG, personalization=pers_map, alpha=0.85, max_iter=50)
            except Exception:
                ppr_scores = {}

        candidates: list[dict[str, Any]] = []

        for nid, vinfo in visited.items():
            meta = meta_dict.get(nid)
            if not meta:
                continue

            _, label, src_path, src_loc, pagerank, degree, content = meta
            dist = vinfo["dist"]
            seed_id = vinfo["seed"]
            seed_bm25 = seed_scores.get(seed_id, 1.0)

            # Direct BM25 score if this node was an explicit seed hit
            direct_bm25 = seed_scores.get(nid, 0.0)

            # Proximity discount factor:
            # 0 hops: 1.0
            # 1 hop:  0.4 * seed_bm25
            # 2 hops: 0.15 * seed_bm25
            decay = 1.0 if dist == 0 else (0.4 if dist == 1 else 0.15)
            proximity_score = decay * seed_bm25

            # Dynamic Personalized PageRank (PPR) bonus
            ppr_val = float(ppr_scores.get(nid, 0.0))
            ppr_bonus = ppr_val * 10.0

            # Static centrality bonus (scaled PageRank)
            pr = float(pagerank or 0.0)
            centrality_bonus = pr * 5.0

            final_score = direct_bm25 + proximity_score + ppr_bonus + centrality_bonus
            # Code definition bonus: Real nodes with source_path and source_location
            # represent actual AST symbol definitions rather than unresolved stubs.
            if src_path and src_loc:
                final_score *= 1.5
                # Code preference: prioritize executable source code over documentation
                if str(src_path).lower().endswith((".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".cpp", ".c", ".h", ".hpp", ".java", ".kt", ".rb", ".cs", ".swift")):
                    final_score *= 2.0
            else:
                final_score *= 0.2

            # Content preview (up to 300 characters)
            preview = content.strip()
            if len(preview) > 300:
                preview = preview[:297] + "..."

            candidates.append({
                "node_id": nid,
                "label": label,
                "score": round(final_score, 4),
                "text": preview,
                "source_path": src_path,
                "source_location": src_loc,
                "connected_via": vinfo["path"],
            })

        # Step 4: Sort descending by score and slice
        candidates.sort(key=lambda x: x["score"], reverse=True)
        res = candidates[:limit]

        if skeleton:
            from graph_fy.skeleton import get_symbol_code_from_file
            for item in res:
                spath = item.get("source_path")
                sloc = item.get("source_location")
                nid = item.get("node_id")
                if spath and sloc:
                    p = Path(spath)
                    eff_file = p if p.is_absolute() else (eff_root / p)
                    if eff_file.is_file():
                        line_m = re.search(r"L?(\d+)", str(sloc))
                        line_num = int(line_m.group(1)) if line_m else 1
                        skel_code = get_symbol_code_from_file(eff_file, line_num, skeletonize=True, compact_docstrings=True)
                        if skel_code:
                            item["skeleton"] = skel_code

                # Bidirectional execution slicing: callers and callees
                if graph is not None and nid and nid in graph:
                    callers, callees = _find_callers_and_callees(graph, nid)
                    item["callers"] = callers
                    item["callees"] = callees

                # 1-hop type dependency bundling: attach interface/base-class skeletons
                if graph is not None and nid and nid in graph:
                    type_skels: list[dict[str, str]] = []
                    if hasattr(graph, "successors"):
                        succs = list(graph.successors(nid))
                    elif hasattr(graph, "neighbors"):
                        succs = list(graph.neighbors(nid))
                    else:
                        succs = []

                    for succ in succs[:3]:
                        ed = graph.get_edge_data(nid, succ) or {}
                        if isinstance(ed, dict) and 0 in ed:
                            ed = ed[0]
                        rel = str(ed.get("relation", "")).lower()
                        if rel in ("inherits", "implements", "extends", "type_of", "returns"):
                            succ_data = graph.nodes[succ]
                            succ_file = succ_data.get("source_file") or succ_data.get("definition_file")
                            succ_loc = succ_data.get("source_location") or succ_data.get("definition_location")
                            if succ_file:
                                sp = Path(succ_file)
                                eff_succ = sp if sp.is_absolute() else (eff_root / sp)
                                if eff_succ.is_file():
                                    lm = re.search(r"L?(\d+)", str(succ_loc or ""))
                                    lnum = int(lm.group(1)) if lm else 1
                                    t_skel = get_symbol_code_from_file(eff_succ, lnum, skeletonize=True, compact_docstrings=True)
                                    if t_skel:
                                        type_skels.append({
                                            "node_id": succ,
                                            "relation": rel,
                                            "skeleton": t_skel,
                                        })
                    if type_skels:
                        item["related_types"] = type_skels

        if terse or caveman:
            return format_query_results(res, terse=True, skeleton=skeleton)
        return res

    finally:
        conn.close()


DEFAULT_RELATION_WEIGHTS: dict[str, float] = {
    # Highway: Direct execution flow & runtime dispatch
    "calls": 1.0,
    "indirect_call": 1.1,
    "routes_to": 1.0,
    "handles": 1.0,
    "triggered_by": 1.0,
    "fetches_from": 1.0,
    "calls_workflow": 1.0,
    "uses_action": 1.1,
    # Arterial: Module imports & inheritance dependencies
    "imports": 1.3,
    "imports_from": 1.3,
    "depends_on": 1.3,
    "uses": 1.4,
    "inherits": 1.2,
    "implements": 1.2,
    # References & metadata links
    "references": 2.0,
    "references_constant": 2.0,
    "references_env": 2.0,
    "references_secret": 2.0,
    # Structural boundaries (penalize traversing via AST containment)
    "defines": 2.5,
    "defines_schema": 2.5,
    "defines_rpc": 2.5,
    "contains": 3.5,
    "has_step": 3.0,
    "has_stage": 3.0,
}
DEFAULT_EDGE_WEIGHT: float = 1.5


def get_edge_weight(rel: str | None) -> float:
    """Return semantic impedance weight for an edge relation."""
    if not rel:
        return 1.0
    return DEFAULT_RELATION_WEIGHTS.get(rel, DEFAULT_EDGE_WEIGHT)


def get_canonical_search_graph(
    G: nx.Graph,
    undirected: bool = False,
    weighted: bool = True,
) -> nx.Graph | nx.DiGraph:
    """Return a deterministic, cached search graph view for pathfinding.

    Preserves node and edge sorting for cross-seed determinism (#2074), recovers
    caller->callee direction from _src/_tgt markers (#2309), applies semantic
    impedance weights to prioritize execution paths over AST containment,
    and caches the materialized view on G.graph to eliminate per-query
    graph rebuilds.
    """
    cache = G.graph.setdefault("_canonical_search_views", {})
    key = (bool(undirected), bool(weighted))
    if key in cache:
        return cache[key]

    if undirected:
        view = nx.Graph()
        view.add_nodes_from(sorted(G.nodes, key=str))
        edge_map: dict[tuple[Any, Any], float] = {}
        for u, v, d in G.edges(data=True):
            a, b = (u, v) if str(u) <= str(v) else (v, u)
            pair = (a, b)
            w = get_edge_weight(d.get("relation")) if weighted else 1.0
            if pair not in edge_map or w < edge_map[pair]:
                edge_map[pair] = w
        for (u, v) in sorted(edge_map.keys(), key=lambda p: (str(p[0]), str(p[1]))):
            view.add_edge(u, v, weight=edge_map[(u, v)])
    else:
        view = nx.DiGraph()
        view.add_nodes_from(sorted(G.nodes, key=str))
        edge_map: dict[tuple[Any, Any], float] = {}
        for u, v, d in G.edges(data=True):
            s = d.get("_src", u)
            t = d.get("_tgt", v)
            pair = (s, t)
            w = get_edge_weight(d.get("relation")) if weighted else 1.0
            if pair not in edge_map or w < edge_map[pair]:
                edge_map[pair] = w
        for (u, v) in sorted(edge_map.keys(), key=lambda p: (str(p[0]), str(p[1]))):
            view.add_edge(u, v, weight=edge_map[(u, v)])

    cache[key] = view
    return view


def trace(
    G: nx.Graph,
    source: str,
    target: str,
    *,
    directed: bool = True,
    weighted: bool = True,
) -> list[dict[str, Any]]:
    """Shortest-path traversal across graph nodes from source to target.

    Returns a list of edge dicts describing each hop in the path.
    """
    if source not in G:
        # Try matching by label
        matches = [n for n, d in G.nodes(data=True) if d.get("label", "").lower() == source.lower()]
        if matches:
            source = matches[0]
        else:
            return []
    if target not in G:
        matches = [n for n, d in G.nodes(data=True) if d.get("label", "").lower() == target.lower()]
        if matches:
            target = matches[0]
        else:
            return []

    try:
        view = get_canonical_search_graph(G, undirected=not directed, weighted=weighted)
        weight_attr = "weight" if weighted else None
        path_nodes = nx.shortest_path(view, source, target, weight=weight_attr)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []

    result: list[dict[str, Any]] = []
    for i in range(len(path_nodes) - 1):
        u = path_nodes[i]
        v = path_nodes[i + 1]
        edata = G.get_edge_data(u, v) or G.get_edge_data(v, u) or {}
        if isinstance(edata, dict) and 0 in edata and not isinstance(edata.get("relation"), str):
            edata = edata[0]
        result.append({
            "from_node": u,
            "to_node": v,
            "relation": edata.get("relation", "relates_to"),
            "confidence": edata.get("confidence", "EXTRACTED"),
            "weight": edata.get("weight", 1.0),
            "source_file": edata.get("source_file", ""),
            "source_location": edata.get("source_location", ""),
        })
    return result


def explain(
    query_text: str,
    graph: nx.Graph | Path | str | None = None,
    db_path: Path | str | None = None,
    *,
    limit: int = 5,
    hops: int = 2,
    fuzzy: bool = True,
    terse: bool = False,
    caveman: bool = False,
) -> list[dict[str, Any]] | str:
    """Retrieve results with a detailed score breakdown (BM25 score, hop distance, centrality contribution)."""
    raw_results = query(query_text, graph=graph, db_path=db_path, limit=limit, hops=hops, fuzzy=fuzzy)
    if isinstance(raw_results, list):
        # Ensure score breakdown format for explainability
        for r in raw_results:
            hops_count = len(r.get("connected_via", [])) - 1 if r.get("connected_via") else 0
            r["hops"] = max(0, hops_count)
    if terse or caveman:
        return format_query_results(raw_results, terse=True)
    return raw_results

