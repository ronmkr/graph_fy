# monitor a folder and auto-trigger --update when files change
from __future__ import annotations
import contextlib
import json
import logging
import os
import posixpath
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

import networkx as nx

# Single source of truth in graph_fy.paths (#1423); re-exported as _graph_fy_OUT.
from graph_fy.paths import (
    graph_fy_OUT as _graph_fy_OUT,
    is_absolute_any_platform,
    os_replace_with_fallback,
)

logger = logging.getLogger(__name__)
from graph_fy.detect import (
    CODE_EXTENSIONS,
    DOC_EXTENSIONS,
    PAPER_EXTENSIONS,
    IMAGE_EXTENSIONS,
    _load_graph_fyignore,
    _is_ignored,
)
from graph_fy.watch_markdown import _reconcile_markdown_links
from graph_fy.watch_paths import (
    _PORTABLE_PATH_KEYS,
    _REMOTE_SOURCE_RE,
    _StoredSourcePaths,
    _changed_path_candidates,
    _is_relative_to,
    _is_remote_source,
    _rebase_relative_source_files,
    _relativize_source_files,
)
from graph_fy.watch_state import (
    _BUILD_CONFIG_FILENAME,
    _PENDING_DRAIN_MAX_PASSES,
    _PENDING_FILENAME,
    _apply_resource_limits,
    _drain_pending,
    _git_head,
    _merge_changed_paths,
    _queue_pending,
    _read_build_excludes,
    _read_build_gitignore,
    _rebuild_lock,
    _report_root_label,
    _write_build_config,
)

_WATCHED_EXTENSIONS = CODE_EXTENSIONS | DOC_EXTENSIONS
_CODE_EXTENSIONS = CODE_EXTENSIONS


def _reconcile_existing_graph(
    existing_graph: Path,
    result: dict,
    *,
    out: Path,
    project_root: Path,
    watch_root: Path,
    code_files: list[Path],
    extract_targets: list[Path],
    full_rebuild: bool,
    deleted_paths: set[str],
    deleted_source_identities: set[str],
    is_ignored_always: Callable[[Path], bool] | None = None,
    is_ignored_full: Callable[[Path], bool] | None = None,
) -> tuple[dict, dict]:
    """Merge fresh extraction with preserved graph entries and evict stale sources.

    ``is_ignored_always``/``is_ignored_full`` (#2495) report whether a LIVE
    ignore rule matches a path, mirroring the detect() call that produced
    ``code_files`` (detect.ignored_predicate). ``is_ignored_always`` covers
    .graph_fyignore + persisted ``--exclude`` patterns — unambiguous
    graph-level intent, honored on every rebuild. ``is_ignored_full`` adds
    .gitignore-driven rules, honored only on a full rebuild (an explicit
    ``graph_fy update``) so the incremental/hook path keeps preserving a
    deliberately-graphed .gitignore'd tree (#1795).
    """
    existing_graph_data: dict = {}
    if not existing_graph.exists():
        return result, existing_graph_data

    # Fail-closed load (#2251): reuse build._load_existing_graph, which raises
    # ValueError when the file exceeds the size cap and RuntimeError when it
    # exists but cannot be parsed. Those failures must PROPAGATE to the caller
    # — swallowing them here left existing_graph_data == {}, which _check_shrink
    # reads as "no baseline, write allowed", so the hook overwrote a graph it
    # merely failed to READ. A missing file (None) keeps first-build behavior:
    # reconcile proceeds with an empty baseline and the write is allowed.
    from graph_fy.build import _load_existing_graph
    if _load_existing_graph(existing_graph) is None:
        return result, existing_graph_data
    # Cap + parse validated above. Reload as the full dict — reconcile needs
    # top-level keys (hyperedges, per-node community for _node_community_map,
    # topology compare) the (nodes, edges, hyperedges) tuple does not carry.
    # A failure here (e.g. a race rewriting the file) still propagates,
    # staying fail-closed.
    existing = json.loads(existing_graph.read_text(encoding="utf-8"))
    existing_graph_data = existing

    # Backfill tier provenance on legacy items (#2334), mirroring
    # build._load_existing_graph (this reconcile path loads the raw dict
    # separately, so the backfill there does not reach it). Stamping preserved
    # items means the graph self-heals on this write.
    from graph_fy.build import _is_ast_tier
    for _bucket in ("nodes", "links", "edges"):
        for _item in existing.get(_bucket, []):
            if isinstance(_item, dict):
                _item.setdefault("_origin", "ast" if _is_ast_tier(_item) else "semantic")

    try:
        from graph_fy.build import _norm_source_file as _nsf
        from graph_fy.extract import _get_extractor
        source_paths = _StoredSourcePaths(
            existing,
            out=out,
            project_root=project_root,
            watch_root=watch_root,
            normalize_source=_nsf,
        )
        new_ast_ids = {n["id"] for n in result["nodes"]}
        current_sources = {
            source_paths.absolute_identity(str(path), project_root) for path in code_files
        }
        rebuilt_source_identities = {
            source_paths.absolute_identity(str(path), project_root) for path in extract_targets
        }
        node_evicted_source_identities = set(deleted_source_identities)
        hyperedge_evicted_source_identities = set(deleted_source_identities)
        # Deletion evicts edges regardless of tier; re-extraction only owns a
        # source's AST-tier edges (checked per-edge below, #1865).
        edge_evicted_source_identities = set(deleted_source_identities)
        if not full_rebuild:
            node_evicted_source_identities.update(rebuilt_source_identities)

        # Reconcile every rebuild against the current watched corpus. Hook change
        # lists can contain only a rename destination, so explicit paths alone
        # cannot identify the stale source. Keep the comparison scoped to the
        # watched root so subfolder updates preserve records outside that subtree.
        #
        # Fail-closed eviction: a source identity missing from the corpus is only
        # DELETION evidence when the file is actually gone from disk. A file that
        # still exists but stopped being collected was *excluded*, and treating
        # that as deletion silently mass-evicts good nodes. Evicting an alive
        # source therefore needs POSITIVE evidence — a live ignore rule matching
        # the path (#2495) — not mere corpus absence: a filter regression, an
        # extractor loss, or the sensitive-file heuristic still preserves, with
        # a loud message (#1795).
        excluded_alive_files: set[str] = set()
        excluded_alive_nodes = 0
        newly_ignored_files: set[str] = set()
        newly_ignored_nodes = 0
        _alive_cache: dict[str, bool] = {}
        _ignored_cache: dict[str, bool] = {}

        def _ignored_now(identity: str) -> bool:
            """True when a live ignore rule matches this alive, corpus-absent source.

            .graph_fyignore/--exclude matches evict on every rebuild;
            .gitignore-driven matches only on a full rebuild (see docstring).
            """
            ignored = _ignored_cache.get(identity)
            if ignored is None:
                target = Path(identity)
                ignored = bool(
                    (is_ignored_always is not None and is_ignored_always(target))
                    or (
                        full_rebuild
                        and is_ignored_full is not None
                        and is_ignored_full(target)
                    )
                )
                _ignored_cache[identity] = ignored
            return ignored
        for node in existing.get("nodes", []):
            source_file = node.get("source_file")
            if not source_file or _is_remote_source(source_file):
                continue  # sourceless stub or remote/virtual source: never evict
            identity = source_paths.identity(source_file)
            if not source_paths.in_watch_root(source_file):
                continue
            if _get_extractor(Path(source_file)) is None:
                # Non-AST source (semantic doc/paper/image — .txt/.pdf/.png/...):
                # never present in current_sources (built from AST-extractable
                # code_files), so corpus absence is meaningless. Deletion
                # evidence here is disk absence — otherwise its semantic nodes
                # are preserved forever and returned as authoritative even after
                # the file is deleted (#2051) — or a live ignore rule matching
                # the alive file (#2495), the same positive evidence the AST
                # branch below requires. A present-but-unexcluded-but-
                # unextractable file stays preserved (alive -> skip).
                if identity:
                    alive = _alive_cache.get(identity)
                    if alive is None:
                        alive = Path(identity).exists()
                        _alive_cache[identity] = alive
                    ignored = alive and _ignored_now(identity)
                    if ignored:
                        newly_ignored_files.add(identity)
                        newly_ignored_nodes += 1
                    if not alive or ignored:
                        normalized = source_paths.normalize(source_file)
                        if normalized:
                            deleted_paths.add(normalized)
                        node_evicted_source_identities.add(identity)
                        edge_evicted_source_identities.add(identity)
                        hyperedge_evicted_source_identities.add(identity)
                continue
            if identity not in current_sources:
                if identity:
                    alive = _alive_cache.get(identity)
                    if alive is None:
                        alive = Path(identity).exists()
                        _alive_cache[identity] = alive
                    if alive:
                        if _ignored_now(identity):
                            # Intentionally excluded by a live ignore rule:
                            # deliberate graph-level intent, so treat it exactly
                            # like a deletion (#2495) — fall through to evict.
                            newly_ignored_files.add(identity)
                            newly_ignored_nodes += 1
                        else:
                            excluded_alive_files.add(identity)
                            excluded_alive_nodes += 1
                            continue
                normalized = source_paths.normalize(source_file)
                if normalized:
                    deleted_paths.add(normalized)
                if identity:
                    node_evicted_source_identities.add(identity)
                    edge_evicted_source_identities.add(identity)
                    hyperedge_evicted_source_identities.add(identity)
        if newly_ignored_files:
            print(
                f"[graph_fy watch] pruned {newly_ignored_nodes} node(s) from "
                f"{len(newly_ignored_files)} newly-ignored file(s) "
                "(matched by a live ignore rule while absent from the scan corpus)."
            )
        if excluded_alive_files:
            print(
                f"[graph_fy watch] fail-closed: kept {excluded_alive_nodes} node(s) "
                f"from {len(excluded_alive_files)} file(s) that left the scan corpus "
                "but still exist on disk and match no current ignore rule (filters "
                "changed?). Add them to .graph_fyignore if the exclusion is intentional."
            )

        # A full re-extraction owns the AST nodes of every source it actually
        # re-extracted (extract_targets, via rebuilt_source_identities) — NOT
        # every AST node under watch_root: a semantic-backed doc is excluded
        # from extract_targets (#1915), so its existing AST layer is not
        # regenerated this run and dropping it would be data loss (#2333,
        # COEXIST — the AST and semantic layers of a file coexist).
        # Incremental extraction owns only nodes from rebuilt or deleted
        # sources. Semantic-tier nodes (per _is_ast_tier) remain preserved.
        preserved_nodes = [
            node
            for node in existing.get("nodes", [])
            if node["id"] not in new_ast_ids
            and not (
                _is_ast_tier(node)
                and (
                    (
                        not node.get("source_file")
                        and (full_rebuild or not code_files)
                    )
                    or (
                        full_rebuild
                        and source_paths.is_evicted(node, rebuilt_source_identities)
                    )
                )
            )
            and not source_paths.is_evicted(node, node_evicted_source_identities)
        ]
        all_ids = new_ast_ids | {node["id"] for node in preserved_nodes}

        # Edges are owned by source_file, but ownership is tier-scoped: the AST
        # pass replaces a re-extracted source's AST edges, while that source's
        # semantic/LLM edges — which the AST pass cannot regenerate — survive
        # until a semantic re-extraction supersedes them. Same provenance rule
        # the node reconciliation above applies via _origin (#1865). Deletion
        # eviction stays provenance-blind.
        preserved_edges = [
            edge
            for edge in existing.get("links", existing.get("edges", []))
            if edge.get("source") in all_ids
            and edge.get("target") in all_ids
            and not source_paths.is_evicted(edge, edge_evicted_source_identities)
            and not (
                _is_ast_tier(edge)
                and source_paths.is_evicted(edge, rebuilt_source_identities)
            )
        ]

        preserved_edges = _reconcile_markdown_links(
            result,
            preserved_edges,
            preserved_nodes,
            code_files,
            extract_targets,
            full_rebuild,
            project_root,
            source_paths,
        )

        new_hyperedge_ids = {
            edge.get("id") for edge in result.get("hyperedges", []) if edge.get("id")
        }
        preserved_hyperedges = []
        for edge in existing.get("hyperedges", []):
            members = edge.get("nodes", edge.get("members", edge.get("node_ids", [])))
            if edge.get("id") in new_hyperedge_ids or source_paths.is_evicted(
                edge, hyperedge_evicted_source_identities
            ):
                continue
            if isinstance(members, list) and any(member not in all_ids for member in members):
                continue
            preserved_hyperedges.append(edge)

        for item in preserved_nodes + preserved_edges + preserved_hyperedges:
            source_paths.rebase_preserved(item)

        return {
            "nodes": result["nodes"] + preserved_nodes,
            "edges": result["edges"] + preserved_edges,
            "hyperedges": result.get("hyperedges", []) + preserved_hyperedges,
            "input_tokens": 0,
            "output_tokens": 0,
        }, existing_graph_data
    except Exception as exc:
        # Post-load reconciliation failure: fall back to the fresh extraction
        # while keeping the loaded baseline, so _check_shrink still guards the
        # write against a collapse. Say so — this used to be silent (#2251).
        print(
            "[graph_fy watch] reconcile of existing graph failed "
            f"({exc.__class__.__name__}: {exc}); proceeding with fresh "
            "extraction only.",
            file=sys.stderr,
        )
        return result, existing_graph_data


