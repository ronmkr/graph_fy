from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _reconcile_markdown_links(
    result: dict,
    preserved_edges: list[dict],
    preserved_nodes: list[dict],
    code_files: list[Path],
    extract_targets: list[Path],
    full_rebuild: bool,
    project_root: Path,
    source_paths: Any,
) -> list[dict]:
    """Reconcile Markdown links without losing semantic-backed targets.

    Semantic-backed documents are deliberately skipped by the AST quick scan
    (#1915/#1954), so their canonical file node can be absent. Re-point an
    authored link only when both files have unique representatives. If either
    side is ambiguous, retain the existing AST edge instead of guessing or
    deleting it. A link removed from its owning Markdown source is pruned.
    """
    from graph_fy.build import _is_ast_tier
    from graph_fy.extract import _file_node_id, _safe_extract_with_xaml_root
    from graph_fy.extractors.base import _make_id
    from graph_fy.extractors.markdown import extract_markdown

    all_nodes = result.get("nodes", []) + preserved_nodes
    nodes_by_source: dict[str, list[dict]] = {}
    for node in all_nodes:
        if source_file := node.get("source_file"):
            normalized = source_paths.normalize(source_file)
            nodes_by_source.setdefault(normalized, []).append(node)

    representatives: dict[str, str | None] = {}
    for source_file, nodes in nodes_by_source.items():
        canonical_id = _file_node_id(Path(source_file))
        canonical = [node for node in nodes if node["id"] == canonical_id]
        pages = [node for node in nodes if node.get("node_kind") == "page"]
        semantic_documents = [
            node
            for node in nodes
            if node.get("file_type") == "document" and node.get("_origin") != "ast"
        ]
        if len(canonical) == 1:
            representatives[source_file] = canonical[0]["id"]
        elif len(pages) == 1:
            representatives[source_file] = pages[0]["id"]
        elif len(semantic_documents) == 1:
            representatives[source_file] = semantic_documents[0]["id"]
        else:
            representatives[source_file] = None

    markdown_files = code_files if full_rebuild else extract_targets
    markdown_files = [path for path in markdown_files if path.suffix.lower() == ".md"]
    parsed_sources: set[str] = set()
    authored_links: set[tuple[str, str]] = set()
    authored_raw_pairs: set[frozenset[str]] = set()
    unresolved_authored_sites: dict[tuple[str, str], set[str]] = {}
    resolved_raw_pairs: dict[frozenset[str], frozenset[str]] = {}
    desired_edges: list[tuple[str, str, dict, str, str]] = []
    node_sources = {
        node["id"]: source_paths.normalize(node["source_file"])
        for node in all_nodes
        if node.get("source_file")
    }

    for markdown_file in markdown_files:
        try:
            relative_source = markdown_file.relative_to(project_root)
        except ValueError:
            relative_source = markdown_file
        source_file = source_paths.normalize(str(relative_source))
        parsed_sources.add(source_file)
        source_rep = representatives.get(source_file)

        extraction = _safe_extract_with_xaml_root(
            extract_markdown, markdown_file, project_root
        )
        for edge in extraction.get("edges", []):
            if edge.get("relation") != "references":
                continue
            if not edge.get("target_file"):
                source_location = str(edge.get("source_location") or "")
                if source_location:
                    unresolved_authored_sites.setdefault(
                        (source_file, source_location), set()
                    ).add(str(edge.get("target") or ""))
                continue
            target_path = Path(edge["target_file"])
            try:
                target_path = target_path.relative_to(project_root)
            except ValueError:
                pass
            target_file = source_paths.normalize(str(target_path))
            authored_links.add((source_file, target_file))
            raw_pair = frozenset(
                (_file_node_id(Path(source_file)), _file_node_id(Path(target_file)))
            )
            authored_raw_pairs.add(raw_pair)

            target_rep = representatives.get(target_file)
            if not source_rep or not target_rep:
                logger.debug(
                    "Preserving ambiguous Markdown link %s -> %s",
                    source_file,
                    target_file,
                )
                continue
            if source_rep != target_rep:
                resolved_raw_pairs[raw_pair] = frozenset((source_rep, target_rep))
                desired_edges.append(
                    (source_rep, target_rep, edge, source_file, target_file)
                )

    desired_file_pairs = {
        frozenset((source_file, target_file))
        for _, _, _, source_file, target_file in desired_edges
    }
    desired_reps_by_file_pair = {
        frozenset((source_file, target_file)): frozenset((source_rep, target_rep))
        for source_rep, target_rep, _, source_file, target_file in desired_edges
    }

    def _matches_unresolved_target(
        raw_target: str, owner: str, target_source: str
    ) -> bool:
        candidate = project_root / Path(owner).parent / Path(target_source).name
        return raw_target == _make_id(str(candidate))

    def _keep_edge(edge: dict) -> bool:
        if not (_is_ast_tier(edge) and edge.get("relation") == "references"):
            return True
        owner = source_paths.normalize(edge.get("source_file"))
        if owner not in parsed_sources:
            return True

        unresolved_targets = unresolved_authored_sites.get(
            (owner, str(edge.get("source_location") or "")), set()
        )
        if unresolved_targets:
            target_sources = {
                source
                for endpoint in (edge.get("source"), edge.get("target"))
                if (source := node_sources.get(endpoint)) and source != owner
            }
            if any(
                _matches_unresolved_target(raw_target, owner, target_source)
                for raw_target in unresolved_targets
                for target_source in target_sources
            ):
                return True

        raw_pair = frozenset((edge.get("source"), edge.get("target")))
        if raw_pair in authored_raw_pairs:
            desired_pair = resolved_raw_pairs.get(raw_pair)
            return desired_pair is None or raw_pair == desired_pair

        endpoint_files = {
            node_sources.get(edge.get("source")),
            node_sources.get(edge.get("target")),
        }
        endpoint_files.discard(None)
        targets = endpoint_files - {owner}
        if len(targets) == 1:
            target_file = targets.pop()
        elif edge.get("target_file"):
            target_path = Path(edge["target_file"])
            try:
                target_path = target_path.relative_to(project_root)
            except ValueError:
                pass
            target_file = source_paths.normalize(str(target_path))
        else:
            return True

        file_pair = frozenset((owner, target_file))
        if (owner, target_file) not in authored_links:
            return False
        if file_pair not in desired_file_pairs:
            return True
        return raw_pair == desired_reps_by_file_pair[file_pair]

    result["edges"] = [edge for edge in result.get("edges", []) if _keep_edge(edge)]
    preserved_edges = [edge for edge in preserved_edges if _keep_edge(edge)]
    existing_pairs = {
        frozenset((edge.get("source"), edge.get("target")))
        for edge in result["edges"] + preserved_edges
    }
    for source_rep, target_rep, edge, source_file, _ in desired_edges:
        pair = frozenset((source_rep, target_rep))
        # graph_fy uses a simple graph. Never overwrite another relation that
        # already occupies the representative pair.
        if pair in existing_pairs:
            continue
        reconciled = edge.copy()
        reconciled.pop("target_file", None)
        reconciled.update(
            source=source_rep,
            target=target_rep,
            source_file=source_file,
            _origin="ast",
        )
        result["edges"].append(reconciled)
        existing_pairs.add(pair)

    return preserved_edges
