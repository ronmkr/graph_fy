"""Graph traversal, contextual filtering, subgraph formatting, and node resolution for graph_fy serve."""
from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
from pathlib import Path
from typing import NamedTuple

import networkx as nx

from graph_fy.build import edge_data, edge_datas
from graph_fy.export import _strip_diacritics
from graph_fy.security import sanitize_label, _CONTROL_CHAR_RE
from graph_fy.serve_search import (
    _search_tokens,
    _query_terms,
    _score_nodes,
    _score_query,
    _pick_scored_endpoint,
    _pick_seeds,
    _trigram_candidates,
    _RELATIONAL_INTENT_TERMS,
)


def _get_serve_attr(name: str, default=None):
    mod = sys.modules.get("graph_fy.serve")
    if mod is not None and hasattr(mod, name):
        val = getattr(mod, name)
        current_func = globals().get(name)
        if val is not current_func:
            return val
    return default

_CONTEXT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("call", ("call", "calls", "called", "caller", "callers", "invoke", "invokes", "invoked")),
    ("import", ("import", "imports", "imported", "module", "modules")),
    ("field", ("field", "fields", "member", "members", "property", "properties")),
    ("parameter_type", ("parameter", "parameters", "param", "params", "argument", "arguments")),
    ("return_type", ("return", "returns", "returned")),
    ("generic_arg", ("generic", "generics", "template", "templates")),
)


_CONTEXT_FILTER_ALIASES: dict[str, str] = {
    "param": "parameter_type",
    "params": "parameter_type",
    "parameter": "parameter_type",
    "parameters": "parameter_type",
    "argument": "parameter_type",
    "arguments": "parameter_type",
    "arg": "parameter_type",
    "args": "parameter_type",
    "return": "return_type",
    "returns": "return_type",
    "returned": "return_type",
    "generic": "generic_arg",
    "generics": "generic_arg",
    "template": "generic_arg",
    "templates": "generic_arg",
    "annotation": "attribute",
    "annotations": "attribute",
    "decorator": "attribute",
    "decorators": "attribute",
    "calls": "call",
    "called": "call",
    "invoke": "call",
    "invocation": "call",
    "fields": "field",
    "property": "field",
    "properties": "field",
    "member": "field",
    "members": "field",
    "imports": "import",
    "imported": "import",
    "module": "import",
    "modules": "import",
    "exports": "export",
    "exported": "export",
}


def _normalize_context_filters(filters: list[str] | None) -> list[str]:
    if not filters:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in filters:
        key = _strip_diacritics(str(value)).strip().lower()
        if not key:
            continue
        key = _CONTEXT_FILTER_ALIASES.get(key, key)
        if key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized


def _infer_context_filters(question: str) -> list[str]:
    lowered = {
        _strip_diacritics(token).lower()
        for token in question.replace("?", " ").replace(",", " ").split()
    }
    inferred: list[str] = []
    for context, hints in _CONTEXT_HINTS:
        if any(hint in lowered for hint in hints):
            inferred.append(context)
    return inferred


def _resolve_context_filters(question: str, explicit_filters: list[str] | None = None) -> tuple[list[str], str | None]:
    normalized = _normalize_context_filters(explicit_filters)
    if normalized:
        return normalized, "explicit"
    inferred = _infer_context_filters(question)
    if inferred:
        return inferred, "heuristic"
    return [], None


def _filter_graph_by_context(G: nx.Graph, context_filters: list[str] | None) -> nx.Graph:
    filters = set(_normalize_context_filters(context_filters))
    if not filters:
        return G
    H = G.__class__()
    H.add_nodes_from(G.nodes(data=True))
    if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)):
        for u, v, key, data in G.edges(keys=True, data=True):
            if data.get("context") in filters:
                H.add_edge(u, v, key=key, **data)
    else:
        for u, v, data in G.edges(data=True):
            if data.get("context") in filters:
                H.add_edge(u, v, **data)
    return H


