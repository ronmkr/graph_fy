"""impact.py — Predictive Impact Analysis & Blast Radius Engine (GitNexus feature).

Computes downstream transitive dependency closure along architectural and
execution edges, identifies affected unit/integration tests, external API
endpoints, IaC/cloud resources, and calculates a holistic blast radius risk score.
"""

from __future__ import annotations

import math
from collections import deque
from pathlib import Path
from typing import Any

import networkx as nx

from graph_fy.affected import _as_repo_relative, _bare_name, _normalize_label, _prefer_file_node

# Edge relations relevant to downstream transitive impact
IMPACT_RELATIONS: frozenset[str] = frozenset({
    "calls",
    "indirect_call",
    "imports",
    "imports_from",
    "inherits",
    "implements",
    "references",
    "uses",
    "depends_on",
    "subscribes_to",
})

# External API endpoint node types and label patterns
_API_NODE_TYPES: frozenset[str] = frozenset({"api", "rpc", "endpoint"})
_API_HTTP_METHODS: tuple[str, ...] = ("GET ", "POST ", "PUT ", "DELETE ", "PATCH ", "HEAD ", "OPTIONS ")

# IaC & config resource node types
_IAC_NODE_TYPES: frozenset[str] = frozenset({"k8s", "terraform", "helm", "kafka", "docker"})

# Built-in or noise labels to exclude from god node detection
_BUILTIN_NOISE_LABELS: frozenset[str] = frozenset({
    "__init__", "self", "cls", "None", "True", "False",
    "string", "number", "boolean", "any", "void", "object",
    "str", "int", "float", "bool", "dict", "list", "set", "tuple",
    "Optional", "Union", "List", "Dict", "Set", "Tuple", "Any",
    "ctx", "context", "err", "error", "nil", "ok",
    "Promise", "Observable", "Task", "Result",
})


def _is_file_or_concept(data: dict) -> bool:
    file_type = str(data.get("file_type") or "").lower()
    return file_type in ("file", "concept", "directory", "module")


def _is_test_node(data: dict) -> bool:
    src_file = str(data.get("source_file") or "").lower()
    label = str(data.get("label") or "").lower()
    return (
        "test_" in src_file
        or "_test" in src_file
        or "tests/" in src_file
        or "/tests/" in src_file
        or src_file.startswith("tests/")
        or "test_" in label
        or "_test" in label
        or label.startswith("test")
    )


def _is_api_endpoint(data: dict) -> bool:
    node_type = str(data.get("type") or data.get("file_type") or data.get("kind") or "").lower()
    if node_type in _API_NODE_TYPES:
        return True
    label = str(data.get("label") or "")
    for method in _API_HTTP_METHODS:
        if label.startswith(method) or f" {method}" in label:
            return True
    return False


def _is_iac_resource(data: dict) -> bool:
    node_type = str(data.get("type") or data.get("file_type") or data.get("kind") or "").lower()
    if node_type in _IAC_NODE_TYPES:
        return True
    src_file = str(data.get("source_file") or "").lower()
    if any(k in src_file for k in ("dockerfile", "docker-compose", "terraform", ".tf", "helm", "k8s")):
        return True
    return False


def resolve_targets(graph: nx.Graph, target: str, root: Path | None = None) -> list[str]:
    """Resolve target query string to matching node ID(s) in graph.

    Resolves by exact ID, exact label, bare callable name, exact or repo-relative source file,
    or label substring.
    """
    target_clean = target.rstrip("/\\") or target
    if target_clean in graph:
        return [target_clean]

    query_lower = _normalize_label(target_clean)

    exact_label = [
        str(n) for n, d in graph.nodes(data=True)
        if _normalize_label(str(d.get("label", ""))) == query_lower
    ]
    if exact_label:
        return exact_label

    query_bare = _bare_name(query_lower)
    bare_matches = [
        str(n) for n, d in graph.nodes(data=True)
        if _bare_name(str(d.get("label", ""))) == query_bare
    ]
    if bare_matches:
        return bare_matches

    query_path = _normalize_label(_as_repo_relative(target_clean, root))
    exact_source = [
        str(n) for n, d in graph.nodes(data=True)
        if _normalize_label(str(d.get("source_file", ""))) in (query_lower, query_path)
    ]
    if exact_source:
        pref = _prefer_file_node(graph, exact_source, _as_repo_relative(target_clean, root))
        if pref is not None:
            return [pref]
        return exact_source

    substr_matches = [
        str(n) for n, d in graph.nodes(data=True)
        if query_lower in _normalize_label(str(d.get("label", "")))
        or query_lower in _normalize_label(str(n))
    ]
    return substr_matches


