"""Cross-language and framework symbol resolution."""
from __future__ import annotations

import os
from pathlib import Path
from collections import Counter
import json

from graph_fy.symbol_resolution import resolve_bash_source_edges
from graph_fy.extractors.go import _GO_PREDECLARED_FUNCS
from graph_fy.extractors.node_merging import (
    _canonicalize_csharp_namespace_nodes,
    _lang_family,
    _lang_is_case_insensitive,
    _merge_csharp_partial_class_nodes,
    _merge_swift_extensions,
    _remap_objc_field_tables,
    _rewire_unique_stub_nodes,
)
from graph_fy.extractors.resolution import (
    _go_import_path_for_file,
)
from graph_fy.extractors.python_imports import (
    _repoint_python_package_imports,
    _repoint_python_sibling_imports,
)
from graph_fy.extractors.csharp import (
    _resolve_cross_file_csharp_imports,
    _resolve_csharp_type_references,
)

from graph_fy.extractors.base import _LANGUAGE_BUILTIN_GLOBALS, _file_stem, _make_id
from graph_fy.paths import disambiguate_ambiguous_candidates
from graph_fy.resolver_registry import run_language_resolvers

from graph_fy.extractors.resolution import (
    _augment_symbol_resolution_edges,
    _merge_decl_def_classes,
    _node_disambiguation_source_key,
    _disambiguate_colliding_node_ids,
    _resolve_cross_file_imports,
    _resolve_cross_file_java_imports,
    _resolve_go_type_references,
    _resolve_java_type_references,
    _resolve_php_type_references,
)