def _complete_induced_edges(G: nx.Graph, visited: set[str], edges_seen: list[tuple]) -> None:
    """Append edges between visited nodes that the traversal never recorded (#2323).

    Both traversals only record an edge that *discovers* an unvisited neighbour,
    so what they return is a traversal tree, not the induced subgraph over the
    nodes they return. `_bfs` marks every seed visited up front, so an edge
    between two seeds can never be recorded — the reported symptom, where both
    endpoints render and the edge between them does not. It drops ordinary
    cross-edges for the same reason. `_dfs` appends on push rather than on
    visit, so it already captured those; its one gap is an edge between two
    non-seed hubs, since neither endpoint is ever expanded.

    Scans only edges incident to `visited`, so cost tracks the subgraph rather
    than the whole graph, bounded by O(2E) overall. A visited hub is rescanned
    in full even though the traversal deliberately did not expand it — that is
    unavoidable, since a hub-to-hub edge is exactly the case `_dfs` misses.
    `G` here is the context-filtered `traversal_graph` (see
    `_query_graph_text`), so a filtered-out relation cannot reappear.

    Self-loops are skipped. A recursive function legitimately carries one, but
    neither traversal has ever recorded one (`n` is always already visited when
    its own self-loop is examined), and surfacing them is a separate output
    change from the missing edges reported here.

    Dedup keys on the ordered pair for directed graphs and the unordered pair
    otherwise: on a DiGraph `u->v` and `v->u` are genuinely distinct edges
    (mutual recursion, circular imports), and collapsing them would drop a real
    one. On a multigraph parallel edges collapse to one entry, matching the
    renderer, which already shows only the first (`_subgraph_to_text`).

    Traversal edges keep their discovery order; completions are appended after.
    """
    directed = G.is_directed()

    def _key(u: str, v: str):
        return (u, v) if directed else frozenset((u, v))

    seen = {_key(u, v) for u, v in edges_seen}
    # sorted() so the appended order can't shift run-to-run with CPython's
    # per-process string-hash seed, the same reason the renderer sorts (#1753).
    for u, v in G.edges(sorted(visited)):
        if u == v or v not in visited:
            continue
        key = _key(u, v)
        if key in seen:
            continue
        seen.add(key)
        edges_seen.append((u, v))