from graph_fy.shrink_guard import (  # noqa: F401
    _node_community_map,
    _canonical_graph_for_compare,
    _canonical_topology_for_compare,
    _topology_from_graph,
    _check_shrink,
    _report_for_compare,
    _json_text,
)


def _stabilize_rebuild_cwd(watch_path: Path) -> bool:
    """Ensure relative rebuild paths have a usable CWD before queue/lock setup.

    Detached git hooks can inherit a transient working directory that is deleted
    before the background rebuild starts. In that state Path.cwd(),
    Path('.').resolve(), and relative graph_fy_out mkdirs raise FileNotFoundError
    before the normal rebuild error handling can run. Hooks that know the repo
    root export graph_fy_REPO_ROOT so the rebuild can recover by chdir'ing there.
    """
    if watch_path.is_absolute():
        return True

    repo_root = os.environ.get("graph_fy_REPO_ROOT", "").strip()
    if repo_root and Path(repo_root).is_dir():
        try:
            os.chdir(repo_root)
            return True
        except OSError:
            pass

    try:
        Path.cwd()
        return True
    except FileNotFoundError:
        print(
            "[graph_fy watch] Rebuild failed: current working directory "
            "no longer exists and graph_fy_REPO_ROOT is not set."
        )
        return False


def _reconcile_graph_html(out: Path, graph_data: dict) -> str | None:
    """Reconcile missing, stale, or explicitly disabled HTML visualization.

    The unchanged-topology update path deliberately avoids clustering and
    rewriting core artifacts. HTML is independently derivable, so rebuild it
    from the communities and names already stored in graph.json when absent or
    marked stale by a prior failed render.

    Returns ``"rendered"`` or ``"removed"`` when it changed the HTML state,
    otherwise ``None``.
    """
    html_target = out / "graph.html"
    from graph_fy.exporters.html import _HTML_STALE_MARKER, _viz_node_limit
    stale_marker = out / _HTML_STALE_MARKER

    def clear_stale_marker() -> None:
        try:
            stale_marker.unlink(missing_ok=True)
        except OSError as exc:
            print(
                "[graph_fy watch] graph.html stale marker could not be cleared; "
                f"regeneration may be retried: {exc}"
            )

    limit = _viz_node_limit()
    if limit <= 0:
        changed = html_target.exists() or stale_marker.exists()
        html_target.unlink(missing_ok=True)
        clear_stale_marker()
        return "removed" if changed else None
    if html_target.exists() and not stale_marker.exists():
        return None

    had_html = html_target.exists()

    node_communities = _node_community_map(graph_data)
    communities: dict[int, list[str]] = {}
    for node_id, cid in node_communities.items():
        communities.setdefault(cid, []).append(node_id)

    labels: dict[int, str] = {}
    for node in graph_data.get("nodes", []):
        cid = node_communities.get(str(node.get("id")))
        name = node.get("community_name")
        if cid is not None and isinstance(name, str) and name:
            labels.setdefault(cid, name)

    try:
        from graph_fy.export import to_html
        from graph_fy.paths import load_node_link_graph

        persisted_graph = load_node_link_graph(graph_data)
        written = to_html(
            persisted_graph,
            communities,
            str(html_target),
            community_labels=labels or None,
            node_limit=limit,
        )
    except Exception as exc:
        if had_html:
            print(
                "[graph_fy watch] Stale graph.html left unchanged; "
                f"regeneration will be retried: {exc}"
            )
        else:
            print(f"[graph_fy watch] Missing graph.html could not be regenerated: {exc}")
        return None

    if not written or not html_target.exists():
        if had_html:
            print(
                "[graph_fy watch] Stale graph.html left unchanged; "
                "no useful community view was generated."
            )
        else:
            print("[graph_fy watch] Missing graph.html has no useful community view; skipped.")
        return None
    clear_stale_marker()
    return "rendered"


