"""Deterministic lint and drift rules (Phase 11).

Deterministic, model-free rules:
1. Unpinned GitHub Actions: flag action references not pinned to a 40-char commit SHA.
2. Environment drift: flag resources defined in one environment (e.g. staging) but missing in another (prod).
3. Missing secret/env definitions: flag secrets/envs referenced in code or config but never defined.
4. Dangling references & orphan nodes: report unresolvable edges and isolated entities.
5. Explicit reverse backlink edges: store backlinks for O(1) "what links here" queries.
6. Dead code & unreferenced symbols: flag unreferenced callable symbols with 0 inbound calls/usages.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import networkx as nx

from graph_fy.cross_resolve import find_secret_and_env_cross_references

_ENV_NAMES = frozenset({
    "dev", "development",
    "staging", "stage",
    "prod", "production",
    "test", "qa", "uat",
})


def _detect_env_from_path(path_str: str) -> str | None:
    """Extract environment name (staging, prod, dev, etc.) from a file path."""
    parts = Path(path_str.replace("\\", "/")).parts
    for part in parts:
        lower_p = part.lower()
        if lower_p in _ENV_NAMES:
            return lower_p
        for env in _ENV_NAMES:
            if lower_p.startswith(f"{env}-") or lower_p.endswith(f"-{env}") or lower_p == env:
                return env
    return None


def check_unpinned_actions(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag GitHub Actions references that are not pinned to a 40-character commit SHA."""
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()

    for n in nodes:
        if n.get("type") == "action" or n.get("label", "").startswith("Action:"):
            act_name = n.get("action_name") or n.get("label", "").replace("Action:", "")
            pin = n.get("pinned_version", "")
            is_sha = n.get("is_sha_pinned", False)
            if not is_sha and pin:
                is_sha = bool(re.match(r"^[0-9a-fA-F]{40}$", pin))

            if not is_sha:
                key = f"{act_name}@{pin}"
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "rule": "unpinned_github_action",
                        "severity": "warning",
                        "action": act_name,
                        "pin": pin or "unversioned",
                        "source_file": n.get("source_file", ""),
                        "source_location": n.get("source_location", "L1"),
                        "message": (
                            f"GitHub Action '{act_name}' is pinned to '{pin or 'unversioned'}', "
                            f"not a 40-character commit SHA."
                        ),
                    })

    # Also inspect edge pinned_version markers
    for e in edges:
        if e.get("relation") == "uses_action":
            pin = e.get("pinned_version", "")
            is_sha = e.get("is_sha_pinned", False)
            if pin and not is_sha and not re.match(r"^[0-9a-fA-F]{40}$", pin):
                tgt = str(e.get("target", ""))
                if tgt not in seen:
                    seen.add(tgt)
                    findings.append({
                        "rule": "unpinned_github_action",
                        "severity": "warning",
                        "action": tgt,
                        "pin": pin,
                        "source_file": e.get("source_file", ""),
                        "source_location": e.get("source_location", "L1"),
                        "message": f"Action invocation target '{tgt}' uses unpinned pin '{pin}'.",
                    })

    return findings