def _bfs(G: nx.Graph, start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    # Compute hub threshold: nodes above this degree are not expanded as transit.
    # p99 of degree distribution, floored at 50 to avoid over-blocking small graphs.
    degrees = [G.degree(n) for n in G.nodes()]
    if degrees:
        degrees_sorted = sorted(degrees)
        p99_idx = int(len(degrees_sorted) * 0.99)
        hub_threshold = max(50, degrees_sorted[p99_idx])
    else:
        hub_threshold = 50
    seed_set = set(start_nodes)
    visited: set[str] = set(start_nodes)
    frontier = set(start_nodes)
    edges_seen: list[tuple] = []
    for _ in range(depth):
        next_frontier: set[str] = set()
        for n in frontier:
            # Don't expand through high-degree hubs (except seeds - a hub that
            # is the starting node should still be explored).
            if n not in seed_set and G.degree(n) >= hub_threshold:
                continue
            for neighbor in G.neighbors(n):
                if neighbor not in visited:
                    next_frontier.add(neighbor)
                    edges_seen.append((n, neighbor))
        visited.update(next_frontier)
        frontier = next_frontier
    _complete_induced_edges(G, visited, edges_seen)
    return visited, edges_seen


def _dfs(G: nx.Graph, start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    degrees = [G.degree(n) for n in G.nodes()]
    if degrees:
        degrees_sorted = sorted(degrees)
        p99_idx = int(len(degrees_sorted) * 0.99)
        hub_threshold = max(50, degrees_sorted[p99_idx])
    else:
        hub_threshold = 50
    seed_set = set(start_nodes)
    visited: set[str] = set()
    edges_seen: list[tuple] = []
    stack = [(n, 0) for n in reversed(start_nodes)]
    while stack:
        node, d = stack.pop()
        if node in visited or d > depth:
            continue
        visited.add(node)
        if node not in seed_set and G.degree(node) >= hub_threshold:
            continue
        for neighbor in G.neighbors(node):
            if neighbor not in visited:
                stack.append((neighbor, d + 1))
                edges_seen.append((node, neighbor))
    _complete_induced_edges(G, visited, edges_seen)
    return visited, edges_seen


def _subgraph_to_text(G: nx.Graph, nodes: set[str], edges: list[tuple], token_budget: int = 2000, *, seeds: list[str] | None = None) -> str:
    """Render subgraph text under token budget (~3 chars/token)."""
    char_budget = token_budget * 3
    lines = []
    # Optional work-memory overlay sidecar.
    overlay = getattr(G, "graph", {}).get("_learning_overlay", {}) or {}
    seed_set = set(seeds or [])
    seed_hits = [n for n in (seeds or []) if n in nodes]
    # Rank non-seeds by hop distance to prioritize close neighbors over hubs.
    def _adj(n):
        if G.is_directed():
            yield from G.successors(n)
            yield from G.predecessors(n)
        else:
            yield from G.neighbors(n)
    dist: dict[str, int] = {n: 0 for n in seed_hits}
    frontier, hop = seed_hits, 0
    while frontier:
        hop += 1
        nxt = []
        for n in frontier:
            for nb in _adj(n):
                if nb in nodes and nb not in dist:
                    dist[nb] = hop
                    nxt.append(nb)
        frontier = nxt
    ordered = seed_hits + sorted(
        nodes - seed_set,
        key=lambda n: (dist.get(n, 1 << 30), -G.degree(n), str(n)),
    )
    for nid in ordered:
        d = G.nodes[nid]
        # Sanitize metadata; count learning suffix in budget.
        entry = overlay.get(str(nid))
        learning_suffix = ""
        if entry:
            status = sanitize_label(str(entry.get("status", "")))
            if status:
                learning_suffix = f" learning={status}{':stale' if entry.get('stale') else ''}"
        attrs = d.get("attributes")
        attrs_suffix = ""
        if isinstance(attrs, dict) and attrs:
            pairs = []
            for k, v in sorted(attrs.items()):
                if isinstance(v, (dict, list)):
                    v_str = json.dumps(v, sort_keys=True)
                elif isinstance(v, str):
                    v_str = f'"{v}"'
                elif isinstance(v, bool):
                    v_str = "true" if v else "false"
                elif v is None:
                    v_str = "null"
                else:
                    v_str = str(v)
                if len(v_str) > 60:
                    v_str = v_str[:57] + "..."
                pairs.append(f"{k}={v_str}")
                if len(pairs) >= 10:
                    pairs.append("...")
                    break
            attrs_summary = ", ".join(pairs)
            if len(attrs_summary) > 200:
                attrs_summary = attrs_summary[:197] + "..."
            attrs_suffix = f" attrs={{{sanitize_label(attrs_summary)}}}"
        line = (
            f"NODE {sanitize_label(d.get('label', nid))} "
            f"[src={sanitize_label(str(d.get('source_file', '')))} "
            f"loc={sanitize_label(str(d.get('source_location', '')))} "
            f"community={sanitize_label(str(d.get('community_name') or d.get('community', '')))}"
            f"{learning_suffix}"
            f"{attrs_suffix}]"
        )
        lines.append(line)
    for u, v in edges:
        if u in nodes and v in nodes:
            raw = G[u][v]
            raw_edges = list(raw.values()) if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)) else [raw]
            for d in raw_edges:
                rels = d.get("relations") or [d.get("relation", "")]
                for rel in rels:
                    # Honor stored _src/_tgt direction over undirected BFS visit order.
                    src = d.get("_src", u)
                    tgt = d.get("_tgt", v)
                    # Guard against dangling endpoints in _src/_tgt.
                    if {src, tgt} != {u, v}:
                        src, tgt = u, v
                    context = d.get("context")
                    context_suffix = f" context={sanitize_label(str(context))}" if context else ""
                    # Use call site location rather than caller def line.
                    _loc = str(d.get("source_location") or "")
                    at_suffix = (
                        f" at={sanitize_label(str(d.get('source_file') or ''))}:{sanitize_label(_loc)}"
                        if _loc else ""
                    )
                    line = (
                        f"EDGE {sanitize_label(G.nodes[src].get('label', src))} "
                        f"--{sanitize_label(str(rel))} "
                        f"[{sanitize_label(str(d.get('confidence', '')))}{context_suffix}]--> "
                        f"{sanitize_label(G.nodes[tgt].get('label', tgt))}{at_suffix}"
                    )
                    lines.append(line)
    output = "\n".join(lines)
    if len(output) > char_budget:
        cut_at = output[:char_budget].rfind("\n")
        cut_at = cut_at if cut_at > 0 else char_budget
        # Preserve queried seed nodes regardless of budget cut.
        if seed_hits:
            seed_block_end = sum(len(lines[i]) + 1 for i in range(len(seed_hits))) - 1
            cut_at = max(cut_at, min(seed_block_end, len(output)))
        total_nodes = sum(1 for l in lines if l.startswith("NODE "))
        shown_nodes = output[:cut_at].count("\nNODE ") + (1 if output.startswith("NODE ") else 0)
        cut_count = total_nodes - shown_nodes
        # If all nodes fit and only edges overflow, return complete answer with notice.
        if cut_count == 0:
            total_edges = sum(1 for l in lines if l.startswith("EDGE "))
            est_tokens = len(output) // 3
            return (
                f"[i] Complete answer over budget: all {total_nodes} nodes and "
                f"{total_edges} edges shown (~{est_tokens} tokens vs the "
                f"requested ~{token_budget}-token budget). Edges are never "
                f"dropped once every node fits, so this is already the full "
                f"answer — raising --budget further will not shrink it. Narrow "
                f"with context_filter=['call'] or use get_node for a specific "
                f"symbol to reduce size instead.\n\n"
            ) + output
        # Prominent top notice for truncated node answers.
        output = (
            f"[!] TRUNCATED: showing {shown_nodes} of {total_nodes} nodes "
            f"(~{token_budget}-token budget). The answer may be among the "
            f"{cut_count} cut nodes — raise the token budget (CLI: --budget) or "
            f"narrow the query (e.g. context_filter=['call'], or get_node for a "
            f"specific symbol).\n\n"
            + output[:cut_at]
            + f"\n... (truncated — {cut_count} more nodes cut by ~{token_budget}-token budget."
            f" Narrow with context_filter=['call'] or use get_node for a specific symbol)"
        )
    return output