def _rebuild_code(
    watch_path: Path,
    *,
    changed_paths: list[Path] | None = None,
    follow_symlinks: bool = False,
    force: bool = False,
    no_cluster: bool = False,
    acquire_lock: bool = True,
    block_on_lock: bool = False,
) -> bool:
    """Re-run AST extraction + build + optional cluster + report for code files. No LLM needed.

    When ``force`` is True the node-count safety check in ``to_json`` is bypassed
    so the rebuilt graph overwrites graph.json even if it has fewer nodes.
    Use this after refactors that legitimately delete code.

    When ``changed_paths`` is provided, only those files are re-extracted; nodes
    for unchanged files are preserved from the existing graph. Deleted paths
    in ``changed_paths`` (paths that no longer exist on disk) are dropped from
    the preserved set. When ``changed_paths`` is None the full code corpus is
    re-extracted (used by the watcher and post-checkout hook).

    ``acquire_lock`` (default True) takes a non-blocking per-repo flock around
    the rebuild so concurrent post-commit hooks across multiple repos do not
    pile up. Returns False with a log line if the lock is held. Pass
    ``block_on_lock=True`` to wait instead of skip (used by the interactive
    ``graph_fy update`` CLI).

    ``no_cluster`` skips community detection and writes raw merged extraction
    JSON to graph_fy_out/graph.json (mirrors ``extract --no-cluster``).

    Returns True on success, False on error or skipped-due-to-lock.
    """
    if not _stabilize_rebuild_cwd(watch_path):
        return False

    out = watch_path / _graph_fy_OUT
    if acquire_lock:
        # #1059: incremental (changed_paths is not None) hooks must not drop
        # their change set when another rebuild is already running. Queue
        # before attempting the lock so a non-blocking failure still records
        # the work; the lock-holder drains the queue and merges it in. Full-
        # corpus rebuilds skip the queue entirely — they already cover every
        # file, so there is nothing to merge.
        if changed_paths is not None and not block_on_lock:
            _queue_pending(out, list(changed_paths))
        with _rebuild_lock(out, blocking=block_on_lock) as got:
            if not got:
                print("[graph_fy watch] Rebuild already in progress for "
                      f"{watch_path.resolve()} - changes queued.")
                return False
            # Lock acquired. Drain anything queued by earlier contenders
            # (including, importantly, the paths we just queued ourselves)
            # and merge with our own change set so a single rebuild covers
            # everything outstanding.
            if changed_paths is not None:
                merged = _merge_changed_paths(changed_paths, _drain_pending(out))
            else:
                # Full-corpus rebuild supersedes any queued incremental work.
                _drain_pending(out)
                merged = None
            ok = _rebuild_code(
                watch_path,
                changed_paths=merged,
                follow_symlinks=follow_symlinks,
                force=force,
                no_cluster=no_cluster,
                acquire_lock=False,
            )
            # Late-arrival drain: another hook may have queued work while we
            # were rebuilding. Loop up to _PENDING_DRAIN_MAX_PASSES times so a
            # storm of commits eventually quiesces without livelocking. A full
            # rebuild already saw everything, so skip this for changed_paths is None.
            if merged is not None:
                for _ in range(_PENDING_DRAIN_MAX_PASSES):
                    late = _drain_pending(out)
                    if not late:
                        break
                    ok = _rebuild_code(
                        watch_path,
                        changed_paths=late,
                        follow_symlinks=follow_symlinks,
                        force=force,
                        no_cluster=no_cluster,
                        acquire_lock=False,
                    ) and ok
            return ok

    watch_root = watch_path.resolve()
    # project_root stays CWD-anchored for a relative invocation on purpose: the
    # persisted graph rehomes source_file across invocation styles against it
    # (tests/test_watch.py:1389, :1428). The manifest is a different artifact
    # with a different anchor — see the save_manifest calls below.
    project_root = Path.cwd().resolve() if not watch_path.is_absolute() else watch_root
    report_root = _report_root_label(watch_path)
    try:
        from graph_fy.extract import extract, _get_extractor
        from graph_fy.detect import detect
        from graph_fy.build import build_from_json, _is_ast_tier, _norm_source_file as _nsf
        from graph_fy.cluster import cluster, remap_communities_to_previous, score_all
        from graph_fy.analyze import god_nodes, surprising_connections, suggest_questions
        from graph_fy.report import generate
        from graph_fy.export import to_json
        from graph_fy.security import check_graph_file_size_cap

        # Re-apply the excludes the initial extract recorded, so an update/watch/
        # hook rebuild does not silently re-include deliberately excluded paths
        # (#1886).
        _persisted_excludes = _read_build_excludes(out)
        _gitignore_enabled = _read_build_gitignore(out)
        detected = detect(
            watch_path, follow_symlinks=follow_symlinks,
            extra_excludes=_persisted_excludes or None,
            gitignore=_gitignore_enabled,
        )
        code_files = [Path(f) for f in detected['files']['code']]

        # #3511: `graph_fy extract` has surfaced files it saw but could not
        # classify since #1692; this update/watch rebuild path never did,
        # so a corpus in a language with no extractor (no supported
        # extension or shebang) rebuilt "successfully" with those files
        # silently absent from the graph. Same wording as the extract path.
        _unclassified = detected.get("unclassified", []) if isinstance(detected, dict) else []
        if _unclassified:
            _names = ", ".join(sorted({Path(p).name for p in _unclassified})[:6])
            _more = f" (+{len(_unclassified) - 6} more)" if len(_unclassified) > 6 else ""
            print(
                f"[graph_fy watch] {len(_unclassified)} file(s) not classified "
                f"(no supported extension or shebang), skipped: {_names}{_more}"
            )

        # #2495: hand reconcile the same ignore decisions the detect() call
        # above made, so a newly-ignored file that still exists on disk is
        # purged from the graph instead of preserved forever by the fail-closed
        # keep. Two tiers: .graph_fyignore + persisted --exclude patterns are
        # unambiguous graph-level intent (evict on every rebuild), while
        # .gitignore-driven exclusion is honored only on a full rebuild — the
        # incremental/hook path keeps preserving a deliberately-graphed
        # .gitignore'd tree (#1795).
        from graph_fy.detect import ignored_predicate
        _ignored_always = ignored_predicate(
            watch_root, extra_excludes=_persisted_excludes or None, gitignore=False,
        )
        _ignored_full = ignored_predicate(
            watch_root, extra_excludes=_persisted_excludes or None, gitignore=True,
        ) if _gitignore_enabled else _ignored_always

        # Include document files that have AST extractors (e.g. .md, .mdx, .qmd)
        ast_doc_files: list[Path] = []
        for doc_file in detected['files'].get('document', []):
            p = Path(doc_file)
            if _get_extractor(p) is not None:
                code_files.append(p)
                ast_doc_files.append(p)

        existing_graph = out / "graph.json"
        if not code_files and not existing_graph.exists():
            print("[graph_fy watch] No code files found - nothing to rebuild.")
            return False

        # #1915: a document that already carries SEMANTIC (LLM) nodes in the
        # existing graph must not ALSO be AST-quick-scanned — otherwise every
        # rebuild mints heading nodes on top of the preserved semantic nodes
        # and the doc is represented twice (~4x graph bloat vs the CLI update
        # path, which AST-extracts only code). A semantic-backed doc is never
        # re-quick-scanned, and any AST layer it already carries coexists and
        # is preserved rather than regenerated (#2333, COEXIST); the
        # quick-scan stays as a fallback for docs with no semantic layer (the
        # no-LLM doc-structure feature, #09b33b7) and for brand-new docs the
        # graph has never seen. These docs stay in ``code_files`` so
        # corpus membership (#1795 fail-closed deletion evidence) and the
        # shrink accounting below still cover them — a previously-bloated
        # graph must be allowed to self-heal on a full rebuild without the
        # shrink-guard refusing the smaller write.
        semantic_doc_files: set[Path] = set()
        if ast_doc_files and existing_graph.exists():
            try:
                check_graph_file_size_cap(existing_graph)
                prior = json.loads(existing_graph.read_text(encoding="utf-8"))
                prior_paths = _StoredSourcePaths(
                    prior,
                    out=out,
                    project_root=project_root,
                    watch_root=watch_root,
                    normalize_source=_nsf,
                )
                # Semantic doc nodes lack the AST origin marker. Gate on the
                # doc-shaped subset of the six-value file_type enum
                # (document/concept/rationale/paper AND code) rather than
                # "document" alone: per the extraction spec, a doc full of named
                # concepts may be represented with ONLY concept/rationale
                # nodes and no separate "document" node — that's still
                # evidence of a semantic layer, not a marker-less AST node
                # (#1954). "code" is included too (#2014): the semantic pass
                # legitimately mints code-typed nodes for symbols surfaced from
                # WITHIN a doc (llm.py `_bind_node_evidence`), and it cannot be
                # confused with a pre-#1865 marker-less AST code node — those are
                # sourced from code files, which never intersect ast_doc_files
                # below, whereas the AST quick-scan of a doc only ever mints
                # "document" nodes (extractors/markdown.py). "image" stays out.
                semantic_doc_identities: set[str] = set()
                for node in prior.get("nodes", []):
                    # _is_ast_tier, not a raw _origin check (#2334): a legacy
                    # unstamped AST heading node (source_location "L<n>") must
                    # not fake a semantic layer, or the doc would be excluded
                    # from the AST quick-scan forever.
                    if _is_ast_tier(node):
                        continue
                    if node.get("file_type") not in (
                        "document", "concept", "rationale", "paper", "code"
                    ):
                        continue
                    identity = prior_paths.identity(node.get("source_file"))
                    if identity:
                        semantic_doc_identities.add(identity)
                if semantic_doc_identities:
                    semantic_doc_files = {
                        p for p in ast_doc_files
                        if prior_paths.absolute_identity(str(p), project_root)
                        in semantic_doc_identities
                    }
            except Exception:
                semantic_doc_files = set()

        # Incremental path: when the caller passed an explicit change list,
        # extract only changed-and-still-existing files. Deleted paths are
        # tracked separately so their stale nodes can be evicted below.
        deleted_paths: set[str] = set()
        deleted_source_identities: set[str] = set()
        def _add_deleted_source(path: Path) -> None:
            deleted_source_identities.add(Path(os.path.abspath(path)).as_posix())
            for root in (project_root, watch_root):
                deleted_paths.add(_nsf(str(path), str(root)) or str(path))

        if changed_paths is not None:
            code_set = {Path(os.path.abspath(p)) for p in code_files}
            # #1915: semantic-backed docs are never AST-quick-scanned; their
            # semantic nodes are the sole representation. Mirroring #1865's
            # tier-scoped edge rule at the node level, they also must NOT
            # enter extract_targets (hence rebuilt/node-evicted identities) on
            # an incremental rebuild, or their semantic nodes would be wiped.
            semantic_doc_set = {Path(os.path.abspath(p)) for p in semantic_doc_files}
            wanted: list[Path] = []
            change_root = Path.cwd().resolve()
            for raw in changed_paths:
                candidates = _changed_path_candidates(
                    raw,
                    change_root=change_root,
                    watch_root=watch_root,
                )
                tracked = next((cand for cand in candidates if cand.exists() and cand in code_set), None)
                if tracked is not None:
                    if tracked not in wanted and tracked not in semantic_doc_set:
                        wanted.append(tracked)
                    continue

                existing_in_root = next(
                    (
                        cand for cand in candidates
                        if cand.exists() and _is_relative_to(cand, watch_root)
                    ),
                    None,
                )
                if existing_in_root is not None:
                    # The path exists under the watched root but detect filtered
                    # it out of code_set (no AST extractor, excluded, or
                    # sensitive). Existence is NOT deletion evidence (#2056): the
                    # file may carry semantic (LLM) nodes an AST rebuild cannot
                    # regenerate, and mis-routing it to _add_deleted_source both
                    # evicts those nodes AND sets had_explicit_deletions, which
                    # disables the shrink guard that would otherwise catch the
                    # loss. Preserve it — a genuine deletion still evicts via the
                    # branch below, the corpus sweep evicts a truly-gone non-AST
                    # source, and a deliberate exclusion is purged by the
                    # corpus sweep's ignore-rule check (#2495).
                    continue

                deleted_in_root = next(
                    (cand for cand in candidates if _is_relative_to(cand, watch_root)),
                    None,
                )
                if deleted_in_root is not None:
                    # File was deleted or renamed away inside the watched root.
                    # Evict preserved nodes that still claim this source path.
                    _add_deleted_source(deleted_in_root)
            from graph_fy.extractors.terraform import refresh_terraform_paths
            wanted = refresh_terraform_paths(wanted, code_files, changed_paths)
            if not wanted and not deleted_paths:
                print("[graph_fy watch] No tracked code files in change set - skipping rebuild.")
                return True
            extract_targets = wanted
        else:
            # Full rebuild: skip the AST quick-scan for semantic-backed docs
            # (#1915). They remain in code_files for corpus membership and
            # shrink accounting, but because they are not extract targets the
            # full-rebuild AST ownership rule (scoped to
            # rebuilt_source_identities, #2333 COEXIST) leaves their existing
            # AST heading layer intact alongside the semantic layer.
            extract_targets = [p for p in code_files if p not in semantic_doc_files]

        # #2406: an incremental rebuild parses only the changed files, so the
        # cross-file resolvers could not see a callee living in an unchanged
        # file and every changed->unchanged `calls` edge disappeared (reconcile
        # evicts the old one as AST-tier output of a re-extracted source, and
        # nothing regenerates it). Hand extract() read-only resolution context:
        # the persisted AST nodes of files this run is NOT re-extracting —
        # including bounded resolver metadata and callability markers — plus
        # the structural edges needed to resolve against unchanged types.
        #
        # Scoping rules, in order of importance:
        #   * AST-tier only — semantic/LLM nodes are not symbol definitions.
        #   * never a file in extract_targets (its fresh nodes are authoritative)
        #     nor a deleted one (its persisted symbols are gone).
        #   * only sources still in the scanned corpus, so a renamed/removed file
        #     cannot stay a resolver target.
        # extract() uses these purely to widen the resolvers' indexes; nothing
        # is parsed, mutated, or emitted from them (see extract()'s docstring).
        resolution_context_nodes: list[dict] = []
        resolution_context_edges: list[dict] = []
        if changed_paths is not None and existing_graph.exists():
            try:
                check_graph_file_size_cap(existing_graph)
                ctx_graph = json.loads(existing_graph.read_text(encoding="utf-8"))
                ctx_paths = _StoredSourcePaths(
                    ctx_graph,
                    out=out,
                    project_root=project_root,
                    watch_root=watch_root,
                    normalize_source=_nsf,
                )
                ctx_live = {
                    ctx_paths.absolute_identity(str(p), project_root) for p in code_files
                }
                ctx_live -= {
                    ctx_paths.absolute_identity(str(p), project_root) for p in extract_targets
                }
                ctx_live -= deleted_source_identities
                ctx_live.discard(None)
                for node in ctx_graph.get("nodes", []):
                    if not node.get("id") or not _is_ast_tier(node):
                        continue
                    source_file = node.get("source_file")
                    if not source_file or ctx_paths.identity(source_file) not in ctx_live:
                        continue
                    ctx_node = {
                        "id": node["id"],
                        "label": node.get("label"),
                        "source_file": source_file,
                        "file_type": node.get("file_type"),
                        "type": node.get("type"),
                    }
                    # #2438: the persisted callability markers are the only
                    # thing that lets an unchanged target pass the
                    # indirect_call guard — never re-derived from the label.
                    for marker in ("_callable", "_callable_class"):
                        if node.get(marker):
                            ctx_node[marker] = node[marker]
                    metadata = node.get("metadata")
                    if isinstance(metadata, dict):
                        ruby_metadata = {
                            key: metadata[key]
                            for key in (
                                "ruby_resolution_schema",
                                "ruby_method_kind",
                                "ruby_lookup_unsafe",
                                "ruby_reopened",
                                "ruby_external_method_owners",
                            )
                            if key in metadata
                        }
                        if ruby_metadata:
                            ctx_node["metadata"] = ruby_metadata
                    resolution_context_nodes.append(ctx_node)
                # #2437: the member-call resolvers map receiver type -> owning
                # class -> method through contains/method edges; hand over the
                # unchanged corpus's, scoped exactly like the nodes above so a
                # deleted/re-extracted file's edges can never resurrect.
                for edge in ctx_graph.get("links", ctx_graph.get("edges", [])):
                    if edge.get("relation") not in (
                        "contains", "method", "inherits"
                    ):
                        continue
                    if not _is_ast_tier(edge):
                        continue
                    source_file = edge.get("source_file")
                    if not source_file or ctx_paths.identity(source_file) not in ctx_live:
                        continue
                    context_edge = {
                        "source": edge.get("source"),
                        "target": edge.get("target"),
                        "relation": edge.get("relation"),
                        "source_file": source_file,
                    }
                    edge_metadata = edge.get("metadata")
                    if (
                        isinstance(edge_metadata, dict)
                        and isinstance(
                            edge_metadata.get("ruby_superclass_ref"), str
                        )
                    ):
                        context_edge["metadata"] = {
                            "ruby_superclass_ref": edge_metadata[
                                "ruby_superclass_ref"
                            ]
                        }
                        lexical_scopes = edge_metadata.get(
                            "ruby_lexical_scopes"
                        )
                        if isinstance(lexical_scopes, list) and all(
                            isinstance(scope, str) for scope in lexical_scopes
                        ):
                            context_edge["metadata"]["ruby_lexical_scopes"] = list(
                                lexical_scopes
                            )
                    resolution_context_edges.append(context_edge)
            except Exception:
                # Unreadable/oversized graph: resolve with the changed batch only
                # (pre-#2406 behavior). Reconcile below still fails closed on it.
                resolution_context_nodes = []
                resolution_context_edges = []

        commit = _git_head(cwd=watch_root)
        result = extract(
            extract_targets,
            cache_root=watch_root,
            resolution_context_nodes=resolution_context_nodes or None,
            resolution_context_edges=resolution_context_edges or None,
        ) if extract_targets else {
            "nodes": [], "edges": [], "hyperedges": [],
            "input_tokens": 0, "output_tokens": 0,
        }
        _rebase_relative_source_files(result, watch_root, project_root)

        # #2543: AST sources that failed this run (error result, or extractor
        # present but zero nodes) must not be stamped kind="ast" below, and any
        # prior stamp must be blanked (clear_ast) — otherwise the incremental
        # gate reports them unchanged forever and only deleting graph_fy_out/
        # recovers. Mirrors the extract CLI's _stamped_manifest_files handling.
        _failed_ast_sources = set(result.get("failed_sources") or [])

        def _ast_manifest_files() -> dict[str, list[str]]:
            """detected["files"] minus this run's failed AST sources (#2543).

            Only the STAMPED set shrinks; scan_corpus at the save sites stays
            the raw detect output so #1908 pruning is unaffected.
            """
            if not _failed_ast_sources:
                return detected["files"]
            failed_res = set(_failed_ast_sources)
            for p in _failed_ast_sources:
                try:
                    failed_res.add(str(Path(p).resolve()))
                except (OSError, RuntimeError):
                    pass

            def _failed(f: str) -> bool:
                if f in failed_res:
                    return True
                try:
                    return str(Path(f).resolve()) in failed_res
                except (OSError, RuntimeError):
                    return False

            return {
                ftype: [f for f in flist if not _failed(f)]
                for ftype, flist in detected["files"].items()
            }

        # Preserve semantic nodes/edges from a previous full run.
        # AST-only rebuild replaces nodes for changed files; everything else is kept.
        # Filter by node ID membership in the new AST output, not by file_type —
        # INFERRED/AMBIGUOUS nodes extracted from code files also carry file_type="code"
        # and would be wrongly dropped by a file_type-based filter.
        # When the caller supplied changed_paths, also evict preserved nodes whose
        # source_file matches a path that was changed (re-extracted) or deleted —
        # otherwise the old nodes for those files would survive forever.
        try:
            result, existing_graph_data = _reconcile_existing_graph(
                existing_graph,
                result,
                out=out,
                project_root=project_root,
                watch_root=watch_root,
                code_files=code_files,
                extract_targets=extract_targets,
                full_rebuild=changed_paths is None,
                deleted_paths=deleted_paths,
                deleted_source_identities=deleted_source_identities,
                is_ignored_always=_ignored_always,
                is_ignored_full=_ignored_full,
            )
        except (RuntimeError, ValueError) as exc:
            # Existing graph present but unreadable — over the size cap
            # (ValueError) or unparseable JSON (RuntimeError, both via
            # build._load_existing_graph). Refuse to overwrite a graph we
            # merely failed to READ (#2251), mirroring the CLI's fail-closed
            # contract (#2169). --force deliberately does NOT bypass this:
            # force means "accept a shrink", not "clobber an unreadable
            # graph".
            print(f"error: {exc}", file=sys.stderr)
            return False

        _relativize_source_files(result, project_root, scope=watch_root)
        # Source files re-extracted this run — their symbol sets may legitimately
        # shrink (a removed function), so the shrink-guard should not block the
        # write when every lost node belongs to one of them (or a deleted file).
        _rebuilt_root = str(project_root)
        if changed_paths is None:
            rebuilt_sources = {
                _nsf(str(p.relative_to(project_root)), _rebuilt_root)
                for p in code_files if p.is_relative_to(project_root)
            }
        else:
            rebuilt_sources = {(_nsf(str(p), _rebuilt_root) or str(p)) for p in extract_targets}
        rebuilt_sources |= set(deleted_paths)
        failed_sources = {
            _nsf(source, _rebuilt_root) or source
            for source in _failed_ast_sources
        }
        out.mkdir(exist_ok=True)
        if _graph_fy_OUT == "graph_fy_out":
            compat_link = out.parent / "graph_fy_out"
            if not compat_link.exists() and not compat_link.is_symlink():
                try:
                    compat_link.symlink_to("graph_fy_out", target_is_directory=True)
                except Exception:
                    pass

        if no_cluster:
            # Normalise to "links" key so schema is consistent with the full clustered path.
            # Dedupe parallel edges (the clustered path's DiGraph collapses them implicitly);
            # without it, --no-cluster + repeated `update` accumulate duplicates and edge
            # counts diverge across build modes (#1317).
            from graph_fy.build import (
                dedupe_edges as _dedupe_edges,
                dedupe_nodes as _dedupe_nodes,
                disambiguate_file_labels_in_nodes as _disamb_labels,
            )
            raw_nodes = _dedupe_nodes(result.get("nodes", []))
            _disamb_labels(raw_nodes)
            candidate_graph_data = {
                **{k: v for k, v in result.items() if k not in ("edges", "nodes")},
                "nodes": raw_nodes,
                "links": _dedupe_edges(result.get("edges", [])),
                # Inherit the existing graph's directed flag (#2342) so
                # `graph_fy update --no-cluster` can't silently drop it -
                # `result` (the raw merged extraction) never carries one.
                "directed": bool((existing_graph_data or {}).get("directed", False)),
            }
            candidate_graph_text = _json_text(candidate_graph_data)
            same_graph = False
            if existing_graph.exists():
                try:
                    check_graph_file_size_cap(existing_graph)
                    existing_payload = json.loads(existing_graph.read_text(encoding="utf-8"))
                except Exception as exc:
                    # A load failure is NOT "graph changed" (#2251): refuse to
                    # overwrite a graph we merely failed to read. Normally
                    # unreachable — the reconcile load above already failed
                    # closed — but a race rewriting the file can land here.
                    print(
                        f"error: Cannot read {existing_graph}: {exc}. "
                        "Refusing to overwrite; delete the file and run a "
                        "full rebuild.",
                        file=sys.stderr,
                    )
                    return False
                try:
                    same_graph = (
                        json.dumps(_canonical_graph_for_compare(existing_payload), sort_keys=True, ensure_ascii=False)
                        == json.dumps(_canonical_graph_for_compare(candidate_graph_data), sort_keys=True, ensure_ascii=False)
                    )
                except Exception:
                    same_graph = False
            if not same_graph:
                if not _check_shrink(
                    force, existing_graph_data, candidate_graph_data,
                    had_explicit_deletions=bool(deleted_paths),
                    rebuilt_sources=rebuilt_sources,
                    failed_sources=failed_sources,
                ):
                    return False
                from graph_fy.export import backup_if_protected as _backup
                _backup(out)
                # Atomic replace via tmp file, matching the clustered path: a
                # crash mid-write must not leave a truncated graph.json.
                # os_replace_with_fallback, not a plain Path.replace (#2689):
                # this function just read existing_graph a few lines up, and
                # on a VMware HGFS shared folder a replace over a destination
                # read earlier in the same process raises PermissionError
                # even on the same drive.
                graph_tmp = out / ".graph.tmp.json"
                graph_tmp.write_text(candidate_graph_text, encoding="utf-8")
                os_replace_with_fallback(graph_tmp, existing_graph)

            # Write the user-supplied path only after the candidate graph is
            # accepted, so a refused shrink cannot mismatch graph and marker.
            (out / ".graph_fy_root").write_text(str(watch_path), encoding="utf-8")

            try:
                from graph_fy.detect import save_manifest
                # detected["files"] is a FULL detect of the watched root, so
                # pass it as the scan corpus too: rows for files that left the
                # scan but still exist on disk (newly excluded) are pruned
                # instead of surviving as phantom "deleted" entries (#1908).
                # Failed AST sources are dropped from the stamped set and
                # their prior hashes blanked (#2543).
                save_manifest(
                    _ast_manifest_files(), manifest_path=str(out / "manifest.json"),
                    kind="ast", root=watch_root,
                    scan_corpus={f for _fl in detected["files"].values() for f in _fl},
                    clear_ast=_failed_ast_sources or None,
                )
            except Exception:
                pass

            if same_graph:
                print("[graph_fy watch] No code-graph changes detected (--no-cluster); outputs left untouched.")
            else:
                print(
                    "[graph_fy watch] Rebuilt (no clustering): "
                    f"{len(candidate_graph_data.get('nodes', []))} nodes, "
                    f"{len(candidate_graph_data.get('links', []))} edges"
                )
                print(f"[graph_fy watch] graph.json updated in {out}")
            return True

        detection = {
            "files": {"code": [str(f) for f in code_files], "document": [], "paper": [], "image": []},
            "total_files": len(code_files),
            "total_words": detected.get("total_words", 0),
            "unclassified": detected.get("unclassified", []),
        }

        # Inherit the existing graph's directed flag (#2342) so `graph_fy
        # update` can't silently downgrade a directed graph to undirected -
        # build_from_json defaults to directed=False otherwise.
        G = build_from_json(result, directed=bool((existing_graph_data or {}).get("directed", False)))
        candidate_topology = _topology_from_graph(G)
        if existing_graph_data:
            try:
                same_topology = (
                    json.dumps(_canonical_topology_for_compare(existing_graph_data), sort_keys=True, ensure_ascii=False)
                    == json.dumps(_canonical_topology_for_compare(candidate_topology), sort_keys=True, ensure_ascii=False)
                )
            except Exception:
                same_topology = False
            if same_topology:
                try:
                    from graph_fy.detect import save_manifest
                    # Full-scan save: prune excluded-but-alive rows (#1908);
                    # leave failed AST sources unstamped (#2543).
                    save_manifest(
                        _ast_manifest_files(), manifest_path=str(out / "manifest.json"),
                        kind="ast", root=watch_root,
                        scan_corpus={f for _fl in detected["files"].values() for f in _fl},
                        clear_ast=_failed_ast_sources or None,
                    )
                except Exception:
                    pass
                html_action = _reconcile_graph_html(out, existing_graph_data)
                if html_action == "rendered":
                    print(
                        "[graph_fy watch] No code-graph topology changes detected; "
                        "regenerated missing or stale graph.html."
                    )
                elif html_action == "removed":
                    print(
                        "[graph_fy watch] No code-graph topology changes detected; "
                        "removed graph.html because HTML visualization is disabled."
                    )
                else:
                    print("[graph_fy watch] No code-graph topology changes detected; outputs left untouched.")
                return True

        communities = cluster(G)
        previous_node_community = _node_community_map(existing_graph_data)
        if previous_node_community:
            communities = remap_communities_to_previous(communities, previous_node_community)
        cohesion = score_all(G, communities)
        gods = god_nodes(G)
        surprises = surprising_connections(G, communities)
        labels_file = out / ".graph_fy_labels.json"
        sig_file = out / (".graph_fy_labels.json" + ".sig")
        try:
            raw = json.loads(labels_file.read_text(encoding="utf-8")) if labels_file.exists() else {}
            # Skip persisted "Community N" placeholders so the hub-fill below
            # replaces them instead of perpetuating them on every rebuild (#2073).
            labels = {
                int(k): v for k, v in raw.items()
                if int(k) in communities and v != f"Community {int(k)}"
            }
        except Exception:
            raw = {}
            labels = {}
        # A saved label belongs to a cid, but re-clustering reassigns cids: after a
        # rebuild that adds nodes, cid 30 can cover a completely different community
        # and its old name is then simply wrong. Validate every reused label against
        # the membership signature saved beside the labels — the same guard the
        # cluster-only path applies — and drop any whose community changed so the
        # hub-fill below renames it, deterministically and correct-by-construction.
        # Without this, an incremental `graph_fy update` launders stale names into
        # labels.json as though they were current (#label-stale).
        from graph_fy.cluster import community_member_sigs
        cur_sigs = community_member_sigs(communities)
        saved_sigs: dict[int, str] = {}
        if sig_file.exists():
            try:
                saved_sigs = {
                    int(k): v for k, v in
                    json.loads(sig_file.read_text(encoding="utf-8")).items()
                    if isinstance(v, str)
                }
            except Exception:
                saved_sigs = {}
        if saved_sigs:
            # Precise: the signature tells us exactly which communities changed.
            stale = {cid for cid in labels if saved_sigs.get(cid) != cur_sigs.get(cid)}
        else:
            # No sidecar (labels predate it). A differing community COUNT means the
            # labels describe a different clustering, so no cid's label is trustworthy;
            # an equal count is the best available "unchanged" signal.
            stale = set(labels) if len(raw) != len(communities) else set()
        for cid in stale:
            del labels[cid]
        missing = {cid: members for cid, members in communities.items() if cid not in labels}
        if missing:
            # Deterministic hub name (highest-degree member) beats a bare "Community N"
            # placeholder for any community without a saved label.
            from graph_fy.cluster import label_communities_by_hub
            labels.update(label_communities_by_hub(G, missing))
        if stale:
            print(
                f"[graph_fy watch] community set changed since labeling "
                f"({len(raw)} saved labels, {len(communities)} communities now; "
                f"renamed {len(stale)} community(ies) by their hub). "
                f"Run `graph_fy label` to refresh names with the LLM.",
                file=sys.stderr,
            )
        questions = suggest_questions(G, communities, labels)
        from graph_fy.report import load_learning_for_report as _llfr
        report = generate(G, communities, cohesion, labels, gods, surprises, detection,
                          {"input": 0, "output": 0}, report_root, suggested_questions=questions,
                          built_at_commit=commit, learning=_llfr(out / "graph.json"))
        report_path = out / "GRAPH_REPORT.md"
        labels_json = json.dumps({str(k): v for k, v in sorted(labels.items())}, ensure_ascii=False, indent=2) + "\n"
        graph_tmp = out / ".graph.tmp.json"
        json_written = to_json(G, communities, str(graph_tmp), force=True, built_at_commit=commit, community_labels=labels)
        if not json_written:
            return False
        candidate_graph_data = json.loads(graph_tmp.read_text(encoding="utf-8"))
        same_graph = False
        same_report = False
        if existing_graph.exists():
            try:
                check_graph_file_size_cap(existing_graph)
                existing_payload = json.loads(existing_graph.read_text(encoding="utf-8"))
            except Exception as exc:
                # A load failure is NOT "graph changed" (#2251): refuse to
                # overwrite a graph we merely failed to read. Normally
                # unreachable — the reconcile load above already failed
                # closed — but a race rewriting the file can land here.
                graph_tmp.unlink(missing_ok=True)
                print(
                    f"error: Cannot read {existing_graph}: {exc}. "
                    "Refusing to overwrite; delete the file and run a "
                    "full rebuild.",
                    file=sys.stderr,
                )
                return False
            try:
                same_graph = (
                    json.dumps(_canonical_graph_for_compare(existing_payload), sort_keys=True, ensure_ascii=False)
                    == json.dumps(_canonical_graph_for_compare(candidate_graph_data), sort_keys=True, ensure_ascii=False)
                )
            except Exception:
                same_graph = False
        if report_path.exists():
            old_report = report_path.read_text(encoding="utf-8")
            same_report = _report_for_compare(old_report) == _report_for_compare(report)
        no_change = same_graph and same_report
        if no_change:
            graph_tmp.unlink(missing_ok=True)
            print("[graph_fy watch] No code-graph changes detected; graph.json/GRAPH_REPORT.md left untouched.")
        else:
            if not _check_shrink(
                force, existing_graph_data, candidate_graph_data,
                tmp=graph_tmp,
                had_explicit_deletions=bool(deleted_paths),
                rebuilt_sources=rebuilt_sources,
                failed_sources=failed_sources,
            ):
                return False
            from graph_fy.exporters.html import _HTML_STALE_MARKER
            # Mark before graph.json advances so an interruption cannot leave a
            # previous visualization looking current to the fast path.
            (out / _HTML_STALE_MARKER).touch()
            from graph_fy.export import backup_if_protected as _backup
            _backup(out)
            # os_replace_with_fallback, not a plain Path.replace (#2689): this
            # function read existing_graph a few lines up for the same_graph
            # comparison, and on a VMware HGFS shared folder a replace over a
            # destination read earlier in the same process raises
            # PermissionError even on the same drive.
            os_replace_with_fallback(graph_tmp, existing_graph)
            report_path.write_text(report, encoding="utf-8")
            labels_file.write_text(labels_json, encoding="utf-8")
            # Keep the membership signatures in step with the labels we just wrote.
            # Skipping this was the other half of the stale-label bug: labels.json
            # advanced every rebuild while the sidecar kept describing an older
            # clustering, so the guard above had nothing accurate to check against.
            sig_file.write_text(
                json.dumps({str(k): v for k, v in cur_sigs.items()}), encoding="utf-8")
            try:
                from graph_fy.retrieval import build_index as _build_fts_index
                _build_fts_index(G, out / "graph.db")
            except Exception:
                pass

        (out / ".graph_fy_root").write_text(str(watch_path), encoding="utf-8")

        try:
            delta_path = out / ".graph_fy_delta.json"
            delta_info = {
                "timestamp": time.time(),
                "built_at_commit": commit,
                "nodes_count": G.number_of_nodes(),
                "edges_count": G.number_of_edges(),
                "changed_files": [str(p) for p in (changed_paths or [])],
                "same_graph": same_graph,
            }
            delta_path.write_text(json.dumps(delta_info, indent=2), encoding="utf-8")
        except Exception:
            pass

        try:
            from graph_fy.detect import save_manifest
            # Full-scan save: prune excluded-but-alive rows (#1908);
            # leave failed AST sources unstamped (#2543).
            save_manifest(
                _ast_manifest_files(), manifest_path=str(out / "manifest.json"),
                kind="ast", root=watch_root,
                scan_corpus={f for _fl in detected["files"].values() for f in _fl},
                clear_ast=_failed_ast_sources or None,
            )
        except Exception:
            pass

        # Reconcile from the persisted graph. The stale marker was written
        # before graph.json advanced, so a failed or interrupted atomic render
        # remains retryable from the unchanged-topology fast path.
        html_written = False
        if not no_change:
            html_action = _reconcile_graph_html(out, candidate_graph_data)
            html_written = html_action == "rendered"

        # Regenerate callflow HTML if the user previously generated one —
        # opt-in by existence so users who never ran callflow-html aren't affected.
        callflow_files = list(out.glob("*-callflow.html"))
        if callflow_files and not no_change:
            try:
                from graph_fy.callflow_html import write_callflow_html
                for cf in callflow_files:
                    write_callflow_html(
                        graph=out / "graph.json",
                        report=out / "GRAPH_REPORT.md",
                        labels=out / ".graph_fy_labels.json",
                        output=cf,
                        verbose=False,
                    )
            except Exception as cf_err:
                print(f"[graph_fy watch] callflow HTML update skipped: {cf_err}")

        if not no_change:
            print(f"[graph_fy watch] Rebuilt: {G.number_of_nodes()} nodes, "
                  f"{G.number_of_edges()} edges, {len(communities)} communities")
            products = "graph.json" + (", graph.html" if html_written else "") + " and GRAPH_REPORT.md"
            if callflow_files:
                products += f", {len(callflow_files)} callflow HTML"
            print(f"[graph_fy watch] {products} updated in {out}")
        return True

    except Exception as exc:
        print(f"[graph_fy watch] Rebuild failed: {exc}")
        return False


