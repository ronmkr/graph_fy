"""Fine-Grained Code Navigation MCP Tools & Token Savings Metric (CodeGraph).

Exposes code intelligence tools for AST-level navigation:
- find_definitions: Symbol declaration lookup with line ranges and docstring
- get_callers: Inbound call hierarchy
- get_callees: Outbound call hierarchy
- find_references: Cross-file usages, calls, and imports
- find_implementations: Interface / class inheritance hierarchy
- estimate_token_savings: Subgraph vs raw file read token economy
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import networkx as nx

from graph_fy.build import edge_data, edge_datas
from graph_fy.paths import graph_fy_OUT, default_graph_json as _default_graph_json


def _safe_edge_datas(G: nx.Graph, u: str, v: str) -> list[dict]:
    """Safely return all edge data dictionaries for (u, v) without KeyError."""
    if G.has_edge(u, v):
        return edge_datas(G, u, v)
    return []


_CHARS_PER_TOKEN = 4
_SUBTYPE_RELATIONS = frozenset({
    "inherits",
    "implements",
    "extends",
    "subclass_of",
    "subtype_of",
    "embeds",
    "mixes_in",
})
_REFERENCE_EXCLUDE_RELATIONS = frozenset({
    "contains",
    "parent_of",
    "has_part",
    "rationale_for",
    "part_of",
})


def estimate_token_savings(subgraph_nodes: list[dict], root_dir: str = ".") -> dict:
    """Compare estimated token size of subgraph vs reading full source files.

    Args:
        subgraph_nodes: List of node dictionaries in the returned subgraph.
        root_dir: Root directory for resolving source file paths.

    Returns:
        dict with:
            - tokens_saved: Tokens saved vs reading full files (int)
            - file_reads_avoided: Count of unique source files touched (int)
            - compression_ratio: Ratio of full source tokens to subgraph tokens (float)
    """
    unique_files: set[str] = set()
    for node in subgraph_nodes:
        f = (
            node.get("source_file")
            or node.get("definition_file")
            or node.get("path")
            or node.get("file")
        )
        if f and isinstance(f, str) and not f.startswith("<") and f != "unknown":
            unique_files.add(f)

    file_reads_avoided = len(unique_files)
    total_source_tokens = 0

    for f in unique_files:
        p = Path(f) if Path(f).is_absolute() else (Path(root_dir) / f)
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                total_source_tokens += max(1, len(content) // _CHARS_PER_TOKEN)
            except OSError:
                pass

    if total_source_tokens == 0 and unique_files:
        for node in subgraph_nodes:
            if "file_tokens" in node:
                total_source_tokens += int(node["file_tokens"])
            elif "file_content" in node:
                total_source_tokens += max(1, len(str(node["file_content"])) // _CHARS_PER_TOKEN)

    if subgraph_nodes:
        serialized = json.dumps(subgraph_nodes, ensure_ascii=False)
        subgraph_tokens = max(1, len(serialized) // _CHARS_PER_TOKEN)
    else:
        subgraph_tokens = 0

    tokens_saved = max(0, total_source_tokens - subgraph_tokens)

    if subgraph_tokens > 0 and total_source_tokens > 0:
        compression_ratio = round(total_source_tokens / subgraph_tokens, 2)
    elif total_source_tokens > 0:
        compression_ratio = float(total_source_tokens)
    else:
        compression_ratio = 1.0 if subgraph_tokens == 0 else 0.0

    return {
        "tokens_saved": tokens_saved,
        "file_reads_avoided": file_reads_avoided,
        "compression_ratio": compression_ratio,
        "subgraph_tokens": subgraph_tokens,
        "total_source_tokens": total_source_tokens,
    }


def _load_graph_safe(graph_path: str | Path | None = None) -> nx.Graph:
    """Load graph as directed DiGraph for hierarchy queries."""
    path = Path(graph_path or _default_graph_json()).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Graph file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "links" not in raw and "edges" in raw:
        raw = dict(raw, links=raw["edges"])
    raw = {**raw, "directed": True}
    from networkx.readwrite import json_graph

    try:
        return json_graph.node_link_graph(raw, edges="links")
    except TypeError:
        return json_graph.node_link_graph(raw)


def _extract_type(data: dict) -> str:
    """Extract semantic symbol type."""
    if data.get("type"):
        return str(data["type"])
    if data.get("kind"):
        return str(data["kind"])
    if data.get("node_kind"):
        return str(data["node_kind"])
    if data.get("_callable_class"):
        return "class"
    if data.get("_callable"):
        return "function"
    return str(data.get("file_type") or "symbol")


def _extract_line_range(data: dict) -> tuple[int | None, int | None]:
    """Parse start_line and end_line from node or edge dictionary."""
    start = data.get("start_line")
    end = data.get("end_line")
    if start is not None:
        try:
            s = int(start)
            e = int(end) if end is not None else s
            return s, e
        except (ValueError, TypeError):
            pass

    for loc_key in ("source_location", "definition_location", "loc", "location"):
        loc_val = data.get(loc_key)
        if loc_val:
            loc_str = str(loc_val).strip()
            m = re.search(r"L?(\d+)(?:[-:]L?(\d+))?", loc_str)
            if m:
                s = int(m.group(1))
                e = int(m.group(2)) if m.group(2) else s
                return s, e
    return None, None


def _extract_docstring(G: nx.Graph, nid: str, data: dict) -> str | None:
    """Retrieve docstring from node attributes or connected rationale node."""
    for key in ("docstring", "doc", "documentation", "description"):
        val = data.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()

    if hasattr(G, "predecessors"):
        for pred in G.predecessors(nid):
            for ed in _safe_edge_datas(G, pred, nid):
                if str(ed.get("relation", "")).lower() == "rationale_for":
                    p_data = G.nodes[pred]
                    text = p_data.get("text") or p_data.get("label") or p_data.get("docstring")
                    if text and isinstance(text, str) and text.strip():
                        return text.strip()

    if hasattr(G, "neighbors") and not G.is_directed():
        for nb in G.neighbors(nid):
            for ed in _safe_edge_datas(G, nb, nid) + _safe_edge_datas(G, nid, nb):
                if str(ed.get("relation", "")).lower() == "rationale_for":
                    nb_data = G.nodes[nb]
                    text = nb_data.get("text") or nb_data.get("label") or nb_data.get("docstring")
                    if text and isinstance(text, str) and text.strip():
                        return text.strip()

    return None


def _match_nodes(G: nx.Graph, query: str) -> list[str]:
    """Find candidate nodes in graph matching symbol query."""
    query_clean = query.strip()
    query_base = query_clean.rstrip("()")
    query_lower = query_clean.lower()
    query_base_lower = query_base.lower()

    exact_matches: list[str] = []
    case_matches: list[str] = []

    for nid, data in G.nodes(data=True):
        label = str(data.get("label", ""))
        name = str(data.get("name", ""))
        norm_label = str(data.get("norm_label", ""))
        slug = str(data.get("slug", ""))

        label_base = label.rstrip("()")

        if nid == query_clean or label == query_clean or name == query_clean or label_base == query_base:
            exact_matches.append(nid)
            continue

        if (
            nid.lower() == query_lower
            or label.lower() == query_lower
            or name.lower() == query_lower
            or label_base.lower() == query_base_lower
            or norm_label == query_base_lower
            or slug == query_base_lower
        ):
            case_matches.append(nid)

    return exact_matches if exact_matches else case_matches


def find_definitions(
    query: str,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
    root_dir: str = ".",
    project_path: str | None = None,
) -> dict:
    """Look up exact symbol declaration node(s) in graph.json.

    Returns dict with symbol definitions (name, type, source_file, start_line, end_line, docstring)
    and token savings metrics.
    """
    if project_path:
        root_dir = project_path
        if not graph_path and not graph:
            graph_path = str(Path(project_path) / graph_fy_OUT / "graph.json")

    G = graph if graph is not None else _load_graph_safe(graph_path)

    matched_nids = _match_nodes(G, query)
    definitions: list[dict[str, Any]] = []
    subgraph_nodes: list[dict] = []

    for nid in matched_nids:
        data = G.nodes[nid]
        subgraph_nodes.append(dict(data))
        s_line, e_line = _extract_line_range(data)
        doc = _extract_docstring(G, nid, data)
        name = data.get("name") or data.get("label") or nid
        if isinstance(name, str) and name.endswith("()"):
            name = name[:-2]

        # 1-hop related type expansion (interfaces, base classes, return/param types)
        related_types: list[dict[str, Any]] = []
        if hasattr(G, "successors"):
            for succ in G.successors(nid):
                for ed in _safe_edge_datas(G, nid, succ):
                    rel = str(ed.get("relation", "")).lower()
                    if rel in _SUBTYPE_RELATIONS or "type" in rel:
                        s_data = G.nodes[succ]
                        related_types.append({
                            "id": succ,
                            "name": s_data.get("name") or s_data.get("label") or succ,
                            "relation": rel,
                            "type": _extract_type(s_data),
                            "source_file": str(s_data.get("source_file") or ""),
                        })

        definitions.append({
            "name": name,
            "type": _extract_type(data),
            "source_file": str(data.get("source_file") or data.get("definition_file") or ""),
            "start_line": s_line,
            "end_line": e_line,
            "docstring": doc,
            "id": nid,
            "related_types": related_types,
        })

    token_savings = estimate_token_savings(subgraph_nodes, root_dir=root_dir)

    return {
        "query": query,
        "definitions": definitions,
        "results": definitions,
        "nodes": definitions,
        "token_savings": token_savings,
        "tokens_saved": token_savings["tokens_saved"],
        "file_reads_avoided": token_savings["file_reads_avoided"],
        "compression_ratio": token_savings["compression_ratio"],
    }


def get_callers(
    symbol: str,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
    root_dir: str = ".",
    project_path: str | None = None,
) -> dict:
    """Query inbound call hierarchy (nodes that have outgoing 'calls' edge to symbol)."""
    if project_path:
        root_dir = project_path
        if not graph_path and not graph:
            graph_path = str(Path(project_path) / graph_fy_OUT / "graph.json")

    G = graph if graph is not None else _load_graph_safe(graph_path)

    matched_nids = _match_nodes(G, symbol)
    callers: list[dict[str, Any]] = []
    visited_callers: set[str] = set()
    subgraph_nodes: list[dict] = []

    for nid in matched_nids:
        subgraph_nodes.append(dict(G.nodes[nid]))
        if hasattr(G, "predecessors"):
            for pred in G.predecessors(nid):
                for ed in _safe_edge_datas(G, pred, nid):
                    rel = str(ed.get("relation", "")).lower()
                    if rel in ("calls", "call"):
                        if pred not in visited_callers:
                            visited_callers.add(pred)
                            p_data = G.nodes[pred]
                            subgraph_nodes.append(dict(p_data))
                            s_line, e_line = _extract_line_range(p_data)
                            call_site_s, _ = _extract_line_range(ed)
                            callers.append({
                                "id": pred,
                                "name": p_data.get("name") or p_data.get("label") or pred,
                                "label": p_data.get("label") or pred,
                                "type": _extract_type(p_data),
                                "source_file": str(p_data.get("source_file") or ""),
                                "start_line": s_line,
                                "end_line": e_line,
                                "source_location": str(p_data.get("source_location") or ""),
                                "call_site": str(ed.get("source_location") or ""),
                                "call_line": call_site_s,
                            })

        if not callers and hasattr(G, "neighbors") and not G.is_directed():
            for nb in G.neighbors(nid):
                for ed in _safe_edge_datas(G, nb, nid):
                    rel = str(ed.get("relation", "")).lower()
                    if rel in ("calls", "call"):
                        if ed.get("source") and ed.get("source") != nb:
                            continue
                        if nb not in visited_callers:
                            visited_callers.add(nb)
                            nb_data = G.nodes[nb]
                            subgraph_nodes.append(dict(nb_data))
                            s_line, e_line = _extract_line_range(nb_data)
                            call_site_s, _ = _extract_line_range(ed)
                            callers.append({
                                "id": nb,
                                "name": nb_data.get("name") or nb_data.get("label") or nb,
                                "label": nb_data.get("label") or nb,
                                "type": _extract_type(nb_data),
                                "source_file": str(nb_data.get("source_file") or ""),
                                "start_line": s_line,
                                "end_line": e_line,
                                "source_location": str(nb_data.get("source_location") or ""),
                                "call_site": str(ed.get("source_location") or ""),
                                "call_line": call_site_s,
                            })

    token_savings = estimate_token_savings(subgraph_nodes, root_dir=root_dir)

    return {
        "symbol": symbol,
        "callers": callers,
        "results": callers,
        "nodes": callers,
        "token_savings": token_savings,
        "tokens_saved": token_savings["tokens_saved"],
        "file_reads_avoided": token_savings["file_reads_avoided"],
        "compression_ratio": token_savings["compression_ratio"],
    }


def get_callees(
    symbol: str,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
    root_dir: str = ".",
    project_path: str | None = None,
) -> dict:
    """Query outbound call hierarchy (nodes that symbol node has outgoing 'calls' edges to)."""
    if project_path:
        root_dir = project_path
        if not graph_path and not graph:
            graph_path = str(Path(project_path) / graph_fy_OUT / "graph.json")

    G = graph if graph is not None else _load_graph_safe(graph_path)

    matched_nids = _match_nodes(G, symbol)
    callees: list[dict[str, Any]] = []
    visited_callees: set[str] = set()
    subgraph_nodes: list[dict] = []

    for nid in matched_nids:
        subgraph_nodes.append(dict(G.nodes[nid]))
        if hasattr(G, "successors"):
            for succ in G.successors(nid):
                for ed in _safe_edge_datas(G, nid, succ):
                    rel = str(ed.get("relation", "")).lower()
                    if rel in ("calls", "call"):
                        if succ not in visited_callees:
                            visited_callees.add(succ)
                            s_data = G.nodes[succ]
                            subgraph_nodes.append(dict(s_data))
                            s_line, e_line = _extract_line_range(s_data)
                            call_site_s, _ = _extract_line_range(ed)
                            callees.append({
                                "id": succ,
                                "name": s_data.get("name") or s_data.get("label") or succ,
                                "label": s_data.get("label") or succ,
                                "type": _extract_type(s_data),
                                "source_file": str(s_data.get("source_file") or ""),
                                "start_line": s_line,
                                "end_line": e_line,
                                "source_location": str(s_data.get("source_location") or ""),
                                "call_site": str(ed.get("source_location") or ""),
                                "call_line": call_site_s,
                            })

        if not callees and hasattr(G, "neighbors") and not G.is_directed():
            for nb in G.neighbors(nid):
                for ed in _safe_edge_datas(G, nid, nb):
                    rel = str(ed.get("relation", "")).lower()
                    if rel in ("calls", "call"):
                        if ed.get("target") and ed.get("target") != nb:
                            continue
                        if nb not in visited_callees:
                            visited_callees.add(nb)
                            nb_data = G.nodes[nb]
                            subgraph_nodes.append(dict(nb_data))
                            s_line, e_line = _extract_line_range(nb_data)
                            call_site_s, _ = _extract_line_range(ed)
                            callees.append({
                                "id": nb,
                                "name": nb_data.get("name") or nb_data.get("label") or nb,
                                "label": nb_data.get("label") or nb,
                                "type": _extract_type(nb_data),
                                "source_file": str(nb_data.get("source_file") or ""),
                                "start_line": s_line,
                                "end_line": e_line,
                                "source_location": str(nb_data.get("source_location") or ""),
                                "call_site": str(ed.get("source_location") or ""),
                                "call_line": call_site_s,
                            })

    token_savings = estimate_token_savings(subgraph_nodes, root_dir=root_dir)

    return {
        "symbol": symbol,
        "callees": callees,
        "results": callees,
        "nodes": callees,
        "token_savings": token_savings,
        "tokens_saved": token_savings["tokens_saved"],
        "file_reads_avoided": token_savings["file_reads_avoided"],
        "compression_ratio": token_savings["compression_ratio"],
    }


def find_references(
    symbol: str,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
    root_dir: str = ".",
    project_path: str | None = None,
) -> dict:
    """Enumerate all cross-file usages, calls, and imports referencing the symbol."""
    if project_path:
        root_dir = project_path
        if not graph_path and not graph:
            graph_path = str(Path(project_path) / graph_fy_OUT / "graph.json")

    G = graph if graph is not None else _load_graph_safe(graph_path)

    matched_nids = _match_nodes(G, symbol)
    references: list[dict[str, Any]] = []
    seen_refs: set[tuple[str, str, str]] = set()
    subgraph_nodes: list[dict] = []

    for nid in matched_nids:
        sym_data = G.nodes[nid]
        subgraph_nodes.append(dict(sym_data))
        sym_file = str(sym_data.get("source_file") or sym_data.get("definition_file") or "").strip()

        candidate_edges: list[tuple[str, str, dict]] = []
        if hasattr(G, "predecessors"):
            for pred in G.predecessors(nid):
                for ed in _safe_edge_datas(G, pred, nid):
                    candidate_edges.append((pred, nid, ed))
        if hasattr(G, "neighbors") and not G.is_directed():
            for nb in G.neighbors(nid):
                for ed in _safe_edge_datas(G, nb, nid):
                    if (nb, nid, ed) not in candidate_edges:
                        candidate_edges.append((nb, nid, ed))

        for src, _tgt, ed in candidate_edges:
            rel = str(ed.get("relation", "")).lower()
            if rel in _REFERENCE_EXCLUDE_RELATIONS:
                continue

            src_data = G.nodes[src]
            src_file = str(ed.get("source_file") or src_data.get("source_file") or "").strip()

            if sym_file and src_file and sym_file == src_file:
                continue

            ref_key = (src, rel, str(ed.get("source_location") or ""))
            if ref_key in seen_refs:
                continue
            seen_refs.add(ref_key)

            subgraph_nodes.append(dict(src_data))
            s_line, e_line = _extract_line_range(src_data)
            ref_site_line, _ = _extract_line_range(ed)

            references.append({
                "id": src,
                "name": src_data.get("name") or src_data.get("label") or src,
                "label": src_data.get("label") or src,
                "type": _extract_type(src_data),
                "source_file": src_file,
                "start_line": s_line,
                "end_line": e_line,
                "source_location": str(src_data.get("source_location") or ""),
                "reference_location": str(ed.get("source_location") or ""),
                "reference_line": ref_site_line,
                "relation": rel,
                "context": str(ed.get("context") or ""),
            })

    token_savings = estimate_token_savings(subgraph_nodes, root_dir=root_dir)

    return {
        "symbol": symbol,
        "references": references,
        "results": references,
        "nodes": references,
        "token_savings": token_savings,
        "tokens_saved": token_savings["tokens_saved"],
        "file_reads_avoided": token_savings["file_reads_avoided"],
        "compression_ratio": token_savings["compression_ratio"],
    }


def find_implementations(
    symbol: str,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
    root_dir: str = ".",
    project_path: str | None = None,
) -> dict:
    """Find all classes/structs inheriting from or implementing the specified interface/abstract class."""
    if project_path:
        root_dir = project_path
        if not graph_path and not graph:
            graph_path = str(Path(project_path) / graph_fy_OUT / "graph.json")

    G = graph if graph is not None else _load_graph_safe(graph_path)

    matched_nids = _match_nodes(G, symbol)
    implementations: list[dict[str, Any]] = []
    visited: set[str] = set(matched_nids)
    subgraph_nodes: list[dict] = []

    queue = list(matched_nids)
    for nid in matched_nids:
        subgraph_nodes.append(dict(G.nodes[nid]))

    while queue:
        curr = queue.pop(0)
        incoming: list[tuple[str, dict]] = []
        if hasattr(G, "predecessors"):
            for pred in G.predecessors(curr):
                for ed in _safe_edge_datas(G, pred, curr):
                    incoming.append((pred, ed))
        if hasattr(G, "neighbors") and not G.is_directed():
            for nb in G.neighbors(curr):
                for ed in _safe_edge_datas(G, nb, curr):
                    if (nb, ed) not in incoming:
                        incoming.append((nb, ed))

        for node_id, ed in incoming:
            if node_id in visited:
                continue
            rel = str(ed.get("relation", "")).lower()
            if rel in _SUBTYPE_RELATIONS:
                visited.add(node_id)
                queue.append(node_id)
                n_data = G.nodes[node_id]
                subgraph_nodes.append(dict(n_data))
                s_line, e_line = _extract_line_range(n_data)
                implementations.append({
                    "id": node_id,
                    "name": n_data.get("name") or n_data.get("label") or node_id,
                    "label": n_data.get("label") or node_id,
                    "type": _extract_type(n_data),
                    "source_file": str(n_data.get("source_file") or ""),
                    "start_line": s_line,
                    "end_line": e_line,
                    "source_location": str(n_data.get("source_location") or ""),
                    "relation": rel,
                    "target": curr,
                })

    token_savings = estimate_token_savings(subgraph_nodes, root_dir=root_dir)

    return {
        "symbol": symbol,
        "implementations": implementations,
        "results": implementations,
        "nodes": implementations,
        "token_savings": token_savings,
        "tokens_saved": token_savings["tokens_saved"],
        "file_reads_avoided": token_savings["file_reads_avoided"],
        "compression_ratio": token_savings["compression_ratio"],
    }


class FlowResult(str):
    """String result of execution flow diagram that also carries structured flow data."""

    data: dict[str, Any]

    def __new__(cls, content: str, data: dict[str, Any] | None = None):
        obj = super().__new__(cls, content)
        obj.data = data or {}
        return obj

    def __getitem__(self, key: Any) -> Any:
        try:
            return super().__getitem__(key)
        except (TypeError, IndexError):
            return self.data[key]


def get_execution_flow(
    entry_point: Any = "",
    sink: str | None = None,
    max_depth: int = 15,
    graph: Any = None,
    graph_path: str | None = None,
    as_dict: bool = False,
    as_json: bool = False,
) -> Any:
    """Trace execution flow from an entry point to data sinks and return formatted diagram.

    Can be invoked with keyword arguments or a single dictionary parameter containing:
      - entry_point: str (e.g. 'POST /login', 'AuthService.authenticate', 'main()')
      - sink: str | None (e.g. 'users table', 'redis_cache')
      - max_depth: int (default 15)
      - graph / graph_path: optional graph object or path to graph.json
    """
    from graph_fy.flow import format_flow_diagram, trace_execution_flow

    entry_point_str = ""
    if isinstance(entry_point, dict):
        args = entry_point
        entry_point_str = str(args.get("entry_point") or "")
        sink = args.get("sink") or sink
        max_depth = int(args.get("max_depth", max_depth))
        graph = args.get("graph", graph)
        graph_path = args.get("graph_path", graph_path)
        as_dict = bool(args.get("as_dict", as_dict))
        as_json = bool(args.get("as_json", as_json))
    elif hasattr(entry_point, "nodes") and callable(getattr(entry_point, "nodes", None)):
        graph = entry_point
        entry_point_str = str(sink or "")
        sink = None
    else:
        entry_point_str = str(entry_point or "")

    g = graph if graph is not None else _load_graph_safe(graph_path)
    res = trace_execution_flow(g, entry_point=entry_point_str, sink=sink, max_depth=max_depth)

    if as_dict:
        return res
    if as_json:
        return json.dumps(res, indent=2)

    diagram = format_flow_diagram(res)
    return FlowResult(diagram, data=res)


FLOW_TOOL_SPEC = {
    "name": "get_execution_flow",
    "description": (
        "Trace execution flow slicing from an entry point (HTTP route, gRPC RPC, CLI command, event consumer) "
        "forward to terminal data sinks (SQL tables, Redis cache, Kafka topics, external HTTP endpoints). "
        "Optionally filter to specific paths ending at a target sink."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "entry_point": {
                "type": "string",
                "description": "Entry point identifier or label (e.g. 'POST /login', 'AuthService.authenticate', 'main()')",
            },
            "sink": {
                "type": "string",
                "description": "Optional terminal sink identifier or label (e.g. 'users table', 'redis_cache')",
            },
            "max_depth": {
                "type": "integer",
                "default": 15,
                "description": "Maximum traversal depth (default 15)",
            },
        },
        "required": ["entry_point"],
    },
}

TOOL_SPEC = FLOW_TOOL_SPEC



def get_impact_analysis(
    target_or_args: Any = "",
    depth: int = 10,
    terse: bool = False,
    graph: Any = None,
    graph_path: str | None = None,
    as_dict: bool = False,
    as_json: bool = False,
) -> Any:
    """Compute predictive impact analysis and blast radius for a target.

    Can be invoked with keyword arguments or a single dictionary parameter containing:
      - target: str (e.g. 'AuthService', 'src/auth.py', 'auth_service')
      - depth: int (default 10)
      - terse: bool (default False)
      - graph / graph_path: optional graph object or path to graph.json
    """
    from graph_fy.impact import compute_impact, format_impact_report

    target = ""
    if isinstance(target_or_args, dict):
        args = target_or_args
        target = str(args.get("target") or "")
        depth = int(args.get("depth", depth))
        terse = bool(args.get("terse", terse))
        graph = args.get("graph", graph)
        graph_path = args.get("graph_path", graph_path)
        as_dict = bool(args.get("as_dict", as_dict))
        as_json = bool(args.get("as_json", as_json))
    elif hasattr(target_or_args, "nodes") and callable(getattr(target_or_args, "nodes", None)):
        graph = target_or_args
        target = str(depth if isinstance(depth, str) else "")
    else:
        target = str(target_or_args)

    if graph is None:
        from graph_fy.affected import load_graph

        gp = Path(graph_path or _default_graph_json()).resolve()
        if not gp.exists():
            msg = f"error: graph file not found: {gp}"
            if as_dict:
                return {"error": msg, "target": target}
            return msg
        try:
            graph = load_graph(gp)
        except Exception as exc:
            msg = f"error: failed to load graph: {exc}"
            if as_dict:
                return {"error": msg, "target": target}
            return msg

    impact_data = compute_impact(graph, target=target, max_depth=depth)

    if as_dict:
        return impact_data
    if as_json:
        return json.dumps(impact_data, indent=2)

    return format_impact_report(impact_data, terse=terse)


get_blast_radius = get_impact_analysis


IMPACT_TOOL_SPEC = {
    "name": "get_impact_analysis",
    "description": (
        "Compute predictive impact analysis and blast radius for a given target symbol, node, or file. "
        "Traverses downstream dependency closure along calls, imports, inherits, and references edges, "
        "detects affected unit/integration tests, external API endpoints, IaC resources, and computes a "
        "blast radius risk score and tier (LOW, MEDIUM, HIGH, CRITICAL)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Target symbol name, node ID, or source file path",
            },
            "depth": {
                "type": "integer",
                "default": 10,
                "description": "Downstream traversal depth (default 10)",
            },
            "terse": {
                "type": "boolean",
                "default": False,
                "description": "Return compact 1-line summary",
            },
        },
        "required": ["target"],
    },
}

BLAST_TOOL_SPEC = {
    "name": "get_blast_radius",
    "description": "Alias for get_impact_analysis: assess blast radius and downstream dependents for a target.",
    "inputSchema": IMPACT_TOOL_SPEC["inputSchema"],
}

FIND_DEFINITIONS_TOOL_SPEC = {
    "name": "find_definitions",
    "description": "Look up exact symbol declaration node(s) in graph.json, returning name, type, source_file, line ranges (start_line, end_line), and docstring with token savings.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Exact symbol name to look up",
            },
        },
        "required": ["query"],
    },
}

GET_CALLERS_TOOL_SPEC = {
    "name": "get_callers",
    "description": "Query inbound call hierarchy (nodes that have outgoing 'calls' edge to the symbol node).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Symbol name to query callers for",
            },
        },
        "required": ["symbol"],
    },
}

GET_CALLEES_TOOL_SPEC = {
    "name": "get_callees",
    "description": "Query outbound call hierarchy (nodes that the symbol node has outgoing 'calls' edges to).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Symbol name to query callees for",
            },
        },
        "required": ["symbol"],
    },
}

FIND_REFERENCES_TOOL_SPEC = {
    "name": "find_references",
    "description": "Enumerate all cross-file usages, calls, and imports referencing the symbol.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Symbol name to find references for",
            },
        },
        "required": ["symbol"],
    },
}

FIND_IMPLEMENTATIONS_TOOL_SPEC = {
    "name": "find_implementations",
    "description": "Find all classes/structs inheriting from or implementing the specified interface/abstract class.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Interface or abstract class symbol name",
            },
        },
        "required": ["symbol"],
    },
}

def get_code_skeleton(
    path: str,
    root_dir: str = ".",
    project_path: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Extract code skeleton for a file or symbol using tree-sitter, eliding function bodies."""
    if project_path:
        root_dir = project_path

    p = Path(path)
    if not p.is_absolute():
        p = Path(root_dir) / p

    from graph_fy.fast_query import skeletonize_single_file

    res = skeletonize_single_file(p, symbol_name=symbol)
    res["file"] = str(path)
    return res


