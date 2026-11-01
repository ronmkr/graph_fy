"""Member-level dispatch from an interface requirement to its implementer(s).

A call through an injected dependency lands on the interface's method node,
because that is what the call site names: `_report.Build()` where `_report` is
an `IReport` resolves to `IReport.Build()`. The implementing method is a
separate node, and nothing joins the two, so a directed walk stops at the
interface and every chain through an injected dependency is cut at that point.

This resolver runs after all files are extracted, with the merged corpus
available, and links the interface's method to every implementing method:

    ireport_ireport_build  --dispatches_to-->  report_report_build
    ireport_ireport_build  --dispatches_to-->  alternatereport_alternatereport_build

For a static structural graph the question a call through the interface asks
is "what can this reach", not "which one does it reach at runtime" — with N
implementers the answer is all N, not none. Each implementer is still subject
to the single-owner guard `resolve_pascal_inherited_calls` and
`resolve_ruby_member_calls` already use: an implementer that itself declares
more than one method of the same name is ambiguous and is skipped on its own,
independently of its siblings, unless the overloads pair up one-to-one by
signature or parameter count (C#, #4584).

The join is per method rather than per call site, so one edge per implementer
reconnects every call that reaches the interface method. Confidence is
`INFERRED` and falls as the candidate set grows, on the extraction rubric's own
tiers: 0.85 ("strong inference") for a single implementer — the target is
forced, the source text just never names it — 0.75 for two, and a 0.65 floor
("weak inference") from three on, rather than inventing finer-grained
precision the rubric does not have.
"""
from __future__ import annotations

from pathlib import Path

DISPATCH_RELATION = "dispatches_to"

_DISPATCH_CONFIDENCE_BY_IMPLEMENTER_COUNT = {1: 0.85, 2: 0.75}
_DISPATCH_CONFIDENCE_FLOOR = 0.65


def _dispatch_confidence(implementer_count: int) -> float:
    """INFERRED confidence for a dispatch edge, given how many implementers exist."""
    return _DISPATCH_CONFIDENCE_BY_IMPLEMENTER_COUNT.get(implementer_count, _DISPATCH_CONFIDENCE_FLOOR)


def declared_in(node: dict | None, suffixes: tuple[str, ...]) -> bool:
    """True when the node is a declaration that lives in a file with one of ``suffixes``."""
    if not node:
        return False
    source_file = node.get("source_file")
    if not source_file:
        return False
    sf = str(source_file).lower()
    return any(sf.endswith(s.lower()) for s in suffixes)


def method_label(node: dict) -> str:
    """Return a method node's bare name, for matching."""
    label = str(node.get("label", "")).strip().removeprefix(".")
    return label.split("(", 1)[0]


def resolve_interface_dispatch(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
    *,
    suffixes: tuple[str, ...],
) -> None:
    """Link each interface method to every implementation that owns it unambiguously."""
    if not any(
        declared_in(result, suffixes) for result in per_file if isinstance(result, dict)
    ) and not any(
        declared_in(n, suffixes) for n in all_nodes
    ):
        return

    node_by_id = {n.get("id"): n for n in all_nodes}

    implementers: dict[str, set[str]] = {}
    methods_of: dict[str, dict[str, set[str]]] = {}
    for e in all_edges:
        rel = e.get("relation")
        if rel == "implements":
            implementers.setdefault(e.get("target"), set()).add(e.get("source"))
        elif rel == "method":
            owner, method_nid = e.get("source"), e.get("target")
            mnode = node_by_id.get(method_nid)
            if mnode is None or not declared_in(mnode, suffixes):
                continue
            name = method_label(mnode)
            if name:
                methods_of.setdefault(owner, {}).setdefault(name, set()).add(method_nid)

    if not implementers or not methods_of:
        return

    existing_pairs = {
        (e.get("source"), e.get("target"))
        for e in all_edges
        if e.get("relation") == DISPATCH_RELATION
    }
    new_edges: list[dict] = []

    for interface_nid, impls in implementers.items():
        interface_node = node_by_id.get(interface_nid)
        if interface_node is None or not declared_in(interface_node, suffixes):
            continue
        interface_methods = methods_of.get(interface_nid, {})
        if not interface_methods:
            continue

        gated_impls: list[tuple[str, dict]] = []
        for impl_nid in impls:
            impl_node = node_by_id.get(impl_nid)
            if impl_node is not None and declared_in(impl_node, suffixes):
                gated_impls.append((impl_nid, impl_node))
        if not gated_impls:
            continue
        score = _dispatch_confidence(len(gated_impls))

        for impl_nid, impl_node in gated_impls:
            impl_methods = methods_of.get(impl_nid, {})
            for name, declared in interface_methods.items():
                for source, target in _overload_pairs(
                    declared, impl_methods.get(name, set()), node_by_id
                ):
                    if source == target or (source, target) in existing_pairs:
                        continue
                    existing_pairs.add((source, target))
                    new_edges.append({
                        "source": source,
                        "target": target,
                        "relation": DISPATCH_RELATION,
                        "context": "call",
                        "confidence": "INFERRED",
                        "confidence_score": score,
                        "source_file": str(impl_node.get("source_file", "")),
                        "source_location": impl_node.get("source_location"),
                        "weight": 1.0,
                    })

    all_edges.extend(new_edges)


def _overload_pairs(
    declared: set[str], candidates: set[str], node_by_id: dict
) -> list[tuple[str, str]]:
    """Pair an interface's methods of one name with an implementer's."""
    if len(declared) == 1 and len(candidates) == 1:
        return [(next(iter(declared)), next(iter(candidates)))]

    def _meta(nid: str) -> dict:
        meta = (node_by_id.get(nid) or {}).get("metadata")
        return meta if isinstance(meta, dict) else {}

    pairs: dict[str, str] = {}
    for key in ("signature", "arity"):
        taken = set(pairs.values())
        for source in sorted(declared):
            want = _meta(source).get(key)
            if source in pairs or want is None:
                continue
            hits = [c for c in candidates if c not in taken and _meta(c).get(key) == want]
            if len(hits) == 1:
                pairs[source] = hits[0]
    targets = list(pairs.values())
    return [(s, t) for s, t in pairs.items() if targets.count(t) == 1]