def check_update(watch_path: Path) -> bool:
    """Check for pending semantic update flag and notify the user if set.

    Cron-safe: always returns True so cron jobs do not alarm.
    Non-code file changes (docs, papers, images) require LLM-backed
    re-extraction via `/graph_fy --update` — this function only signals
    that the update is needed.
    """
    flag = Path(watch_path) / _graph_fy_OUT / "needs_update"
    if flag.exists():
        print(f"[graph_fy check-update] Pending non-code changes in {watch_path}.")
        print("[graph_fy check-update] Run `/graph_fy --update` to apply semantic re-extraction.")
    return True


def _notify_only(watch_path: Path) -> None:
    """Write a flag file and print a notification (fallback for non-code-only corpora)."""
    flag = watch_path / _graph_fy_OUT / "needs_update"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("1", encoding="utf-8")
    print(f"\n[graph_fy watch] New or changed files detected in {watch_path}")
    print("[graph_fy watch] Non-code files changed - semantic re-extraction requires LLM.")
    print("[graph_fy watch] Run `/graph_fy --update` in Claude Code to update the graph.")
    print(f"[graph_fy watch] Flag written to {flag}")


def _has_non_code(changed_paths: list[Path]) -> bool:
    return any(p.suffix.lower() not in _CODE_EXTENSIONS for p in changed_paths)