def _cut_lines_to_budget(lines: list[str], token_budget: int, narrow_hint: str) -> str:
    """Render pre-built lines under the same ~3-chars/token budget rule as
    _subgraph_to_text; over-budget output is cut at a line boundary with a count and a
    narrowing hint instead of flooding the caller's context window."""
    output = "\n".join(lines)
    char_budget = token_budget * 3
    if len(output) <= char_budget:
        return output
    cut_at = output[:char_budget].rfind("\n")
    cut_at = cut_at if cut_at > 0 else char_budget
    kept = output[:cut_at]
    shown = kept.count("\n") + 1
    cut_count = len(lines) - shown
    # Announce truncation at the TOP as well, matching _subgraph_to_text — a
    # bottom-only marker reads as silence/absence (the BUG-2 fix rationale). The
    # notice sits outside char_budget by design (one bounded wrapper line).
    return (
        f"[!] TRUNCATED: showing {shown} of {len(lines)} lines "
        f"(~{token_budget}-token budget). {narrow_hint}\n\n"
        + kept
        + f"\n... (truncated — {cut_count} more lines cut by ~{token_budget}-token budget. "
        + narrow_hint
        + ")"
    )


def _display_graph_path(graph_path: str) -> str:
    """Render a graph path for the query header.

    Relative to the CWD when it sits underneath it — `graph_fy_out/graph.json`,
    which is the ordinary case and stays short. Absolute otherwise, because a
    graph outside the directory you are standing in is precisely the situation
    the header exists to make visible (#2789). Always POSIX separators so the
    line reads the same on either platform. Falls back to the path as given if
    it cannot be resolved; this is a display helper and must never be the reason
    a query fails.
    """
    try:
        p = Path(graph_path).resolve()
        try:
            return p.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            return p.as_posix()
    except (OSError, RuntimeError, ValueError):
        return str(graph_path)


def _traversal_view(G: nx.Graph) -> nx.Graph:
    """Undirected copy of `G` for BFS/DFS, with true direction kept per edge.

    `_load_graph` forces `directed: True` so renderers can recover stored arc
    order (#2309), and on a DiGraph `G.neighbors()` yields successors only. The
    query traversals rely on `neighbors()`, so a seed with no outgoing edges — a
    leaf function that is only ever called, imported and contained — expanded
    to nothing: `query_graph` over MCP answered with the seed alone while the
    CLI `query`, which loads the same file undirected, returned the callers,
    the test and the neighbouring modules. Every other MCP tool was unaffected:
    `get_neighbors` walks successors and predecessors explicitly, and
    `shortest_path` builds its own graph from `_src`/`_tgt`.

    Mirrors the CLI loader: traverse undirected, stash `_src`/`_tgt` on each
    edge so `_subgraph_to_text` still renders caller->callee regardless of the
    side the traversal reached the edge from. Markers already present on an
    edge win, for the same reason as in the CLI (#2309). An undirected input is
    returned as-is, so the CLI path is unchanged.

    Mutual arcs `u->v` and `v->u` (mutual recursion, a circular import) fold
    into one undirected edge on a plain `DiGraph` input, the later one winning
    — the same fold the CLI loader performs when `json_graph.node_link_graph`
    reads the undirected on-disk graph into an `nx.Graph`, which is what keeps
    the two surfaces' output identical. The renderer shows one edge per pair
    in any case. On a `MultiDiGraph` input the copy is a `MultiGraph` and the
    stored keys are not carried over: a key is unique per unordered pair on an
    undirected multigraph, so mutual arcs that happen to share a key would
    fold there too; letting networkx assign the keys keeps both, and nothing
    downstream reads the key.

    A fresh copy per query rather than a cached one: `_filter_graph_by_context`
    already copies per query when a filter applies, and the copy shares node
    data dicts with `G`, so only the edge dicts are duplicated.
    """
    if not G.is_directed():
        return G
    H = nx.MultiGraph() if G.is_multigraph() else nx.Graph()
    H.graph.update(G.graph)
    H.add_nodes_from(G.nodes(data=True))
    for u, v, d in G.edges(data=True):
        H.add_edge(u, v, **{**d, "_src": d.get("_src", u), "_tgt": d.get("_tgt", v)})
    return H