def fast_ast_search(
    query: str,
    target: str = ".",
    limit: int = 10,
    root_dir: str = ".",
    project_path: str | None = None,
) -> dict[str, Any]:
    """Fast unindexed lexical search + on-demand Tree-sitter AST extraction (<100ms)."""
    if project_path:
        root_dir = project_path
    p = Path(target)
    if not p.is_absolute():
        p = Path(root_dir) / p

    from graph_fy.fast_query import fast_ast_grep
    import time

    t0 = time.perf_counter()
    hits = fast_ast_grep(query, target=p, limit=limit)
    elapsed = time.perf_counter() - t0
    return {
        "query": query,
        "target": str(target),
        "count": len(hits),
        "elapsed_s": round(elapsed, 4),
        "hits": hits,
    }


def get_symbol_implementation(
    node_id_or_symbol: str,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
    root_dir: str = ".",
    project_path: str | None = None,
    skeletonize: bool = False,
) -> dict[str, Any]:
    """Retrieve full or skeletonized implementation code for a specific symbol node."""
    if project_path:
        root_dir = project_path
        if not graph_path and not graph:
            graph_path = str(Path(project_path) / graph_fy_OUT / "graph.json")

    G = graph if graph is not None else _load_graph_safe(graph_path)

    target_nid = node_id_or_symbol.strip()
    if target_nid not in G:
        matches = _match_nodes(G, target_nid)
        if matches:
            target_nid = matches[0]
        else:
            return {"error": f"Symbol not found: {node_id_or_symbol}", "code": ""}

    data = G.nodes[target_nid]
    src_file = str(data.get("source_file") or data.get("definition_file") or "").strip()
    src_loc = str(data.get("source_location") or data.get("definition_location") or "").strip()

    if not src_file:
        return {"error": f"No source file for {target_nid}", "code": ""}

    p = Path(src_file)
    if not p.is_absolute():
        p = Path(root_dir) / p

    line_m = re.search(r"L?(\d+)", src_loc)
    line_num = int(line_m.group(1)) if line_m else 1

    from graph_fy.skeleton import get_symbol_code_from_file

    code = get_symbol_code_from_file(p, line_num, skeletonize=skeletonize)

    return {
        "node_id": target_nid,
        "label": data.get("label", target_nid),
        "source_file": src_file,
        "source_location": src_loc,
        "code": code,
        "skeletonized": skeletonize,
    }


