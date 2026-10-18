"""resolution facade re-exporting modular language resolvers."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

# Re-export all resolvers from graph_fy.resolvers
from graph_fy.resolvers.c_cpp import (
    _DECLDEF_HEADER_SUFFIXES,
    _DECLDEF_IMPL_SUFFIXES,
    _decldef_class_stem,
    _merge_decl_def_classes,
    _resolve_c_include_path,
    _source_stem,
)
from graph_fy.resolvers.common import (
    _cached_realpath,
    _cached_source_key,
    _disambiguate_colliding_node_ids,
    _get_process_cwd,
    _is_type_like_definition,
    _node_disambiguation_source_key,
    _resolve_cached,
    _source_key,
)
from graph_fy.resolvers.go import (
    _go_import_path_for_file,
    _resolve_go_type_references,
)
from graph_fy.resolvers.javascript import (
    _EXPORT_CONDITION_PRIORITY,
    _JS_INDEX_FILES,
    _JS_PRIMITIVE_TYPES,
    _JS_RESOLVE_EXTS,
    _PACKAGE_IMPORTS_CACHE,
    _TSCONFIG_ALIAS_CACHE,
    _TSCONFIG_BASEURL_CACHE,
    _VUE_SCRIPT_LANG_RE,
    _VUE_SCRIPT_RE,
    _WORKSPACE_MANIFEST_NAMES,
    _collect_js_symbol_resolution_facts,
    _contained_in_package,
    _exports_candidates,
    _find_js_config,
    _find_js_project_anchor,
    _find_workspace_root,
    _importer_platform,
    _js_call_identifier,
    _js_default_export_name,
    _js_default_import_name,
    _js_export_clause,
    _js_export_statement_is_star,
    _js_exported_declaration_names,
    _js_lexical_aliases,
    _js_module_specifier,
    _js_named_specifiers,
    _js_namespace_export_name,
    _js_source_path,
    _js_top_level_function_bodies,
    _load_package_imports,
    _load_tsconfig_aliases,
    _load_tsconfig_base_url,
    _load_workspace_packages,
    _match_subpath_import,
    _match_tsconfig_alias,
    _package_entry_candidates,
    _parse_js_tree,
    _platform_variants,
    _pnpm_workspace_globs,
    _read_json_config,
    _read_tsconfig_aliases,
    _resolve_export_target,
    _resolve_export_targets,
    _resolve_js_import_path,
    _resolve_js_import_target,
    _resolve_js_module_path,
    _resolve_package_import,
    _resolve_tsconfig_alias,
    _resolve_workspace_import,
    _strip_jsonc,
    _ts_collect_type_refs,
    _ts_heritage_clause_entries,
    _ts_walk_class_members,
    _vue_mask_non_script,
    _walk_js_tree,
    _workspace_globs,
)
from graph_fy.resolvers.jvm import (
    _resolve_cross_file_java_imports,
    _resolve_java_type_references,
)
from graph_fy.resolvers.lua import _resolve_lua_import_target
from graph_fy.resolvers.pascal import (
    _pascal_class_stem_cache,
    _pascal_project_root,
    _pascal_resolve_class,
    _pascal_resolve_unit,
    _pascal_unit_cache,
)
from graph_fy.resolvers.php import (
    _PHP_REPOINT_RELATIONS,
    _PHP_SUPERTYPE_RELATIONS,
    _php_fqn_from_raw,
    _resolve_php_type_references,
)
from graph_fy.resolvers.python import (
    _PYTHON_WORKSPACE_ROOTS_CACHE,
    _collect_python_symbol_resolution_facts,
    _parse_python_tree,
    _parse_python_tree_cached,
    _probe_python_module_candidate,
    _python_call_identifier,
    _python_import_from_module,
    _python_imported_names,
    _python_top_level_function_bodies,
    _python_workspace_package_roots,
    _resolve_python_module_path,
    _resolve_python_namespace_dir,
    _walk_python_tree,
)

from graph_fy.extractors.base import (
    _LANGUAGE_BUILTIN_GLOBALS,
    _file_stem,
    _make_id,
    _read_text,
)
from graph_fy.extractors.models import (
    LanguageConfig,
    _JS_CACHE_BYPASS_SUFFIXES,
    _NamespaceExportFact,
    _StarExportFact,
    _SymbolAliasFact,
    _SymbolDeclarationFact,
    _SymbolExportFact,
    _SymbolImportFact,
    _SymbolResolutionFacts,
    _SymbolUseFact,
    _WORKSPACE_PACKAGE_CACHE,
)


def _apply_symbol_resolution_facts(
    paths: list[Path],
    nodes: list[dict],
    edges: list[dict],
    root: Path,
    facts: _SymbolResolutionFacts,
    resolution_context_nodes: list[dict] | None = None,
) -> None:
    """Apply language-provided import/export/use facts to graph edges."""
    if not (
        facts.declarations
        or facts.imports
        or facts.aliases
        or facts.exports
        or facts.star_exports
        or facts.namespace_exports
        or facts.uses
        or facts.module_imports
    ):
        return

    path_by_resolved = {_resolve_cached(path): path for path in paths}
    source_file_id = {_resolve_cached(path): _make_id(str(path)) for path in paths}
    symbol_nodes: dict[tuple[Path, str], str] = {}
    # Member nodes (`.method()` labels) share their bare name with top-level
    # symbols once the leading dot is stripped. A module can only re-export
    # top-level bindings, so a star-export walk must never bind an imported
    # name to a class/interface member (#3436). Track those keys separately
    # and never let a member shadow a same-named top-level symbol.
    member_symbol_keys: set[tuple[Path, str]] = set()

    if resolution_context_nodes:
        _fresh_node_ids = {n.get("id") for n in nodes if n.get("id")}
        _fresh_paths = {_resolve_cached(p) for p in paths}
        for node in resolution_context_nodes:
            nid = node.get("id")
            if not nid or nid in _fresh_node_ids:
                continue
            source_path = _js_source_path(str(node.get("source_file", "")), root)
            if source_path is None or source_path in _fresh_paths:
                continue
            raw_label = str(node.get("label", "")).strip()
            label = raw_label.strip("()").lstrip(".")
            if not label:
                continue
            key = (source_path, label)
            if raw_label.startswith("."):
                if key in symbol_nodes:
                    continue
                member_symbol_keys.add(key)
            else:
                member_symbol_keys.discard(key)
            symbol_nodes[key] = str(nid)
    for node in nodes:
        source_path = _js_source_path(str(node.get("source_file", "")), root)
        if source_path is None:
            continue
        raw_label = str(node.get("label", "")).strip()
        label = raw_label.strip("()").lstrip(".")
        if not label or not node.get("id"):
            continue
        key = (source_path, label)
        if raw_label.startswith("."):
            if key in symbol_nodes:
                continue
            member_symbol_keys.add(key)
        else:
            member_symbol_keys.discard(key)
        symbol_nodes[key] = str(node["id"])

    def ensure_symbol_node(path: Path, name: str, line: int) -> str:
        resolved_path = _resolve_cached(path)
        existing = symbol_nodes.get((resolved_path, name))
        if existing is not None:
            return existing
        node_id = _make_id(_file_stem(path), name)
        symbol_nodes[(resolved_path, name)] = node_id
        nodes.append({
            "id": node_id,
            "label": name,
            "file_type": "code",
            "source_file": str(path),
            "source_location": f"L{line}",
        })
        return node_id

    existing_edges = {
        (
            str(edge.get("source")),
            str(edge.get("target")),
            str(edge.get("relation")),
            str(edge.get("context") or ""),
        )
        for edge in edges
    }

    def add_edge(source: str, target: str, relation: str, context: str, line: int, source_path: Path, target_file: str | None = None, local_alias: str | None = None, type_only: bool = False) -> None:
        key = (source, target, relation, context or "")
        if key in existing_edges:
            return
        existing_edges.add(key)
        edge = {
            "source": source,
            "target": target,
            "relation": relation,
            "context": context,
            "confidence": "EXTRACTED",
            "source_file": str(source_path),
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        # A re-export edge's target is a FILE node that can collapse with a
        # same-basename cross-extension sibling; stamp the resolved target file so
        # the id-disambiguation salt is keyed by the TARGET, not the importer (#1814).
        if target_file is not None:
            edge["target_file"] = target_file
        # The local name this import bound in the importing file, when it differs
        # from the target's own name (`from pkg import mod as alias`) -- lets the
        # cross-file member-call resolver match `alias.func()` (#2082).
        if local_alias is not None:
            edge["local_alias"] = local_alias
        # Erased at compile time (#3123): Import Cycles skips these edges.
        if type_only:
            edge["type_only"] = True
        edges.append(edge)

    for declaration in facts.declarations:
        ensure_symbol_node(declaration.file_path, declaration.name, declaration.line)

    local_aliases_by_file: dict[Path, dict[str, tuple[Path, str]]] = {}
    for import_fact in facts.imports:
        file_path = _resolve_cached(import_fact.file_path)
        local_aliases_by_file.setdefault(file_path, {})[import_fact.local_name] = (
            _resolve_cached(import_fact.target_path),
            import_fact.imported_name,
        )

    pending_aliases_by_file: dict[Path, list[_SymbolAliasFact]] = {}
    for alias_fact in facts.aliases:
        pending_aliases_by_file.setdefault(_resolve_cached(alias_fact.file_path), []).append(alias_fact)

    for file_path, aliases in pending_aliases_by_file.items():
        local_aliases = local_aliases_by_file.setdefault(file_path, {})
        changed = True
        while changed:
            changed = False
            for alias_fact in aliases:
                if alias_fact.alias in local_aliases:
                    continue
                origin = local_aliases.get(alias_fact.target_name)
                if origin is not None:
                    local_aliases[alias_fact.alias] = origin
                    changed = True

    named_exports_by_file: dict[Path, dict[str, tuple[Path, str]]] = {}
    export_origins: dict[tuple[Path, str], set[tuple[Path, str]]] = {}
    star_exports_by_file: dict[Path, list[Path]] = {}

    for star_fact in facts.star_exports:
        source_path = _resolve_cached(star_fact.file_path)
        target_path = _resolve_cached(star_fact.target_path)
        star_exports_by_file.setdefault(source_path, []).append(target_path)
        source_id = source_file_id.get(source_path)
        if source_id is not None:
            add_edge(
                source_id,
                _make_id(str(path_by_resolved.get(target_path, target_path))),
                "re_exports",
                "export",
                star_fact.line,
                star_fact.file_path,
                target_file=str(path_by_resolved.get(target_path, target_path)),
                type_only=star_fact.type_only,
            )

    for namespace_fact in facts.namespace_exports:
        source_path = _resolve_cached(namespace_fact.file_path)
        target_path = _resolve_cached(namespace_fact.target_path)
        namespace_id = ensure_symbol_node(
            namespace_fact.file_path,
            namespace_fact.exported_name,
            namespace_fact.line,
        )
        named_exports_by_file.setdefault(source_path, {})[
            namespace_fact.exported_name
        ] = (source_path, namespace_fact.exported_name)
        source_id = source_file_id.get(source_path)
        if source_id is not None:
            add_edge(
                source_id,
                namespace_id,
                "contains",
                "namespace_export",
                namespace_fact.line,
                namespace_fact.file_path,
            )
            add_edge(
                source_id,
                _make_id(str(path_by_resolved.get(target_path, target_path))),
                "re_exports",
                "export",
                namespace_fact.line,
                namespace_fact.file_path,
                target_file=str(path_by_resolved.get(target_path, target_path)),
                type_only=namespace_fact.type_only,
            )

    for export_fact in facts.exports:
        file_path = _resolve_cached(export_fact.file_path)
        origin: tuple[Path, str] | None = None
        if export_fact.target_path is not None and export_fact.target_name is not None:
            origin = (_resolve_cached(export_fact.target_path), export_fact.target_name)
        elif export_fact.local_name is not None:
            origin = local_aliases_by_file.get(file_path, {}).get(export_fact.local_name)
            if origin is None and (file_path, export_fact.local_name) in symbol_nodes:
                origin = (file_path, export_fact.local_name)
        if origin is None:
            # An explicit export remains evidence even when extraction did not
            # materialize its binding. Do not mistake it for an absent export.
            if export_fact.local_name is not None:
                export_origins.setdefault((file_path, export_fact.exported_name), set()).add(
                    (file_path, export_fact.local_name)
                )
            continue
        export_origins.setdefault((file_path, export_fact.exported_name), set()).add(origin)
        named_exports_by_file.setdefault(file_path, {})[export_fact.exported_name] = origin
        if origin[0] != file_path:
            source_id = source_file_id.get(file_path)
            if source_id is not None:
                add_edge(
                    source_id,
                    _make_id(str(path_by_resolved.get(origin[0], origin[0]))),
                    "re_exports",
                    "export",
                    export_fact.line,
                    export_fact.file_path,
                    target_file=str(path_by_resolved.get(origin[0], origin[0])),
                    type_only=export_fact.type_only,
                )

    def resolve_exported_origin(target_path: Path, imported_name: str, seen: set[tuple[Path, str]] | None = None) -> tuple[Path, str]:
        target_path = _resolve_cached(target_path)
        key = (target_path, imported_name)
        if seen is None:
            seen = set()
        if key in seen:
            return key
        seen.add(key)
        origins = export_origins.get(key)
        if origins and len(origins) > 1:
            return key  # Ambiguous re-export chain: do not guess by last-write-wins (#2034)
        origin = named_exports_by_file.get(target_path, {}).get(imported_name)
        if origin is not None:
            return resolve_exported_origin(origin[0], origin[1], seen)
        for star_target in star_exports_by_file.get(target_path, []):
            star_key = (star_target, imported_name)
            if star_key in symbol_nodes and star_key not in member_symbol_keys:
                return star_key
            resolved = resolve_exported_origin(star_target, imported_name, seen)
            if resolved in symbol_nodes and resolved not in member_symbol_keys:
                return resolved
        return key

    def exported_candidates(
        key: tuple[Path, str], seen: frozenset[tuple[Path, str]]
    ) -> tuple[set[tuple[Path, str]], bool]:
        # Distinguish a cycle cutoff from an absent export. A cycle contributes
        # no origin; a named export of an unrepresented binding remains unknown.
        if key in seen:
            return set(), True
        seen = seen | {key}
        origins = export_origins.get(key)
        candidates: set[tuple[Path, str]] = set()
        cyclic = False
        if origins is None:
            for path in star_exports_by_file.get(key[0], []):
                branch, branch_cyclic = exported_candidates((path, key[1]), seen)
                candidates.update(branch)
                cyclic |= branch_cyclic
            return candidates, cyclic
        for origin in origins:
            if origin == key:
                candidates.add(origin)
            else:
                branch, branch_cyclic = exported_candidates(origin, seen)
                candidates.update(branch)
                cyclic |= branch_cyclic
                if not branch and not branch_cyclic:
                    candidates.add(origin)
        return candidates, cyclic

    # The structural extractor names the immediate barrel's symbol. Resolve
    # that exact authored export site through the shared import/export facts,
    # including `import {x}; export {x}` bridges with no local declaration.
    export_sites: dict[tuple[Path, str, Path], list[_SymbolExportFact]] = {}
    for export_fact in facts.exports:
        if export_fact.target_path is not None and export_fact.target_name is not None:
            site = (
                _resolve_cached(export_fact.file_path),
                f"L{export_fact.line}",
                _resolve_cached(export_fact.target_path),
            )
            export_sites.setdefault(site, []).append(export_fact)
    owned_ids = {node.get("id") for node in nodes}
    for edge in edges:
        if edge.get("relation") != "re_exports" or edge.get("target") in owned_ids:
            continue
        target_file = edge.get("target_file")
        source_path = _js_source_path(str(edge.get("source_file", "")), root)
        if not target_file or source_path is None:
            continue
        target_path = Path(target_file)
        site = (source_path, str(edge.get("source_location", "")), _resolve_cached(target_path))
        for export_fact in export_sites.get(site, []):
            expected = _make_id(_file_stem(target_path), export_fact.target_name)
            if edge.get("target") != expected:
                continue
            candidates, _ = exported_candidates(
                (_resolve_cached(export_fact.target_path), export_fact.target_name), frozenset()
            )
            if len(candidates) == 1:
                origin = next(iter(candidates))
                target_id = symbol_nodes.get(origin)
                if target_id in owned_ids:
                    edge["target"] = target_id
                    edge["target_file"] = str(path_by_resolved.get(origin[0], origin[0]))
            break

    for import_fact in facts.imports:
        source_id = source_file_id.get(_resolve_cached(import_fact.file_path))
        if source_id is None:
            continue
        origin_path, origin_symbol = resolve_exported_origin(
            import_fact.target_path,
            import_fact.imported_name,
        )
        target_id = symbol_nodes.get((origin_path, origin_symbol))
        if target_id is None:
            continue
        # Drop the dangling provisional barrel-named import edge once resolved through a barrel (#41, #4486)
        resolved_barrel_path = _resolve_cached(import_fact.target_path)
        if origin_path != resolved_barrel_path:
            barrel_stem = _file_stem(import_fact.target_path)
            barrel_target = _make_id(barrel_stem, import_fact.imported_name)
            if barrel_target not in owned_ids:
                edges[:] = [
                    e for e in edges
                    if not (
                        e.get("source") == source_id
                        and e.get("relation") == "imports"
                        and e.get("target") == barrel_target
                    )
                ]
                existing_edges.discard((source_id, barrel_target, "imports", "import"))
        add_edge(
            source_id,
            target_id,
            "imports",
            "import",
            import_fact.line,
            import_fact.file_path,
        )

    # #1146: emit file-to-file imports_from edges for package-form submodule imports.
    for from_path, to_path, line, local_name in facts.module_imports:
        try:
            from_rel = from_path.relative_to(root)
            to_rel = to_path.relative_to(root)
        except ValueError:
            continue
        source_id = _make_id(_file_stem(from_rel))
        target_id = _make_id(_file_stem(to_rel))
        add_edge(
            source_id, target_id, "imports_from", "submodule_import", line, from_path,
            local_alias=local_name if local_name != to_path.stem else None,
        )

    # #2262 producer guard: never emit a `calls` use-edge from a source id
    # that owns no node. All node appends (ensure_symbol_node, declarations,
    # namespace exports) happened above, so the owned set is complete here.
    # A node-less caller id can never be canonicalized by the extract()
    # remaps (they learn only from nodes), so an absolute-derived one would
    # leak the machine/scan-path slug into the edge source. Reattribute the
    # edge to the caller's FILE node — the true file-level dependency
    # survives, and the file id is exactly what the #2231 remap
    # canonicalizes — or drop it when no file node id is available.
    owned = {str(n.get("id")) for n in nodes}
    for use_fact in facts.uses:
        file_path = _resolve_cached(use_fact.file_path)
        target_id = None
        unresolved_origin = local_aliases_by_file.get(file_path, {}).get(use_fact.local_name)
        if unresolved_origin is not None:
            origin_path, origin_symbol = resolve_exported_origin(*unresolved_origin)
            target_id = symbol_nodes.get((origin_path, origin_symbol))
        if target_id is None and use_fact.relation in ("inherits", "implements"):
            # Same-file fallback for HERITAGE only: a base declared in the same
            # file (`class X extends Y`, `interface A extends B`) has no import
            # alias, so resolve it directly against the file's own symbol nodes.
            # Scoped to heritage because same-file calls/uses already resolve via
            # the dedicated call-graph pass; widening this would duplicate those
            # edges. Import resolution still takes precedence (#1095).
            target_id = symbol_nodes.get((file_path, use_fact.local_name))
        if target_id is None:
            continue
        source_id = use_fact.source_id
        # Structural walking can omit named-function-local declarations while
        # materializing callback-local ones. Only actual node ownership decides
        # whether a type relationship has a represented source declaration.
        if use_fact.relation in ("inherits", "implements", "references") and source_id not in owned:
            continue
        if use_fact.relation == "calls" and source_id not in owned:
            source_id = source_file_id.get(file_path)
            if source_id is None:
                continue
        add_edge(
            source_id,
            target_id,
            use_fact.relation,
            use_fact.context,
            use_fact.line,
            use_fact.file_path,
        )


def _augment_symbol_resolution_edges(
    paths: list[Path],
    nodes: list[dict],
    edges: list[dict],
    root: Path,
    resolution_context_nodes: list[dict] | None = None,
) -> None:
    facts = _SymbolResolutionFacts()
    _collect_js_symbol_resolution_facts(paths, facts)
    _collect_python_symbol_resolution_facts(paths, root, facts)
    if resolution_context_nodes:
        _apply_symbol_resolution_facts(
            paths, nodes, edges, root, facts,
            resolution_context_nodes=resolution_context_nodes,
        )
    else:
        _apply_symbol_resolution_facts(paths, nodes, edges, root, facts)

def _resolve_cross_file_imports(
    per_file: list[dict],
    paths: list[Path],
    all_nodes: list[dict] | None = None,
    all_edges: list[dict] | None = None,
) -> list[dict]:
    """
    Two-pass import resolution: turn file-level imports into class-level edges.

    Pass 1 - build a global map: class/function name → node_id, per stem.
    Pass 2 - for each `from .module import Name`, look up Name in the global
              map and add a direct INFERRED edge from each class in the
              importing file to the imported entity.

    This turns:
        auth.py --imports_from--> models.py          (obvious, filtered out)
    Into:
        DigestAuth --uses--> Response  [INFERRED]    (cross-file, interesting!)
        BasicAuth  --uses--> Request   [INFERRED]
    """
    try:
        import tree_sitter_python  # noqa: F401  (availability check only)
    except ImportError:
        return []

    # Pass 1: _file_stem(path) → {ClassName: node_id}
    # Keyed by directory-qualified stem (e.g. "auth_models") to avoid collisions
    # when multiple files share the same filename in different directories.
    # A secondary bare-stem index handles absolute imports where only the module
    # name is known — first writer wins when names collide (inherently ambiguous).
    stem_to_entities: dict[str, dict[str, str]] = {}
    bare_to_qualified: dict[str, str] = {}
    for file_result in per_file:
        for node in file_result.get("nodes", []):
            src = node.get("source_file", "")
            if not src:
                continue
            src_path = Path(src)
            fq_stem = _file_stem(src_path)
            label = node.get("label", "")
            nid = node.get("id", "")
            # Index class-level entities only. Function/method labels end in "()"
            # so are excluded by the `endswith(")")` filter; file nodes end in ".py";
            # private/internal labels start with "_"; rationale nodes carry
            # file_type=="rationale" and must never participate in cross-file
            # import resolution (#563).
            if (
                label
                and not label.endswith((")", ".py"))
                and "_" not in label[:1]
                and node.get("file_type") != "rationale"
            ):
                stem_to_entities.setdefault(fq_stem, {})[label] = nid
                if src_path.stem not in bare_to_qualified:
                    bare_to_qualified[src_path.stem] = fq_stem

    # Pass 2: for each file, find `from .X import A, B, C`, then attribute the
    # `uses` edge to the specific local symbol (class OR function) whose body
    # actually references the imported name — not to every class that merely
    # shares the file (#2652). The edge is anchored at the real reference, not
    # the import line, so `source_location` points at genuine corroboration.
    new_edges: list[dict] = []
    node_by_id = {node["id"]: node for node in all_nodes if node.get("id")} if all_nodes else {}
    repointed_from: set[str] = set()
    TYPE_REPOINT_RELATIONS = frozenset({"references", "inherits", "implements", "extends"})

    for file_result, path in zip(per_file, paths):
        str_path = str(path)
        file_srcs = {n.get("source_file") for n in file_result.get("nodes", []) if n.get("source_file")}
        file_srcs.add(str_path)
        file_srcs.add(path.as_posix())

        # Map each local symbol (class or function) to its node id, keyed by the
        # bare symbol name. Function labels end in "()"; the file node ends in
        # ".py"; rationale nodes never import (#563). First writer wins on a
        # name collision (inherently ambiguous within a file).
        name_to_nid: dict[str, str] = {}
        for n in file_result.get("nodes", []):
            if n.get("source_file") != str_path or n.get("file_type") == "rationale":
                continue
            label = n.get("label", "")
            if not label or label.endswith(".py"):
                continue
            sym_name = label[:-2] if label.endswith("()") else label
            if sym_name and sym_name not in name_to_nid:
                name_to_nid[sym_name] = n["id"]
        if not name_to_nid:
            continue

        # Parse imports from this file (shared with the facts pass via the
        # mtime-keyed memo, so each .py is parsed once across both passes).
        parsed = _parse_python_tree(path)
        if parsed is None:
            continue
        source, root_node = parsed

        # local_name -> target node id (local_name honours `import X as Y`, so a
        # reference to the alias in the body still attributes correctly).
        import_targets: dict[str, str] = {}
        # referenced name -> {source symbol nid: first reference line}
        ref_sources: dict[str, dict[str, int]] = {}

        def _text(n) -> str:
            return source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

        def resolve_import(node) -> None:
            # Find the module name - handles both absolute and relative imports.
            # Relative: `from .models import X` → relative_import → dotted_name
            # Absolute: `from models import X`  → module_name field
            # target_fq is the directory-qualified stem used as the key in
            # stem_to_entities. Relative imports are resolved exactly via the
            # importing file's directory; absolute imports fall back to the
            # bare-stem secondary index (first-writer-wins when names collide).
            target_fq: str | None = None
            for child in node.children:
                if child.type == "relative_import":
                    prefix_text = ""
                    dotted_text = ""
                    for sub in child.children:
                        if sub.type == "import_prefix":
                            prefix_text = _text(sub)
                        elif sub.type == "dotted_name":
                            dotted_text = _text(sub)
                    dots = prefix_text.count(".") if prefix_text else 1
                    cur_dir = path.parent
                    for _ in range(dots - 1):
                        cur_dir = cur_dir.parent
                    if dotted_text:
                        candidate = cur_dir.joinpath(*dotted_text.split(".")).with_suffix(".py")
                    else:
                        candidate = cur_dir / "__init__.py"
                    target_fq = _file_stem(candidate)
                    break
                if child.type == "dotted_name" and target_fq is None:
                    dotted_name = _text(child)
                    dotted_as_path = "/".join(dotted_name.split("."))
                    if dotted_as_path in stem_to_entities:
                        target_fq = dotted_as_path
                    else:
                        suffix_matches = [
                            fq for fq in stem_to_entities
                            if fq.endswith(f"/{dotted_as_path}") or fq.endswith(f"\\{dotted_as_path}")
                        ]
                        if len(suffix_matches) == 1:
                            target_fq = suffix_matches[0]
                        else:
                            bare = dotted_name.split(".")[-1]
                            target_fq = bare_to_qualified.get(bare)

            if not target_fq or target_fq not in stem_to_entities:
                return

            # Imported names come AFTER the 'import' keyword token. For
            # `import X as Y` the target is found via X but the body uses Y.
            past_import_kw = False
            for child in node.children:
                if child.type == "import":
                    past_import_kw = True
                    continue
                if not past_import_kw:
                    continue
                imported_name: str | None = None
                local_name: str | None = None
                if child.type == "dotted_name":
                    imported_name = local_name = _text(child)
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is not None:
                        imported_name = _text(name_node)
                        local_name = _text(alias_node) if alias_node is not None else imported_name
                if not imported_name or not local_name:
                    continue
                tgt_nid = stem_to_entities[target_fq].get(imported_name)
                if tgt_nid:
                    import_targets[local_name] = tgt_nid

        def visit(node, current_nid: str | None) -> None:
            # Identifiers inside an import statement are the import itself, not a
            # real use — resolve the import here and don't descend into it.
            if node.type == "import_from_statement":
                resolve_import(node)
                return
            # Attribute references to the top-level symbol that contains them: a
            # class is a unit (a reference inside one of its methods counts for
            # the class, matching the documented DigestAuth->Response edge), and
            # a module-level function is its own source. Only set at module scope
            # (current_nid is None) so nested defs never override the container.
            if current_nid is None and node.type in ("class_definition", "function_definition"):
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    mapped = name_to_nid.get(_text(name_node))
                    if mapped is not None:
                        current_nid = mapped
            if node.type == "identifier" and current_nid is not None:
                slot = ref_sources.setdefault(_text(node), {})
                slot.setdefault(current_nid, node.start_point[0] + 1)
            for child in node.children:
                visit(child, current_nid)

        visit(root_node, None)

        for name, tgt_nid in import_targets.items():
            for src_nid, line in ref_sources.get(name, {}).items():
                if src_nid == tgt_nid:
                    continue
                new_edges.append({
                    "source": src_nid,
                    "target": tgt_nid,
                    "relation": "uses",
                    "confidence": "INFERRED",
                    # 0.95 = "direct structural evidence (named cross-file
                    # reference)" from the extraction-spec rubric, which is
                    # exactly what this edge is: a name this file imports, then
                    # references. Omitting the score entirely fell through to the
                    # 0.5 default the same rubric forbids outright (#2813).
                    "confidence_score": 0.95,
                    "source_file": str_path,
                    "source_location": f"L{line}",
                    "weight": 0.8,
                })

        # Repoint AST type-reference and inheritance edges from sourceless stubs
        # to the exact imported target definition (#3252).
        if all_edges and import_targets:
            for edge in all_edges:
                if edge.get("source_file") not in file_srcs:
                    continue
                if edge.get("relation") not in TYPE_REPOINT_RELATIONS:
                    continue
                tgt = edge.get("target")
                tgt_node = node_by_id.get(tgt)
                if not tgt_node or tgt_node.get("source_file"):
                    continue
                stub_label = tgt_node.get("label", "")
                resolved_id = import_targets.get(stub_label)
                if resolved_id and resolved_id != tgt:
                    edge["target"] = resolved_id
                    repointed_from.add(tgt)

    if all_nodes is not None and all_edges is not None and repointed_from:
        still_referenced = {e.get("source") for e in all_edges} | {e.get("target") for e in all_edges}
        all_nodes[:] = [
            node for node in all_nodes
            if node.get("id") not in repointed_from or node.get("id") in still_referenced
        ]

    return new_edges