def _query_graph_text(
    G: nx.Graph,
    question: str,
    *,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
    context_filters: list[str] | None = None,
    graph_path: str | None = None,
) -> str:
    terms = _query_terms(question)
    # One graph scoring pass produces both the combined ranking (used to drive
    # the gap-based seed selection below) and the per-token singleton winners
    # (used by _pick_seeds' per-term guarantee). Previously this was T+1 passes
    # — one combined + one per query token — re-walking the whole graph each
    # time; on a 100k-node, three-term benchmark ~71% of scoring time was
    # spent in those redundant per-term passes.
    score_fn = _get_serve_attr("_score_query", _score_query)
    qs = score_fn(G, terms, collect_per_term_seeds=True)
    # Relational-intent verbs ("calls", "uses", ...) describe the relation the
    # question asks about, not a symbol to seed from; drop them from the
    # per-term seed GUARANTEE so an incidental verb match cannot seat a decoy
    # BFS root (#2507). They keep their place in `qs.ranked`, so a genuine
    # identifier named after a verb can still win a seat on merit via the gap
    # window — and when the query consists ONLY of intent words (bare "calls"),
    # the guarantee is left intact so such an identifier stays reachable.
    best_seed_by_term = qs.best_seed_by_term
    intent = {t for t in best_seed_by_term if t in _RELATIONAL_INTENT_TERMS}
    if intent and any(t not in _RELATIONAL_INTENT_TERMS for t in terms):
        best_seed_by_term = {
            t: nid for t, nid in best_seed_by_term.items() if t not in intent
        }
    pick_fn = _get_serve_attr("_pick_seeds", _pick_seeds)
    start_nodes = pick_fn(qs.ranked, G=G, best_seed_by_term=best_seed_by_term)
    if not start_nodes:
        return "No matching nodes found."
    resolved_filters, filter_source = _resolve_context_filters(question, context_filters)
    traversal_graph = _filter_graph_by_context(_traversal_view(G), resolved_filters)
    nodes, edges = _dfs(traversal_graph, start_nodes, depth) if mode == "dfs" else _bfs(traversal_graph, start_nodes, depth)
    header_parts = [
        f"Traversal: {mode.upper()} depth={depth}",
        f"Start: {[G.nodes[n].get('label', n) for n in start_nodes]}",
    ]
    # Name the graph this answer came from. `graph_fy_out/` resolves against the
    # CWD, so running a query from a parent project while thinking about a
    # vendored subproject silently answers from the wrong corpus — the output is
    # well-formed and confidently wrong, and nothing in it said which graph was
    # opened (#2789). Shown relative when the graph is under the CWD (the normal
    # case, and short), absolute when it is not — which is exactly the case worth
    # noticing. The node count travels with it because "355 nodes" vs "3178
    # nodes" is often the first thing that looks wrong.
    if graph_path:
        header_parts.insert(0, f"Graph: {_display_graph_path(graph_path)} "
                               f"({G.number_of_nodes()} nodes)")
    if resolved_filters:
        header_parts.append(f"Context: {', '.join(resolved_filters)} ({filter_source})")
    header_parts.append(f"{len(nodes)} nodes found")
    header = " | ".join(header_parts) + "\n\n"
    # Pass the seeds so the queried symbol renders first and survives truncation
    # (#BUG2): a branch merge had silently dropped this argument, leaving the
    # seed-first ordering as dead code.
    return header + _subgraph_to_text(traversal_graph, nodes, edges, token_budget, seeds=start_nodes)