def get_diff_context(
    base_ref: str = "HEAD",
    root_dir: str = ".",
    token_budget: int = 2000,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
) -> dict:
    """Retrieve git diff-scoped ego-network context for code review / PR analysis."""
    from graph_fy.query_engine import GraphQueryEngine
    return GraphQueryEngine.diff_context(
        graph=graph,
        base_ref=base_ref,
        repo_root=root_dir,
        token_budget=token_budget,
        graph_path=graph_path,
    )


def refine_context(
    code_draft: str,
    root_dir: str = ".",
    token_budget: int = 2000,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
) -> dict:
    """Iterative feedback querying for missing dependencies from code draft."""
    from graph_fy.retrieval import refine_query
    return refine_query(
        code_draft=code_draft,
        graph=graph,
        token_budget=token_budget,
        graph_path=graph_path,
    )


def get_agent_context(
    task: str,
    root_dir: str = ".",
    token_budget: int = 1500,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
    include_diagram: bool = True,
    include_bodies: bool | str = "auto",
) -> dict:
    """Generate a holistic, deterministic pre-agent architectural briefing before LLM calls."""
    from graph_fy.query_engine import GraphQueryEngine
    return GraphQueryEngine.task_context(
        task=task,
        graph=graph,
        root_dir=root_dir,
        token_budget=token_budget,
        graph_path=graph_path,
        include_diagram=include_diagram,
        include_bodies=include_bodies,
    )