def _batch_triggers_rebuild(batch: list[Path]) -> bool:
    """True when a debounced watch batch needs an immediate rebuild.

    Code changes always rebuild (AST extraction needs no LLM). Deletions of
    ANY watched file also rebuild: eviction needs no LLM either — the full
    corpus reconcile drops nodes whose source is gone from disk. Without
    this, a doc-only deletion batch would sit behind the needs_update flag
    until the next code event or a manual `graph_fy update` (#2580).
    """
    has_code = any(p.suffix.lower() in _CODE_EXTENSIONS for p in batch)
    has_deletion = any(not p.exists() for p in batch)
    return has_code or has_deletion


_READ_ONLY_EVENT_TYPES = frozenset({"opened", "closed_no_write"})


def _is_read_only_event(event) -> bool:
    """True for watchdog events that mean a file was merely READ, not changed.

    On Linux, inotify (watchdog >= 2.3) reports ``opened`` and, since watchdog 4,
    ``closed_no_write`` for every file open/close — including the watcher's own
    AST rebuild reading the tree, hook guards stat-ing sources, and editors or
    agents reading files. Counting those as changes makes the watcher re-trigger
    itself forever ("N file(s) changed" while nothing was modified), burn CPU and
    keep re-writing the ``needs_update`` flag. Only creation, modification, move,
    deletion and close-after-write are changes; macOS (PollingObserver) never
    emits these, so the filter is a no-op there.
    """
    return getattr(event, "event_type", None) in _READ_ONLY_EVENT_TYPES