def _resolve_path_scoped_symbol(G: nx.Graph, path_part: str, symbol_part: str) -> list[str]:
    """Nodes whose source_file matches path_part and label/id matches symbol_part.

    Backs the `path::Symbol` query form (#3485): a bare path resolves to the
    FILE node (`_find_node_tiers`'s own `source_exact` tier, which prefers
    the file over its members once #2032-disambiguated), and a bare symbol
    name can be ambiguous across files -- the same-named local declaration
    guard from #3176 exists for exactly that case, and its own suggested
    retry ("the repo-relative path") pointed at a form that resolved to the
    wrong node or nothing at all, since no prior tier combined a path with
    a label. This combines both constraints in one query, so a specific
    symbol in a specific file is reachable without needing its opaque id.
    """
    path_tokens = " ".join(_search_tokens(path_part))
    norm_path_query = _strip_diacritics(path_part).lower().strip()
    symbol_term = " ".join(_search_tokens(symbol_part))
    norm_symbol_query = _strip_diacritics(symbol_part).lower().strip()
    if not (path_tokens or norm_path_query) or not (symbol_term or norm_symbol_query):
        return []
    candidate_ids = _trigram_candidates(G, [symbol_term, norm_symbol_query])
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    matches: list[str] = []
    for nid, d in node_iter:
        source_file = d.get("source_file") or ""
        source_tokens = " ".join(_search_tokens(source_file))
        norm_source = _strip_diacritics(source_file).lower().strip()
        if not (
            source_tokens == path_tokens
            or norm_source == norm_path_query
            or (norm_path_query and norm_source.endswith("/" + norm_path_query))
            or (path_tokens and source_tokens.endswith(" " + path_tokens))
        ):
            continue
        norm_label = d.get("norm_label") or _strip_diacritics(d.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        label_tokens = " ".join(_search_tokens(d.get("label") or ""))
        nid_lower = nid.lower()
        if (
            symbol_term == norm_label or symbol_term == bare_label
            or symbol_term == label_tokens or symbol_term == nid_lower
            or norm_symbol_query == norm_label or norm_symbol_query == bare_label
        ):
            matches.append(nid)
    return matches


def _label_has_literal_exact_match(G: nx.Graph, term: str, norm_query: str) -> bool:
    """Does the RAW, unsplit query already exact-match some node's own label/id?

    Mirrors the `exact` tier's own condition below, run early and standalone so
    `_find_node_tiers` can tell a literal `::`-bearing label (Rust modules, C++
    namespaces) from a deliberately path-scoped query before choosing between
    them. A real label essentially never equals a whole `path::symbol` string
    verbatim, so this is a safe way to prefer the literal interpretation
    whenever one genuinely exists.
    """
    candidate_ids = _trigram_candidates(G, [term, norm_query])
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    for nid, d in node_iter:
        norm_label = d.get("norm_label") or _strip_diacritics(d.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        label_tokens = " ".join(_search_tokens(d.get("label") or ""))
        nid_lower = nid.lower()
        nid_norm = nid_lower if nid.isascii() else _strip_diacritics(nid).lower()
        if (
            term == norm_label or term == bare_label or term == label_tokens or term == nid_lower
            or norm_query == norm_label or norm_query == bare_label or norm_query == nid_norm
        ):
            return True
    return False


def _find_node_tiers(
    G: nx.Graph, label: str
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return match tiers in precedence order: (source_exact, exact, prefix, substring).

    Split out of `_find_node` so callers that must not guess between equally-good
    matches can inspect the winning tier alone. `_find_node` flattens these, and
    its consumers take `[0]` — which resolves by graph-iteration order when one
    tier holds several nodes from different files. See `find_node_ambiguity`.
    """
    term = " ".join(_search_tokens(label))
    # Punctuation-preserving normalized query. `term` tokenizes on \w+ (so
    # "blockStream.ts" -> "blockstream ts", space where the '.' was), but a node's
    # stored `norm_label` keeps punctuation ("blockstream.ts"). Matching only via
    # `term`/`label_tokens` works when the node label tokenizes the same way, but is
    # fragile if `label` and `norm_label` diverge. `norm_query` matches `norm_label`
    # symmetrically so an exactly-typed punctuated label always resolves (#1704).
    # `nid_norm` below extends that symmetry to node ids, which keep their
    # punctuation too and are compared raw against the tokenized `term` (#2467).
    norm_query = _strip_diacritics(str(label)).lower().strip()

    # `path::Symbol` restricts the label match to nodes defined in that
    # file (#3485) -- checked before the ordinary tiers below so a
    # deliberately path-scoped query never falls back to guessing among
    # same-named symbols in other files. Returned as source_exact (the
    # tier `find_node_ambiguity` already treats as maximally specific) so
    # existing callers need no changes; an empty result falls through to
    # ordinary matching rather than reporting no match outright, in case
    # "::" is meaningful some other way to a caller this was not designed
    # for. Skipped when the raw label already literally matches a node (a
    # native `::`-bearing label, e.g. Rust modules or C++ namespaces) so that
    # an unrelated file whose path happens to resemble the label's prefix
    # cannot hijack a query that was never meant to be path-scoped.
    if "::" in label and not _label_has_literal_exact_match(G, term, norm_query):
        path_part, _, symbol_part = label.partition("::")
        path_part, symbol_part = path_part.strip(), symbol_part.strip()
        if path_part and symbol_part:
            scoped = _resolve_path_scoped_symbol(G, path_part, symbol_part)
            if scoped:
                return scoped, [], [], []

    if not term:
        return [], [], [], []
    source_exact: list[str] = []
    exact: list[str] = []
    prefix: list[str] = []
    substring: list[str] = []
    # Trigram prefilter (graph-iteration order preserved so exact/prefix/substring
    # ordering — and thus matches[0] — is byte-identical to the full scan).
    candidate_ids = _trigram_candidates(G, [term, norm_query])
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    for nid, d in node_iter:
        norm_label = d.get("norm_label") or _strip_diacritics(d.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        label_tokens = " ".join(_search_tokens(d.get("label") or ""))
        source_tokens = " ".join(_search_tokens(d.get("source_file") or ""))
        nid_lower = nid.lower()
        # `_strip_diacritics` is the identity on ASCII, so the NFKD fold is only
        # paid for ids that actually carry non-ASCII text.
        nid_norm = nid_lower if nid.isascii() else _strip_diacritics(nid).lower()
        if term == source_tokens:
            source_exact.append(nid)
        elif (
            term == norm_label or term == bare_label or term == label_tokens or term == nid_lower
            or norm_query == norm_label or norm_query == bare_label or norm_query == nid_norm
        ):
            exact.append(nid)
        elif (
            norm_label.startswith(term)
            or bare_label.startswith(term)
            or label_tokens.startswith(term)
            or nid_lower.startswith(term)
            or norm_label.startswith(norm_query)
            or bare_label.startswith(norm_query)
        ):
            prefix.append(nid)
        elif term in norm_label or term in label_tokens or norm_query in norm_label:
            substring.append(nid)

    if source_exact:
        query_basename = _strip_diacritics(Path(label).name).lower()
        preferred = []
        for nid in source_exact:
            if str(G.nodes[nid].get("source_location", "")) != "L1":
                continue
            # File-node label is the bare basename OR a directory-qualified form
            # from the #2032 disambiguation pass (e.g. "process-order/index.ts").
            lbl = _strip_diacritics(str(G.nodes[nid].get("label") or "")).lower()
            if lbl == query_basename or lbl.endswith("/" + query_basename):
                preferred.append(nid)
        if len(preferred) == 1:
            source_exact = preferred + [nid for nid in source_exact if nid != preferred[0]]

    return source_exact, exact, prefix, substring


def _find_node(G: nx.Graph, label: str) -> list[str]:
    """Return node IDs whose label or ID matches the search term (diacritic-insensitive).

    Results are ordered by precedence: exact source-file path match first, then
    exact (label/ID) match, then prefix match, then substring match. Node-ID exact
    matches are grouped with label exact matches.
    """
    source_exact, exact, prefix, substring = _find_node_tiers(G, label)
    return source_exact + exact + prefix + substring


def find_node_ambiguity(G: nx.Graph, label: str) -> list[str]:
    """Return rival candidates when the winning match tier spans several source files.

    `_find_node` ranks matches but never reports that a tie was broken, so callers
    taking `[0]` present one arbitrary file as the answer. Two workspaces that each
    define `MetricsPort` put both nodes in the same `exact` tier, separated only by
    `G.nodes()` iteration order — reorder the graph and the same query answers with
    a different file, equally confidently.

    Returns one representative node id per distinct source file when the winning
    tier is split that way, else `[]`. Several matches *within one file* (a file
    node plus its members) are ordinary precedence, not ambiguity, and return `[]`.

    `_disambiguate_file_node_labels` (#2032) already relabels colliding *file*
    nodes; this covers the symbol case it does not reach.
    """
    for tier in _find_node_tiers(G, label):
        if not tier:
            continue
        by_source: dict[str, str] = {}
        for nid in tier:
            source = str(G.nodes[nid].get("source_file") or "")
            by_source.setdefault(source, nid)
        return list(by_source.values()) if len(by_source) > 1 else []
    return []


def _node_arg(arguments: dict) -> str:
    """Extract a node label or id from arguments, accepting 'label', 'node_id', or 'id'."""
    return str(arguments.get("label") or arguments.get("node_id") or arguments.get("id") or "")


def _resolve_single_node(G: nx.Graph, label: str) -> tuple[str | None, str | None]:
    """Shared node resolution for the get_node / get_neighbors tools.

    Returns ``(node_id, None)`` when *label* resolves to a single winner via the
    tiered `_find_node` ranking, or ``(None, message)`` when there is no match or
    the winning tier spans several source files. Routing both tools through this
    keeps get_node from silently returning a `G.nodes()` iteration-order match for
    a hub name while get_neighbors reports the same lookup as ambiguous (#ADR-0001).
    """
    matches = _find_node(G, label)
    if not matches:
        return None, f"No node matching '{label}' found."
    rivals = find_node_ambiguity(G, label)
    if rivals:
        listing = "\n".join(
            f"  {G.nodes[r].get('source_file') or r}\n    id: {r}" for r in rivals
        )
        return None, (
            f"Ambiguous: '{label}' matches {len(rivals)} nodes in different files.\n"
            f"{listing}\n"
            f"Retry with path::symbol using one of the paths above (e.g. "
            f"<path>::{label}) or the full node id."
        )
    return matches[0], None


def _shortest_path_text(G: nx.Graph, arguments: dict) -> str:
    """Body of the `shortest_path` MCP tool (module-level so tests can call it
    without an mcp install).

    Directed by default (#2487): the returned path must follow stored
    caller→callee direction; pass ``undirected=True`` to ignore it.
    """
    src_scored = _score_nodes(G, [t.lower() for t in arguments["source"].split()])
    tgt_scored = _score_nodes(G, [t.lower() for t in arguments["target"].split()])
    if not src_scored:
        return f"No node matching source '{arguments['source']}' found."
    if not tgt_scored:
        return f"No node matching target '{arguments['target']}' found."
    src_nid = _pick_scored_endpoint(G, src_scored, arguments["source"])
    tgt_nid = _pick_scored_endpoint(G, tgt_scored, arguments["target"])
    # Ambiguity guard: when both queries resolve to the same node, the
    # shortest path is trivially zero hops, which is almost never what the
    # caller wanted (see bug #828).
    if src_nid == tgt_nid:
        return (
            f"'{arguments['source']}' and '{arguments['target']}' both resolved to "
            f"the same node '{src_nid}'. Use a more specific label or the exact node ID."
        )
    warnings: list[str] = []
    for name, scored, nid in (
        ("source", src_scored, src_nid),
        ("target", tgt_scored, tgt_nid),
    ):
        # Only meaningful when the raw score head is what got picked — a
        # full-token override was chosen on token coverage, not score.
        if len(scored) >= 2 and nid == scored[0][1]:
            top, runner = scored[0][0], scored[1][0]
            if top > 0 and (top - runner) / top < 0.10:
                warnings.append(
                    f"warning: {name} match was ambiguous "
                    f"(top score {top:g}, runner-up {runner:g})"
                )
    max_hops = int(arguments.get("max_hops", 8))
    undirected = bool(arguments.get("undirected", False))
    weighted = bool(arguments.get("weighted", True))
    try:
        from graph_fy.retrieval import get_canonical_search_graph
        view = get_canonical_search_graph(G, undirected=undirected, weighted=weighted)
        weight_attr = "weight" if weighted else None
        path_nodes = nx.shortest_path(view, src_nid, tgt_nid, weight=weight_attr)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        src_label = G.nodes[src_nid].get("label", src_nid)
        tgt_label = G.nodes[tgt_nid].get("label", tgt_nid)
        if undirected:
            return f"No path found between '{src_label}' and '{tgt_label}'."
        return (
            f"No directed path found between '{src_label}' and '{tgt_label}'. "
            "Retry with undirected=true to search ignoring edge direction."
        )
    hops = len(path_nodes) - 1
    if hops > max_hops:
        return f"Path exceeds max_hops={max_hops} ({hops} hops found)."
    segments = []
    for i in range(len(path_nodes) - 1):
        u, v = path_nodes[i], path_nodes[i + 1]
        # Report the actual stored relation(s), never a fabricated `calls`;
        # fall back to an honest "related" when the edge has no relation (#2074).
        # Direction truth lives in the per-link _src/_tgt markers (#2309): a
        # legacy canonicalized file can persist a flipped arc, so classify each
        # hop by _src (falling back to the arc tail) instead of raw arc order.
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
    prefix = ("\n".join(warnings) + "\n") if warnings else ""
    return prefix + f"Shortest path ({hops} hops):\n  " + " ".join(segments)


def _filter_blank_stdin() -> None:
    """Filter blank lines from stdin before MCP reads it.

    Some MCP clients (Claude Desktop, etc.) send blank lines between JSON
    messages. The MCP stdio transport tries to parse every line as a
    JSONRPCMessage, so a bare newline triggers a Pydantic ValidationError.
    This installs an OS-level pipe that relays stdin while dropping blanks.
    """
    r_fd, w_fd = os.pipe()
    saved_fd = os.dup(sys.stdin.fileno())

    def _relay() -> None:
        try:
            with open(saved_fd, "rb") as src, open(w_fd, "wb") as dst:
                for line in src:
                    if line.strip():
                        dst.write(line)
                        dst.flush()
        except Exception:
            pass

    threading.Thread(target=_relay, daemon=True).start()
    os.dup2(r_fd, sys.stdin.fileno())
    os.close(r_fd)
    sys.stdin = open(0, "r", closefd=False)


def _community_header(cid: int, community_name) -> str:
    # Header for get_community: "Community N — Name", matching get_node / query
    # output which read the community_name attribute to_json writes onto nodes.
    # Skip the name when it is just the "Community N" placeholder (written for
    # unnamed communities) so the header never reads "Community 12 — Community 12";
    # also falls back to the bare id when there is no name. Name is sanitised
    # (F-010) like every other LLM-derived field.
    base = f"Community {cid}"
    if community_name:
        clean = sanitize_label(str(community_name))
        if clean and clean != base:
            return f"{base} — {clean}"
    return base

