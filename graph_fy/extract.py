"""Deterministic structural extraction from source code using tree-sitter. Outputs nodes+edges dicts."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import textwrap
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Callable

from .cache import load_cached, save_cached
from .mcp_ingest import extract_mcp_config, is_mcp_config_path
from .manifest_ingest import extract_package_manifest, is_package_manifest_path
from .resolver_registry import (
    LanguageResolver,
    register as register_language_resolver,
    run_language_resolvers,
)
from .ruby_resolution import resolve_ruby_member_calls
from .csharp_dispatch import resolve_csharp_interface_dispatch
from .pascal_resolution import resolve_pascal_inherited_calls
from .markdown_resolution import MARKDOWN_MENTION_SUFFIXES, resolve_markdown_mentions

# --- migrated to graph_fy/extractors/ (see graph_fy/extractors/MIGRATION.md) ---
from graph_fy.extractors.base import (  # noqa: F401
    _LANGUAGE_BUILTIN_GLOBALS,
    _file_stem,
    _make_id,
    _read_text,
)
from graph_fy.extractors.apex import extract_apex  # noqa: F401
from graph_fy.extractors.bash import extract_bash  # noqa: F401
from graph_fy.extractors.blade import extract_blade  # noqa: F401
from graph_fy.extractors.cobol import extract_cobol  # noqa: F401
from graph_fy.extractors.csharp import (
    CsharpNameResolver,
    _resolve_cross_file_csharp_imports,
    _resolve_csharp_type_references,
)
from graph_fy.extractors.dart import extract_dart  # noqa: F401
from graph_fy.extractors.dm import extract_dm, extract_dmf, extract_dmi, extract_dmm  # noqa: F401
from graph_fy.extractors.elixir import extract_elixir  # noqa: F401
from graph_fy.extractors.fortran import _cpp_preprocess, extract_fortran  # noqa: F401
from graph_fy.extractors.go import _GO_PREDECLARED_FUNCS, extract_go  # noqa: F401
from graph_fy.extractors.json_config import extract_json  # noqa: F401
from graph_fy.extractors.commonlisp import extract_commonlisp  # noqa: F401
from graph_fy.extractors.markdown import extract_markdown, extract_pdf, _MD_LINK_INDEX_CACHE  # noqa: F401
from graph_fy.extractors.ocaml import extract_ocaml  # noqa: F401
from graph_fy.extractors.pascal_forms import extract_delphi_form, extract_lazarus_form  # noqa: F401
from graph_fy.extractors.powershell import extract_powershell, extract_powershell_manifest  # noqa: F401
from graph_fy.extractors.razor import extract_razor  # noqa: F401
from graph_fy.extractors.robot import extract_robot  # noqa: F401
from graph_fy.extractors.rust import extract_rust  # noqa: F401
from graph_fy.extractors.sln import extract_sln  # noqa: F401
from graph_fy.extractors.lazarus import extract_lazarus_package  # noqa: F401
from graph_fy.extractors.csproj import extract_slnx, extract_csproj  # noqa: F401
from graph_fy.extractors.xaml import extract_xaml, _XAML_CSHARP_CLASS_CACHE  # noqa: F401
from graph_fy.extractors.xml_util import _PROJECT_XML_MAX_BYTES, _project_xml_is_safe  # noqa: F401
from graph_fy.extractors.sql import extract_sql  # noqa: F401
from graph_fy.extractors.terraform import extract_terraform, prepare_terraform, resolve_terraform_modules  # noqa: F401
from graph_fy.extractors.verilog import extract_verilog  # noqa: F401
from graph_fy.extractors.zig import extract_zig  # noqa: F401
from graph_fy.extractors.iac import (  # noqa: F401
    extract_avro,
    extract_cloudformation,
    extract_helm,
    extract_kafka_properties,
    extract_kubernetes,
    is_cloudformation_template,
    is_helm_chart_file,
    is_kafka_config,
    is_kubernetes_manifest,
)
from graph_fy.extractors.protobuf import extract_protobuf, is_protobuf_file  # noqa: F401
from graph_fy.extractors.docker import extract_dockerfile, extract_docker_compose, is_docker_compose  # noqa: F401
from graph_fy.extractors.github_actions import extract_github_workflow, is_github_workflow  # noqa: F401
from graph_fy.extractors.api_schema import extract_api_or_schema, is_api_or_schema  # noqa: F401
from graph_fy.extractors.bazel import extract_bazel, is_bazel_file  # noqa: F401
from graph_fy.extractors.monorepo import extract_monorepo, is_monorepo_file  # noqa: F401
from graph_fy.extractors.argocd import extract_argocd, is_argocd_manifest  # noqa: F401
from graph_fy.extractors.rtk import extract_rtk, is_rtk_file  # noqa: F401
from graph_fy.security import sanitize_metadata
from graph_fy.paths import disambiguate_ambiguous_candidates

from graph_fy.extractors.models import LanguageConfig, _JS_CACHE_BYPASS_SUFFIXES, _NamespaceExportFact, _StarExportFact, _SymbolAliasFact, _SymbolDeclarationFact, _SymbolExportFact, _SymbolImportFact, _SymbolResolutionFacts, _SymbolUseFact, _WORKSPACE_PACKAGE_CACHE  # noqa: E402,F401

from graph_fy.extractors.resolution import (  # noqa: E402,F401
    _DECLDEF_HEADER_SUFFIXES,
    _DECLDEF_IMPL_SUFFIXES,
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
    _apply_symbol_resolution_facts,
    _augment_symbol_resolution_edges,
    _cached_realpath,
    _cached_source_key,
    _collect_js_symbol_resolution_facts,
    _collect_python_symbol_resolution_facts,
    _contained_in_package,
    _decldef_class_stem,
    _disambiguate_colliding_node_ids,
    _find_workspace_root,
    _go_import_path_for_file,
    _is_type_like_definition,
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
    _load_tsconfig_aliases,
    _load_tsconfig_base_url,
    _load_workspace_packages,
    _match_tsconfig_alias,
    _merge_decl_def_classes,
    _node_disambiguation_source_key,
    _package_entry_candidates,
    _parse_js_tree,
    _parse_python_tree,
    _pascal_class_stem_cache,
    _pascal_project_root,
    _pascal_resolve_class,
    _pascal_resolve_unit,
    _pascal_unit_cache,
    _pnpm_workspace_globs,
    _python_call_identifier,
    _python_import_from_module,
    _python_imported_names,
    _python_top_level_function_bodies,
    _read_tsconfig_aliases,
    _resolve_c_include_path,
    _resolve_cross_file_imports,
    _resolve_cross_file_java_imports,
    _resolve_export_target,
    _resolve_go_type_references,
    _resolve_java_type_references,
    _resolve_php_type_references,
    _resolve_js_import_path,
    _resolve_js_import_target,
    _resolve_js_module_path,
    _resolve_lua_import_target,
    _probe_python_module_candidate,
    _resolve_python_module_path,
    _resolve_tsconfig_alias,
    _resolve_workspace_import,
    _source_key,
    _strip_jsonc,
    _ts_collect_type_refs,
    _ts_heritage_clause_entries,
    _ts_walk_class_members,
    _vue_mask_non_script,
    _walk_js_tree,
    _walk_python_tree,
    _workspace_globs,
)

from graph_fy.symbol_resolution import resolve_bash_source_edges  # noqa: E402

from graph_fy.extractors.engine import REFERENCE_CONTEXTS, _CSHARP_TYPE_PARAMETER_SCOPE_DECLARATIONS, _C_PRIMITIVE_TYPE_NODES, _JAVA_BUILTIN_TYPES, _JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS, _JS_FUNCTION_VALUE_TYPES, _JS_SCOPE_BOUNDARY, _PYTHON_ANNOTATION_NOISE, _PYTHON_TYPE_CONTAINERS, _RUBY_CLASS_FACTORIES, _c_collect_type_refs, _cpp_collect_type_refs, _cpp_declarator_name, _cpp_local_var_types, _csharp_attribute_names, _csharp_classify_base, _csharp_collect_type_refs, _csharp_extra_walk, _csharp_namespace_id, _csharp_namespace_name, _csharp_pre_scan_interfaces, _csharp_type_parameters_in_scope, _dynamic_import_js, _extract_generic, _find_body, _find_require_call, _get_cpp_func_name, _java_annotation_names, _java_collect_type_refs, _java_extra_walk, _java_type_parameters_in_scope, _js_collect_pattern_idents, _js_dispatch_value_idents, _js_extra_walk, _js_local_bound_names, _js_member_assignment_target, _js_module_bound_names, _kotlin_collect_type_refs, _kotlin_function_return_type_node, _kotlin_property_type_node, _kotlin_user_type_name, _php_collect_type_refs, _php_method_return_type_node, _php_name_text, _python_collect_assignment_targets, _python_collect_param_refs, _python_collect_type_refs, _python_local_bound_names, _python_module_bound_names, _python_param_names, _read_csharp_type_name, _require_imports_js, _ruby_const_last_name, _ruby_extra_walk, _ruby_local_class_bindings, _ruby_new_class_name, _scala_collect_type_refs, _semantic_reference_edge, _source_location, _swift_classify_base, _swift_collect_type_refs, _swift_constructor_type, _swift_declaration_keyword, _swift_extra_walk, _swift_local_var_types, _swift_pre_scan, _swift_property_name, _swift_property_type_node, _swift_receiver_name, _swift_user_type_name, _ts_decorator_name, _ts_descendant_decorators, _ts_emit_decorator_edges, _ts_extra_walk, _ts_method_name, _ts_receiver_type_table  # noqa: E402,F401

from graph_fy.extractors.pascal import _PAS_BEGIN_END_TOKEN_RE, _PAS_CALL_RE, _PAS_END_SEMI_RE, _PAS_IMPL_HEADER_RE, _PAS_KEYWORDS, _PAS_METHOD_DECL_RE, _PAS_MODULE_RE, _PAS_TOKEN_RE, _PAS_TYPE_HEADER_RE, _PAS_USES_RE, _extract_pascal_regex, _pascal_find_body, _pascal_split_bases, _pascal_split_sections, _pascal_split_uses, _pascal_strip_comments, extract_pascal  # noqa: E402,F401

from graph_fy.extractors.objc import _objc_local_var_types, extract_objc  # noqa: E402,F401

from graph_fy.extractors.julia import extract_julia  # noqa: E402,F401

_RECURSION_LIMIT = 10_000

# Language built-in globals that AST may classify as call targets when used as
# constructors or coercion functions (e.g. String(x), Number(x), Boolean(x)).
# Without this filter they become god-nodes accumulating spurious edges from
# every call site. Filter applied at same-file and cross-file resolution.
# See issue #726.


def _raise_recursion_limit() -> None:
    if sys.getrecursionlimit() < _RECURSION_LIMIT:
        sys.setrecursionlimit(_RECURSION_LIMIT)


def _safe_extract(extractor: Callable, path: Path) -> dict:
    try:
        return extractor(path)
    except RecursionError:
        print(f"  warning: skipped {path} (recursion limit exceeded)", file=sys.stderr, flush=True)
        return {"nodes": [], "edges": [], "error": "recursion_limit_exceeded"}
    except Exception as e:
        if os.environ.get("graph_fy_DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)
        print(f"  warning: skipped {path} ({type(e).__name__}: {e})", file=sys.stderr, flush=True)
        return {"nodes": [], "edges": [], "error": f"{type(e).__name__}: {e}"}


def _file_node_id(rel_path: Path) -> str:
    """File-level node ID matching the skill.md spec: ``{parent_dir}_{stem}`` —
    one parent directory level, no extension. ``rel_path`` MUST be relative to
    the project root so top-level files collapse to a bare stem (``setup.py`` ->
    ``setup``) instead of picking up the root directory name. This must equal the
    ID semantic subagents generate, or AST and semantic extraction split a file
    into two disconnected ghost nodes (#1033)."""
    return _make_id(_file_stem(rel_path))


from graph_fy.extractors.python_imports import (  # noqa: F401
    _repoint_python_package_imports,
    _repoint_python_sibling_imports,
)




SEMANTIC_RELATIONS = frozenset({
    "inherits", "implements", "mixes_in", "embeds", "references",
    "calls", "imports", "imports_from", "re_exports", "contains", "method",
})


# Condition keys consulted when resolving an `exports` target, in priority
# order. `default` is Node's catch-all and must be consulted LAST so a more
# specific condition (source/import/module/etc.) wins when several match.


# ── Modular Language Extractors ───────────────────────────────────────────────
from graph_fy.extractors.python import (  # noqa: F401
    extract_python,
    _PYTHON_CONFIG,
    _import_python,
    _extract_python_rationale,
    _shorten_rationale_label,
    _is_autogenerated_python,
)
from graph_fy.extractors.javascript import (  # noqa: F401
    extract_js,
    _JS_CONFIG,
    _TS_CONFIG,
    _TSX_CONFIG,
    _import_js,
)
from graph_fy.extractors.java import (  # noqa: F401
    extract_java,
    extract_groovy,
    _JAVA_CONFIG,
    _GROOVY_CONFIG,
    _import_java,
    _is_spock_file,
    _extract_spock_fallback,
)
from graph_fy.extractors.c_cpp import (  # noqa: F401
    extract_c,
    extract_cpp,
    _C_CONFIG,
    _CPP_CONFIG,
    _import_c,
    _get_c_func_name,
    _augment_cpp_string_tests,
    _normalize_cpp_cli,
)
from graph_fy.extractors.ruby import (  # noqa: F401
    extract_ruby,
    _RUBY_CONFIG,
    _ruby_sanitize_method_name,
)
from graph_fy.extractors.csharp import (  # noqa: F401
    extract_csharp,
    _CSHARP_CONFIG,
    _import_csharp,
)
from graph_fy.extractors.kotlin import (  # noqa: F401
    extract_kotlin,
    extract_scala,
    _KOTLIN_CONFIG,
    _SCALA_CONFIG,
    _import_kotlin,
    _import_scala,
)
from graph_fy.extractors.php import (  # noqa: F401
    extract_php,
    _PHP_CONFIG,
    _import_php,
)
from graph_fy.extractors.lua import (  # noqa: F401
    extract_lua,
    _LUA_CONFIG,
    _import_lua,
)
from graph_fy.extractors.swift import (  # noqa: F401
    extract_swift,
    _SWIFT_CONFIG,
    _import_swift,
)

from graph_fy.extractors.ts_normalize import (  # noqa: F401
    _TS_IMPORT_CALL_RE,
    _ts_import_is_code,
    _ts_type_argument_ranges,
    _TsRangeIndex,
    _ts_ranges_containing,
    _ts_build_range_index,
    _ts_error_nodes,
    _ts_mask_candidate_is_malformed,
    _normalize_ts_import_types,
)

from graph_fy.extractors.js_web import (  # noqa: F401
    _rescue_js_dynamic_imports,
    _JS_RATIONALE_PREFIXES,
    _JS_DOC_REF_RE,
    _JS_COMMENT_LINE_RE,
    _extract_js_rationale,
    _resolve_rescued_specifier,
    _emit_rescued_import,
    extract_svelte,
    extract_astro,
    extract_vue,
)

# ── Cross-file import resolution ──────────────────────────────────────────────


from graph_fy.extractors.node_merging import (  # noqa: F401
    _canonicalize_csharp_namespace_nodes,
    _CASE_INSENSITIVE_EXTS,
    _CASE_INSENSITIVE_EXTENSIONS,
    _lang_is_case_insensitive,
    _LANG_FAMILY_BY_EXT,
    _EXT_TO_LANG,
    _LANG_BY_EXT,
    _lang_family,
    _LANGUAGE_BUILTIN_BASE_CLASSES,
    _LANGUAGE_BUILTIN_BASE_CLASSES_CI,
    _node_label_key,
    _is_top_level_function_definition,
    _rewire_unique_stub_nodes,
    _remap_objc_field_tables,
    _merge_swift_extensions,
    _merge_csharp_partial_class_nodes,
)



from graph_fy.member_resolution import (
    UNRESOLVED_CALLS_KEY,
    _MAX_PARKED_CALLS_PER_NODE,
    _park_unresolved_member_call,
    _resolve_swift_member_calls,
    _resolve_python_member_calls,
    _resolve_typescript_member_calls,
    _resolve_cpp_member_calls,
    _resolve_csharp_member_calls,
    _bind_member_field_tables,
    _resolve_java_member_calls,
    _resolve_objc_member_calls,
    _kotlin_package_index,
    _resolve_kotlin_import_targets,
    _resolve_csharp_qualified_calls,
    _resolve_kotlin_qualified_calls,
)
from graph_fy.extractors.solidity import (
    extract_solidity,
    resolve_solidity_inherited_calls,
)


_KOTLIN_IMPORT_TARGET_RESOLVER = LanguageResolver(
    "kotlin_import_targets", frozenset({".kt", ".kts"}), _resolve_kotlin_import_targets
)


# Register the cross-file, language-specific member-call resolvers into the shared
# registry (framework lives in graph_fy.resolver_registry). A new language plugs in
# by adding one register() call below — no edits to extract()'s body. Order
# preserved from the prior inlined wiring: Swift (#1356) before Python (#1446).
register_language_resolver(
    LanguageResolver("terraform_modules", frozenset({".tf"}), resolve_terraform_modules)
)
register_language_resolver(
    LanguageResolver("swift_member_calls", frozenset({".swift"}), _resolve_swift_member_calls)
)
register_language_resolver(
    LanguageResolver("python_member_calls", frozenset({".py"}), _resolve_python_member_calls)
)
# Ruby type-aware member-call resolution (Class.new + typed var.method). Lives in
# graph_fy.ruby_resolution; registered here as a second consumer of the framework.
register_language_resolver(
    LanguageResolver("ruby_member_calls", frozenset({".rb", ".rake"}), resolve_ruby_member_calls)
)
register_language_resolver(
    LanguageResolver("typescript_member_calls", frozenset({".ts", ".tsx", ".mts", ".cts", ".js", ".jsx"}), _resolve_typescript_member_calls)
)
# C++ (#1547) and ObjC (#1556) receiver-typed member-call resolution. `.h` is in
# both suffix sets because it routes to extract_cpp or extract_objc by content; the
# resolvers each claim only their own raw_calls via the extractor-stamped `lang`.
register_language_resolver(
    LanguageResolver(
        "cpp_member_calls",
        frozenset({".cpp", ".cc", ".cxx", ".hpp", ".cu", ".cuh", ".metal", ".h"}),
        _resolve_cpp_member_calls,
    )
)
register_language_resolver(
    LanguageResolver(
        "objc_member_calls",
        frozenset({".m", ".mm", ".h"}),
        _resolve_objc_member_calls,
    )
)
# C# receiver-typed member-call resolution (#1609): `field/param/local.Method()`
# bound to the receiver's declared type instead of a bare same-named match.
register_language_resolver(
    LanguageResolver("csharp_member_calls", frozenset({".cs"}), _resolve_csharp_member_calls)
)
register_language_resolver(
    LanguageResolver("java_member_calls", frozenset({".java"}), _resolve_java_member_calls)
)
# Pascal/Delphi cross-file inherited-method-call resolution: a call from a
# manual descendant class to a method it inherits from an ancestor declared
# in a DIFFERENT file (the common generated-base/manual-descendant split,
# e.g. Sistec's Th0Xxx/Th5Xxx) falls outside the per-file extractor's own
# scope. Lives in graph_fy.pascal_resolution; registered here as a consumer
# of the framework, same as the Ruby resolver above.
register_language_resolver(
    LanguageResolver(
        "pascal_inherited_calls",
        frozenset({".pas", ".pp", ".dpr", ".dpk", ".inc"}),
        resolve_pascal_inherited_calls,
    )
)
# Kotlin fully-qualified call resolution (#2550): `com.pkg.Fn()` /
# `com.pkg.Object.method()` raw_calls the shared pass skips (member calls with
# no receiver). Runs in the tail registry like the other member-call resolvers;
# its sibling import-target pass runs earlier (see _KOTLIN_IMPORT_TARGET_RESOLVER).
register_language_resolver(
    LanguageResolver(
        "kotlin_qualified_calls", frozenset({".kt", ".kts"}), _resolve_kotlin_qualified_calls
    )
)
# C# qualified construction (#2997): `new A.B.Cache()` arrives as the bare name,
# so a colliding `Cache` elsewhere makes it ambiguous. Runs in the tail registry
# beside csharp_member_calls and matches the prefix against declared namespaces.
register_language_resolver(
    LanguageResolver(
        "csharp_qualified_calls", frozenset({".cs"}), _resolve_csharp_qualified_calls
    )
)
register_language_resolver(
    LanguageResolver(
        "solidity_inherited_calls", frozenset({".sol"}), resolve_solidity_inherited_calls
    )
)
# C# member-level interface dispatch (#3003): a call through an injected
# dependency lands on the interface's method, so the implementation sits in the
# graph unreachable from the call site. Lives in graph_fy.csharp_dispatch;
# registered here as a consumer of the framework, like the Pascal resolver.
register_language_resolver(
    LanguageResolver(
        "csharp_interface_dispatch", frozenset({".cs"}), resolve_csharp_interface_dispatch
    )
)
# Markdown code-span mentions (`Widget`, `mod.py::Widget::render`) become
# heading --references--> symbol edges once every file is extracted and ids are
# final. Lives in graph_fy.markdown_resolution; the shared call pass above skips
# these raw_calls so a mention is never mistaken for a call.
register_language_resolver(
    LanguageResolver(
        "markdown_mentions", MARKDOWN_MENTION_SUFFIXES, resolve_markdown_mentions
    )
)


# Inline markdown link: [text](target "optional title"). The negative lookbehind
# excludes images (![alt](src)). The target stops at whitespace/closing paren so
# an optional "title" after the URL is dropped; an optional <...> wrapper is too.
# Reference-style link definition line: [label]: target "optional title"
# Obsidian-style wikilink: [[target]] / [[target|alias]] / [[target#anchor]].

# Extensions graph_fy creates document file nodes for. A link to one of these
# resolves to that file's node; links to code/assets are skipped (left to the
# language extractors).


# ── Pascal / Delphi extractor ─────────────────────────────────────────────────


# Size cap for project XML files we parse with stdlib ElementTree.
# Real .csproj/.fsproj/.vbproj/.lpk files are well under 2 MiB; anything
# larger is either malformed or hostile.
# ── Main extract and collect_files ────────────────────────────────────────────


def _check_tree_sitter_version() -> None:
    """Raise a clear error if tree-sitter is too old for the new Language API."""
    try:
        from tree_sitter import LANGUAGE_VERSION
    except ImportError:
        raise ImportError(
            "tree-sitter is not installed. Run: pip install 'tree-sitter>=0.23.0'"
        )
    # Language API v2 starts at LANGUAGE_VERSION 14
    if LANGUAGE_VERSION < 14:
        import tree_sitter as _ts
        raise RuntimeError(
            f"tree-sitter {getattr(_ts, '__version__', 'unknown')} is too old. "
            f"graph_fy requires tree-sitter >= 0.23.0 (Language API v2). "
            f"Run: pip install --upgrade tree-sitter"
        )


# ── DM (BYOND DreamMaker) extractor ──────────────────────────────────────────
# DM identity is path-based (`/datum/object/proc/New()`), not block-based, so
# the generic class-body walker doesn't fit well.


# ── DMI (BYOND icon files) ────────────────────────────────────────────────────
# .dmi is a PNG with a tEXt/zTXt "Description" chunk containing BYOND state
# metadata. We want the icon state names (icon_state = "X" in DM code
# references them).


# ── DMM (BYOND map files) ─────────────────────────────────────────────────────
# A .dmm starts with a tile dictionary — each "key" = (type, type{var=val}, ...)
# names one or more types that compose a tile — then a grid. We only need the
# dictionary section: every type path referenced is a `uses` edge.


# ── DMF (BYOND interface forms) ───────────────────────────────────────────────


# Head tokens in an HCL traversal that are meta/builtins, not references to a
# block defined in the corpus (count.index, each.key, self.*, path.module, ...).


_DISPATCH: dict[str, Any] = {
    ".py": extract_python,
    ".js": extract_js,
    ".jsx": extract_js,
    ".mjs": extract_js,
    ".cjs": extract_js,
    ".ts": extract_js,
    ".tsx": extract_js,
    ".mts": extract_js,
    ".cts": extract_js,
    ".go": extract_go,
    ".rs": extract_rust,
    ".java": extract_java,
    ".groovy": extract_groovy,
    ".gradle": extract_groovy,
    ".c": extract_c,
    ".h": extract_c,
    ".cpp": extract_cpp,
    ".cc": extract_cpp,
    ".cxx": extract_cpp,
    ".hpp": extract_cpp,
    ".cu": extract_cpp,
    ".cuh": extract_cpp,
    ".metal": extract_cpp,
    ".rb": extract_ruby, ".rake": extract_ruby,
    ".cs": extract_csharp,
    ".cbl": extract_cobol,
    ".cob": extract_cobol,
    ".cobol": extract_cobol,
    ".cpy": extract_cobol,
    ".kt": extract_kotlin,
    ".kts": extract_kotlin,
    ".scala": extract_scala,
    ".php": extract_php,
    ".swift": extract_swift,
    ".lua": extract_lua,
    ".luau": extract_lua,
    ".toc": extract_lua,
    ".zig": extract_zig,
    ".ps1": extract_powershell,
    ".psm1": extract_powershell,
    ".psd1": extract_powershell_manifest,
    ".ex": extract_elixir,
    ".exs": extract_elixir,
    ".m": extract_objc,
    ".mm": extract_objc,
    ".jl": extract_julia,
    ".f": extract_fortran,
    ".F": extract_fortran,
    ".f90": extract_fortran,
    ".F90": extract_fortran,
    ".f95": extract_fortran,
    ".F95": extract_fortran,
    ".f03": extract_fortran,
    ".F03": extract_fortran,
    ".f08": extract_fortran,
    ".F08": extract_fortran,
    ".vue": extract_vue,
    ".svelte": extract_svelte,
    ".astro": extract_astro,
    ".dart": extract_dart,
    ".ml": extract_ocaml,
    ".mli": extract_ocaml,
    ".lisp": extract_commonlisp,
    ".cl": extract_commonlisp,
    ".lsp": extract_commonlisp,
    ".asd": extract_commonlisp,
    ".v": extract_verilog,
    ".sv": extract_verilog,
    ".svh": extract_verilog,
    ".sql": extract_sql,
    ".md": extract_markdown,
    ".mdx": extract_markdown,
    ".qmd": extract_markdown,
    ".skill": extract_markdown,
    ".rst": extract_markdown,
    ".pdf": extract_pdf,
    ".pas": extract_pascal,
    ".pp": extract_pascal,
    ".dpr": extract_pascal,
    ".dpk": extract_pascal,
    ".lpr": extract_pascal,
    ".inc": extract_pascal,
    ".dfm": extract_delphi_form,
    ".lfm": extract_lazarus_form,
    ".lpk": extract_lazarus_package,
    ".sh": extract_bash,
    ".bash": extract_bash,
    ".json": extract_json,
    ".tf": extract_terraform,
    ".tfvars": extract_terraform,
    ".hcl": extract_terraform,
    ".dm": extract_dm,
    ".dme": extract_dm,
    ".dmi": extract_dmi,
    ".dmm": extract_dmm,
    ".dmf": extract_dmf,
    ".sln": extract_sln,
    ".slnx": extract_slnx,
    ".csproj": extract_csproj,
    ".fsproj": extract_csproj,
    ".vbproj": extract_csproj,
    ".xaml": extract_xaml,
    ".razor": extract_razor,
    ".cshtml": extract_razor,
    ".robot": extract_robot,
    ".resource": extract_robot,
    ".cls": extract_apex,
    ".trigger": extract_apex,
    ".proto": extract_protobuf,
    ".avsc": extract_avro,
    ".cbl": extract_cobol,
    ".cob": extract_cobol,
    ".cobol": extract_cobol,
    ".cpy": extract_cobol,
    ".sol": extract_solidity,
}


# Extensions whose extractor depends on an optional-dependency extra
# (pyproject [project.optional-dependencies]) and hard-fails without it,
# rather than falling back like Pascal does. Used by the #1745 warning in
# extract() to tell the user which extra restores the language.
_EXTRA_FOR_EXTENSION = {
    ".sql": "sql",
    ".tf": "terraform",
    ".tfvars": "terraform",
    ".hcl": "terraform",
    ".dm": "dm",
    ".dme": "dm",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".lisp": "commonlisp",
    ".cl": "commonlisp",
    ".lsp": "commonlisp",
    ".asd": "commonlisp",
    ".robot": "robot",
    ".resource": "robot",
}

# Substrings an extractor's error carries to classify why a dependency-backed
# file contributed nothing, used by the #1745 warning in extract(). A grammar
# that is present but fails to load (#2602) must not be reported as missing —
# the "install the extra" hint would be a no-op.
_DEP_MISSING_MARKER = "not installed"
_DEP_LOAD_FAILED_MARKER = "failed to load"


# Extensionless executables (CLI entry points like `devctl` or `manage`) carry
# their language in the shebang, not the suffix. detect.classify_file already
# routes them to the CODE path via _shebang_interpreter; _get_extractor must
# honor the same signal or these files are classified as code and then silently
# dropped by extraction. Only interpreters with a real extractor are mapped —
# detect's wider set (perl, fish, tcsh, Rscript) stays unmapped and skipped.
_SHEBANG_DISPATCH: dict[str, Any] = {
    "python": extract_python,
    "python2": extract_python,
    "python3": extract_python,
    "bash": extract_bash,
    "sh": extract_bash,
    "dash": extract_bash,
    "zsh": extract_bash,
    "ksh": extract_bash,
    "node": extract_js,
    "nodejs": extract_js,
    "ruby": extract_ruby,
    "lua": extract_lua,
    "php": extract_php,
    "julia": extract_julia,
}


# ObjC-only directives. They are illegal in C and C++, so finding one in a `.h`
# file is a near-zero-false-positive signal that the header is Objective-C (and so
# belongs to extract_objc, not extract_c). `@property` is deliberately excluded: it
# doubles as a Doxygen comment command and ObjC properties only ever live inside an
# @interface/@protocol anyway, so the stronger directives already cover them.
#
# `#import` is included because an ObjC *bridging* header is often nothing but
# `#import "X.h"` lines with no @interface (#1556). Routed to extract_c it parses
# `#import` as a `preproc_call` (not `preproc_include`), so every import edge is
# dropped and the header is isolated. `#import` is an ObjC-only directive (illegal
# in C and C++), so this won't hijack genuine C/C++ headers, and extract_objc
# resolves quoted imports via _resolve_c_include_path.
_OBJC_HEADER_MARKERS = (b"@interface", b"@protocol", b"@implementation", b"@import", b"#import")


def _is_objc_header(path: Path) -> bool:
    """Whether a `.h` file is Objective-C rather than C/C++ (#1475).

    `.h` is shared by C, C++, and ObjC; the suffix map routes it to extract_c,
    which silently drops every @interface/@protocol/@property/method (1 node, 0
    edges). Sniffing for an ObjC-only directive reroutes genuine ObjC headers to
    extract_objc while leaving every C/C++ header on its existing extractor.
    """
    try:
        head = path.read_bytes()[:256 * 1024]
    except OSError:
        return False
    return any(marker in head for marker in _OBJC_HEADER_MARKERS)


# C++-only signals. None of these are valid in a plain C header, so finding one
# in a `.h` is a high-confidence signal the header is C++ (#1547). The C grammar
# has no class_specifier, so a `class Foo { ... };` header routed to extract_c
# loses the class and its method prototypes (a junk `foo_foo` node + a sourceless
# `class` stub); routing to extract_cpp recovers the real type. Kept CONSERVATIVE:
# a plain C header with none of these stays on extract_c. ObjC sniffing keeps
# priority (an ObjC header can legitimately contain `::`/`class` inside an inline
# C++ block when compiled as Objective-C++).
_CPP_HEADER_MARKERS = (
    b"class ", b"namespace ", b"template", b"::",
    b"public:", b"private:", b"protected:",
)


def _is_objc_source(path: Path) -> bool:
    """Whether a `.m` file is Objective-C rather than MATLAB/Octave (#1702).

    `.m` is shared by Objective-C implementation files and MATLAB (also Octave).
    The suffix map routes `.m` to extract_objc unconditionally, which force-parses
    MATLAB through the Objective-C tree-sitter grammar and emits garbage nodes/edges
    (worse than skipping). A genuine ObjC `.m` always carries an ObjC directive
    (@implementation/@interface/@import/#import); MATLAB has none of them. Reuses
    the same marker set as the `.h` sniff. `.mm` is unambiguously Objective-C++ and
    is not sniffed.
    """
    return _is_objc_header(path)


def _is_cpp_header(path: Path) -> bool:
    """Whether a `.h` file is C++ rather than plain C (#1547).

    Mirrors `_is_objc_header`: sniffs for a C++-only token. Used only to reroute
    a `.h` from extract_c to extract_cpp when no ObjC marker is present (ObjC has
    priority). Conservative by construction — a plain C header matches nothing
    here and keeps its existing extract_c routing.
    """
    try:
        head = path.read_bytes()[:256 * 1024]
    except OSError:
        return False
    return any(marker in head for marker in _CPP_HEADER_MARKERS)


def _get_extractor(path: Path) -> Any | None:
    """Return the correct extractor function for a file, or None if unsupported."""
    if path.name.lower().endswith(".blade.php"):
        return extract_blade
    # MCP config files (.mcp.json, claude_desktop_config.json, ...) are routed
    # by filename before generic .json dispatch so they get MCP-aware nodes
    # (servers, commands, packages, env vars) instead of opaque JSON keys.
    if is_mcp_config_path(path):
        return extract_mcp_config
    # Package manifests (apm.yml, pyproject.toml, go.mod, pom.xml) → a canonical
    # package node + depends_on edges, by filename before generic suffix dispatch
    if is_package_manifest_path(path) and path.name.lower() != "package.json":
        return extract_package_manifest
    if path.suffix.lower() == ".txt":
        return extract_markdown
    # IaC & config extractors (Kubernetes, Helm, CloudFormation, Kafka, Argo CD)
    if is_helm_chart_file(path):
        return extract_helm
    if is_argocd_manifest(path):
        return extract_argocd
    if is_kubernetes_manifest(path):
        return extract_kubernetes
    if is_cloudformation_template(path):
        return extract_cloudformation
    if is_kafka_config(path):
        return extract_kafka_properties
    if path.name.lower() == "dockerfile" or path.name.lower().startswith("dockerfile.") or path.suffix.lower() == ".dockerfile":
        return extract_dockerfile
    if is_docker_compose(path):
        return extract_docker_compose
    if is_github_workflow(path):
        return extract_github_workflow
    if is_api_or_schema(path):
        return extract_api_or_schema
    if is_bazel_file(path):
        return extract_bazel
    if is_monorepo_file(path):
        return extract_monorepo
    if is_protobuf_file(path):
        return extract_protobuf
    # `.h` is C/C++/ObjC-ambiguous; route Objective-C headers to extract_objc
    # (the suffix map sends `.h` to extract_c, which can't read @interface etc.).
    # ObjC sniffing has priority over the C++ sniff: an Objective-C++ header can
    # contain both `@interface` and inline C++ (`::`), and it must parse as ObjC.
    suffix = path.suffix
    if suffix not in _DISPATCH and suffix.lower() in _DISPATCH:
        suffix = suffix.lower()
    if suffix == ".h":
        if _is_objc_header(path):
            return extract_objc
        # A C++ class header routed to extract_c loses the class entirely (the C
        # grammar has no class_specifier). Reroute to extract_cpp (#1547).
        if _is_cpp_header(path):
            return extract_cpp
    # `.m` is Objective-C OR MATLAB. extract_objc unconditionally would force-parse
    # MATLAB through the ObjC grammar into garbage (#1702). Route to extract_objc
    # only when the file actually looks like Objective-C; otherwise leave it without
    # an extractor (surfaced by the no-AST-extractor warning, #1689) rather than
    # mis-parsed. `.mm` is unambiguously Objective-C++ and stays on extract_objc.
    if suffix == ".m" and not _is_objc_source(path):
        return None
    # Extensionless files: resolve by shebang, mirroring detect.classify_file.
    # Without this, detect labels e.g. `#!/usr/bin/env bash` CLIs as code but
    # extraction returns no extractor and the file silently contributes nothing.
    if not suffix:
        from graph_fy.detect import _shebang_interpreter
        interp = _shebang_interpreter(path)
        if interp is not None:
            return _SHEBANG_DISPATCH.get(interp)
    return _DISPATCH.get(suffix)


_extract_file_ast = _get_extractor


_XAML_ACTIVE_EXTRACT_ROOT: Path | None = None


def _safe_extract_with_xaml_root(extractor, path: Path, root: Path) -> dict:
    global _XAML_ACTIVE_EXTRACT_ROOT
    from graph_fy.extractors.xaml import set_active_extract_root
    previous_root = _XAML_ACTIVE_EXTRACT_ROOT
    _XAML_ACTIVE_EXTRACT_ROOT = root.resolve()
    set_active_extract_root(root)
    try:
        return _safe_extract(extractor, path)
    finally:
        _XAML_ACTIVE_EXTRACT_ROOT = previous_root
        set_active_extract_root(previous_root)


def _extract_single_file(args: tuple) -> tuple[int, dict]:
    """Worker function for parallel extraction. Runs in a subprocess.

    Must be at module level (not a closure) so it can be pickled by
    ProcessPoolExecutor.

    Args:
        args: (index, path_str, root_str, cache_location_str) tuple. ``root``
            anchors hash keys / node ids / the XAML boundary; ``cache_location``
            is where the cache dir is written, decoupled per #1774. A legacy
            3-tuple (no cache_location) is still accepted for back-compat.

    Returns:
        (index, result_dict) so results can be placed back in order.
    """
    if len(args) == 4:
        idx, path_str, root_str, cache_location_str = args
    else:  # legacy 3-tuple: location == anchor
        idx, path_str, root_str = args
        cache_location_str = root_str
    path = Path(path_str)
    root = Path(root_str)
    cache_location = Path(cache_location_str)
    _raise_recursion_limit()
    bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES

    # Check cache first (avoid re-extraction)
    if not bypass_cache:
        cached = load_cached(path, root, cache_root=cache_location)
        if cached is not None:
            return idx, cached

    extractor = _get_extractor(path)
    if extractor is None:
        return idx, {"nodes": [], "edges": []}

    result = _safe_extract_with_xaml_root(extractor, path, root)
    # Never cache a zero-node result for an extractable file. Every supported
    # source produces at least a file node, so an empty node list is anomalous
    # (e.g. a transient batch/parallel hiccup). Caching it makes the empty
    # byte-stable across runs and silently blinds affected/explain to and
    # through the file (#1666); skipping the write lets a rerun self-heal.
    if not bypass_cache and "error" not in result and result.get("nodes"):
        save_cached(path, result, root, cache_root=cache_location)
    return idx, result


def _extract_parallel(
    uncached_work: list[tuple[int, Path]],
    per_file: list[dict | None],
    root: Path,
    max_workers: int | None,
    total_files: int,
    cache_location: Path | None = None,
) -> bool:
    """Extract uncached files in parallel using ProcessPoolExecutor.

    Returns True if the pool ran to completion. Returns False if the pool
    failed in a recoverable way (typically Windows-spawn without an
    ``if __name__ == "__main__"`` guard in the calling script, which causes
    BrokenProcessPool); the caller should fall back to sequential extraction.
    """
    import concurrent.futures

    if max_workers is None:
        # Honour graph_fy_MAX_WORKERS env override; otherwise scale to the
        # full CPU. The historical `, 8)` cap was a safety bound for laptops
        # in 2023 — on a 32-thread workstation it costs a 4x slowdown
        # (issue #792). Capping at len(uncached_work) keeps small jobs
        # from spawning useless idle workers.
        env_raw = os.environ.get("graph_fy_MAX_WORKERS", "").strip()
        env_cap = None
        if env_raw:
            try:
                v = int(env_raw)
                if v > 0:
                    env_cap = v
            except ValueError:
                pass
        cpu_cap = env_cap if env_cap is not None else (os.cpu_count() or 4)
        max_workers = min(cpu_cap, len(uncached_work))

    # Windows ProcessPoolExecutor hard-caps at 61 workers (CPython limitation
    # tied to WaitForMultipleObjects). Clamp here so every path — auto-compute,
    # graph_fy_MAX_WORKERS, and --max-workers — stays valid on >61-core boxes
    # (issue #1298). Guard against 0 from an empty work list.
    if sys.platform == "win32":
        max_workers = min(max_workers, 61)
    max_workers = max(max_workers, 1)

    # A one-worker pool buys no parallelism: it still pays process spawn plus an
    # IPC round trip per file, and it is the one residual case where the parent's
    # rebuild watchdog (os._exit) can orphan a worker that is mid-task. The
    # Windows post-commit hook exports graph_fy_MAX_WORKERS=1, so this is the
    # default there. Hand the work back so the caller extracts sequentially in
    # this process instead (#2173).
    if max_workers == 1:
        return False

    # root anchors hash keys / node ids / XAML boundary; cache_location is where
    # the cache dir is written (defaults to root when not decoupled) (#1774).
    root_str = str(root)
    cache_loc_str = str(cache_location if cache_location is not None else root)
    work_items = [(idx, str(path), root_str, cache_loc_str) for idx, path in uncached_work]

    done_count = 0
    failed: list[int] = []  # positions into uncached_work whose future failed
    _PROGRESS_INTERVAL = 100
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_extract_single_file, item): pos
                for pos, item in enumerate(work_items)
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, result = future.result()
                    per_file[idx] = result
                except concurrent.futures.process.BrokenProcessPool:
                    # #2444: a pool that dies while results are being consumed
                    # raises BrokenProcessPool from every pending future. It
                    # must reach the pool-level handler below (which returns
                    # False so the caller falls back to sequential), not be
                    # swallowed here per-future — that left the remaining
                    # per_file slots empty and silently dropped the files.
                    raise
                except Exception as exc:
                    pos = futures[future]
                    print(
                        f"  warning: worker failed for {work_items[pos][1]}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    failed.append(pos)
                done_count += 1
                if (
                    total_files >= _PROGRESS_INTERVAL
                    and done_count % _PROGRESS_INTERVAL == 0
                ):
                    print(
                        f"  AST extraction: {done_count}/{len(uncached_work)} uncached files "
                        f"({done_count * 100 // len(uncached_work)}%) [{max_workers} workers]",
                        flush=True,
                    )
    except concurrent.futures.process.BrokenProcessPool:
        # On Windows (spawn start method) the worker subprocesses re-import the
        # caller's __main__. Inline invocations like `python -c "..."` have no
        # __main__ guard, so worker bootstrap raises and the pool dies before
        # any work completes. Fall back to in-process sequential extraction —
        # slower but correct.
        print(
            "  warning: parallel extraction failed (BrokenProcessPool); "
            "falling back to sequential. On Windows this usually means the "
            'caller is missing an `if __name__ == "__main__":` guard. Pass '
            "parallel=False to extract() to skip the pool entirely.",
            flush=True,
        )
        return False
    except OSError as exc:
        # The pool could not even start. On macOS, leaked POSIX semaphores
        # exhaust kern.posix.sem.max and sem_open fails with ENOSPC ("No space
        # left on device", disk nowhere near full). Sequential needs no pool.
        print(
            f"  warning: parallel extraction unavailable ({exc}); "
            "falling back to sequential.",
            flush=True,
        )
        return False
    if failed:
        # #2445: retry per-future failures once, in-process, instead of leaving
        # their per_file slots None (which the defensive fill downstream turned
        # into well-formed empties — silent data loss). This is bounded, not a
        # loop: _extract_sequential goes through _safe_extract, which converts
        # a second failure into an error-carrying result.
        _extract_sequential(
            [uncached_work[pos] for pos in failed],
            per_file, root, total_files, cache_location,
        )
    if total_files >= _PROGRESS_INTERVAL:
        # Report the same denominator the intermediate lines used (uncached files
        # actually processed this run), not total_files — switching to the full
        # corpus made the count jump upward at the end (cached hits + files with no
        # extractor never entered uncached_work), which read as inconsistent (#1693).
        _done = len(uncached_work)
        print(
            f"  AST extraction: {_done}/{_done} uncached files (100%) [{max_workers} workers]",
            flush=True,
        )
    return True


def _extract_sequential(
    uncached_work: list[tuple[int, Path]],
    per_file: list[dict | None],
    root: Path,
    total_files: int,
    cache_location: Path | None = None,
) -> None:
    """Extract uncached files sequentially (fallback for small batches)."""
    _PROGRESS_INTERVAL = 100
    for work_idx, (idx, path) in enumerate(uncached_work):
        if (
            total_files >= _PROGRESS_INTERVAL
            and work_idx % _PROGRESS_INTERVAL == 0
            and work_idx > 0
        ):
            print(
                f"  AST extraction: {work_idx}/{len(uncached_work)} uncached files ({work_idx * 100 // len(uncached_work)}%)",
                flush=True,
            )
        extractor = _get_extractor(path)
        if extractor is None:
            per_file[idx] = {"nodes": [], "edges": []}
            continue
        bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES
        # XAML boundary anchors on `root` (the corpus), not the cache location.
        result = _safe_extract_with_xaml_root(extractor, path, root)
        # See _extract_single_file: don't cache an anomalous zero-node result (#1666).
        if not bypass_cache and "error" not in result and result.get("nodes"):
            save_cached(path, result, root, cache_root=cache_location)
        per_file[idx] = result
    if total_files >= _PROGRESS_INTERVAL:
        # Consistent denominator with the intermediate lines (#1693).
        _done = len(uncached_work)
        print(f"  AST extraction: {_done}/{_done} uncached files (100%)", flush=True)


_PARALLEL_THRESHOLD = 20



from graph_fy.pipeline.phase1_discovery import collect_files, infer_root_and_cache
from graph_fy.pipeline.phase2_workers import run_worker_phase
from graph_fy.pipeline.phase3_resolvers import run_resolution_phase
from graph_fy.pipeline.phase4_assembly import assemble_graph


def extract(
    paths: list[Path],
    cache_root: Path | None = None,
    *,
    root: Path | None = None,
    parallel: bool = True,
    max_workers: int | None = None,
    resolution_context_nodes: list[dict] | None = None,
    resolution_context_edges: list[dict] | None = None,
) -> dict:
    """Extract AST nodes and edges from a list of code files via a 4-phase pipeline."""
    paths = [Path(p) for p in paths]
    anchor_root = Path(root) if root is not None else None
    _check_tree_sitter_version()
    _raise_recursion_limit()
    _WORKSPACE_PACKAGE_CACHE.clear()
    _TSCONFIG_ALIAS_CACHE.clear()
    _TSCONFIG_BASEURL_CACHE.clear()
    _PACKAGE_IMPORTS_CACHE.clear()
    _XAML_CSHARP_CLASS_CACHE.clear()
    _MD_LINK_INDEX_CACHE.clear()
    _cached_realpath.cache_clear()
    _cached_source_key.cache_clear()

    scan_root, cache_location = infer_root_and_cache(paths, cache_root=cache_root, anchor_root=anchor_root)

    per_file, failed_sources = run_worker_phase(
        paths,
        scan_root,
        cache_location,
        parallel=parallel,
        max_workers=max_workers,
        get_extractor_fn=_get_extractor,
        extract_parallel_fn=_extract_parallel,
        extract_sequential_fn=_extract_sequential,
        prepare_terraform_fn=prepare_terraform,
    )

    all_nodes, all_edges = run_resolution_phase(
        paths,
        per_file,
        scan_root,
        resolution_context_nodes=resolution_context_nodes,
        resolution_context_edges=resolution_context_edges,
        file_node_id_fn=_file_node_id,
    )

    return assemble_graph(
        all_nodes,
        all_edges,
        scan_root,
        paths,
        failed_sources,
        file_node_id_fn=_file_node_id,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m graph_fy.extract <file_or_dir> ...", file=sys.stderr)
        sys.exit(1)

    paths: list[Path] = []
    for arg in sys.argv[1:]:
        paths.extend(collect_files(Path(arg)))

    result = extract(paths)
    print(json.dumps(result, indent=2))