def _batch_needs_llm_flag(batch: list[Path]) -> bool:
    """True when the batch contains a non-code file that still exists on disk.

    Only surviving non-code files need the needs_update flag (LLM-backed
    re-extraction); deleted ones are already handled by the rebuild's
    reconcile sweep, so a pure-deletion batch must not leave a stale flag.
    """
    return _has_non_code([p for p in batch if p.exists()])


def patch_graph_in_memory(
    G: nx.Graph,
    changed_files: list[Path],
    root: Path,
    *,
    out_dir: Path | None = None,
) -> tuple[nx.Graph, dict]:
    """Incrementally patch an in-memory graph G for changed_files and re-index.

    1. Incrementally extracts AST for changed_files using extract(changed_files, root=root).
    2. Evicts old nodes and edges whose source_file matches the changed files.
    3. Adds newly extracted nodes and edges into the live graph G.
    4. Updates and preserves cross-file edge references.
    5. Re-exports graph.json and rebuilds FTS5 index.db without a slow cold process restart.

    Returns:
        tuple[nx.Graph, dict]: the patched graph G and a stats dictionary.
    """
    root_path = Path(root).resolve()
    out = Path(out_dir).resolve() if out_dir is not None else (root_path / _graph_fy_OUT)

    if not changed_files:
        return G, {
            "added_nodes": [],
            "removed_nodes": [],
            "added_edges": 0,
            "removed_edges": 0,
            "node_count": G.number_of_nodes(),
            "edge_count": G.number_of_edges(),
            "extraction": {"nodes": [], "edges": []},
        }

    from graph_fy.build import _abs_identity, _norm_source_file
    from graph_fy.ids import normalize_id as _normalize_id

    changed_abs_identities: set[str] = set()
    for f in changed_files:
        ident = _abs_identity(str(f), str(root_path))
        if ident:
            changed_abs_identities.add(ident)
        p = f if f.is_absolute() else (root_path / f)
        try:
            changed_abs_identities.add(p.resolve().as_posix())
        except (OSError, ValueError):
            pass
        changed_abs_identities.add(p.as_posix())

    def _source_file_matches(sf: str | None) -> bool:
        if not sf:
            return False
        ident = _abs_identity(sf, str(root_path))
        if ident and ident in changed_abs_identities:
            return True
        norm = _norm_source_file(sf, str(root_path))
        if norm:
            for c in changed_files:
                c_norm = _norm_source_file(str(c), str(root_path))
                if c_norm and norm == c_norm:
                    return True
        return False

    nodes_to_remove = [
        n for n, d in G.nodes(data=True)
        if _source_file_matches(d.get("source_file")) or _source_file_matches(d.get("definition_file"))
    ]
    nodes_to_remove_set = set(nodes_to_remove)

    preserved_cross_edges: list[tuple[Any, Any, dict]] = []
    for u, v, data in G.edges(data=True):
        edge_sf = data.get("source_file")
        if not _source_file_matches(edge_sf):
            if u in nodes_to_remove_set or v in nodes_to_remove_set:
                preserved_cross_edges.append((u, v, data.copy()))

    edges_to_remove = [
        (u, v) for u, v, data in G.edges(data=True)
        if _source_file_matches(data.get("source_file"))
    ]
    G.remove_edges_from(edges_to_remove)
    G.remove_nodes_from(nodes_to_remove)

    resolution_context_nodes = [
        {
            "id": nid,
            "label": attrs.get("label"),
            "source_file": attrs.get("source_file"),
            "file_type": attrs.get("file_type"),
            "type": attrs.get("type"),
            **({"_callable": attrs["_callable"]} if attrs.get("_callable") else {}),
            **({"_callable_class": attrs["_callable_class"]} if attrs.get("_callable_class") else {}),
        }
        for nid, attrs in G.nodes(data=True)
        if attrs.get("_origin", "ast") == "ast" and attrs.get("source_file")
    ]
    resolution_context_edges = [
        {
            "source": u,
            "target": v,
            "relation": d.get("relation"),
            "source_file": d.get("source_file"),
        }
        for u, v, d in G.edges(data=True)
        if d.get("relation") in ("contains", "method", "inherits")
    ]

    from graph_fy.extract import extract

    existing_files = [
        f if f.is_absolute() or f.exists() else (root_path / f)
        for f in changed_files
    ]
    existing_files = [f for f in existing_files if f.exists()]

    if existing_files:
        extract_targets = changed_files if len(existing_files) == len(changed_files) else existing_files
        extracted = extract(
            extract_targets,
            root=root_path,
            resolution_context_nodes=resolution_context_nodes or None,
            resolution_context_edges=resolution_context_edges or None,
        )
    else:
        extracted = {"nodes": [], "edges": [], "hyperedges": []}

    added_node_ids: list[str] = []
    for node in extracted.get("nodes", []):
        if not isinstance(node, dict) or "id" not in node:
            continue
        nid = str(node["id"])
        node_attrs = dict(node)
        node_attrs.pop("id", None)
        if "source_file" in node_attrs and node_attrs["source_file"]:
            node_attrs["source_file"] = _norm_source_file(node_attrs["source_file"], str(root_path))
        if "definition_file" in node_attrs and node_attrs["definition_file"]:
            node_attrs["definition_file"] = _norm_source_file(node_attrs["definition_file"], str(root_path))
        node_attrs.setdefault("_origin", "ast")
        G.add_node(nid, **node_attrs)
        added_node_ids.append(nid)

    node_set = set(G.nodes())
    norm_to_id: dict[str, str] = {_normalize_id(n): n for n in node_set}

    added_edges_count = 0
    for edge in extracted.get("edges", []):
        if not isinstance(edge, dict):
            continue
        src = edge.get("source")
        tgt = edge.get("target")
        if not src or not tgt:
            continue
        src = str(src)
        tgt = str(tgt)
        if src not in node_set:
            src = norm_to_id.get(_normalize_id(src), src)
        if tgt not in node_set:
            tgt = norm_to_id.get(_normalize_id(tgt), tgt)
        if src in node_set and tgt in node_set:
            attrs = {k: v for k, v in edge.items() if k not in ("source", "target", "target_file", "local_alias")}
            if "source_file" in attrs and attrs["source_file"]:
                attrs["source_file"] = _norm_source_file(attrs["source_file"], str(root_path))
            else:
                attrs["source_file"] = (
                    G.nodes[src].get("source_file")
                    or G.nodes[tgt].get("source_file")
                    or ""
                )
            if attrs.get("definition_file"):
                attrs["definition_file"] = _norm_source_file(attrs["definition_file"], str(root_path))
            attrs.setdefault("_origin", "ast")
            attrs["_src"] = src
            attrs["_tgt"] = tgt
            G.add_edge(src, tgt, **attrs)
            added_edges_count += 1

    for u, v, attrs in preserved_cross_edges:
        u_resolved = u if u in node_set else norm_to_id.get(_normalize_id(u))
        v_resolved = v if v in node_set else norm_to_id.get(_normalize_id(v))
        if u_resolved and v_resolved and u_resolved in node_set and v_resolved in node_set:
            G.add_edge(u_resolved, v_resolved, **attrs)
            added_edges_count += 1

    communities: dict[int, list[str]] = {}
    for nid, attrs in G.nodes(data=True):
        cid = attrs.get("community")
        if cid is not None:
            try:
                communities.setdefault(int(cid), []).append(nid)
            except (ValueError, TypeError):
                pass

    if not communities and len(G) > 0:
        try:
            from graph_fy.cluster import cluster
            communities = cluster(G)
        except Exception:
            communities = {0: list(G.nodes())}

    for nid in added_node_ids:
        if G.nodes[nid].get("community") is None:
            assigned_cid = None
            for neighbor in G.neighbors(nid):
                ncid = G.nodes[neighbor].get("community")
                if ncid is not None:
                    try:
                        assigned_cid = int(ncid)
                        break
                    except (ValueError, TypeError):
                        pass
            if assigned_cid is None:
                assigned_cid = next(iter(communities.keys()), 0)
            G.nodes[nid]["community"] = assigned_cid
            communities.setdefault(assigned_cid, []).append(nid)

    out.mkdir(parents=True, exist_ok=True)
    graph_json_path = out / "graph.json"
    graph_db_path = out / "graph.db"

    labels = None
    labels_file = out / ".graph_fy_labels.json"
    if labels_file.exists():
        try:
            raw_labels = json.loads(labels_file.read_text(encoding="utf-8"))
            labels = {int(k): v for k, v in raw_labels.items() if int(k) in communities}
        except Exception:
            labels = None

    from graph_fy.export import to_json
    to_json(G, communities, str(graph_json_path), force=True, community_labels=labels)
    (out / ".graph_fy_root").write_text(str(root), encoding="utf-8")

    try:
        from graph_fy.retrieval import build_index
        build_index(G, graph_db_path)
    except Exception as exc:
        logger.debug("Failed to build FTS index during patch: %s", exc)

    stats = {
        "added_nodes": added_node_ids,
        "removed_nodes": nodes_to_remove,
        "added_edges": added_edges_count,
        "removed_edges": len(edges_to_remove),
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
        "extraction": extracted,
    }
    return G, stats