def get_test_selection(
    since: str = "HEAD~1",
    root_dir: str = ".",
    coverage_file: str | None = None,
    runner: str = "auto",
    max_tests: int = 50,
    graph: nx.Graph | None = None,
    graph_path: str | None = None,
) -> dict:
    """Select the minimal subset of tests affected by git changes."""
    from graph_fy.commands.test_select import select_affected_tests
    from graph_fy.affected import load_graph
    if graph is None:
        gp = Path(graph_path) if graph_path else Path(root_dir) / "graph_fy_out" / "graph.json"
        if not gp.exists():
            gp = Path(root_dir) / "graph.json"
        if gp.exists():
            graph = load_graph(gp)
        else:
            return {"error": "Knowledge graph not found", "selected_tests": []}
    return select_affected_tests(
        graph=graph,
        since=since,
        root_dir=root_dir,
        coverage_file=coverage_file,
        runner=runner,
        max_tests=max_tests,
    )


GET_CODE_SKELETON_TOOL_SPEC = {
    "name": "get_code_skeleton",
    "description": "Extract Tree-sitter interface skeleton of a file or specific symbol (signatures, types, docstrings; bodies elided).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Source file path to extract skeleton for",
            },
            "symbol": {
                "type": "string",
                "description": "Optional symbol name to extract skeleton for a specific class or function within the file",
            },
        },
        "required": ["path"],
    },
}