def run_resolution_phase(
    paths: list[Path],
    per_file: list[dict],
    root: Path,
    resolution_context_nodes: list[dict] | None = None,
    resolution_context_edges: list[dict] | None = None,
    file_node_id_fn=None,
) -> tuple[list[dict], list[dict]]:
    """Execute Phase 3: cross-file symbol, call, and framework route resolution."""
    if file_node_id_fn is None:
        from graph_fy.extractors.base import _file_node_id
        file_node_id_fn = _file_node_id

    _file_node_id = file_node_id_fn
    from graph_fy.extract import (
        _KOTLIN_IMPORT_TARGET_RESOLVER,
        _DISPATCH,
        _get_extractor,
    )
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    all_raw_calls: list[dict] = []
    for result in per_file:
        all_nodes.extend(result.get("nodes", []))
        all_edges.extend(result.get("edges", []))
        all_raw_calls.extend(result.get("raw_calls", []))
    # Function / method / class def ids for the cross-file indirect_call callable
    # guard. Built from the `_callable` node marker AFTER the id-remap / disambiguation
    # passes below (which rewrite node ids), so it can never go stale — see the
    # marker set in the per-file extractor. Populated just before the pass that uses it.
    callable_nids: set[str] = set()

    _augment_symbol_resolution_edges(
        paths, all_nodes, all_edges, root,
        resolution_context_nodes=resolution_context_nodes,
    )

    # Merge a header-declared class (and its methods) with its sibling-impl
    # definition into ONE node (C/C++/ObjC #1547/#1556). Runs BEFORE the id-remap
    # below: a header symbol and its impl counterpart share an id only while both
    # still carry the raw file-stem prefix; the per-file prefix remap then diverges
    # them (foo_h vs foo_cpp), so the collapse must happen first. Collapsing here
    # also means disambiguation sees one source_file per id and won't split them.
    _merge_decl_def_classes(all_nodes, all_edges)

    # Remap file node IDs from absolute-path-derived to the canonical
    # {parent_dir}_{stem} spec form so (a) graph.json edge endpoints are stable
    # across machines (#502) and (b) AST file nodes match the IDs semantic
    # subagents generate (#1033). Resolve before relativizing so paths passed in
    # relative form still anchor to the (resolved) root.
    id_remap: dict[str, str] = {}
    # A target OUTSIDE the scan root (an out-of-root ProjectReference/.sln/bash
    # `source`/#include/relative import) can't be made relative to root; leaving
    # it absolute leaked the scan path including the OS username into a
    # committed graph.json (#1899). Fall back to a walk-up relative form, or the
    # bare basename when that would still embed foreign path segments (a
    # far-away or cross-drive target). Shared below by both the target_file
    # remap loop (edges with no node of their own, #2243) and the node-level
    # relativization pass further down (#1899/#2195).
    def _portable_out_of_root_sf(p: Path) -> str:
        try:
            rel = os.path.relpath(str(p), str(root)).replace("\\", "/")
        except ValueError:
            return p.name  # different Windows drive: no relative path exists
        updepth = 0
        for seg in rel.split("/"):
            if seg == "..":
                updepth += 1
            else:
                break
        # More than a couple of walk-ups means the target lives well outside the
        # corpus; its ancestor dirs would embed foreign (possibly user-named)
        # segments, so collapse to the basename.
        return p.name if updepth > 3 else rel

    # Symbol node IDs embed the file stem as a prefix (_file_node_id of the path
    # the extractor saw). For a root-level file that stem picks up the absolute
    # parent directory name, so a symbol becomes <rootdir>_main_run while the
    # file node is correctly relativized to main and the skill.md spec wants
    # main_run -- splitting the symbol into AST/semantic ghosts (#1096). Relativize
    # the symbol prefix the same way, gated by source_file so two files sharing a
    # prefix can't cross-contaminate. Keyed by resolved path -> (old_pref, new_pref).
    # Each file maps from up to TWO old prefixes — the input-form prefix
    # _file_node_id(path) and the absolute-resolved-form prefix
    # _file_node_id(path.resolve()). Alias/workspace imports resolve specifiers
    # through .resolve(), so their edge targets are keyed off the ABSOLUTE form;
    # when inputs are relative the two forms differ and absolute-derived targets
    # would otherwise orphan (#1529). Stored as a list so the symbol-prefix remap
    # below can try both (identical forms collapse to one — a no-op).
    prefix_remap: dict[Path, list[tuple[str, str]]] = {}
    # Canonical stem plus every prefix form a file's symbol ids may appear
    # under, keyed by resolved path — consumed by the target_file-guided
    # barrel repoint below (#1983). Unlike prefix_remap this records ALL
    # in-root files, not just those whose prefix changed.
    stem_forms: dict[Path, tuple[str, list[str]]] = {}
    # Canonicalize edge-target files too, not just this batch's inputs (#2169).
    # On an incremental run `paths` is only the CHANGED files, so a changed
    # file's cross-file import/re-export edges keep absolute-path-derived
    # target ids the remap below never learns — they match no node in the
    # merged graph and silently dangle. The target_file stamp (set at edge
    # emit time) names each resolved target, so registering id_remap /
    # stem_forms for those in-root files as well lets the edge remap and the
    # target_file-guided repoint pass fix them exactly as on a full scan.
    remap_paths: list[Path] = list(paths)
    _remap_seen: set[Path] = set()
    for _p in paths:
        try:
            _remap_seen.add(_p.resolve())
        except (OSError, RuntimeError):
            pass
    for _e in all_edges:
        _tf = _e.get("target_file")
        if not _tf:
            continue
        _raw_tp = Path(_tf)
        try:
            _tp = _raw_tp.resolve()
        except (OSError, RuntimeError):
            continue
        if _tp in _remap_seen:
            # Already covered: either the target is in this batch (its input
            # form is the same form the extractors minted ids from, and the
            # per-path loop registers both that and the resolved form) or an
            # earlier stamped edge registered it. Re-appending it here would
            # re-run its per-path iteration AFTER later batch files and could
            # flip the last-writer of a colliding old-id key.
            continue
        _remap_seen.add(_tp)
        try:
            _tp.relative_to(root)
        except ValueError:
            # Out-of-root target: `_file_node_id` (used below for in-root
            # targets) needs a root-relative path, so it cannot help here.
            # No node stands for this target either (target_file-stamped
            # edges intentionally mint no stub node, #2195), so unlike an
            # out-of-root node the belt-and-braces pass below never learns
            # this id from anywhere — without registering it here the raw
            # scan-path slug survives in the edge forever (#2243). Give it
            # the same portable "ext_" id an out-of-root node would get; a
            # target that does not actually exist on disk stays dangling,
            # exactly as before.
            try:
                if _tp.is_file():
                    ext_new_id = _make_id("ext", _portable_out_of_root_sf(_tp))
                    id_remap[_make_id(str(_tp))] = ext_new_id
                    if _raw_tp != _tp:
                        id_remap[_make_id(str(_raw_tp))] = ext_new_id
                    # Bash entrypoint endpoints suffix the file-level id with
                    # "__entry" (script-invocation `calls` edges,
                    # extractors/bash.py); register the suffixed forms too so
                    # an out-of-root invoked script canonicalizes instead of
                    # keeping the absolute scan-path slug (#2243).
                    id_remap.setdefault(
                        _make_id(str(_tp)) + "__entry", ext_new_id + "__entry")
                    if _raw_tp != _tp:
                        id_remap.setdefault(
                            _make_id(str(_raw_tp)) + "__entry",
                            ext_new_id + "__entry")
            except OSError:
                pass
            continue
        try:
            if not _tp.is_file():
                # Speculatively-resolved target that doesn't exist (e.g. an
                # import of a not-yet-created sibling): keep its raw id
                # dangling, exactly as before, so no false canonical edge is
                # fabricated toward a nonexistent file.
                continue
        except OSError:
            continue
        remap_paths.append(_tp)
        # Also register the AS-STAMPED (unresolved) form. The edge target id
        # was minted from the stamped path exactly as written (e.g.
        # ``str(base / rel)`` for a Python relative import), which under a
        # symlinked root (macOS /tmp -> /private/tmp) or relative inputs
        # differs from the resolved form; the per-path loop below derives the
        # old ids from whichever Path it is given, so a missing form would
        # leave the edge target unmapped and dangling.
        if _raw_tp != _tp:
            _remap_seen.add(_raw_tp)
            remap_paths.append(_raw_tp)
    for path in remap_paths:
        old_id = _make_id(str(path))
        try:
            rel = path.relative_to(root)
        except ValueError:
            try:
                rel = path.resolve().relative_to(root)
            except ValueError:
                continue
        new_id = _file_node_id(rel)
        if old_id != new_id:
            id_remap[old_id] = new_id
        # Also register the absolute-resolved form of the file-level id so
        # alias/workspace import targets (resolved via .resolve()) remap to
        # canonical instead of orphaning (#1529).
        old_id_abs = _make_id(str(path.resolve()))
        if old_id_abs != new_id:
            id_remap[old_id_abs] = new_id
        old_prefs: list[tuple[str, str]] = []
        old_pref = _file_node_id(path)
        if old_pref != new_id:
            old_prefs.append((old_pref, new_id))
        old_pref_abs = _file_node_id(path.resolve())
        if old_pref_abs != new_id and old_pref_abs != old_pref:
            old_prefs.append((old_pref_abs, new_id))
        # Bash entrypoint node ids append "__entry" to the file-level id
        # (extractors/bash.py), so a script-invocation edge endpoint minted
        # from an out-of-batch target path keeps a suffixed absolute-derived
        # id neither the plain id_remap key above nor the source_file-gated
        # prefix pass below (nodes only) can reach on an incremental run
        # (#2243). Register the suffixed forms, preserving the extension tail
        # exactly as the prefix remap yields for in-batch entry nodes
        # (`c.sh` -> `c_sh__entry`): new prefix + the tail of the minted id.
        for _old, _pref in ((old_id, old_pref), (old_id_abs, old_pref_abs)):
            if not _old.startswith(_pref):
                continue
            _entry_new = new_id + _old[len(_pref):] + "__entry"
            _entry_old = _old + "__entry"
            if _entry_old != _entry_new:
                id_remap.setdefault(_entry_old, _entry_new)
        if old_prefs:
            prefix_remap[path.resolve()] = old_prefs
        # Absolute form first: it is the longest, so prefix decomposition can
        # try forms in order without a shorter form shadowing it.
        stem_forms[path.resolve()] = (
            new_id, [old_pref_abs, old_pref, new_id]
        )
    if id_remap:
        for n in all_nodes:
            if n.get("id") in id_remap:
                n["id"] = id_remap[n["id"]]
        for e in all_edges:
            if e.get("source") in id_remap:
                e["source"] = id_remap[e["source"]]
            if e.get("target") in id_remap:
                e["target"] = id_remap[e["target"]]
        # raw_calls carry caller_nid, consumed by the cross-file call pass far
        # below (after this remap). A module-TOP-LEVEL indirect_call/callback
        # records the FILE-level id as its caller — the id minted from the
        # absolute input path that this very remap just rewrote on the file
        # node. Without rewriting the raw_calls too, the emitted
        # indirect_call edge keeps the machine-specific absolute-derived
        # source and matches no node in the graph (#2231). Mirrors the
        # sym_remap raw_calls rewrite in the prefix pass below.
        for rc in all_raw_calls:
            cn = rc.get("caller_nid")
            if cn in id_remap:
                rc["caller_nid"] = id_remap[cn]
        # swift_extensions[].nid is the same kind of id carrier as caller_nid
        # above (cache.py remaps both), consumed by _merge_swift_extensions far
        # below. Left stale it matches no node, so whether the extension merge
        # runs at all depends on the FORM of the paths handed to extract() —
        # relative input already yields the post-remap slug, absolute input does
        # not (#2538). Remap it here so both agree.
        for result in per_file:
            for ext in result.get("swift_extensions", []) or []:
                en = ext.get("nid")
                if en in id_remap:
                    ext["nid"] = id_remap[en]
        # objc_field_types["tables"] is keyed BY class node id - the one bucket
        # the #1529 rewrites missed (#3150).
        _remap_objc_field_tables(per_file, id_remap)
    if prefix_remap:
        sym_remap: dict[str, str] = {}
        edge_alias_candidates: dict[str, set[str]] = {}
        for n in all_nodes:
            sf = n.get("source_file")
            if not sf:
                continue
            # Package nodes carry a canonical name-keyed id (pkg_<name>) that must
            # stay identical across every manifest that references the package, so
            # they are exempt from the file-stem prefix remap (#1377), like the
            # type=module anchors (#1327).
            if n.get("type") == "package":
                continue
            try:
                entry = prefix_remap.get(Path(sf).resolve())
            except Exception:
                continue
            if entry is None:
                continue
            nid = n.get("id", "")
            # Try both the input-form and absolute-form prefixes for this file
            # (#1529). source_file gating above already prevents cross-file
            # contamination, so the first matching prefix wins.
            canonical_nid: str | None = None
            for old_pref, new_pref in entry:
                if nid.startswith(old_pref + "_"):
                    canonical_nid = new_pref + nid[len(old_pref):]
                    if canonical_nid != nid:
                        sym_remap[nid] = canonical_nid
                    break
                if nid.startswith(new_pref + "_"):
                    canonical_nid = nid
                    break
            if canonical_nid is None:
                continue
            # Named alias imports/re-exports can retain an absolute-prefixed target
            # when the symbol node is already canonical. Record every old form
            # so a redundant import edge or dangling re-export target can be fixed
            # without globally reinterpreting an id that another real node may own.
            for old_pref, new_pref in entry:
                if not canonical_nid.startswith(new_pref + "_"):
                    continue
                old_nid = old_pref + canonical_nid[len(new_pref):]
                if old_nid != canonical_nid:
                    edge_alias_candidates.setdefault(old_nid, set()).add(canonical_nid)
        if sym_remap:
            for n in all_nodes:
                if n.get("id") in sym_remap:
                    n["id"] = sym_remap[n["id"]]
            for e in all_edges:
                if e.get("source") in sym_remap:
                    e["source"] = sym_remap[e["source"]]
                if e.get("target") in sym_remap:
                    e["target"] = sym_remap[e["target"]]
            # raw_calls carry caller_nid (a symbol id) consumed by the cross-file
            # call pass below, after this remap — rewrite it too or those edges
            # would dangle on their (stale) source.
            for rc in all_raw_calls:
                cn = rc.get("caller_nid")
                if cn in sym_remap:
                    rc["caller_nid"] = sym_remap[cn]
            # Same for swift_extensions[].nid (see the id_remap pass above).
            for result in per_file:
                for ext in result.get("swift_extensions", []) or []:
                    en = ext.get("nid")
                    if en in sym_remap:
                        ext["nid"] = sym_remap[en]
            _remap_objc_field_tables(per_file, sym_remap)
        if edge_alias_candidates:
            def _edge_key(edge: dict) -> str:
                # target_file is a transient stamp (#1814/#1983); exclude it
                # from twin identity or an alias edge (stamped) never matches
                # the canonical twin the shared resolver emits (unstamped).
                return json.dumps(
                    {k: v for k, v in edge.items() if k != "target_file"},
                    sort_keys=True, separators=(",", ":"), default=str,
                )
            edge_key_counts = Counter(_edge_key(edge) for edge in all_edges)
            owned_node_ids = {node.get("id") for node in all_nodes}
            deduped_edges: list[dict] = []
            for edge in all_edges:
                if edge.get("relation") == "re_exports":
                    candidates = edge_alias_candidates.get(edge.get("target", ""), set())
                    if len(candidates) == 1 and edge.get("target") not in owned_node_ids:
                        edge["target"] = next(iter(candidates))
                    deduped_edges.append(edge)
                    continue
                candidates = (
                    edge_alias_candidates.get(edge.get("target", ""), set())
                    if edge.get("relation") == "imports"
                    else set()
                )
                if len(candidates) == 1:
                    candidate = next(iter(candidates))
                    twin_key = _edge_key({**edge, "target": candidate})
                    # Drop only when the shared resolver emitted the exact
                    # canonical twin. Otherwise the target may be a legitimate
                    # owned node id.
                    if edge_key_counts[twin_key]:
                        if edge.get("target") in owned_node_ids:
                            edge_key_counts[twin_key] -= 1
                        continue
                deduped_edges.append(edge)
            all_edges[:] = deduped_edges

    # Repoint symbol-level alias edges that resolve THROUGH a barrel (#1983
    # follow-up). The candidates rewrite above learns old→canonical forms only
    # from symbols a file DEFINES; a barrel defines nothing, so a re-export or
    # named import that resolves to one keeps an absolute-prefixed, dangling
    # target no rewrite ever learns. Use the target_file stamp to decompose
    # such a target into (canonical file stem, symbol), follow the barrel's own
    # already-canonical re_exports edge to the defining symbol — iterating so
    # multi-hop barrel chains resolve one hop per pass — and, when no chain
    # leads to a real node, canonicalize the prefix anyway so a checkout path
    # never survives in an edge target.
    if stem_forms:
        owned_ids = {n.get("id") for n in all_nodes}

        def _decompose(target: str, tf: str) -> "tuple[str, str] | None":
            try:
                forms = stem_forms.get(Path(tf).resolve())
            except (OSError, RuntimeError):
                return None
            if not forms:
                return None
            canonical, prefixes = forms
            for pref in prefixes:
                if pref and target.startswith(pref + "_"):
                    return canonical, target[len(pref) + 1:]
            return None

        # (canonical file id, symbol) → set of owned targets, learned from
        # symbol-level re_exports edges that already point at a real node. A set
        # (not last-write-wins): when a barrel re-exports the SAME local name
        # from two different modules (`export {x} from './a'; export {x as y}
        # from './b'` — both key on local name `x`), the key becomes ambiguous
        # and must NOT be guessed, or we fabricate a wrong edge. Ambiguous keys
        # resolve to None so the edge falls to the dangling-canonical fallback
        # (dropped at build), while the shared resolver's correct edge survives.
        chain: dict[tuple[str, str], set] = {}

        def _resolve1(key) -> "str | None":
            targets = chain.get(key)
            return next(iter(targets)) if targets and len(targets) == 1 else None

        def _learn(e: dict) -> None:
            tf = e.get("target_file")
            if not tf or e.get("target") not in owned_ids:
                return
            dec = _decompose(e.get("target", ""), tf)
            if dec is not None:
                chain.setdefault((e.get("source"), dec[1]), set()).add(e["target"])

        for e in all_edges:
            if e.get("relation") == "re_exports":
                _learn(e)

        pending = [
            e for e in all_edges
            if e.get("relation") in ("re_exports", "imports")
            and e.get("target_file")
            and e.get("target") not in owned_ids
        ]
        for _ in range(8):  # bounded: each pass resolves one barrel hop
            progressed = False
            still: list[dict] = []
            for e in pending:
                dec = _decompose(e.get("target", ""), e["target_file"])
                resolved_target = _resolve1((dec[0], dec[1])) if dec else None
                if resolved_target is None:
                    still.append(e)
                    continue
                e["target"] = resolved_target
                if e.get("relation") == "re_exports":
                    # This barrel's edge now feeds the next hop. Learn it
                    # directly — decomposing the repointed target against this
                    # edge's own target_file would fail, since the target now
                    # carries the DEFINING file's stem, not the barrel's.
                    chain.setdefault((e.get("source"), dec[1]), set()).add(resolved_target)
                progressed = True
            pending = still
            if not progressed:
                break
        for e in pending:
            dec = _decompose(e.get("target", ""), e["target_file"])
            if dec is not None:
                e["target"] = f"{dec[0]}_{dec[1]}"

    # Repoint Python absolute imports onto the real file nodes under a nested
    # (src/) package root before the resolver/import-evidence passes run, so the
    # graph is identical regardless of scan root (#2072).
    _repoint_python_package_imports(paths, all_nodes, all_edges, root)
    _repoint_python_sibling_imports(paths, all_nodes, all_edges, root)
    _merge_swift_extensions(per_file, all_nodes, all_edges)
    _merge_csharp_partial_class_nodes(per_file, all_nodes, all_edges, paths, root)
    _disambiguate_colliding_node_ids(all_nodes, all_edges, all_raw_calls, root)
    _canonicalize_csharp_namespace_nodes(all_nodes, all_edges)
    # PHP namespace/use disambiguation must run BEFORE the unique-stub rewire:
    # the false merge (#1923) happens inside the rewire when a bare-name stub
    # matches a unique internal class from a different namespace.
    _php_exts = {".php", ".phtml", ".php3", ".php4", ".php5", ".php7", ".phps"}
    _php_sel = [
        (r, p) for r, p in zip(per_file, paths)
        if p.suffix.lower() in _php_exts and not p.name.lower().endswith(".blade.php")
    ]
    if _php_sel:
        try:
            _resolve_php_type_references(
                [r for r, _ in _php_sel], [p for _, p in _php_sel], all_nodes, all_edges
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("PHP type-reference resolution failed, skipping: %s", exc)
    # Java package/import disambiguation must likewise run BEFORE the rewire
    # (#2504): an EXTERNAL import (`org.springframework.stereotype.Component`)
    # leaves a bare `Component` stub that the rewire would collapse onto the only
    # internal class with that simple name, manufacturing a false hub. Parking
    # such references on an FQN-labeled stub first prevents the merge, and
    # import-exact resolution of internal references (#1318/#1744) still applies.
    _java_sel = [(r, p) for r, p in zip(per_file, paths) if p.suffix == ".java"]
    if _java_sel:
        try:
            _resolve_java_type_references(
                [r for r, _ in _java_sel], [p for _, p in _java_sel], all_nodes, all_edges
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Java type-reference resolution failed, skipping: %s", exc)
    # Resolve internal Go pkg.Type references exactly and park external ones
    # before the generic bare-label stub rewire can manufacture a collision.
    _go_sel = [(r, p) for r, p in zip(per_file, paths) if p.suffix == ".go"]
    if _go_sel:
        try:
            _resolve_go_type_references(
                [r for r, _ in _go_sel], [p for _, p in _go_sel],
                all_nodes, all_edges, root,
                resolution_context_nodes, resolution_context_edges,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Go type-reference resolution failed, skipping: %s", exc
            )
    # Cross-file Python import resolution and type-reference repointing (#3252)
    py_paths = [p for p in paths if p.suffix == ".py"]
    if py_paths:
        py_results = [r for r, p in zip(per_file, paths) if p.suffix == ".py"]
        try:
            cross_file_edges = _resolve_cross_file_imports(py_results, py_paths, all_nodes, all_edges)
            all_edges.extend(cross_file_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Cross-file import resolution failed, skipping: %s", exc)
    _rewire_unique_stub_nodes(all_nodes, all_edges, root=root)

    # Cross-file Java import resolution
    java_paths = [p for p in paths if p.suffix == ".java"]
    if java_paths:
        java_results = [r for r, p in zip(per_file, paths) if p.suffix == ".java"]
        try:
            all_edges.extend(_resolve_cross_file_java_imports(java_results, java_paths))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Java cross-file import resolution failed, skipping: %s", exc)

    # Cross-file C# type-reference resolution: re-point dangling inherits/implements/
    # references edges left on shadow stubs, disambiguating same-named types by the
    # referencing file's `using` directives + enclosing namespace (mirrors Java #1318).
    _DOTNET_TYPE_EXTS = {".cs", ".razor", ".cshtml"}
    cs_paths = [p for p in paths if p.suffix.lower() in _DOTNET_TYPE_EXTS]
    if cs_paths:
        cs_results = [r for r, p in zip(per_file, paths) if p.suffix.lower() in _DOTNET_TYPE_EXTS]
        try:
            _resolve_csharp_type_references(cs_results, cs_paths, all_nodes, all_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("C# type-reference resolution failed, skipping: %s", exc)
        try:
            _resolve_cross_file_csharp_imports(cs_results, cs_paths, all_nodes, all_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("C# cross-file import resolution failed, skipping: %s", exc)

    # Cross-file Bash source-backed call resolution: a call to a function defined
    # in a file this one `source`s is left unresolved by the per-file extractor
    # (it only links calls to same-file functions, #2141). Match each bash raw_call
    # against functions in the sourced files and emit the calls edge — scoped to
    # the source relationship, so a call to an external command never binds to a
    # same-named function in an unsourced file. Runs after the id-remap passes
    # above so caller_nids and function node ids are final; dedups the source
    # edge the extractor already emitted via existing_edges.
    # Selecting by filename suffix alone missed extensionless scripts:
    # _SHEBANG_DISPATCH routes a `#!/usr/bin/env bash` file with no extension to
    # extract_bash, so its functions get indexed, but a suffix-only filter left it
    # out of this pass and calls into it never resolved (#2171). Select by shape
    # too — the bash extractor tags every node it emits with
    # metadata.language == "bash" — while keeping the suffix check so an empty
    # .sh file (no nodes to inspect) still participates.
    def _looks_like_bash(result: object) -> bool:
        if not isinstance(result, dict):
            return False
        nodes = result.get("nodes")
        if not isinstance(nodes, list):
            return False
        for n in nodes:
            if not isinstance(n, dict):
                continue
            md = n.get("metadata")
            if isinstance(md, dict) and md.get("language") == "bash":
                return True
        return False

    sh_pairs = [
        (r, p) for r, p in zip(per_file, paths)
        if p.suffix in (".sh", ".bash") or _looks_like_bash(r)
    ]
    if sh_pairs:
        sh_results = [r for r, _ in sh_pairs]
        sh_paths = [p for _, p in sh_pairs]
        try:
            all_edges.extend(
                resolve_bash_source_edges(sh_results, sh_paths, root, existing_edges=all_edges)
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Bash cross-file call resolution failed, skipping: %s", exc)

    # Cross-file call resolution for all languages
    # Each extractor saved unresolved calls in raw_calls. Now that we have all
    # nodes from all files, resolve any callee that exists in another file.
    # Build name → ALL matching node IDs so we can skip ambiguous common names
    # (e.g. "log", "execute", "find") that appear in multiple files — resolving
    # those inflates god_nodes ranking with spurious cross-file edges.
    # Build label -> node_id index for cross-file call resolution.
    # Skip rationale nodes (their labels are docstring text, not callable
    # identifiers, and they were polluting matches for short names — #563).
    global_label_to_nids: dict[str, list[str]] = {}      # exact-case (all languages)
    global_label_to_nids_ci: dict[str, list[str]] = {}   # case-INSENSITIVE-language nodes
    # #2406: on an incremental rebuild only the CHANGED files are parsed, so
    # `all_nodes` alone cannot see a callee that lives in an unchanged file and
    # every changed->unchanged DIRECT call silently vanished (while the file-level
    # `imports` edge survived, because the JS/Python symbol-resolution pass
    # reads the import TARGET off disk instead of off the node list). Extend the
    # resolution indexes — and ONLY the indexes — with the caller-supplied
    # unchanged-corpus nodes. Fresh nodes win on id collision, nothing is
    # appended to `all_nodes`, and raw_calls still come solely from `paths`, so
    # the emitted edges remain sourced by the re-extracted files.
    #
    # Scope: this list feeds the shared direct-call loop below, the
    # indirect_call callable guard (#2438, via the persisted `_callable` /
    # `_callable_class` markers), and — together with resolution_context_edges —
    # the member-call resolvers run by run_language_resolvers (#2437).
    resolution_nodes = all_nodes
    if resolution_context_nodes:
        _fresh_ids = {n["id"] for n in all_nodes}
        resolution_nodes = all_nodes + [
            n for n in resolution_context_nodes
            if n.get("id") and n["id"] not in _fresh_ids
        ]
    for n in resolution_nodes:
        if n.get("file_type") == "rationale" or n.get("type") == "namespace":
            continue
        raw = n.get("label", "")
        normalised = raw.strip("()").lstrip(".")
        if normalised:
            # Case is semantic in most languages, so index (and match, below) by exact
            # case — folding collapses `Path` (class) into `PATH` (env var) and makes a
            # single shell variable the #1 god-node (#1581). Only case-insensitive
            # languages (PHP/SQL/Nim) also get a folded key for legitimate fold-matching.
            global_label_to_nids.setdefault(normalised, []).append(n["id"])
            if _lang_is_case_insensitive(n.get("source_file")):
                global_label_to_nids_ci.setdefault(normalised.lower(), []).append(n["id"])

    # Callable-def ids for the indirect_call callable guard, read from the `_callable`
    # marker on the FINAL (post-remap) nodes — so a callback resolves only to a real
    # function/method/class, never a same-named data symbol, and the guard never goes
    # stale when node ids were relativized/disambiguated above (#1566). Read from
    # `resolution_nodes`, not `all_nodes` (#2438): an unchanged callee's context node
    # carries the marker persisted in graph.json, so an incremental rebuild keeps
    # resolving callbacks into unchanged files while data symbols stay excluded.
    callable_nids = {n["id"] for n in resolution_nodes if n.get("_callable")}
    # Class defs are callable only via their constructor; they are frequently passed
    # as descriptive values (`select(Model)`, exception tuples), not invoked. Exclude
    # them from the indirect_call guard below to avoid false edges (#2137).
    class_nids = {n["id"] for n in resolution_nodes if n.get("_callable_class")}

    # Kotlin import targets (#2526): rewrite each `imports` edge from the bare
    # last-segment id to the node its written FQN names, via the per-file
    # package declarations. Runs HERE — after the id-remap/disambiguation passes
    # (ids are final) but before the import-evidence index just below reads the
    # edges — so genuine imported calls get promoted INFERRED -> EXTRACTED. The
    # tail registry run (run_language_resolvers below) would be too late.
    run_language_resolvers(
        paths, per_file, all_nodes, all_edges,
        resolvers=[_KOTLIN_IMPORT_TARGET_RESOLVER],
    )

    # Build evidence index from import edges so cross-file calls backed by an
    # explicit import statement can be promoted from INFERRED to EXTRACTED.
    # Direct symbol imports (`import { foo }` / `const { foo } = require()`) are
    # the strongest evidence — caller's file_id has an `imports` edge directly to
    # the callee's symbol id. Module imports (`imports_from`) are weaker but still
    # confirm the caller pulled in the callee's source file.
    file_to_symbol_imports: dict[str, set[str]] = {}
    file_to_module_imports: dict[str, set[str]] = {}
    for e in all_edges:
        if e.get("relation") == "imports":
            file_to_symbol_imports.setdefault(e["source"], set()).add(e["target"])
        elif e.get("relation") == "imports_from":
            file_to_module_imports.setdefault(e["source"], set()).add(e["target"])

    # Map each node back to its containing file node id so we can ask
    # "did the caller's file import the callee's file?"
    # A node and its file node share the exact same ``source_file`` string, and a
    # file node is the one whose label is the basename (``add_node(file_nid,
    # path.name)``). Resolving file membership by that shared string is robust
    # against the path-resolution/symlink mismatch that makes
    # ``relative_to(root.resolve())`` throw and fall back to a non-matching
    # absolute-derived id — which would spuriously fail import evidence and (with
    # the #1659 JS/TS gate below) drop a legitimately-imported call.
    sf_to_file_nid: dict[str, str] = {}
    for n in resolution_nodes:
        sf = n.get("source_file")
        if sf and n.get("label") == Path(str(sf)).name:
            sf_to_file_nid.setdefault(str(sf), n["id"])
    nid_to_file_nid: dict[str, str] = {}
    # nid -> raw source_file string, for the ambiguous-name tie-breakers below
    # (test/non-test classification + path proximity). Kept separate from the
    # file-node-id map because tie-breaking compares the actual file paths.
    nid_to_source_file: dict[str, str] = {}
    for n in resolution_nodes:
        sf = n.get("source_file")
        if not sf:
            continue
        nid_to_source_file[n["id"]] = str(sf)
        fnid = sf_to_file_nid.get(str(sf))
        if fnid is not None:
            nid_to_file_nid[n["id"]] = fnid
            continue
        # Fallback (no file node found for this source_file): derive it the old
        # way from the relativized path.
        sf_path = Path(sf)
        try:
            sf_rel = sf_path.relative_to(root) if sf_path.is_absolute() else sf_path
        except ValueError:
            sf_rel = sf_path
        nid_to_file_nid[n["id"]] = _file_node_id(sf_rel)

    existing_pairs = {(e["source"], e["target"]) for e in all_edges}
    # Call-like pairs only, for the indirect_call dedup: an `imports` edge from a
    # file to the symbol it imports is EXPECTED and must not suppress an
    # indirect_call to that same symbol (JS/TS named imports create such an edge).
    call_like_pairs = {
        (e["source"], e["target"]) for e in all_edges
        if e.get("relation") in ("calls", "indirect_call")
    }
    # JS/TS/JSX modules have no implicit cross-module scope: a call into another
    # file is real ONLY if the caller imported it. So a cross-file call from one
    # of these files with no import evidence is gated below (#1659).
    _JS_TS_CALL_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
    _go_module_cache: dict[Path, str | None] = {}
    for rc in all_raw_calls:
        callee = rc.get("callee", "")
        if not callee:
            continue
        if callee in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        # Skip member-call callees: obj.log() → "log" has no import evidence
        # and collides with any top-level function named "log" in the corpus.
        if rc.get("is_member_call"):
            continue
        # Skip Ruby include/extend/prepend mixin markers: they carry a module
        # name as `callee` but are not calls — the Ruby resolver turns them into
        # `mixes_in` edges. Letting the shared pass emit a `calls` edge here would
        # both mislabel the relation and block the mixes_in emit as a dup (#1668).
        if rc.get("is_mixin"):
            continue
        # Bash calls are resolved only by resolve_bash_source_edges (run above),
        # which scopes resolution to the files a script actually `source`s. The
        # global name match here would bind a bash call to any same-named function
        # in an unsourced file (an INFERRED phantom edge) and would resolve calls
        # to external commands that merely share a name with a function elsewhere
        # in the corpus — exactly what #2141 must not do.
        if rc.get("language") == "bash":
            continue
        if rc.get("solidity_call"):
            continue
        # A Markdown code span names a symbol, it does not call one. The
        # markdown_mentions resolver turns it into a `references` edge with the
        # cited file as evidence; a global name match here would mint a `calls`
        # edge from a heading to whatever shares the name.
        if rc.get("language") == "markdown":
            continue
        # A Go predeclared function is never a cross-file call: the extractor
        # already drops bare `append(s, x)` (extractors/go.py), so this is the
        # backstop for Go raw_calls minted on any other path. Language-gated
        # rather than folded into _LANGUAGE_BUILTIN_GLOBALS because `new`,
        # `close` and `delete` are ordinary method names elsewhere (#2296).
        if rc.get("language") == "go" and callee in _GO_PREDECLARED_FUNCS:
            continue
        # Exact-case match first (case is semantic). Fold only when the CALLING
        # file's language is case-insensitive, and only against the folded index of
        # case-insensitive-language definitions — so a Python `Path()` call can never
        # resolve to a shell `PATH` node (#1581).
        candidates = global_label_to_nids.get(callee, [])
        if not candidates and _lang_is_case_insensitive(rc.get("source_file")):
            candidates = global_label_to_nids_ci.get(callee.lower(), [])
        if not candidates:
            continue
        # Cross-language guard: never bind a call to a definition in a different
        # language family. Name-only matching was resolving a TSX callback passed
        # by name to a same-named Kotlin method in the Android half of the repo
        # (and a Python call to a Kotlin fun) — phantom edges the extraction spec
        # explicitly forbids. Candidates whose family is unknown (no source_file,
        # non-code nodes) are kept, preserving the previous permissive behavior;
        # real interop pairs (Kotlin↔Java, C↔C++↔ObjC, JS↔TS) share a family and
        # still resolve.
        caller_family = _lang_family(rc.get("source_file"))
        if caller_family is not None:
            candidates = [
                c for c in candidates
                if (candidate_family := _lang_family(nid_to_source_file.get(c))) is None
                or candidate_family == caller_family
            ]
            if not candidates:
                continue
        # Imported Go selectors carry exact package evidence. External package
        # calls yield no internal candidate instead of binding by bare name.
        go_exact_import = False
        if rc.get("language") == "go" and rc.get("import_path"):
            import_path = str(rc["import_path"])
            candidates = [
                candidate for candidate in candidates
                if _go_import_path_for_file(
                    nid_to_source_file.get(candidate, ""), root, _go_module_cache
                ) == import_path
            ]
            if not candidates:
                continue
            go_exact_import = True
        caller = rc["caller_nid"]
        # Resolve the caller's file via the raw_call's own source_file string,
        # which is stable regardless of any caller_nid remap. An indirect
        # callback's caller_nid is the file node, whose id may have been
        # relativized after the raw_call was recorded, so a caller_nid lookup can
        # miss and (with the #1659 gate) drop a legitimately-imported callback.
        caller_file_nid = (
            sf_to_file_nid.get(str(rc.get("source_file", "")))
            or nid_to_file_nid.get(caller)
        )
        imported_symbols = file_to_symbol_imports.get(caller_file_nid, set())
        imported_modules = file_to_module_imports.get(caller_file_nid, set())

        def _has_import_evidence(candidate_id: str) -> bool:
            # Direct symbol import (`import { foo }`) is the strongest evidence:
            # the caller's file has an `imports` edge straight to this symbol.
            # A module import (`import './helper.js'`) confirms the caller pulled
            # in the file the candidate lives in.
            candidate_file_nid = nid_to_file_nid.get(candidate_id)
            return (
                candidate_id in imported_symbols
                or (candidate_file_nid is not None and candidate_file_nid in imported_modules)
            )

        if len(candidates) == 1:
            tgt = candidates[0]
            has_import_evidence = go_exact_import or _has_import_evidence(tgt)
        else:
            # Ambiguous name (defined in 2+ files). Don't bail outright (#1219):
            # if the caller has explicit import evidence pointing at exactly one
            # of the candidates, that named import disambiguates unambiguously.
            # Prefer direct symbol-import matches; fall back to module-import
            # matches only when they too collapse to a single target. Without a
            # unique evidence-backed pick we skip, preserving the #543 guard
            # against over-connecting common short names (log, execute, find).
            symbol_matches = [c for c in candidates if c in imported_symbols]
            if len(symbol_matches) == 1:
                tgt = symbol_matches[0]
                has_import_evidence = True
            else:
                module_matches = [
                    c for c in candidates
                    if (cf := nid_to_file_nid.get(c)) is not None and cf in imported_modules
                ]
                if len(module_matches) == 1:
                    tgt = module_matches[0]
                    has_import_evidence = True
                else:
                    # No unique import evidence. Instead of dropping the edge
                    # outright (which let a single same-named test mock erase the
                    # real call graph, #1553), apply the shared god-node
                    # tie-breakers (non-test preference, then path proximity).
                    # Resolve only if exactly one candidate survives; otherwise
                    # the #543/#1219 guard still holds and we skip.
                    tgt = disambiguate_ambiguous_candidates(
                        candidates,
                        {c: nid_to_source_file.get(c, "") for c in candidates},
                        rc.get("source_file", ""),
                    )
                    if tgt is None:
                        continue
                    has_import_evidence = False
        # A source-less candidate is an unresolved external stub, not a
        # project-defined callable. It can remain a target of type/reference
        # edges, but a name-only call must not turn it into a cross-file calls
        # hub (#3156). Source-backed project definitions continue normally.
        if not nid_to_source_file.get(tgt):
            continue
        if rc.get("indirect"):
            # Cross-file indirect dispatch: a callback passed BY NAME
            # (`from .h import fn; pool.submit(fn)`, or listed in a dispatch
            # table). Resolved through the same single-definition / import-evidence
            # candidate logic as a direct call, but emitted as a distinct INFERRED
            # `indirect_call` and ONLY when the target is a real callable def —
            # never a same-named data symbol. Stays INFERRED even with import
            # evidence: the name is referenced as a value here, not invoked. Dedup
            # is call-aware (an existing direct `calls` edge pre-empts it; a benign
            # `imports` edge to the same symbol does NOT suppress it).
            if tgt != caller and (caller, tgt) not in call_like_pairs and tgt in callable_nids and tgt not in class_nids:
                call_like_pairs.add((caller, tgt))
                all_edges.append({
                    "source": caller,
                    "target": tgt,
                    "relation": "indirect_call",
                    "context": rc.get("context", "argument"),
                    "confidence": "INFERRED",
                    # 0.85, not 0.8: the rubric in references/extraction-spec.md
                    # is a discrete set {0.55, 0.65, 0.75, 0.85, 0.95} and 0.8 is
                    # not in it. Same tier, same meaning ("strong inference"),
                    # now a value the documented scale actually contains (#2813).
                    "confidence_score": 0.85,
                    "source_file": rc.get("source_file", ""),
                    "source_location": rc.get("source_location"),
                    "weight": 1.0,
                })
            continue
        # #1659: a JS/TS DIRECT call with no import evidence is almost always an
        # unrelated same-named export in a package that was never imported — a
        # phantom cross-package edge (a 14-package monorepo had `platform` and
        # `sidecar` shown as depending on `registry-protocol` purely because it
        # exported generically-named symbols). JS/TS modules have no implicit
        # cross-module scope, so leave it unresolved rather than binding by name
        # alone. Other languages keep the #1553 single-candidate resolution:
        # C/C++ headers, Ruby autoload, and same-package implicit scope
        # legitimately call across files without an explicit import. Scoped to
        # direct calls: the indirect_call path above is already conservative
        # (INFERRED, callable-target-gated) and independent of import evidence.
        if not has_import_evidence and str(rc.get("source_file", "")).endswith(_JS_TS_CALL_SUFFIXES):
            continue
        if tgt != caller and (caller, tgt) not in existing_pairs:
            existing_pairs.add((caller, tgt))
            # Promote to EXTRACTED when there's a direct import edge from the
            # caller's file pointing at either the callee symbol itself or the
            # file the callee lives in.
            if has_import_evidence:
                confidence = "EXTRACTED"
                confidence_score = 1.0
            else:
                confidence = "INFERRED"
                # 0.85 rather than 0.8 — the rubric's INFERRED set is discrete
                # and does not contain 0.8 (#2813).
                confidence_score = 0.85
            all_edges.append({
                "source": caller,
                "target": tgt,
                "relation": "calls",
                "context": "call",
                "confidence": confidence,
                "confidence_score": confidence_score,
                "source_file": rc.get("source_file", ""),
                "source_location": rc.get("source_location"),
                "weight": 1.0,
            })

    # Cross-file, language-specific member-call resolution. Runs after the shared
    # call pass so node ids/caller_nids are final; each pass is additive (only the
    # receiver-typed/qualified calls the shared pass skipped) with its own
    # single-definition god-node guard. Registered in graph_fy.resolver_registry so
    # a new language plugs in without editing this body (#1356 Swift, #1446 Python).
    #
    # #2437: on an incremental rebuild the resolvers must also see the unchanged
    # corpus — its nodes (types/methods, from resolution_nodes above) and its
    # persisted contains/method edges (resolution_context_edges) — or the
    # single-definition guards bail on every changed->unchanged member call. Run
    # them over SCRATCH lists that include the context, then keep only the fresh
    # results: raw_calls come solely from `paths`, so nothing sourced by an
    # unchanged file is ever emitted, and the ambiguity guards count the same
    # candidates a full build would (the context is the whole unchanged corpus).
    if resolution_context_nodes or resolution_context_edges:
        _rl_nodes = list(resolution_nodes)
        _rl_edges = all_edges + list(resolution_context_edges or [])
        _n0, _e0 = len(_rl_nodes), len(_rl_edges)
        run_language_resolvers(paths, per_file, _rl_nodes, _rl_edges)
        all_nodes.extend(_rl_nodes[_n0:])
        all_edges.extend(_rl_edges[_e0:])
    else:
        run_language_resolvers(paths, per_file, all_nodes, all_edges)

    # Cross-format dependency resolution (Terraform, Helm, Kubernetes, AWS, Docker)
    try:
        from graph_fy.cross_resolve import resolve_cross_format_dependencies
        resolve_cross_format_dependencies(all_nodes, all_edges)
    except Exception:
        pass

    # Cross-language FFI resolution (Rust <-> C/C++, PyO3, pybind11, wasm-bindgen) (#25)
    try:
        from graph_fy.ffi_resolve import resolve_ffi_boundaries
        resolve_ffi_boundaries(all_nodes, all_edges, root=root)
    except Exception:
        pass

    # Cross-layer configuration & environment variable lineage resolution (#28)
    try:
        from graph_fy.resolvers.config_lineage import resolve_config_lineage
        resolve_config_lineage(all_nodes, all_edges, root=root)
    except Exception:
        pass

    for e in all_edges:
        e.pop("_rust_pub_use", None)
        e.pop("_rust_use_name", None)
    for n in all_nodes:
        n.pop("_rust_use_roots", None)

    return all_nodes, all_edges