def compute_impact(
    graph: nx.Graph,
    target: str,
    max_depth: int = 10,
    root: Path | None = None,
    pagerank: dict[Any, float] | None = None,
) -> dict[str, Any]:
    """Compute downstream transitive dependency closure and blast radius risk.

    Args:
        graph: NetworkX graph.
        target: Target node id, label, or file path.
        max_depth: Maximum traversal depth for downstream impact (default 10).
        root: Optional repository root for path resolution.
        pagerank: Optional precomputed PageRank scores dictionary.

    Returns:
        Dictionary containing matched targets, downstream dependents, categorized
        affected assets, blast radius score, risk tier, and detailed breakdown.
    """
    targets = resolve_targets(graph, target, root=root)
    if not targets:
        return {
            "target": target,
            "target_nodes": [],
            "target_labels": [],
            "error": f"No node found matching '{target}'",
            "downstream_count": 0,
            "downstream_nodes": [],
            "affected_tests": [],
            "affected_apis": [],
            "affected_iac": [],
            "communities_crossed": [],
            "god_nodes_impacted": [],
            "risk_score": 0.0,
            "risk_tier": "LOW",
            "explanation": f"Target '{target}' could not be resolved to any graph nodes.",
        }

    # Identify god nodes (top 5% or top 10 most connected real entities)
    god_node_set = getattr(graph, "_cached_god_nodes", None)
    if god_node_set is None:
        degrees = dict(graph.degree())
        real_degrees = {
            n: d for n, d in degrees.items()
            if not _is_file_or_concept(graph.nodes[n])
            and graph.nodes[n].get("label", "") not in _BUILTIN_NOISE_LABELS
        }
        god_threshold = 10
        if real_degrees:
            sorted_degs = sorted(real_degrees.values(), reverse=True)
            # Top 5% threshold or minimum 10
            p95_idx = max(0, int(len(sorted_degs) * 0.05) - 1)
            god_threshold = max(10, sorted_degs[p95_idx])
        god_node_set = {n for n, d in real_degrees.items() if d >= god_threshold}
        try:
            graph._cached_god_nodes = god_node_set
        except Exception:
            pass

    # Precalculate PageRank if graph has nodes and not provided (avoid expensive full-graph PR on huge graphs)
    if pagerank is None:
        pagerank = {}
        if 0 < len(graph) <= 2000:
            from graph_fy.retrieval import compute_pagerank
            pagerank = compute_pagerank(graph)

    # Graph edge traversal:
    # An edge A -> B with relation in IMPACT_RELATIONS means A depends on B (calls, imports, inherits, references B).
    # If B (the callee/target) changes, A (the caller/dependent) is impacted.
    # Also handle reverse-directed storage: check _src / _tgt if present.
    # To find downstream dependents of seed: we look for nodes that depend on seed.
    # In graph terms, if caller -> callee, we follow in-edges in DiGraph, or edges where target==current.

    seen = set(targets)
    queue: deque[tuple[str, int]] = deque((t, 0) for t in targets)
    downstream_entries: list[dict[str, Any]] = []

    # Also seed with member nodes (methods/contains) if any target is a class/module
    for t in targets:
        if hasattr(graph, "out_edges"):
            out_edges = graph.out_edges(t, data=True)
        else:
            out_edges = (
                (t, nbr if s == t else s, d)
                for s, nbr, d in graph.edges(t, data=True)
            )
        for _s, member, edata in out_edges:
            rel = str(edata.get("relation", "")).lower()
            if rel in ("method", "contains"):
                m_str = str(member)
                if m_str not in seen:
                    seen.add(m_str)
                    queue.append((m_str, 0))

    while queue:
        if len(downstream_entries) >= 2000:
            break
        curr, d = queue.popleft()
        if d >= max_depth:
            continue

        # In-edges where someone points to curr
        if hasattr(graph, "in_edges"):
            incoming = graph.in_edges(curr, data=True)
        else:
            incoming = (
                (nbr if u == curr else u, curr, edata)
                for u, nbr, edata in graph.edges(curr, data=True)
            )
        for u, _v, edata in incoming:
            rel = str(edata.get("relation", "")).lower()
            if rel not in IMPACT_RELATIONS:
                continue
            u_str = str(u)
            if u_str not in seen:
                seen.add(u_str)
                u_data = graph.nodes[u_str]
                downstream_entries.append({
                    "id": u_str,
                    "label": str(u_data.get("label") or u_str),
                    "depth": d + 1,
                    "relation": rel,
                    "source_file": str(u_data.get("source_file") or ""),
                    "community": u_data.get("community"),
                    "type": str(u_data.get("type") or u_data.get("file_type") or ""),
                })
                queue.append((u_str, d + 1))

    # Detect affected tests, APIs, IaC
    affected_tests: list[dict[str, Any]] = []
    affected_apis: list[dict[str, Any]] = []
    affected_iac: list[dict[str, Any]] = []
    communities: set[int] = set()

    for item in downstream_entries:
        nid = item["id"]
        ndata = graph.nodes[nid]
        c = ndata.get("community")
        if c is not None:
            try:
                communities.add(int(c))
            except (ValueError, TypeError):
                pass

        if _is_test_node(ndata):
            affected_tests.append(item)
        if _is_api_endpoint(ndata):
            affected_apis.append(item)
        if _is_iac_resource(ndata):
            affected_iac.append(item)

    god_impacted = [
        item for item in downstream_entries
        if item["id"] in god_node_set
    ]

    # Calculate Blast Radius Risk Score
    # Factors:
    # 1. Dependent count (log-scaled)
    # 2. God nodes impacted (high impact multiplier)
    # 3. Community boundaries crossed (architectural spread)
    # 4. Centrality (PageRank / degree centrality of target and dependents)

    downstream_count = len(downstream_entries)
    total_graph_nodes = max(1, len(graph))

    # Metric 1: Dependent scale factor (0.0 to 0.40)
    # 1 node -> ~0.08, 5 nodes -> ~0.20, 20+ nodes -> ~0.35, 50+ nodes -> 0.40
    count_score = min(0.40, 0.40 * (math.log(downstream_count + 1) / math.log(50)))

    # Metric 2: God node factor (0.0 to 0.25)
    # Impacting god nodes severely increases regression and ripple risk
    god_score = min(0.25, len(god_impacted) * 0.125)

    # Metric 3: Community boundary factor (0.0 to 0.20)
    # Crosses multi-community boundaries = architectural impact
    comm_count = len(communities)
    comm_score = min(0.20, 0.05 * comm_count)

    # Metric 4: Centrality & Target importance (0.0 to 0.15)
    if pagerank:
        target_prs = [pagerank.get(t, 0.0) for t in targets]
        target_max_pr = max(target_prs, default=0.0)
        max_graph_pr = max(pagerank.values(), default=1.0) or 1.0
        pr_ratio = min(1.0, target_max_pr / max_graph_pr) if max_graph_pr > 0 else 0.0
    else:
        max_deg = max(real_degrees.values(), default=1) or 1
        target_max_deg = max((real_degrees.get(t, 0) for t in targets), default=0)
        pr_ratio = min(1.0, target_max_deg / max_deg) if max_deg > 0 else 0.0
    centrality_score = 0.15 * pr_ratio

    total_score = round(count_score + god_score + comm_score + centrality_score, 3)
    total_score = max(0.0, min(1.0, total_score))

    if total_score >= 0.70:
        tier = "CRITICAL"
    elif total_score >= 0.45:
        tier = "HIGH"
    elif total_score >= 0.20:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    # Explanation construction
    explanations: list[str] = [
        f"{downstream_count} downstream dependent{'s' if downstream_count != 1 else ''} found within depth {max_depth}."
    ]
    if god_impacted:
        god_names = ", ".join(f"'{g['label']}'" for g in god_impacted[:3])
        explanations.append(f"Directly impacts {len(god_impacted)} architectural hub/god node(s): {god_names}.")
    if comm_count > 1:
        explanations.append(f"Crosses {comm_count} architectural community boundaries.")
    if affected_apis:
        explanations.append(f"{len(affected_apis)} external API endpoint(s) potentially affected.")
    if affected_iac:
        explanations.append(f"{len(affected_iac)} IaC/cloud config resource(s) impacted.")
    # Dynamic Language Risk & Confidence Rating
    dynamic_risks: set[str] = set()
    root_path = root if root is not None else Path(".")

    all_evaluated = list(targets) + [d["id"] for d in downstream_entries[:30]]
    for nid in all_evaluated:
        ndata = graph.nodes.get(nid, {})
        lbl = str(ndata.get("label", ""))
        ctx = str(ndata.get("context", ""))
        if any(p in lbl.lower() for p in ("getattr", "setattr", "hasattr", "importlib", "eval", "exec", "**kwargs")):
            dynamic_risks.add(f"dynamic-symbol:{lbl}")
        if ndata.get("has_dependency_injection") or ctx == "fastapi_di":
            dynamic_risks.add("fastapi-di")
        if ctx == "pytest_fixture":
            dynamic_risks.add("pytest-fixture")
        if ctx == "express_middleware":
            dynamic_risks.add("express-middleware")
        if ctx == "react_hook":
            dynamic_risks.add("react-hook")

    seen_files: set[str] = set()
    for nid in all_evaluated[:15]:
        sf = str(graph.nodes.get(nid, {}).get("source_file", ""))
        if sf and sf not in seen_files:
            seen_files.add(sf)
            fp = Path(sf) if Path(sf).is_absolute() else (root_path / sf)
            if fp.is_file():
                try:
                    ftext = fp.read_text(encoding="utf-8", errors="ignore")
                    if "getattr(" in ftext or "setattr(" in ftext or "hasattr(" in ftext:
                        dynamic_risks.add("reflection/getattr")
                    if "importlib" in ftext or "__import__" in ftext:
                        dynamic_risks.add("dynamic-import")
                    if "Depends(" in ftext:
                        dynamic_risks.add("fastapi-di")
                    if "@pytest.fixture" in ftext:
                        dynamic_risks.add("pytest-fixture")
                    if "**kwargs" in ftext:
                        dynamic_risks.add("kwargs-forwarding")
                except OSError:
                    pass

    if dynamic_risks:
        confidence_rating = "DYNAMIC_RISK_DETECTED"
        confidence_score = 0.65
        explanations.append(
            f"Dynamic language risk detected ({', '.join(sorted(dynamic_risks)[:4])}); static blast radius may not capture runtime reflection or dynamic dispatch."
        )
    else:
        confidence_rating = "HIGH_CONFIDENCE"
        confidence_score = 1.0

    return {
        "target": target,
        "target_nodes": targets,
        "target_labels": [str(graph.nodes[t].get("label") or t) for t in targets],
        "confidence_rating": confidence_rating,
        "confidence_score": confidence_score,
        "dynamic_risks": sorted(dynamic_risks),
        "downstream_count": downstream_count,
        "downstream_nodes": downstream_entries,
        "affected_tests": affected_tests,
        "affected_apis": affected_apis,
        "affected_iac": affected_iac,
        "communities_crossed": sorted(communities),
        "god_nodes_impacted": god_impacted,
        "risk_score": total_score,
        "risk_tier": tier,
        "explanation": " ".join(explanations),
    }