FAST_AST_SEARCH_TOOL_SPEC = {
    "name": "fast_ast_search",
    "description": "Sub-second (<100ms) unindexed lexical search and on-demand Tree-sitter AST extraction without requiring a pre-built graph.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Symbol name or pattern to search for",
            },
            "target": {
                "type": "string",
                "default": ".",
                "description": "Directory or file to search",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "description": "Maximum number of AST symbol results to return",
            },
        },
        "required": ["query"],
    },
}

GET_SYMBOL_IMPLEMENTATION_TOOL_SPEC = {
    "name": "get_symbol_implementation",
    "description": "Retrieve exact implementation code for a specific node_id or symbol on demand.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "node_id_or_symbol": {
                "type": "string",
                "description": "Node ID or symbol name to extract implementation for",
            },
            "skeletonize": {
                "type": "boolean",
                "default": False,
                "description": "Whether to elide nested function bodies inside the symbol",
            },
        },
        "required": ["node_id_or_symbol"],
    },
}

GET_DIFF_CONTEXT_TOOL_SPEC = {
    "name": "get_diff_context",
    "description": "Retrieve git diff-scoped ego-network context (touched symbols, 1-hop callers, affected tests, API routes) for code review.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "base_ref": {
                "type": "string",
                "default": "HEAD",
                "description": "Git ref to diff against (e.g. HEAD, origin/main, HEAD~1)",
            },
            "token_budget": {
                "type": "integer",
                "default": 2000,
                "description": "Maximum token budget for returned review context",
            },
        },
    },
}

