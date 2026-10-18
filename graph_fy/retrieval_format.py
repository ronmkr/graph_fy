from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import networkx as nx


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


def format_compact_path(path: list[dict[str, Any]]) -> str:
    """Format a sequence of edge steps into compact path: [A] -(calls)-> [B] -(imports)-> [C]."""
    if not path:
        return ""
    start = path[0].get("from", "")
    parts = [f"[{start}]"]
    for step in path:
        rel = step.get("relation", "connected")
        to = step.get("to", "")
        parts.append(f"-({rel})-> [{to}]")
    return " ".join(parts)


def _format_source_location(item: dict[str, Any]) -> str:
    src_path = str(item.get("source_path") or item.get("source_file") or "").strip()
    src_loc = str(item.get("source_location") or "").strip()
    if src_path and src_loc:
        if src_loc.startswith(f"{src_path}:"):
            return src_loc
        return f"{src_path}:{src_loc}"
    return src_path or src_loc


def format_query_results(
    results: list[dict[str, Any]] | dict[str, Any] | str,
    *,
    terse: bool = False,
    caveman: bool = False,
    skeleton: bool = False,
) -> str:
    """Format query/retrieval results into dense fragments or standard JSON."""
    if not (terse or caveman):
        if isinstance(results, str):
            return results
        return json.dumps(results, indent=2)

    if isinstance(results, str):
        lines: list[str] = []
        for line in results.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("NODE "):
                m = re.match(r"^NODE\s+(.*?)\s+\[(.*)\]$", s)
                if m:
                    lbl = m.group(1).strip()
                    attrs_raw = m.group(2)
                    attrs = dict(re.findall(r"(\w+)=([^\s\]]+)", attrs_raw))
                    src = attrs.get("src", "")
                    loc = attrs.get("loc", "")
                    loc_str = f"{src}:{loc}" if src and loc else (src or loc)
                    lines.append(f"[{lbl}] {lbl}{' ' + loc_str if loc_str else ''}".strip())
                else:
                    lines.append(s)
            elif s.startswith("EDGE "):
                m = re.match(r"^EDGE\s+(\S+)\s+--(\w+)\s+\[.*?\]-->\s+(\S+)", s)
                if m:
                    u, rel, v = m.group(1), m.group(2), m.group(3)
                    lines.append(f"  {rel} -> {v}")
                    lines.append(f"  [{u}] -({rel})-> [{v}]")
                else:
                    lines.append(s)
        return "\n".join(lines)

    items: list[dict[str, Any]] = [results] if isinstance(results, dict) else list(results)
    if not items:
        return ""

    lines = []
    for item in items:
        nid = str(item.get("node_id") or item.get("id") or "").strip()
        label = str(item.get("label") or nid).strip()
        loc = _format_source_location(item)
        header = f"[{nid}] {label}{' ' + loc if loc else ''}".strip()
        lines.append(header)

        path = item.get("connected_via") or item.get("path") or []
        if isinstance(path, list) and path:
            for step in path:
                if isinstance(step, dict):
                    rel = step.get("relation", "connected")
                    to = step.get("to") or step.get("target", "")
                    if to:
                        lines.append(f"  {rel} -> {to}")
            compact_path = format_compact_path(path)
            if compact_path:
                lines.append(f"  {compact_path}")

        edges = item.get("edges") or item.get("connections") or []
        if isinstance(edges, list):
            for edge in edges:
                if isinstance(edge, dict):
                    rel = edge.get("relation", "connected")
                    tgt = edge.get("target") or edge.get("to", "")
                    if tgt:
                        lines.append(f"  {rel} -> {tgt}")
                elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
                    lines.append(f"  {edge[0]} -> {edge[1]}")

        if skeleton and item.get("callers"):
            lines.append(f"  [callers: {', '.join(item['callers'])}]")
        if skeleton and item.get("callees"):
            lines.append(f"  [callees: {', '.join(item['callees'])}]")

        if skeleton and item.get("skeleton"):
            lines.append("  ```")
            for sk_line in str(item["skeleton"]).splitlines():
                lines.append(f"  {sk_line}")
            lines.append("  ```")

        if skeleton and item.get("related_types"):
            for rt in item["related_types"]:
                rt_nid = rt.get("node_id", "")
                rt_rel = rt.get("relation", "")
                lines.append(f"  [type: {rt_rel} -> {rt_nid}]")
                lines.append("  ```")
                for rsk_line in str(rt.get("skeleton", "")).splitlines():
                    lines.append(f"  {rsk_line}")
                lines.append("  ```")

    return "\n".join(lines)