def check_environment_drift(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag resources defined in one environment (e.g. staging) missing from another (e.g. prod)."""
    findings: list[dict[str, Any]] = []

    # Map: env -> dict[resource_identifier, node]
    env_resources: dict[str, dict[str, dict[str, Any]]] = {}

    for n in nodes:
        sf = n.get("source_file", "")
        if not sf:
            continue
        env = _detect_env_from_path(sf)
        if not env:
            continue

        res_key = n.get("resource_name") or n.get("label", "")
        if not res_key or n.get("type") in ("file", "package"):
            continue

        # Canonical identifier: resource type + simple name (stripped of env prefix/suffix)
        clean_name = re.sub(rf"[-_]{env}|{env}[-_]", "", str(res_key), flags=re.IGNORECASE)
        rtype = n.get("resource_kind") or n.get("resource_type") or n.get("type") or "resource"
        norm_id = f"{rtype}::{clean_name.lower()}"

        env_resources.setdefault(env, {})[norm_id] = n

    envs = sorted(env_resources.keys())
    # Compare staging vs prod or pairs of envs
    if "staging" in env_resources and "prod" in env_resources:
        pairs = [("staging", "prod")]
    elif len(envs) >= 2:
        pairs = [(envs[0], envs[1])]
    else:
        pairs = []

    for env_a, env_b in pairs:
        res_a = env_resources[env_a]
        res_b = env_resources[env_b]

        # Present in A but missing in B
        for rid, node_a in res_a.items():
            if rid not in res_b:
                findings.append({
                    "rule": "environment_drift",
                    "severity": "warning",
                    "resource": rid,
                    "present_in": env_a,
                    "missing_in": env_b,
                    "source_file": node_a.get("source_file", ""),
                    "message": f"Resource '{rid}' defined in environment '{env_a}' is missing in '{env_b}'.",
                })

        # Present in B but missing in A
        for rid, node_b in res_b.items():
            if rid not in res_a:
                findings.append({
                    "rule": "environment_drift",
                    "severity": "warning",
                    "resource": rid,
                    "present_in": env_b,
                    "missing_in": env_a,
                    "source_file": node_b.get("source_file", ""),
                    "message": f"Resource '{rid}' defined in environment '{env_b}' is missing in '{env_a}'.",
                })

    return findings


def check_missing_secrets_and_envs(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flag secrets or environment variables referenced in code/config but never defined."""
    findings: list[dict[str, Any]] = []
    ref_data = find_secret_and_env_cross_references(nodes, edges)

    for sec in ref_data["secrets"]["missing"]:
        findings.append({
            "rule": "missing_secret_definition",
            "severity": "error",
            "name": sec,
            "message": f"Secret '{sec}' is referenced in configuration/code but has no matching secret definition.",
        })

    for ev in ref_data["envs"]["missing"]:
        findings.append({
            "rule": "missing_env_definition",
            "severity": "warning",
            "name": ev,
            "message": f"Environment variable '{ev}' is referenced but not declared in any ConfigMap or environment specification.",
        })

    return findings


def check_dangling_and_orphans(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Report dangling edge targets and orphan nodes."""
    findings: list[dict[str, Any]] = []
    node_ids = {str(n.get("id")) for n in nodes if "id" in n}

    connected_nodes: set[str] = set()

    for e in edges:
        src = str(e.get("source", ""))
        tgt = str(e.get("target", ""))
        rel = str(e.get("relation", ""))

        if src not in node_ids:
            findings.append({
                "rule": "dangling_reference",
                "severity": "error",
                "endpoint": "source",
                "node_id": src,
                "relation": rel,
                "source_file": e.get("source_file", ""),
                "message": f"Edge references nonexistent source node '{src}'.",
            })
        else:
            connected_nodes.add(src)

        if tgt not in node_ids:
            findings.append({
                "rule": "dangling_reference",
                "severity": "error",
                "endpoint": "target",
                "node_id": tgt,
                "relation": rel,
                "source_file": e.get("source_file", ""),
                "message": f"Edge references nonexistent target node '{tgt}'.",
            })
        else:
            connected_nodes.add(tgt)

    for n in nodes:
        nid = str(n.get("id", ""))
        if nid and nid not in connected_nodes:
            findings.append({
                "rule": "orphan_node",
                "severity": "info",
                "node_id": nid,
                "label": n.get("label", nid),
                "source_file": n.get("source_file", ""),
                "message": f"Node '{n.get('label', nid)}' is an orphan with no connections.",
            })

    return findings


def check_rtk_cache_and_state(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Lint Redux Toolkit and RTK Query cache tag and route consistency.

    Checks:
    1. Undeclared cache tags: tags used in provides_tags or invalidates_tags
       that are not declared in the parent API slice's tag_types.
    2. Orphaned mutations: mutations that invalidate cache tags that no active query provides.
    """
    findings: list[dict[str, Any]] = []

    # Map API slice id -> set of tagTypes
    api_tag_types: dict[str, set[str]] = {}
    for n in nodes:
        if n.get("type") == "rtkq_api":
            api_id = str(n.get("id"))
            tags = set(n.get("tag_types", []))
            api_tag_types[api_id] = tags

    # Collect query endpoints, provides_tags, and mutation invalidates_tags
    all_provided_tags: set[str] = set()
    query_endpoints: list[dict[str, Any]] = []
    mutation_endpoints: list[dict[str, Any]] = []

    # Map endpoint id -> parent api_id
    ep_to_api: dict[str, str] = {}
    for e in edges:
        if e.get("relation") == "declares_endpoint":
            ep_to_api[str(e.get("target"))] = str(e.get("source"))

    for n in nodes:
        ntype = n.get("type")
        if ntype == "rtkq_query":
            query_endpoints.append(n)
            for t in n.get("provides_tags", []):
                all_provided_tags.add(str(t))
        elif ntype == "rtkq_mutation":
            mutation_endpoints.append(n)

    # 1. Check for undeclared tags in queries and mutations
    for ep in query_endpoints:
        ep_id = str(ep.get("id"))
        parent_api = ep_to_api.get(ep_id)
        declared_tags = api_tag_types.get(parent_api, set()) if parent_api else set()
        if declared_tags:
            for tag in ep.get("provides_tags", []):
                if str(tag) not in declared_tags:
                    findings.append({
                        "rule": "rtk_undeclared_cache_tag",
                        "severity": "warning",
                        "endpoint": ep.get("endpoint_name", ep_id),
                        "tag": str(tag),
                        "source_file": ep.get("source_file", ""),
                        "source_location": ep.get("source_location", "L1"),
                        "message": (
                            f"Query endpoint '{ep.get('endpoint_name', ep_id)}' provides cache tag '{tag}' "
                            f"which is not declared in tagTypes of parent API slice."
                        ),
                    })

    for ep in mutation_endpoints:
        ep_id = str(ep.get("id"))
        parent_api = ep_to_api.get(ep_id)
        declared_tags = api_tag_types.get(parent_api, set()) if parent_api else set()
        inv_tags = ep.get("invalidates_tags", [])
        if declared_tags:
            for tag in inv_tags:
                if str(tag) not in declared_tags:
                    findings.append({
                        "rule": "rtk_undeclared_cache_tag",
                        "severity": "warning",
                        "endpoint": ep.get("endpoint_name", ep_id),
                        "tag": str(tag),
                        "source_file": ep.get("source_file", ""),
                        "source_location": ep.get("source_location", "L1"),
                        "message": (
                            f"Mutation endpoint '{ep.get('endpoint_name', ep_id)}' invalidates cache tag '{tag}' "
                            f"which is not declared in tagTypes of parent API slice."
                        ),
                    })

        # 2. Check for orphaned mutations (invalidating tags no query provides)
        if query_endpoints and inv_tags:
            unmatched = set(str(t) for t in inv_tags) - all_provided_tags
            for tag in sorted(unmatched):
                findings.append({
                    "rule": "rtk_orphaned_mutation_tag",
                    "severity": "warning",
                    "endpoint": ep.get("endpoint_name", ep_id),
                    "tag": tag,
                    "source_file": ep.get("source_file", ""),
                    "source_location": ep.get("source_location", "L1"),
                    "message": (
                        f"Mutation '{ep.get('endpoint_name', ep_id)}' invalidates tag '{tag}', "
                        f"but no active query endpoint provides this tag."
                    ),
                })

    return findings


_DEAD_CODE_EXEMPT_NAMES = frozenset({
    "main", "cli", "run", "execute", "handler", "setup",
    "teardown", "setUp", "tearDown", "app", "create_app",
})

_CALL_RELATIONS = frozenset({
    "calls", "references", "imports", "inherits", "instantiates", "uses", "subscribes_to",
})


def check_dead_code(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flag callable code symbols (functions, methods, classes) with no inbound references."""
    findings: list[dict[str, Any]] = []

    # Target node IDs with inbound call/reference relations from other nodes
    inbound_refs: set[str] = set()
    for e in edges:
        rel = str(e.get("relation", ""))
        tgt = str(e.get("target", ""))
        src = str(e.get("source", ""))
        if src != tgt and rel in _CALL_RELATIONS:
            inbound_refs.add(tgt)

    for n in nodes:
        if n.get("file_type") != "code":
            continue
        if not n.get("_callable") and n.get("type") not in ("function", "method", "class"):
            continue

        raw_label = str(n.get("label", "")).strip("()").lstrip(".")
        if not raw_label:
            continue

        # Skip dunder methods
        if raw_label.startswith("__") and raw_label.endswith("__"):
            continue

        # Skip common entrypoints
        if raw_label in _DEAD_CODE_EXEMPT_NAMES:
            continue

        # Skip test files and test functions
        source_file = str(n.get("source_file", "")).replace("\\", "/")
        p = Path(source_file)
        file_name = p.name.lower()
        parts = {part.lower() for part in p.parts}
        if (
            file_name.startswith("test_")
            or file_name.endswith("_test.py")
            or "tests" in parts
            or "test" in parts
            or "fixtures" in parts
            or "scripts" in parts
            or "tools" in parts
            or raw_label.startswith("test_")
        ):
            continue

        nid = str(n.get("id", ""))
        if nid and nid not in inbound_refs:
            findings.append({
                "rule": "dead_code",
                "severity": "warning",
                "node_id": nid,
                "label": n.get("label", nid),
                "symbol": raw_label,
                "source_file": n.get("source_file", ""),
                "source_location": n.get("source_location", "L1"),
                "message": (
                    f"Symbol '{raw_label}' has no inbound references, calls, or import edges "
                    f"in the codebase (potential dead code)."
                ),
            })

    return findings


def check_cyclic_dependencies(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    max_cycles: int = 15,
) -> list[dict[str, Any]]:
    """Detect circular/cyclic dependencies between files and modules.

    Constructs a directed file-level dependency graph from `imports`, `imports_from`,
    and `depends_on` relations across distinct files, then detects elementary
    cycles using NetworkX.
    """
    findings: list[dict[str, Any]] = []
    nodes_by_id = {str(n.get("id")): n for n in nodes if "id" in n}

    file_graph = nx.DiGraph()
    dependency_relations = {"imports", "imports_from", "depends_on"}

    for e in edges:
        rel = str(e.get("relation", ""))
        if rel not in dependency_relations:
            continue
        src_id = str(e.get("source", ""))
        tgt_id = str(e.get("target", ""))
        src_file = e.get("source_file") or (nodes_by_id.get(src_id, {}).get("source_file"))
        tgt_file = nodes_by_id.get(tgt_id, {}).get("source_file")
        if src_file and tgt_file and src_file != tgt_file:
            file_graph.add_edge(str(src_file), str(tgt_file))

    if not file_graph.number_of_nodes():
        return findings

    try:
        # Use progressive length-bounded cycle search to prevent exponential hang on monorepos
        cycles = []
        for bound in (2, 3, 4, 6, 8):
            bound_cycles = []
            try:
                for c in nx.simple_cycles(file_graph, length_bound=bound):
                    bound_cycles.append(c)
                    if len(bound_cycles) >= max_cycles * 5:
                        break
            except TypeError:
                for c in nx.simple_cycles(file_graph):
                    bound_cycles.append(c)
                    if len(bound_cycles) >= max_cycles * 5:
                        break
                cycles = bound_cycles
                break
            if bound_cycles:
                cycles = bound_cycles
                if len(cycles) >= max_cycles:
                    break
    except Exception:
        cycles = []

    cycles.sort(key=lambda c: (len(c), c))
    seen_cycle_keys = set()
    for cycle in cycles[:max_cycles]:
        min_idx = cycle.index(min(cycle))
        canonical = tuple(cycle[min_idx:] + cycle[:min_idx])
        if canonical in seen_cycle_keys:
            continue
        seen_cycle_keys.add(canonical)

        cycle_display = " -> ".join(cycle + [cycle[0]])
        findings.append({
            "rule": "cyclic_dependency",
            "severity": "warning",
            "cycle": cycle + [cycle[0]],
            "cycle_length": len(cycle),
            "source_file": cycle[0],
            "source_location": "L1",
            "message": f"Cyclic dependency detected across {len(cycle)} files: {cycle_display}",
        })

    return findings


def check_layer_violations(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    custom_rules: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Detect architectural layer violations where inner/lower layers import outer/upper layers.

    Clean Architecture Invariant:
    Core / Domain / Engine / Storage must NOT import Presentation / CLI / UI / Views.
    """
    findings: list[dict[str, Any]] = []
    nodes_by_id = {str(n.get("id")): n for n in nodes if "id" in n}

    rules = custom_rules or [
        ("domain", "cli"),
        ("core", "cli"),
        ("engine", "cli"),
        ("extractors", "cli"),
        ("models", "cli"),
        ("db", "cli"),
        ("domain", "ui"),
        ("core", "ui"),
        ("engine", "ui"),
        ("models", "ui"),
        ("core", "views"),
        ("domain", "controllers"),
    ]

    seen = set()
    for e in edges:
        rel = str(e.get("relation", ""))
        if rel not in ("imports", "imports_from", "calls"):
            continue

        src_id = str(e.get("source", ""))
        tgt_id = str(e.get("target", ""))
        src_file = str(e.get("source_file") or nodes_by_id.get(src_id, {}).get("source_file") or "").lower().replace("\\", "/")
        tgt_file = str(nodes_by_id.get(tgt_id, {}).get("source_file") or "").lower().replace("\\", "/")

        if not src_file or not tgt_file or src_file == tgt_file:
            continue

        for inner_pat, outer_pat in rules:
            if (f"/{inner_pat}/" in f"/{src_file}" or src_file.startswith(f"{inner_pat}/") or f"_{inner_pat}" in src_file) and \
               (f"/{outer_pat}/" in f"/{tgt_file}" or tgt_file.startswith(f"{outer_pat}/") or f"_{outer_pat}" in tgt_file):
                key = (src_file, tgt_file, inner_pat, outer_pat)
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "rule": "architectural_layer_violation",
                        "severity": "error",
                        "source_file": e.get("source_file") or nodes_by_id.get(src_id, {}).get("source_file", ""),
                        "source_location": e.get("source_location", "L1"),
                        "inner_layer": inner_pat,
                        "outer_layer": outer_pat,
                        "imported_file": nodes_by_id.get(tgt_id, {}).get("source_file", ""),
                        "message": (
                            f"Architectural layer violation: inner layer '{src_file}' ({inner_pat}) "
                            f"imports outer layer '{tgt_file}' ({outer_pat})."
                        ),
                    })

    return findings


def add_explicit_backlink_edges(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Store explicit reverse backlink edges for O(1) 'what links here' traversal.

    For every forward edge (u -> v, relation=R), emits a reverse edge
    (v -> u, relation=backlink:R, is_backlink=True).
    """
    seen: set[tuple[str, str, str]] = {
        (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        for e in edges
    }
    backlinks: list[dict[str, Any]] = []

    for e in edges:
        if e.get("is_backlink"):
            continue
        src = str(e.get("source", ""))
        tgt = str(e.get("target", ""))
        rel = str(e.get("relation", ""))
        rev_rel = f"backlink:{rel}"

        key = (tgt, src, rev_rel)
        if key not in seen:
            seen.add(key)
            backlink_edge = {
                "source": tgt,
                "target": src,
                "relation": rev_rel,
                "forward_relation": rel,
                "is_backlink": True,
                "confidence": e.get("confidence", "EXTRACTED"),
                "weight": e.get("weight", 1.0),
                "source_file": e.get("source_file", ""),
                "source_location": e.get("source_location", "L1"),
            }
            backlinks.append(backlink_edge)

    return edges + backlinks


def check_context_manager_lifetimes(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flag classes that implement __enter__ without __exit__, or __aenter__ without __aexit__.

    Prevents unhandled resource leaks (unclosed DB connections, lingering locks, or hanging streams).
    """
    findings: list[dict[str, Any]] = []
    class_methods: dict[str, set[str]] = {}
    class_nodes: dict[str, dict[str, Any]] = {}

    for n in nodes:
        ntype = n.get("type", "")
        nid = str(n.get("id", ""))
        label = str(n.get("label", ""))
        if ntype == "class" or "class " in label.lower():
            class_nodes[nid] = n
            class_methods.setdefault(nid, set())

    for e in edges:
        rel = str(e.get("relation", ""))
        src = str(e.get("source", ""))
        tgt = str(e.get("target", ""))
        if rel in ("method", "contains", "defines") and src in class_nodes:
            tgt_lbl = str(e.get("target_label", tgt))
            class_methods[src].add(tgt_lbl.lower())

    for cid, methods in class_methods.items():
        node = class_nodes[cid]
        cname = node.get("label", cid)
        sfile = node.get("source_file", "")
        sloc = node.get("source_location", "L1")

        has_enter = any("__enter__" in m for m in methods)
        has_exit = any("__exit__" in m for m in methods)
        has_aenter = any("__aenter__" in m for m in methods)
        has_aexit = any("__aexit__" in m for m in methods)

        if has_enter and not has_exit:
            findings.append({
                "rule": "missing_context_manager_exit",
                "severity": "error",
                "class": cname,
                "source_file": sfile,
                "source_location": sloc,
                "message": (
                    f"Class '{cname}' implements synchronous '__enter__' but is missing "
                    f"'__exit__' teardown handler, risking unclosed resource leaks."
                ),
            })
        if has_aenter and not has_aexit:
            findings.append({
                "rule": "missing_async_context_manager_exit",
                "severity": "error",
                "class": cname,
                "source_file": sfile,
                "source_location": sloc,
                "message": (
                    f"Class '{cname}' implements asynchronous '__aenter__' but is missing "
                    f"'__aexit__' teardown handler, risking unclosed async connection leaks."
                ),
            })

    return findings


def check_ast_signature_drift(
    current_nodes: list[dict[str, Any]],
    base_nodes_or_graph: list[dict[str, Any]] | nx.Graph | dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Flag public API removals and breaking AST parameter signature mutations compared to a base graph."""
    if not base_nodes_or_graph:
        return []

    findings: list[dict[str, Any]] = []

    if isinstance(base_nodes_or_graph, nx.Graph):
        base_nodes = [{"id": n, **attrs} for n, attrs in base_nodes_or_graph.nodes(data=True)]
    elif isinstance(base_nodes_or_graph, dict):
        base_nodes = base_nodes_or_graph.get("nodes", [])
    else:
        base_nodes = base_nodes_or_graph

    # Index base public symbols: label -> node
    base_symbols: dict[str, dict[str, Any]] = {}
    for bn in base_nodes:
        lbl = bn.get("label", "")
        ntype = bn.get("type", "")
        if ntype in ("function", "method", "class") or "(" in lbl:
            clean_lbl = lbl.split("(")[0].strip()
            if clean_lbl and not clean_lbl.startswith("_"):
                base_symbols[clean_lbl] = bn

    # Index current symbols
    curr_symbols: dict[str, dict[str, Any]] = {}
    for cn in current_nodes:
        lbl = cn.get("label", "")
        ntype = cn.get("type", "")
        if ntype in ("function", "method", "class") or "(" in lbl:
            clean_lbl = lbl.split("(")[0].strip()
            if clean_lbl:
                curr_symbols[clean_lbl] = cn

    # 1. Detect removed public symbols
    for sym, bnode in base_symbols.items():
        if sym not in curr_symbols:
            findings.append({
                "rule": "breaking_api_removal",
                "severity": "error",
                "symbol": sym,
                "source_file": bnode.get("source_file", ""),
                "source_location": bnode.get("source_location", "L1"),
                "message": (
                    f"Public API symbol '{sym}' was removed from the codebase. "
                    f"Downstream consumers will fail on import."
                ),
            })
        else:
            # 2. Check for signature changes
            b_sig = str(bnode.get("signature") or bnode.get("label", ""))
            c_sig = str(curr_symbols[sym].get("signature") or curr_symbols[sym].get("label", ""))
            if "(" in b_sig and "(" in c_sig:
                b_params = b_sig[b_sig.find("(") + 1 : b_sig.rfind(")")].split(",")
                c_params = c_sig[c_sig.find("(") + 1 : c_sig.rfind(")")].split(",")
                b_params = [p.strip() for p in b_params if p.strip()]
                c_params = [p.strip() for p in c_params if p.strip()]

                b_req = [p for p in b_params if "=" not in p and not p.startswith("*")]
                c_req = [p for p in c_params if "=" not in p and not p.startswith("*")]
                if len(c_req) > len(b_req):
                    findings.append({
                        "rule": "breaking_signature_change",
                        "severity": "warning",
                        "symbol": sym,
                        "source_file": curr_symbols[sym].get("source_file", ""),
                        "source_location": curr_symbols[sym].get("source_location", "L1"),
                        "message": (
                            f"Symbol '{sym}' added required parameters "
                            f"({', '.join(c_req[len(b_req):])}) without default values, "
                            f"breaking existing caller invocations."
                        ),
                    })

    return findings


def run_lint(
    graph_or_extraction: nx.Graph | dict[str, Any],
    check_actions: bool = True,
    check_drift: bool = True,
    check_secrets: bool = True,
    check_orphans: bool = True,
    check_rtk: bool = True,
    check_dead: bool = True,
    check_cycles: bool = True,
    check_layers: bool = True,
    check_context_managers: bool = True,
    base_graph: nx.Graph | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute deterministic lint and drift rules against a graph or extraction dict.

    Returns structured findings and a summary with error/warning counts.
    """
    if isinstance(graph_or_extraction, nx.Graph):
        nodes = [
            {"id": n, **attrs} for n, attrs in graph_or_extraction.nodes(data=True)
        ]
        edges = [
            {"source": u, "target": v, **attrs}
            for u, v, attrs in graph_or_extraction.edges(data=True)
        ]
    else:
        nodes = graph_or_extraction.get("nodes", [])
        edges = graph_or_extraction.get("edges") or graph_or_extraction.get("links", [])

    findings: list[dict[str, Any]] = []

    # 1. Unpinned GitHub Actions
    if check_actions:
        findings.extend(check_unpinned_actions(nodes, edges))

    # 2. Environment Drift
    if check_drift:
        findings.extend(check_environment_drift(nodes))

    # 3. Missing Secrets and Envs
    if check_secrets:
        findings.extend(check_missing_secrets_and_envs(nodes, edges))

    # 4. Dangling edges & orphan nodes
    if check_orphans:
        findings.extend(check_dangling_and_orphans(nodes, edges))

    # 5. Redux Toolkit & RTK Query cache tag consistency
    if check_rtk:
        findings.extend(check_rtk_cache_and_state(nodes, edges))

    # 6. Dead Code / Unreferenced Symbols
    if check_dead:
        findings.extend(check_dead_code(nodes, edges))

    # 7. Cyclic dependencies between files
    if check_cycles:
        findings.extend(check_cyclic_dependencies(nodes, edges))

    # 8. Architectural Layer Violations
    if check_layers:
        findings.extend(check_layer_violations(nodes, edges))

    # 9. Context Manager Lifetimes
    if check_context_managers:
        findings.extend(check_context_manager_lifetimes(nodes, edges))

    # 10. AST Signature Drift
    if base_graph:
        findings.extend(check_ast_signature_drift(nodes, base_graph))

    errors = sum(1 for f in findings if f.get("severity") == "error")
    warnings = sum(1 for f in findings if f.get("severity") == "warning")
    info = sum(1 for f in findings if f.get("severity") == "info")

    return {
        "status": "failed" if errors > 0 else ("warning" if warnings > 0 else "passed"),
        "findings": findings,
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "info": info,
            "total": len(findings),
        },
    }