REFINE_CONTEXT_TOOL_SPEC = {
    "name": "refine_context",
    "description": "Iterative feedback querying (RepoCoder): extract unresolved identifiers from draft code and retrieve missing skeletons.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "code_draft": {
                "type": "string",
                "description": "Incomplete code draft written by agent",
            },
            "token_budget": {
                "type": "integer",
                "default": 2000,
                "description": "Maximum token budget for returned dependency context",
            },
        },
        "required": ["code_draft"],
    },
}

GET_AGENT_CONTEXT_TOOL_SPEC = {
    "name": "get_agent_context",
    "description": "Generate a holistic, deterministic pre-agent architectural briefing (subsystems, Mermaid diagram, Tier 1 AST skeletons, execution flow, co-changes, tests, token savings) before calling frontier models.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Coding task or architectural inquiry description",
            },
            "token_budget": {
                "type": "integer",
                "default": 1500,
                "description": "Maximum token budget for prompt-ready briefing",
            },
            "include_diagram": {
                "type": "boolean",
                "default": True,
                "description": "Whether to include a Mermaid architectural flow diagram",
            },
            "include_bodies": {
                "type": "boolean",
                "default": False,
                "description": "Whether to progressively hydrate full verified AST function/method bodies for primary focus symbols instead of eliding to Tier 1 skeletons",
            },
        },
        "required": ["task"],
    },
}