def watch_hot(
    watch_path: Path,
    debounce: float = 0.5,
    *,
    initial_graph: nx.Graph | None = None,
    stop_event: Any = None,
    max_iterations: int | None = None,
    on_patched: Callable[[nx.Graph, dict], None] | None = None,
) -> nx.Graph:
    """Hot in-memory graph patching & instant re-indexing daemon.

    Keeps the live NetworkX graph in memory across filesystem events.
    When code files change, incrementally extracts and patches G and re-indexes
    FTS5 index.db in under 100ms without a cold process restart.
    """
    try:
        from watchdog.observers import Observer
        from watchdog.observers.polling import PollingObserver
        from watchdog.events import FileSystemEventHandler
    except ImportError as e:
        raise ImportError("watchdog not installed. Run: pip install watchdog") from e

    watch_root = watch_path.resolve()
    out = watch_path / _graph_fy_OUT
    existing_graph = out / "graph.json"

    if initial_graph is not None:
        G = initial_graph
    elif existing_graph.exists():
        try:
            from graph_fy.build import build_from_json
            data = json.loads(existing_graph.read_text(encoding="utf-8"))
            G = build_from_json(data, directed=bool(data.get("directed", False)), root=watch_root)
            print(f"[graph_fy watch] Loaded in-memory graph: {len(G)} nodes, {G.number_of_edges()} edges")
        except Exception:
            _rebuild_code(watch_path)
            data = json.loads(existing_graph.read_text(encoding="utf-8"))
            from graph_fy.build import build_from_json
            G = build_from_json(data, directed=bool(data.get("directed", False)), root=watch_root)
    else:
        print("[graph_fy watch] Initializing graph...")
        _rebuild_code(watch_path)
        if existing_graph.exists():
            data = json.loads(existing_graph.read_text(encoding="utf-8"))
            from graph_fy.build import build_from_json
            G = build_from_json(data, directed=bool(data.get("directed", False)), root=watch_root)
        else:
            G = nx.DiGraph()

    last_trigger: float = 0.0
    pending: bool = False
    changed: set[Path] = set()

    ignore_patterns = _load_graph_fyignore(
        watch_root,
        gitignore=_read_build_gitignore(out),
    )

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            nonlocal last_trigger, pending
            if event.is_directory or _is_read_only_event(event):
                return
            path = Path(os.fsdecode(event.src_path))
            if ignore_patterns and _is_ignored(path, watch_root, ignore_patterns):
                return
            if path.suffix.lower() not in _WATCHED_EXTENSIONS:
                return
            try:
                filter_parts = path.relative_to(watch_root).parts
            except ValueError:
                filter_parts = path.parts
            if any(part.startswith(".") for part in filter_parts):
                return
            if _graph_fy_OUT in filter_parts:
                return
            last_trigger = time.monotonic()
            pending = True
            changed.add(path)

    handler = Handler()
    observer = PollingObserver() if sys.platform == "darwin" else Observer()
    observer.schedule(handler, str(watch_path), recursive=True)
    observer.start()

    print(f"[graph_fy watch --hot] Watching {watch_root} (Hot in-memory patch mode)")
    print("[graph_fy watch --hot] Code changes patch graph & re-index in <100ms.")
    print(f"[graph_fy watch --hot] Debounce: {debounce}s")

    iterations = 0
    try:
        while True:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            time.sleep(0.05)
            if pending and (time.monotonic() - last_trigger) >= debounce:
                pending = False
                batch = list(changed)
                changed.clear()
                print(f"\n[graph_fy watch --hot] {len(batch)} file(s) changed")
                if _batch_triggers_rebuild(batch):
                    code_batch = [p for p in batch if p.suffix.lower() in _CODE_EXTENSIONS or not p.exists()]
                    if code_batch:
                        t0 = time.perf_counter()
                        G, stats = patch_graph_in_memory(G, code_batch, root=watch_path)
                        latency_ms = (time.perf_counter() - t0) * 1000
                        print(
                            f"[graph_fy watch --hot] Patched in {latency_ms:.1f}ms: "
                            f"{len(G)} nodes, {G.number_of_edges()} edges"
                        )
                        if on_patched:
                            on_patched(G, stats)
                    else:
                        _rebuild_code(watch_path, changed_paths=batch)
                if _batch_needs_llm_flag(batch):
                    _notify_only(watch_path)
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
    except KeyboardInterrupt:
        print("\n[graph_fy watch --hot] Stopped.")
    finally:
        observer.stop()
        observer.join()

    return G