def retrieve(
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
) -> list[dict[str, Any]] | str:
    """Execute GraphRAG retrieval with optional terse/caveman token conservation."""
    from graph_fy.retrieval import query

    raw = query(
        query_text,
        graph=graph,
        db_path=db_path,
        limit=limit,
        hops=hops,
        graph_path=graph_path,
        synonyms=synonyms,
        fuzzy=fuzzy,
        fuzzy_min_results=fuzzy_min_results,
        skeleton=skeleton,
    )
    if terse or caveman:
        return format_query_results(raw, terse=True, skeleton=skeleton)
    return raw


def format_rag_prompt(
    results: list[dict[str, Any]],
    question: str,
    *,
    graph: nx.Graph | None = None,
    token_budget: int = 2000,
) -> str:
    """Format retrieval results into a compact, prompt-cache friendly RAG context block.

    Features:
      - Adaptive multi-tier elision ladder (Tier 1 full skeletons for top seeds, Tier 2 compact signatures,
        Tier 3 compact path:line citations when budget is nearly exhausted).
      - Bidirectional execution slicing (caller and callee bundling).
      - Prompt-cache friendly deterministic ordering (ordered by path and line number).
      - Stable prefix structure with isolated task suffix for maximum TTFT prompt caching.
    """
    if not results:
        return f"### Codebase Architecture & Context\n\n### Codebase Context for: {question}\n"

    from graph_fy.skeleton import compact_signature

    # 1. Execution slicing enrichment if graph provided
    if graph is not None:
        for item in results:
            nid = item.get("node_id")
            if nid and nid in graph:
                callers, callees = _find_callers_and_callees(graph, nid)
                if "callers" not in item:
                    item["callers"] = callers
                if "callees" not in item:
                    item["callees"] = callees

    # 2. Identify top seeds vs medium/periphery before sorting
    max_score = max((float(it.get("score", 0.0)) for it in results), default=1.0)
    if max_score <= 0.0:
        max_score = 1.0

    candidate_target_tiers: dict[str, int] = {}
    for idx, it in enumerate(results):
        nid = str(it.get("node_id") or f"cand_{idx}")
        score = float(it.get("score", 0.0))
        dist = len(it.get("connected_via") or [])
        if idx == 0 or score >= max_score * 0.85:
            candidate_target_tiers[nid] = 1
        elif dist > 1 or score < max_score * 0.4:
            candidate_target_tiers[nid] = 3
        else:
            candidate_target_tiers[nid] = 2

    # 3. Deterministically sort candidate blocks by (source_path, line_number, label)
    def _sort_key(it: dict[str, Any]) -> tuple[str, int, str]:
        spath = str(it.get("source_path") or it.get("source_file") or "").lower()
        sloc = str(it.get("source_location") or "")
        m = re.search(r"L?(\d+)", sloc)
        line_num = int(m.group(1)) if m else 0
        lbl = str(it.get("label") or "")
        return (spath, line_num, lbl)

    sorted_results = sorted(results, key=_sort_key)

    def _render_tier1(item: dict[str, Any]) -> str:
        label = str(item.get("label") or item.get("node_id") or "")
        loc = _format_source_location(item)
        score = item.get("score", 0.0)
        lines = [f"#### {label} ({loc}) [relevance: {score}]"]
        if item.get("skeleton"):
            lines.append("```")
            lines.append(str(item["skeleton"]))
            lines.append("```")
        elif item.get("text"):
            lines.append(f"> {item['text']}")

        if item.get("callers"):
            top_c = item["callers"][:6]
            c_extra = len(item["callers"]) - len(top_c)
            lines.append(f"- Callers: {', '.join(top_c)}{f' (+{c_extra} more)' if c_extra > 0 else ''}")
        if item.get("callees"):
            top_cal = item["callees"][:6]
            cal_extra = len(item["callees"]) - len(top_cal)
            lines.append(f"- Callees: {', '.join(top_cal)}{f' (+{cal_extra} more)' if cal_extra > 0 else ''}")

        if item.get("related_types"):
            for rt in item["related_types"]:
                rt_nid = rt.get("node_id", "")
                rt_rel = rt.get("relation", "")
                lines.append(f"- Related {rt_rel} -> `{rt_nid}`:")
                lines.append("```")
                lines.append(str(rt.get("skeleton", "")))
                lines.append("```")
        return "\n".join(lines) + "\n\n"

    def _render_tier2(item: dict[str, Any]) -> str:
        label = str(item.get("label") or item.get("node_id") or "")
        loc = _format_source_location(item)
        score = item.get("score", 0.0)
        lines = [f"#### {label} ({loc}) [relevance: {score}]"]
        raw_code = str(item.get("skeleton") or item.get("text") or label)
        sig = compact_signature(raw_code)
        if sig:
            lines.append("```")
            lines.append(sig)
            lines.append("```")

        if item.get("callers"):
            top_c = item["callers"][:4]
            c_extra = len(item["callers"]) - len(top_c)
            lines.append(f"- Callers: {', '.join(top_c)}{f' (+{c_extra} more)' if c_extra > 0 else ''}")
        if item.get("callees"):
            top_cal = item["callees"][:4]
            cal_extra = len(item["callees"]) - len(top_cal)
            lines.append(f"- Callees: {', '.join(top_cal)}{f' (+{cal_extra} more)' if cal_extra > 0 else ''}")

        if item.get("related_types"):
            for rt in item["related_types"]:
                rt_nid = rt.get("node_id", "")
                rt_rel = rt.get("relation", "")
                rt_sig = compact_signature(str(rt.get("skeleton", "")))
                lines.append(f"- Related {rt_rel} -> `{rt_nid}`: {rt_sig}")
        return "\n".join(lines) + "\n\n"

    def _render_tier3(item: dict[str, Any]) -> str:
        loc_str = _format_source_location(item)
        lbl = str(item.get("label") or item.get("node_id") or "")
        if not lbl.startswith("def ") and not lbl.startswith("class ") and "(" not in lbl:
            lbl = f"def {lbl}()"
        return f"{loc_str} :: {lbl}\n"

    prefix = "### Codebase Architecture & Context\n\n"
    suffix = f"\n### Codebase Context for: {question}\n"

    prefix_tokens = len(prefix) // 4
    suffix_tokens = len(suffix) // 4
    rem_tokens = max(token_budget - prefix_tokens - suffix_tokens, 0)

    blocks: list[str] = []

    for item in sorted_results:
        nid = str(item.get("node_id") or "")
        target_tier = candidate_target_tiers.get(nid, 2)

        # Budget nearly exhausted -> degrade to Tier 3
        if rem_tokens < max(token_budget * 0.15, 60):
            target_tier = 3

        chosen_block = None

        if target_tier == 1:
            t1_block = _render_tier1(item)
            t1_tokens = len(t1_block) // 4
            if t1_tokens <= rem_tokens:
                chosen_block = t1_block
                rem_tokens -= t1_tokens
            else:
                t2_block = _render_tier2(item)
                t2_tokens = len(t2_block) // 4
                if t2_tokens <= rem_tokens:
                    chosen_block = t2_block
                    rem_tokens -= t2_tokens
                else:
                    t3_block = _render_tier3(item)
                    t3_tokens = len(t3_block) // 4
                    if t3_tokens <= rem_tokens:
                        chosen_block = t3_block
                        rem_tokens -= t3_tokens

        elif target_tier == 2:
            t2_block = _render_tier2(item)
            t2_tokens = len(t2_block) // 4
            if t2_tokens <= rem_tokens:
                chosen_block = t2_block
                rem_tokens -= t2_tokens
            else:
                t3_block = _render_tier3(item)
                t3_tokens = len(t3_block) // 4
                if t3_tokens <= rem_tokens:
                    chosen_block = t3_block
                    rem_tokens -= t3_tokens

        else:
            t3_block = _render_tier3(item)
            t3_tokens = len(t3_block) // 4
            if t3_tokens <= rem_tokens:
                chosen_block = t3_block
                rem_tokens -= t3_tokens

        if chosen_block is not None:
            blocks.append(chosen_block)
        else:
            blocks.append("// [remaining context omitted to stay within token budget]\n")
            break

    return prefix + "".join(blocks) + suffix
