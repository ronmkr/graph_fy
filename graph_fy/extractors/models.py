"""models — moved verbatim from graph_fy/extract.py."""
from __future__ import annotations

from typing import Any, Callable
from pathlib import Path
from dataclasses import dataclass, field


_WORKSPACE_PACKAGE_CACHE: dict[str, dict[str, Path]] = {}

_JS_CACHE_BYPASS_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte"}

@dataclass
class LanguageConfig:
    ts_module: str                                   # e.g. "tree_sitter_python"
    ts_language_fn: str = "language"                 # attr to call: e.g. tslang.language()

    class_types: frozenset = frozenset()
    function_types: frozenset = frozenset()
    import_types: frozenset = frozenset()
    call_types: frozenset = frozenset()
    static_prop_types: frozenset = frozenset()
    helper_fn_names: frozenset = frozenset()
    container_bind_methods: frozenset = frozenset()
    event_listener_properties: frozenset = frozenset()

    # Name extraction
    name_field: str = "name"
    name_fallback_child_types: tuple = ()

    # Body detection
    body_field: str = "body"
    body_fallback_child_types: tuple = ()   # e.g. ("declaration_list", "compound_statement")

    # Call name extraction
    call_function_field: str = "function"           # field on call node for callee
    call_accessor_node_types: frozenset = frozenset()  # member/attribute nodes
    call_accessor_field: str = "attribute"          # field on accessor for method name
    call_accessor_object_field: str = ""            # field on accessor for the receiver/object

    # Stop recursion at these types in walk_calls
    function_boundary_types: frozenset = frozenset()

    # Import handler: called for import nodes instead of generic handling
    import_handler: Callable | None = None

    # Optional custom name resolver for functions (C, C++ declarator unwrapping)
    resolve_function_name_fn: Callable | None = None

    # Optional symbol name sanitizer for node ID generation (e.g. Ruby suffixed methods)
    sanitize_symbol_name_fn: Callable[[str], str] | None = None

    # Extra label formatting for functions: if True, functions get "name()" label
    function_label_parens: bool = True

    # Extra walk hook called after generic dispatch (for JS arrow functions, C# namespaces, etc.)
    extra_walk_fn: Callable | None = None

from graph_fy.resolvers.models import (
    _WORKSPACE_PACKAGE_CACHE,
    _JS_CACHE_BYPASS_SUFFIXES,
    _SymbolDeclarationFact,
    _SymbolImportFact,
    _SymbolAliasFact,
    _SymbolExportFact,
    _StarExportFact,
    _NamespaceExportFact,
    _SymbolUseFact,
    _SymbolResolutionFacts,
)