def format_impact_report(impact_data: dict[str, Any], terse: bool = False) -> str:
    """Render a clean ASCII / markdown blast radius report.

    Args:
        impact_data: Output dictionary from compute_impact.
        terse: If True, outputs a compact 1-2 line summary.

    Returns:
        Formatted string representation.
    """
    if "error" in impact_data and not impact_data.get("target_nodes"):
        return f"Blast Radius Analysis: {impact_data['error']}"

    target_name = ", ".join(impact_data.get("target_labels", [])) or impact_data.get("target", "unknown")
    tier = impact_data.get("risk_tier", "LOW")
    score = impact_data.get("risk_score", 0.0)
    conf_rating = impact_data.get("confidence_rating", "HIGH_CONFIDENCE")
    conf_risks = impact_data.get("dynamic_risks", [])
    conf_display = (
        f"[DYNAMIC RISK DETECTED] ({', '.join(conf_risks[:3]) if conf_risks else 'Dynamic reflection/dispatch'})"
        if conf_rating == "DYNAMIC_RISK_DETECTED"
        else "[HIGH CONFIDENCE] (Static AST Verified)"
    )
    count = impact_data.get("downstream_count", 0)
    tests = impact_data.get("affected_tests", [])
    apis = impact_data.get("affected_apis", [])
    iac = impact_data.get("affected_iac", [])
    gods = impact_data.get("god_nodes_impacted", [])
    comms = impact_data.get("communities_crossed", [])

    if terse:
        return (
            f"Impact for {target_name}: [{tier} | {score:.2f} | {conf_rating}] "
            f"{count} dependents, {len(tests)} tests, {len(apis)} APIs, "
            f"{len(iac)} IaC, {len(comms)} communities"
        )

    # Full report
    lines: list[str] = [
        "================================================================================",
        f" PREDICTIVE IMPACT ANALYSIS & BLAST RADIUS: {target_name}",
        "================================================================================",
        f"Risk Tier:         {tier} (Score: {score:.3f} / 1.000)",
        f"Confidence:        {conf_display}",
        f"Downstream Nodes:  {count}",
        f"Communities:       {len(comms)} crossed ({comms if comms else 'none'})",
        f"God Nodes Touched: {len(gods)}",
        "--------------------------------------------------------------------------------",
        f"Summary: {impact_data.get('explanation', '')}",
        "--------------------------------------------------------------------------------",
    ]

    if apis:
        lines.append(f"\nAffected External API Endpoints ({len(apis)}):")
        for a in apis[:10]:
            lines.append(f"  * {a['label']} (depth {a['depth']}) [{a['source_file']}]")
        if len(apis) > 10:
            lines.append(f"  * ... and {len(apis) - 10} more endpoints")

    if iac:
        lines.append(f"\nAffected IaC & Cloud Resources ({len(iac)}):")
        for i in iac[:10]:
            lines.append(f"  * {i['label']} (depth {i['depth']}) [{i['source_file']}]")
        if len(iac) > 10:
            lines.append(f"  * ... and {len(iac) - 10} more resources")

    if tests:
        lines.append(f"\nAffected Tests ({len(tests)}):")
        for t in tests[:10]:
            lines.append(f"  * {t['label']} (depth {t['depth']}) [{t['source_file']}]")
        if len(tests) > 10:
            lines.append(f"  * ... and {len(tests) - 10} more tests")
    else:
        lines.append("\nAffected Tests: None detected (recommend adding regression tests)")

    if gods:
        lines.append(f"\nGod Nodes Impacted ({len(gods)}):")
        for g in gods:
            lines.append(f"  ! {g['label']} [{g['source_file']}]")

    if impact_data.get("downstream_nodes"):
        nodes = impact_data["downstream_nodes"]
        lines.append(f"\nDownstream Dependency Closure ({len(nodes)}):")
        for n in nodes[:15]:
            lines.append(f"  - [{n['relation']}] {n['label']} (depth {n['depth']}) -> {n['source_file']}")
        if len(nodes) > 15:
            lines.append(f"  - ... and {len(nodes) - 15} more dependents")

    lines.append("================================================================================")
    return "\n".join(lines)