def watch(watch_path: Path, debounce: float = 3.0, *, hot: bool = False) -> None:
    """
    Watch watch_path for new or modified files and auto-update the graph.

    For code-only changes: re-runs AST extraction + rebuild immediately (no LLM).
    For doc/paper/image changes: writes a needs_update flag and notifies the user
    to run /graph_fy --update (LLM extraction required).

    debounce: seconds to wait after the last change before triggering (avoids
    running on every keystroke when many files are saved at once).
    hot: if True, runs in hot in-memory patching daemon mode (<100ms latency).
    """
    if hot:
        watch_hot(watch_path, debounce=debounce)
        return

    try:
        from watchdog.observers import Observer
        from watchdog.observers.polling import PollingObserver
        from watchdog.events import FileSystemEventHandler
    except ImportError as e:
        raise ImportError("watchdog not installed. Run: pip install watchdog") from e

    last_trigger: float = 0.0
    pending: bool = False
    changed: set[Path] = set()

    # Load .graph_fyignore patterns ONCE at startup so the handler does not
    # re-parse the file on every filesystem event. Watchdog's handler runs on
    # the observer thread and is invoked for every event the OS delivers
    # (Time Machine writes, Docker/Colima VM I/O, Spotlight indexing, …) —
    # without this short-circuit a busy volume can saturate a CPU core
    # discarding events one extension at a time. (gh-928)
    watch_root_for_ignore = watch_path.resolve()
    ignore_patterns = _load_graph_fyignore(
        watch_root_for_ignore,
        gitignore=_read_build_gitignore(watch_path / _graph_fy_OUT),
    )

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            nonlocal last_trigger, pending
            if event.is_directory or _is_read_only_event(event):
                return
            path = Path(os.fsdecode(event.src_path))
            # Check .graph_fyignore BEFORE the extension/dotfile/out filters so
            # the cheapest short-circuit for users with broad ignore patterns
            # (node_modules/, .venv/, build/, …) fires first. _is_ignored
            # tolerates absolute paths outside watch_root via its internal
            # relative_to guard, so a stray symlinked event won't raise.
            if ignore_patterns and _is_ignored(path, watch_root_for_ignore, ignore_patterns):
                return
            if path.suffix.lower() not in _WATCHED_EXTENSIONS:
                return
            try:
                filter_parts = path.relative_to(watch_root_for_ignore).parts
            except ValueError:
                filter_parts = path.parts
            if any(part.startswith(".") for part in filter_parts):
                return
            if _graph_fy_OUT in filter_parts:
                return
            last_trigger = time.monotonic()
            pending = True
            changed.add(path)

    handler = Handler()
    # Use polling observer on macOS — FSEvents can miss rapid saves in some editors
    observer = PollingObserver() if sys.platform == "darwin" else Observer()
    observer.schedule(handler, str(watch_path), recursive=True)
    observer.start()

    print(f"[graph_fy watch] Watching {watch_path.resolve()} - press Ctrl+C to stop")
    print(f"[graph_fy watch] Code changes rebuild graph automatically. "
          f"Doc/image changes require /graph_fy --update.")
    print(f"[graph_fy watch] Debounce: {debounce}s")

    try:
        while True:
            time.sleep(0.5)
            if pending and (time.monotonic() - last_trigger) >= debounce:
                pending = False
                batch = list(changed)
                changed.clear()
                print(f"\n[graph_fy watch] {len(batch)} file(s) changed")
                if _batch_triggers_rebuild(batch):
                    _rebuild_code(watch_path)
                if _batch_needs_llm_flag(batch):
                    _notify_only(watch_path)
    except KeyboardInterrupt:
        print("\n[graph_fy watch] Stopped.")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Watch a folder and auto-update the graph_fy graph")
    parser.add_argument("path", nargs="?", default=".", help="Folder to watch (default: .)")
    parser.add_argument("--debounce", type=float, default=3.0,
                        help="Seconds to wait after last change before updating (default: 3)")
    parser.add_argument("--hot", action="store_true", default=False,
                        help="Enable hot in-memory graph patching and instant re-indexing")
    args = parser.parse_args()
    watch(Path(args.path), debounce=args.debounce, hot=args.hot)