GET_TEST_SELECTION_TOOL_SPEC = {
    "name": "get_test_selection",
    "description": "Select the minimal subset of unit/integration tests affected by a git diff or code modification and output ready-to-run test execution commands.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "default": "HEAD~1",
                "description": "Git ref to diff against (e.g. HEAD~1, origin/main, HEAD)",
            },
            "coverage_file": {
                "type": "string",
                "description": "Optional path to coverage artifact (coverage.xml, lcov.info, .coverage)",
            },
            "runner": {
                "type": "string",
                "default": "auto",
                "description": "Test runner override (pytest, cargo, vitest, jest, npm, go, mvn, gradle)",
            },
            "max_tests": {
                "type": "integer",
                "default": 50,
                "description": "Maximum number of affected tests to return",
            },
        },
    },
}

CODEGRAPH_TOOL_SPECS = [
    FIND_DEFINITIONS_TOOL_SPEC,
    GET_CALLERS_TOOL_SPEC,
    GET_CALLEES_TOOL_SPEC,
    FIND_REFERENCES_TOOL_SPEC,
    FIND_IMPLEMENTATIONS_TOOL_SPEC,
    GET_CODE_SKELETON_TOOL_SPEC,
    GET_SYMBOL_IMPLEMENTATION_TOOL_SPEC,
    GET_DIFF_CONTEXT_TOOL_SPEC,
    REFINE_CONTEXT_TOOL_SPEC,
    GET_AGENT_CONTEXT_TOOL_SPEC,
    GET_TEST_SELECTION_TOOL_SPEC,
    FAST_AST_SEARCH_TOOL_SPEC,
]


def serve(graph_path: str | None = None) -> None:
    """Start the MCP server over stdio."""
    from graph_fy.serve import serve as _serve

    _serve(graph_path)
