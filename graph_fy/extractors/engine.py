"""engine — generic AST extraction substrate and re-exports for language extractors."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

from graph_fy.extractors.base import (
    _LANGUAGE_BUILTIN_GLOBALS,
    _file_stem,
    _make_id,
    _read_text,
    _semantic_reference_edge,
    _source_location,
    _find_body,
    REFERENCE_CONTEXTS,
)
from graph_fy.ids import normalize_id
from graph_fy.extractors.models import LanguageConfig
from graph_fy.extractors.resolution import _resolve_js_import_target
from graph_fy.security import sanitize_metadata

import graph_fy.extractors.csharp as _cs
import graph_fy.extractors.java as _jv
import graph_fy.extractors.swift as _sw
import graph_fy.extractors.kotlin as _kt
import graph_fy.extractors.ruby as _rb
import graph_fy.extractors.php as _ph
import graph_fy.extractors.c_cpp as _cc
import graph_fy.extractors.python as _py
import graph_fy.extractors.javascript as _js
import graph_fy.extractors.lua as _lu

# C# helpers
_csharp_namespace_id = _cs._csharp_namespace_id
_csharp_pre_scan_interfaces = _cs._csharp_pre_scan_interfaces
_csharp_classify_base = _cs._csharp_classify_base
_CSHARP_TYPE_PARAMETER_SCOPE_DECLARATIONS = _cs._CSHARP_TYPE_PARAMETER_SCOPE_DECLARATIONS
_csharp_type_parameters_in_scope = _cs._csharp_type_parameters_in_scope
_csharp_collect_type_refs = _cs._csharp_collect_type_refs
_csharp_attribute_names = _cs._csharp_attribute_names
_read_csharp_type_name = _cs._read_csharp_type_name
_csharp_bare_call_name = _cs._csharp_bare_call_name
_csharp_receiver_type_name = _cs._csharp_receiver_type_name
_csharp_method_receiver_types = _cs._csharp_method_receiver_types
_csharp_scoped_receiver_type = _cs._csharp_scoped_receiver_type
_csharp_namespace_name = _cs._csharp_namespace_name
_csharp_extra_walk = _cs._csharp_extra_walk

# Java helpers
_JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS = _jv._JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS
_java_type_parameters_in_scope = _jv._java_type_parameters_in_scope
_JAVA_BUILTIN_TYPES = _jv._JAVA_BUILTIN_TYPES
_java_collect_type_refs = _jv._java_collect_type_refs
_java_receiver_type_name = _jv._java_receiver_type_name
_java_declarator_names = _jv._java_declarator_names
_java_lambda_parameters = _jv._java_lambda_parameters
_java_method_receiver_types = _jv._java_method_receiver_types
_java_annotation_nodes = _jv._java_annotation_nodes
_java_annotation_names = _jv._java_annotation_names
_java_annotation_class_literal_refs = _jv._java_annotation_class_literal_refs
_java_extra_walk = _jv._java_extra_walk

# Swift helpers
_swift_declaration_keyword = _sw._swift_declaration_keyword
_swift_pre_scan = _sw._swift_pre_scan
_swift_classify_base = _sw._swift_classify_base
_swift_user_type_name = _sw._swift_user_type_name
_swift_collect_type_refs = _sw._swift_collect_type_refs
_swift_property_type_node = _sw._swift_property_type_node
_swift_attribute_type_name = _sw._swift_attribute_type_name
_swift_factory_call = _sw._swift_factory_call
_swift_property_name = _sw._swift_property_name
_swift_constructor_type = _sw._swift_constructor_type
_swift_receiver_name = _sw._swift_receiver_name
_swift_local_var_types = _sw._swift_local_var_types
_swift_extra_walk = _sw._swift_extra_walk

# Kotlin helpers
_kotlin_user_type_name = _kt._kotlin_user_type_name
_kotlin_collect_type_refs = _kt._kotlin_collect_type_refs
_kotlin_property_type_node = _kt._kotlin_property_type_node
_kotlin_function_return_type_node = _kt._kotlin_function_return_type_node
_scala_collect_type_refs = _kt._scala_collect_type_refs
_kotlin_extra_walk = _kt._kotlin_extra_walk
_kotlin_package_name = _kt._kotlin_package_name
_kotlin_nav_identifier_segments = _kt._kotlin_nav_identifier_segments

# Ruby helpers
_ruby_new_class_name = _rb._ruby_new_class_name
_ruby_local_class_bindings = _rb._ruby_local_class_bindings
_ruby_const_last_name = _rb._ruby_const_last_name
_ruby_const_full_name = _rb._ruby_const_full_name
_ruby_body_has_lookup_barrier = _rb._ruby_body_has_lookup_barrier
_ruby_file_has_refinement_barrier = _rb._ruby_file_has_refinement_barrier
_ruby_external_method_owners = _rb._ruby_external_method_owners
_ruby_extra_walk = _rb._ruby_extra_walk
_RUBY_CLASS_FACTORIES = _rb._RUBY_CLASS_FACTORIES

# PHP helpers
_php_name_text = _ph._php_name_text
_php_collect_type_refs = _ph._php_collect_type_refs
_php_method_return_type_node = _ph._php_method_return_type_node

# C/C++ helpers
_C_PRIMITIVE_TYPE_NODES = _cc._C_PRIMITIVE_TYPE_NODES
_c_collect_type_refs = _cc._c_collect_type_refs
_cpp_collect_type_refs = _cc._cpp_collect_type_refs
_get_cpp_func_name = _cc._get_cpp_func_name
_cpp_declarator_name = _cc._cpp_declarator_name
_cpp_local_var_types = _cc._cpp_local_var_types

# Python helpers
_PYTHON_TYPE_CONTAINERS = _py._PYTHON_TYPE_CONTAINERS
_PYTHON_ANNOTATION_NOISE = _py._PYTHON_ANNOTATION_NOISE
_PYTHON_DECORATOR_NOISE = _py._PYTHON_DECORATOR_NOISE
_python_collect_type_refs = _py._python_collect_type_refs
_python_collect_param_refs = _py._python_collect_param_refs
_python_param_names = _py._python_param_names
_python_collect_assignment_targets = _py._python_collect_assignment_targets
_python_local_bound_names = _py._python_local_bound_names
_python_module_bound_names = _py._python_module_bound_names
_scan_python_nested_function_declarations = _py._scan_python_nested_function_declarations
_python_decorator_name = _py._python_decorator_name
_python_pre_scan_underscore_collisions = _py._python_pre_scan_underscore_collisions
_python_underscore_salted_nid = _py._python_underscore_salted_nid

# JavaScript helpers
_js_pre_scan_member_collisions = _js._js_pre_scan_member_collisions
_js_member_salted_nid = _js._js_member_salted_nid
_js_collect_pattern_idents = _js._js_collect_pattern_idents
_js_local_bound_names = _js._js_local_bound_names
_js_module_bound_names = _js._js_module_bound_names
_js_import_binds_external = _js._js_import_binds_external
_js_external_import_names = _js._js_external_import_names
_js_dispatch_value_idents = _js._js_dispatch_value_idents
_dynamic_import_js = _js._dynamic_import_js
_ts_receiver_type_table = _js._ts_receiver_type_table
_find_require_call = _js._find_require_call
_require_imports_js = _js._require_imports_js
_scan_js_nested_function_declarations = _js._scan_js_nested_function_declarations
_js_topmost_closures = _js._js_topmost_closures
_js_member_assignment_target = _js._js_member_assignment_target
_js_scan_member_assignments = _js._js_scan_member_assignments
_js_extra_walk = _js._js_extra_walk
_ts_extra_walk = _js._ts_extra_walk
_ts_decorator_name = _js._ts_decorator_name
_ts_method_name = _js._ts_method_name
_ts_descendant_decorators = _js._ts_descendant_decorators
_ts_emit_decorator_edges = _js._ts_emit_decorator_edges
_JS_SCOPE_BOUNDARY = _js._JS_SCOPE_BOUNDARY
_JS_FUNCTION_VALUE_TYPES = _js._JS_FUNCTION_VALUE_TYPES

# Lua helpers
_lua_is_require_call = _lu._lua_is_require_call


def _first_parse_error_line(root) -> int:
    """1-based line of the first ERROR/MISSING node under ``root`` (#2551).

    Descends the first erroring child at each level (document order), so it
    lands on the earliest error region. Some recoveries set ``has_error``
    without materializing an ERROR/MISSING child (zero-width recovery); the
    deepest still-erroring node's line is reported for those.
    """
    node = root
    while True:
        if node.type == "ERROR" or node.is_missing:
            return node.start_point[0] + 1
        child = next((c for c in node.children if c.has_error), None)
        if child is None:
            return node.start_point[0] + 1
        node = child


def _has_multiline_error(root) -> bool:
    """True if any materialized ERROR node spans more than one line (a
    recovery region large enough to plausibly drop symbols, vs a tiny
    single-line recovery that extracts completely)."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" and n.end_point[0] > n.start_point[0]:
            return True
        stack.extend(c for c in n.children if c.has_error)
    return False


def _extract_generic(
    path: Path, config: LanguageConfig, *, source_override: bytes | None = None
) -> dict:
    """Generic AST extractor driven by LanguageConfig.

    ``source_override`` parses the given bytes instead of reading ``path``, while
    still keying nodes/edges off ``path``. Lets container formats (e.g. Vue SFCs)
    mask the wrapper and parse just the embedded ``<script>``.
    """
    try:
        mod = importlib.import_module(config.ts_module)
        from tree_sitter import Language, Parser
        lang_fn = getattr(mod, config.ts_language_fn, None)
        if lang_fn is None:
            # Fallback for PHP: try "language_php" then "language"
            lang_fn = getattr(mod, "language", None)
        if lang_fn is None:
            return {"nodes": [], "edges": [], "error": f"No language function in {config.ts_module}"}
        language = Language(lang_fn())
    except ImportError:
        return {"nodes": [], "edges": [], "error": f"{config.ts_module} not installed"}
    except TypeError as e:
        # tree-sitter version mismatch: old Language() expects (lib_path),
        # new Language() expects (language_capsule, name). Surface a hint
        # so users see the upgrade path instead of a bare TypeError.
        hint = (
            f"tree-sitter version mismatch for {config.ts_module}: {e}. "
            "Try: pip install --upgrade tree-sitter tree-sitter-languages"
        )
        return {"nodes": [], "edges": [], "error": hint}
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    try:
        parser = Parser(language)
        source = path.read_bytes() if source_override is None else source_override
        # In C and C++, if the .h file does not end with a newline '\n' an error
        # is throwed even if the file is valid. In order to avoid this, a new line
        # char is added only if the original file does not end with it.
        if source and not source.endswith(b"\n"):
            source = source + b"\n"
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    # Names bound by an import of a module outside the corpus. Module-scoped, so it
    # is computed once per file and consulted from every scope — see
    # `_js_external_import_names`.
    js_external_imports: set[str] = (
        _js_external_import_names(root, source, str_path)
        if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
        else set()
    )
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    namespace_stack: list[str] = []
    # Ruby only: enclosing module/class segments, so `module Foo::Bar` (compact)
    # and `module Foo; module Bar` (nested) label the same node `Foo::Bar` and
    # `include Foo::Bar` resolves for both spellings (#2302). Kept separate from
    # namespace_stack so Ruby method ids/labels are unchanged.
    ruby_namespace: list[str] = []
    # Exact Ruby Module.nesting frames. Compact `module A::B` contributes one
    # frame, unlike nested `module A; module B`, which contributes two.
    ruby_lexical_scopes: list[str] = []
    scope_stack: list[str] = []
    function_bodies: list[tuple[str, object]] = []
    # nids of function / method / class definitions in this file. The indirect-
    # dispatch guard (Python) resolves a call-argument identifier to an edge only
    # when it names one of these callable defs — never an arbitrary same-named
    # node — so `process(config)` can't manufacture an edge to a non-callable.
    callable_def_nids: set[str] = set()
    # Subset of callable_def_nids that are CLASS defs (callable only via their
    # constructor). Classes are frequently passed as descriptive values, not for
    # invocation (`select(Model)`, exception tuples), so the cross-file indirect_call
    # guard excludes them to avoid false edges (#2137).
    callable_class_nids: set[str] = set()
    # Python only: per-function set of locally-bound names (params + local
    # assignment / for / with-as / comprehension targets). The indirect-dispatch
    # guard skips any call-argument identifier in the enclosing function's set,
    # so a param/local that shadows a module function name yields no edge.
    local_bound_names: dict[str, set[str]] = {}
    # Python nested-function lexical scope tracking (#3405): maps child_nid -> parent_scope_nid
    scope_parents: dict[str, str] = {}
    # maps scope_nid -> {bare_func_name -> nested_func_nid}
    lexical_nids_by_scope: dict[str, dict[str, str]] = {}
    # JS/TS only (#2568): per-BODY locals for sibling closures tracked under a
    # single const nid by the #2552 branch (`const h = wrapper(cb1, cb2)`).
    # Keyed by id(body) — like receiver_types_by_body — and fed to the per-body
    # walk_calls as extra_locals, so each closure sees only its own
    # params/locals instead of a shared union that over-suppresses siblings.
    closure_locals_by_body: dict[int, set[str]] = {}
    pending_listen_edges: list[tuple[str, str, int]] = []
    # tree-sitter-swift parses both `class Foo` and `extension Foo` as
    # `class_declaration`. Same-file pairs collapse via seen_ids, but cross-file
    # extensions don't (file stem is part of the id), so they're collected here
    # for a corpus-level merge after every file has been parsed.
    swift_extensions: list[dict] = []
    # #1356: call expressions in property/field initializers (e.g.
    # `let vm = VM()`) live outside function bodies, so the call-walk never
    # reaches them. Collect (owner_nid, call_node) here and walk them too.
    initializer_nodes: list[tuple[str, object]] = []
    # Ruby include/extend/prepend mixins collected during the node walk (#1668),
    # merged into raw_calls after the call-walk populates it (raw_calls does not
    # exist yet while walk() runs). Resolved cross-file by the Ruby resolver.
    _ruby_mixin_calls: list[dict] = []
    # Ruby method declarations share one graph shape, even though instance and
    # singleton dispatch are distinct. Persist the parser-known kind so the
    # corpus resolver can fail closed instead of guessing across that boundary.
    ruby_singleton_context_depth = 0
    ruby_method_kinds: dict[str, set[str]] = {}
    ruby_method_counts: dict[str, int] = {}
    # #1356: per-file map of local name -> declared type (properties + params),
    # threaded out as `swift_type_table` so member calls (`vm.update()`) can be
    # resolved to the receiver's real definition in _resolve_swift_member_calls.
    type_table: dict[str, str] = {}
    # #2561: pending factory bindings (`let x = Factory.make()`), name ->
    # (FactoryType, method). Label-only (no nids, so the per-file AST cache
    # stays valid); resolved corpus-side in _resolve_swift_member_calls against
    # the factory method's marked plain return type.
    swift_factory_bindings: dict[str, tuple[str, str]] = {}
    # Java receiver typing is method-scoped: current-class fields are shared,
    # while parameters and locals belong only to their declaring method.
    java_field_types: dict[str, dict[str, str]] = {}
    java_method_scopes: dict[int, tuple[object, str]] = {}
    # C# receiver typing is method-scoped too (#2299): class fields/properties
    # are shared, parameters and locals belong only to their declaring method —
    # the old file-wide table let one method's untypable rebinding poison a
    # same-named, explicitly typed receiver in a different method.
    csharp_field_types: dict[str, dict[str, str]] = {}
    csharp_method_scopes: dict[int, tuple[object, str]] = {}
    csharp_caller_class: dict[str, str] = {}

    csharp_interface_names: set[str] = set()
    csharp_method_info: dict[tuple[int, int], tuple[str, int, bool]] = {}
    csharp_class_methods: dict[tuple[str, str], list[tuple[str, int]]] = {}
    if config.ts_module == "tree_sitter_c_sharp":
        csharp_interface_names = _csharp_pre_scan_interfaces(root, source)

    swift_protocol_names: set[str] = set()
    swift_class_names: set[str] = set()
    if config.ts_module == "tree_sitter_swift":
        swift_protocol_names, swift_class_names = _swift_pre_scan(root, source)

    python_underscore_groups: dict[str, set[str]] = {}
    if config.ts_module == "tree_sitter_python":
        python_underscore_groups = _python_pre_scan_underscore_collisions(root, source, stem)

    js_member_groups: dict[str, set[str]] = {}
    if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
        js_member_groups = _js_pre_scan_member_collisions(root, source, stem)

    def add_node(nid: str, label: str, line: int, *, node_type: str | None = None,
                 metadata: dict | None = None) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        merged = dict(metadata or {})
        if namespace_stack:
            merged.setdefault("namespace", ".".join(namespace_stack))
        if scope_stack and node_type != "namespace":
            merged.setdefault("scope_chain", list(scope_stack))
        node = {
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": str_path,
            "source_location": f"L{line}",
        }
        if node_type:
            node["type"] = node_type
        if merged:
            node["metadata"] = sanitize_metadata(merged)
        nodes.append(node)

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 context: str | None = None,
                 metadata: dict | None = None) -> None:
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        }
        if context:
            edge["context"] = context
        if metadata:
            edge["metadata"] = sanitize_metadata(metadata)
        edges.append(edge)

    def ensure_named_node(name: str, line: int) -> str:
        nid = _make_id(stem, ".".join(namespace_stack), name)
        if config.ts_module == "tree_sitter_python":
            nid = _python_underscore_salted_nid(nid, name, python_underscore_groups)
        elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
            nid = _js_member_salted_nid(nid, name, js_member_groups)
        if nid in seen_ids:
            return nid
        nid = _make_id(name)
        if nid not in seen_ids:
            # The name isn't defined in this file, so this is a cross-file reference
            # (e.g. a `Thing` type annotation imported from another module). Emit a
            # SOURCELESS stub — like the inheritance-base path below — so the
            # corpus-level rewire can collapse it onto the real definition. A sourced
            # stub here makes _disambiguate_colliding_node_ids bake the referencing
            # file's path (with extension) into the id and blocks the rewire, which is
            # the phantom-duplicate-node bug (#1402).
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "origin_file": str_path,
            })
        return nid

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)
    if config.ts_module == "tree_sitter_ruby":
        file_node = next(n for n in nodes if n["id"] == file_nid)
        file_metadata = {"ruby_resolution_schema": 1}
        if _ruby_body_has_lookup_barrier(
            root, source
        ) or _ruby_file_has_refinement_barrier(root, source):
            file_metadata["ruby_lookup_unsafe"] = True
        external_owners = _ruby_external_method_owners(root, source)
        if external_owners:
            file_metadata["ruby_external_method_owners"] = external_owners
        file_node["metadata"] = sanitize_metadata(file_metadata)

    def walk(node, parent_class_nid: str | None = None) -> None:
        nonlocal ruby_singleton_context_depth
        t = node.type

        # Lua: `require("mod")` used as a bare statement (no assignment) is
        # the dominant idiom in Neovim configs, but it doesn't fit any
        # *_declaration node type — it is just a function_call. Adding
        # function_call wholesale to import_types would swallow every plain
        # call (e.g. `vim.keymap.set(.., function() ... end)`), skipping the
        # walker's recursion into its callback and losing whatever is
        # declared inside. So this only fires for a call that IS require,
        # checked ahead of (and independently from) the import_types dispatch
        # below (#3320).
        if (config.ts_module == "tree_sitter_lua" and t == "function_call"
                and config.import_handler and _lua_is_require_call(node, source)):
            config.import_handler(node, source, file_nid, stem, edges, str_path, scope_stack)
            return

        # Import types
        if t in config.import_types:
            if config.import_handler:
                imported_modules = config.import_handler(node, source, file_nid, stem, edges, str_path, scope_stack)
                # Module-level import handlers (Swift) name a module, not a file
                # path, so there is no pre-existing node to anchor the edge to.
                # They return (id, label) pairs for which we materialize a
                # `type=module` node; otherwise build_from_json prunes every such
                # import edge as a dangling/external reference. The same module
                # imported from N files shares one id (file_type=code keeps
                # build.py validation happy; `type=module` exempts it from
                # id-disambiguation) so it collapses to one shared node (#1327).
                if imported_modules:
                    line = node.start_point[0] + 1
                    for mod_nid, mod_label in imported_modules:
                        if mod_nid not in seen_ids:
                            seen_ids.add(mod_nid)
                            nodes.append({
                                "id": mod_nid,
                                "label": mod_label,
                                "file_type": "code",
                                "type": "module",
                                "source_file": str_path,
                                "source_location": f"L{line}",
                            })
            # For export_statement: only return (skip children) if it's a re-export
            # (has a `from` source). Otherwise fall through to walk children which may
            # contain function_declaration, class_declaration, etc.
            if t == "export_statement":
                has_source = any(c.type == "string" for c in node.children)
                if not has_source:
                    for child in node.children:
                        walk(child, parent_class_nid)
            return

        # Class types
        if t in config.class_types:
            # Resolve class name
            name_node = node.child_by_field_name(config.name_field)
            if name_node is None:
                for child in node.children:
                    if child.type in config.name_fallback_child_types:
                        name_node = child
                        break
            if not name_node:
                return
            class_name = _read_text(name_node, source)
            ruby_absolute_declaration = (
                config.ts_module == "tree_sitter_ruby" and class_name.startswith("::")
            )
            # Ruby: fully qualify the module/class label with its enclosing
            # scope, splitting compact `Foo::Bar` names into segments so both
            # declaration styles converge on one `Foo::Bar` label (#2302).
            ruby_segments: list[str] = []
            if config.ts_module == "tree_sitter_ruby":
                ruby_segments = class_name.split("::")
                class_name = "::".join(ruby_namespace + ruby_segments)
            class_nid = _make_id(stem, ".".join(namespace_stack), class_name)
            if config.ts_module == "tree_sitter_python" and parent_class_nid:
                class_nid = _python_underscore_salted_nid(
                    _make_id(parent_class_nid, class_name), class_name, python_underscore_groups, kind="class"
                )
            elif config.ts_module == "tree_sitter_python":
                class_nid = _python_underscore_salted_nid(
                    class_nid, class_name, python_underscore_groups, kind="class"
                )
            elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                class_nid = _js_member_salted_nid(class_nid, class_name, js_member_groups)
            line = node.start_point[0] + 1
            metadata = None
            ruby_reopened = (
                config.ts_module == "tree_sitter_ruby" and class_nid in seen_ids
            )
            if config.ts_module == "tree_sitter_c_sharp":
                if parent_class_nid:
                    metadata = {"is_nested_type": True}
                # #2332: `partial class Foo` split across files mints one node
                # per file (the id carries the file stem). Stamp the halves so
                # the corpus-level _merge_csharp_partial_class_nodes pass can
                # collapse them onto one canonical node. Grammar: `partial` is
                # a `modifier` direct child of the type declaration.
                if t in (
                    "class_declaration",
                    "struct_declaration",
                    "interface_declaration",
                    "record_declaration",
                ) and any(
                    c.type == "modifier" and _read_text(c, source) == "partial"
                    for c in node.children
                ):
                    metadata = dict(metadata or {})
                    metadata["is_partial"] = True
                if t in (
                    "class_declaration",
                    "struct_declaration",
                    "interface_declaration",
                    "record_declaration",
                ):
                    c_body = node.child_by_field_name("body")
                    if not c_body:
                        for c in node.children:
                            if c.type == "declaration_list":
                                c_body = c
                                break
                    if c_body:
                        m_by_name: dict[str, list[tuple[object, int, str]]] = {}
                        for member in c_body.children:
                            if member.type == "method_declaration":
                                mn_node = member.child_by_field_name("name")
                                if not mn_node:
                                    continue
                                m_raw = _read_text(mn_node, source)
                                if not m_raw:
                                    continue
                                m_san = (
                                    config.sanitize_symbol_name_fn(m_raw)
                                    if config.sanitize_symbol_name_fn is not None
                                    else m_raw
                                )
                                plist = member.child_by_field_name("parameters")
                                ar = (
                                    sum(1 for p in plist.children if p.type == "parameter")
                                    if plist
                                    else 0
                                )
                                m_by_name.setdefault(m_san, []).append((member, ar, m_raw))

                        for m_san, mlist in m_by_name.items():
                            is_ov = len(mlist) >= 2
                            seen_ar: set[int] = set()
                            for idx, (mnode, ar, m_raw) in enumerate(mlist):
                                if is_ov:
                                    if ar not in seen_ar:
                                        m_nid = _make_id(class_nid, f"{m_san}_a{ar}")
                                        seen_ar.add(ar)
                                    else:
                                        m_nid = _make_id(class_nid, f"{m_san}_a{ar}_{idx}")
                                else:
                                    m_nid = _make_id(class_nid, m_san)
                                m_key = (mnode.start_byte, mnode.end_byte)
                                csharp_method_info[m_key] = (m_nid, ar, is_ov)
                                csharp_class_methods.setdefault((class_nid, m_san), []).append((m_nid, ar))
                                if m_raw != m_san:
                                    csharp_class_methods.setdefault((class_nid, m_raw), []).append((m_nid, ar))
            if config.ts_module == "tree_sitter_ruby":
                ruby_body = _find_body(node, config)
                if ruby_absolute_declaration or ruby_reopened or (
                    ruby_body is not None
                    and _ruby_body_has_lookup_barrier(ruby_body, source)
                ):
                    metadata = dict(metadata or {})
                    if ruby_absolute_declaration:
                        metadata["ruby_lookup_unsafe"] = True
                    if ruby_reopened:
                        metadata["ruby_reopened"] = True
                    if ruby_body is not None and _ruby_body_has_lookup_barrier(
                        ruby_body, source
                    ):
                        metadata["ruby_lookup_unsafe"] = True
            add_node(class_nid, class_name, line, metadata=metadata)
            if config.ts_module == "tree_sitter_ruby" and metadata:
                # Reopened declarations collapse onto the same file-local id;
                # merge fail-closed markers that add_node intentionally skips.
                class_node = next(n for n in nodes if n["id"] == class_nid)
                class_metadata = dict(class_node.get("metadata") or {})
                class_metadata.update(metadata)
                class_node["metadata"] = sanitize_metadata(class_metadata)
            callable_def_nids.add(class_nid)  # a class is callable (constructor)
            callable_class_nids.add(class_nid)  # ...but only via its constructor (#2137)
            # A nested class/object/trait is contained by its ENCLOSING type, not
            # the file (#2040). parent_class_nid is threaded down the walk for
            # every language and is always a real class-like node (never a
            # namespace — namespace handlers pass it through unchanged), so it is
            # a valid edge source. The `!= class_nid` guard avoids a self-loop
            # when same-name nesting (`class Foo: class Foo`) collides ids, since
            # class ids omit the enclosing type name. Top-level types (parent
            # None) still source from the file, keeping the containment tree
            # connected: file -> Outer -> Inner.
            if parent_class_nid and parent_class_nid != class_nid:
                add_edge(parent_class_nid, class_nid, "contains", line)
            else:
                add_edge(file_nid, class_nid, "contains", line)

            # TS/JS decorators on the class and its members (@Component, @Injectable,
            # @Input, @Inject, @Entity, …). Decorators live only in class subtrees.
            if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                _ts_emit_decorator_edges(node, class_nid, stem, source,
                                         ensure_named_node, add_edge, js_member_groups)

            if config.ts_module == "tree_sitter_swift" and any(
                c.type == "extension" for c in node.children
            ):
                swift_extensions.append({"nid": class_nid, "label": class_name})

            # Python-specific: inheritance
            if config.ts_module == "tree_sitter_python":
                args = node.child_by_field_name("superclasses")
                if args:
                    for arg in args.children:
                        if arg.type == "identifier":
                            base = _read_text(arg, source)
                            base_nid = ensure_named_node(base, line)
                            add_edge(class_nid, base_nid, "inherits", line)

            # Swift-specific: conformance / inheritance
            if config.ts_module == "tree_sitter_swift":
                swift_kind = _swift_declaration_keyword(node) if t == "class_declaration" else "protocol"
                seen_swift_base = False
                for child in node.children:
                    if child.type != "inheritance_specifier":
                        continue
                    base_name: str | None = None
                    user_type_node = None
                    for sub in child.children:
                        if sub.type == "user_type":
                            user_type_node = sub
                            base_name = _swift_user_type_name(sub, source)
                            break
                        if sub.type == "type_identifier":
                            base_name = _read_text(sub, source) or None
                            break
                    if not base_name:
                        continue
                    base_nid = _make_id(stem, base_name)
                    if base_nid not in seen_ids:
                        base_nid = _make_id(base_name)
                        if base_nid not in seen_ids:
                            nodes.append({
                                "id": base_nid,
                                "label": base_name,
                                "file_type": "code",
                                "source_file": "",
                                "source_location": "",
                            })
                            seen_ids.add(base_nid)
                    if t == "protocol_declaration":
                        relation = "inherits"
                    else:
                        relation = _swift_classify_base(
                            base_name, swift_kind, not seen_swift_base,
                            swift_protocol_names, swift_class_names,
                        )
                    seen_swift_base = True
                    add_edge(class_nid, base_nid, relation, line)
                    if user_type_node is not None:
                        for arg_child in user_type_node.children:
                            if arg_child.type != "type_arguments":
                                continue
                            for arg in arg_child.children:
                                if not arg.is_named:
                                    continue
                                refs: list[tuple[str, str]] = []
                                _swift_collect_type_refs(arg, source, True, refs)
                                for ref_name, _role in refs:
                                    target = ensure_named_node(ref_name, line)
                                    add_edge(class_nid, target, "references", line,
                                             context="generic_arg")

            # PHP-specific: extends → inherits, implements → implements, use → mixes_in
            if config.ts_module == "tree_sitter_php":
                def _php_emit_base(base_name: str, rel: str, at_line: int) -> None:
                    if not base_name:
                        return
                    base_nid = _make_id(stem, base_name)
                    if base_nid not in seen_ids:
                        base_nid = _make_id(base_name)
                        if base_nid not in seen_ids:
                            nodes.append({
                                "id": base_nid,
                                "label": base_name,
                                "file_type": "code",
                                "source_file": "",
                                "source_location": "",
                            })
                            seen_ids.add(base_nid)
                    add_edge(class_nid, base_nid, rel, at_line)

                for child in node.children:
                    if child.type == "base_clause":
                        for sub in child.children:
                            if sub.type in ("name", "qualified_name"):
                                _php_emit_base(_php_name_text(sub, source) or "",
                                                "inherits", child.start_point[0] + 1)
                    elif child.type == "class_interface_clause":
                        for sub in child.children:
                            if sub.type in ("name", "qualified_name"):
                                _php_emit_base(_php_name_text(sub, source) or "",
                                                "implements", child.start_point[0] + 1)
                body = node.child_by_field_name("body")
                if body is None:
                    for c in node.children:
                        if c.type == "declaration_list":
                            body = c
                            break
                if body is not None:
                    for member in body.children:
                        if member.type != "use_declaration":
                            continue
                        for sub in member.children:
                            if sub.type in ("name", "qualified_name"):
                                _php_emit_base(_php_name_text(sub, source) or "",
                                                "mixes_in", member.start_point[0] + 1)

            # Kotlin-specific: delegation_specifiers → inherits (constructor_invocation) / implements (user_type)
            if config.ts_module == "tree_sitter_kotlin":
                for child in node.children:
                    if child.type != "delegation_specifiers":
                        continue
                    for spec in child.children:
                        if spec.type != "delegation_specifier":
                            continue
                        relation = "implements"
                        user_type_node = None
                        for sub in spec.children:
                            if sub.type == "constructor_invocation":
                                relation = "inherits"
                                for inner in sub.children:
                                    if inner.type == "user_type":
                                        user_type_node = inner
                                        break
                                break
                            if sub.type == "user_type":
                                user_type_node = sub
                                break
                            # `class Foo : Bar by baz` wraps the delegated
                            # interface `Bar` in an `explicit_delegation`
                            # node; grab its first `user_type` descendant so
                            # the implements edge (and generic-arg recovery)
                            # still fire.
                            if sub.type == "explicit_delegation":
                                for inner in sub.children:
                                    if inner.type == "user_type":
                                        user_type_node = inner
                                        break
                                break
                        if user_type_node is None:
                            continue
                        base = _kotlin_user_type_name(user_type_node, source)
                        if not base:
                            continue
                        base_nid = ensure_named_node(base, line)
                        add_edge(class_nid, base_nid, relation, line)
                        for arg_child in user_type_node.children:
                            if arg_child.type != "type_arguments":
                                continue
                            for arg in arg_child.children:
                                if arg.type == "type_projection":
                                    for inner in arg.children:
                                        if not inner.is_named:
                                            continue
                                        refs: list[tuple[str, str]] = []
                                        _kotlin_collect_type_refs(inner, source, True, refs)
                                        for ref_name, _role in refs:
                                            target = ensure_named_node(ref_name, line)
                                            add_edge(class_nid, target, "references", line,
                                                     context="generic_arg")

            # Ruby: `class Dog < Animal` puts the base class in the `superclass`
            # field (a `<` token followed by a constant or scope_resolution).
            # There was no Ruby branch, so every Ruby inherits edge was dropped.
            if config.ts_module == "tree_sitter_ruby":
                sup = node.child_by_field_name("superclass")
                if sup is not None:
                    base = ""
                    raw_base = ""
                    for sub in sup.children:
                        if sub.type == "constant":
                            base = _read_text(sub, source)
                            raw_base = base
                            break
                        if sub.type == "scope_resolution":
                            raw_base = _read_text(sub, source).strip()
                            consts = [c for c in sub.children if c.type == "constant"]
                            if consts:
                                base = _read_text(consts[-1], source)
                            break
                    if base:
                        base_nid = ensure_named_node(base, line)
                        add_edge(
                            class_nid,
                            base_nid,
                            "inherits",
                            line,
                            metadata={
                                "ruby_superclass_ref": raw_base,
                                "ruby_lexical_scopes": list(
                                    reversed(ruby_lexical_scopes)
                                ),
                            },
                        )

                # `include`/`extend`/`prepend <Const>` in the class/module body ->
                # a `mixes_in` edge to the module (#1668). The module usually lives
                # in another file, so defer resolution to the cross-file Ruby
                # resolver (reusing the #1634 candidate logic and the #1640 module
                # nodes as targets). Only bare/namespaced constant arguments count;
                # `extend self`, `include some_var`, etc. are skipped.
                _rb_body = _find_body(node, config)
                if _rb_body is not None:
                    for _stmt in _rb_body.children:
                        if _stmt.type != "call" or _stmt.child_by_field_name("receiver") is not None:
                            continue
                        _m = _stmt.child_by_field_name("method")
                        if _m is None or _read_text(_m, source) not in ("include", "extend", "prepend"):
                            continue
                        _args = _stmt.child_by_field_name("arguments")
                        if _args is None:
                            continue
                        for _arg in _args.children:
                            if _arg.type not in ("constant", "scope_resolution"):
                                continue
                            # Full path, not last segment: `include Foo::Bar`
                            # must reference `Foo::Bar`, and truncating
                            # `ActiveSupport::Concern` to `Concern` fabricated
                            # edges to any local `Concern` module (#2302).
                            _mod = _ruby_const_full_name(_arg, source)
                            if _mod:
                                _ruby_mixin_calls.append({
                                    "caller_nid": class_nid,
                                    "callee": _mod,
                                    "is_mixin": True,
                                    "source_file": str_path,
                                    "source_location": f"L{_stmt.start_point[0] + 1}",
                                })

            # C#-specific: inheritance / interface implementation via base_list
            if config.ts_module == "tree_sitter_c_sharp":
                csharp_type_params = _csharp_type_parameters_in_scope(node, source)
                for child in node.children:
                    if child.type != "base_list":
                        continue
                    for sub in child.children:
                        if sub.type not in ("identifier", "generic_name", "qualified_name"):
                            continue
                        base_info = _read_csharp_type_name(sub, source)
                        if base_info is None:
                            continue
                        base, qualified, qualifier = base_info
                        if not base or base in csharp_type_params:
                            continue
                        base_nid = _make_id(stem, ".".join(namespace_stack), base)
                        if base_nid not in seen_ids:
                            base_nid = _make_id(base)
                            if base_nid not in seen_ids:
                                nodes.append({
                                    "id": base_nid,
                                    "label": base,
                                    "file_type": "code",
                                    "source_file": "",
                                    "source_location": "",
                                })
                                seen_ids.add(base_nid)
                        # An `interface`'s base_list holds base interfaces, so every
                        # entry is interface inheritance (`inherits`) -- the same way the
                        # Java extractor treats `extends_interfaces`. Only class/struct/
                        # record declarations use the name-based class-vs-interface split.
                        if t == "interface_declaration":
                            relation = "inherits"
                        else:
                            relation = _csharp_classify_base(base, csharp_interface_names)
                        metadata = {"ref_token": base}
                        if qualified:
                            metadata["qualified"] = True
                        if qualifier:
                            metadata["ref_qualifier"] = qualifier
                        add_edge(class_nid, base_nid, relation, line, metadata=metadata)
                        if sub.type == "generic_name":
                            for tal in sub.children:
                                if tal.type != "type_argument_list":
                                    continue
                                for arg in tal.children:
                                    if not arg.is_named:
                                        continue
                                    refs: list[tuple[str, str, bool, str]] = []
                                    _csharp_collect_type_refs(
                                        arg, source, True, refs, csharp_type_params
                                    )
                                    for ref_name, _role, ref_qualified, ref_qualifier in refs:
                                        target = ensure_named_node(ref_name, line)
                                        metadata = {"ref_token": ref_name}
                                        if ref_qualified:
                                            metadata["qualified"] = True
                                        if ref_qualifier:
                                            metadata["ref_qualifier"] = ref_qualifier
                                        add_edge(class_nid, target, "references", line,
                                                 context="generic_arg", metadata=metadata)

            # Java-specific: extends (superclass) / implements (interfaces) / interface-extends
            if config.ts_module in ("tree_sitter_java", "tree_sitter_groovy"):
                def _emit_java_parent(base_name: str, rel: str, at_line: int) -> None:
                    if not base_name:
                        return
                    base_nid = _make_id(stem, base_name)
                    if base_nid not in seen_ids:
                        base_nid = _make_id(base_name)
                        if base_nid not in seen_ids:
                            nodes.append({
                                "id": base_nid,
                                "label": base_name,
                                "file_type": "code",
                                "source_file": "",
                                "source_location": "",
                            })
                            seen_ids.add(base_nid)
                    add_edge(class_nid, base_nid, rel, at_line)

                def _emit_java_parent_type(type_node, rel: str, at_line: int) -> None:
                    refs: list[tuple[str, str]] = []
                    _java_collect_type_refs(type_node, source, False, refs)
                    parent_emitted = False
                    for ref_name, role in refs:
                        if role == "type" and not parent_emitted:
                            _emit_java_parent(ref_name, rel, at_line)
                            parent_emitted = True
                        elif role == "generic_arg":
                            target_nid = ensure_named_node(ref_name, at_line)
                            if target_nid != class_nid:
                                add_edge(class_nid, target_nid, "references", at_line,
                                         context="generic_arg")

                sup = node.child_by_field_name("superclass")
                if sup is not None:
                    for sub in sup.children:
                        if sub.is_named:
                            _emit_java_parent_type(sub, "inherits", line)
                            break

                ifs = node.child_by_field_name("interfaces")
                if ifs is not None:
                    for sub in ifs.children:
                        if sub.type == "type_list":
                            for tid in sub.children:
                                if tid.is_named:
                                    _emit_java_parent_type(tid, "implements", line)

                if t == "interface_declaration":
                    for child in node.children:
                        if child.type == "extends_interfaces":
                            for sub in child.children:
                                if sub.type == "type_list":
                                    for tid in sub.children:
                                        if tid.is_named:
                                            _emit_java_parent_type(tid, "inherits", line)

                annotation_targets: set[str] = set()
                for anno_name, anno_raw in _java_annotation_names(node, source):
                    # An inline-qualified annotation (`@org.pkg.Foo`) keeps its
                    # full dotted name so a bare same-named local class can't
                    # absorb it; _resolve_java_type_references maps internal
                    # FQNs back to their real nodes (#2504). Groovy has no such
                    # resolver pass, so it keeps the legacy bare-name stub.
                    if "." in anno_raw and config.ts_module == "tree_sitter_java":
                        anno_name = anno_raw
                    target_nid = ensure_named_node(anno_name, line)
                    if target_nid != class_nid and target_nid not in annotation_targets:
                        add_edge(class_nid, target_nid, "references", line,
                                 context="attribute")
                        annotation_targets.add(target_nid)
                for ref_name in _java_annotation_class_literal_refs(node, source):
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != class_nid and target_nid not in annotation_targets:
                        add_edge(class_nid, target_nid, "references", line,
                                 context="attribute")
                        annotation_targets.add(target_nid)

                if t == "record_declaration":
                    components = node.child_by_field_name("parameters")
                    if components is not None:
                        for component in components.children:
                            if component.type == "formal_parameter":
                                type_node = component.child_by_field_name("type")
                            elif component.type == "spread_parameter":
                                type_node = next(
                                    (
                                        child
                                        for child in component.children
                                        if child.is_named
                                        and child.type not in ("modifiers", "variable_declarator")
                                    ),
                                    None,
                                )
                            else:
                                continue
                            refs: list[tuple[str, str]] = []
                            _java_collect_type_refs(type_node, source, False, refs)
                            component_line = component.start_point[0] + 1
                            for ref_name, role in refs:
                                ctx = "generic_arg" if role == "generic_arg" else "field"
                                target_nid = ensure_named_node(ref_name, component_line)
                                if target_nid != class_nid:
                                    add_edge(class_nid, target_nid, "references",
                                             component_line, context=ctx)

            # Scala: extends_clause carries `extends Base with Trait1 with Trait2`.
            # The first base after `extends` is `inherits`; each subsequent
            # type after `with` is `mixes_in`. Also walk class_parameters for
            # constructor-as-field type references.
            if config.ts_module == "tree_sitter_scala":
                extend = node.child_by_field_name("extend")
                if extend is None:
                    for c in node.children:
                        if c.type == "extends_clause":
                            extend = c
                            break
                if extend is not None:
                    bases: list[tuple[str, int]] = []

                    def scala_base_name(type_node) -> str | None:
                        if type_node.type == "type_identifier":
                            return _read_text(type_node, source)
                        if type_node.type == "stable_type_identifier":
                            tail = next(
                                (
                                    child
                                    for child in reversed(type_node.children)
                                    if child.type in ("type_identifier", "identifier")
                                ),
                                None,
                            )
                            return _read_text(tail, source) if tail is not None else None
                        if type_node.type == "generic_type":
                            base = type_node.child_by_field_name("type")
                            if base is None:
                                base = next(
                                    (
                                        child
                                        for child in type_node.children
                                        if child.type
                                        in ("type_identifier", "stable_type_identifier")
                                    ),
                                    None,
                                )
                            return scala_base_name(base) if base is not None else None
                        return None

                    for c in extend.children:
                        base_name = scala_base_name(c)
                        if base_name is not None:
                            bases.append((base_name, c.start_point[0] + 1))
                    for idx, (base_name, base_line) in enumerate(bases):
                        rel = "inherits" if idx == 0 else "mixes_in"
                        base_nid = ensure_named_node(base_name, base_line)
                        if base_nid != class_nid:
                            add_edge(class_nid, base_nid, rel, base_line)
                for c in node.children:
                    if c.type != "class_parameters":
                        continue
                    for cp in c.children:
                        if cp.type != "class_parameter":
                            continue
                        ptype = cp.child_by_field_name("type")
                        if ptype is None:
                            continue
                        cp_line = cp.start_point[0] + 1
                        refs: list[tuple[str, str]] = []
                        _scala_collect_type_refs(ptype, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "field"
                            target_nid = ensure_named_node(ref_name, cp_line)
                            if target_nid != class_nid:
                                add_edge(class_nid, target_nid, "references",
                                         cp_line, context=ctx)

            # C#: a primary constructor (`class Foo(IBar bar)`, C# 12+) declares
            # its dependencies on the type declaration itself rather than in a
            # field or property, so neither the field_declaration nor the
            # property_declaration handler ever sees them — the parameter type
            # got no references edge, and because the name was never registered
            # in csharp_field_types, _csharp_method_receiver_types could not type
            # the receiver either, so calls through it (`bar.Baz()`) lost their
            # calls edge as well. The Scala class_parameters branch directly
            # above is the analogue; Kotlin's is #2063. Grammar note: the list is
            # an UNNAMED child of the declaration, so child_by_field_name(
            # "parameters") returns None and the children must be scanned.
            if config.ts_module == "tree_sitter_c_sharp" and t in (
                "class_declaration",
                "record_declaration",
                "struct_declaration",
            ):
                csharp_type_params = _csharp_type_parameters_in_scope(node, source)
                for c in node.children:
                    if c.type != "parameter_list":
                        continue
                    for param in c.children:
                        if param.type != "parameter":
                            continue
                        ptype = param.child_by_field_name("type")
                        if ptype is None:
                            continue
                        pname = param.child_by_field_name("name")
                        p_line = param.start_point[0] + 1
                        # Receiver binding mirrors the field_declaration rule:
                        # Pascal-case only (a primitive owns no resolvable
                        # method) and never a bare type parameter (`T item`).
                        recv = _csharp_receiver_type_name(ptype, source)
                        if (pname is not None and recv and recv[:1].isupper()
                                and recv not in csharp_type_params):
                            csharp_field_types.setdefault(class_nid, {})[
                                _read_text(pname, source)
                            ] = recv
                        refs = []
                        _csharp_collect_type_refs(
                            ptype, source, False, refs, csharp_type_params
                        )
                        for ref_name, role, qualified, qualifier in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "field"
                            target_nid = ensure_named_node(ref_name, p_line)
                            if target_nid != class_nid:
                                metadata = {"ref_token": ref_name}
                                if qualified:
                                    metadata["qualified"] = True
                                if qualifier:
                                    metadata["ref_qualifier"] = qualifier
                                add_edge(class_nid, target_nid, "references",
                                         p_line, context=ctx, metadata=metadata)

            # C++-specific: inheritance via base_class_clause (class and struct).
            # tree-sitter-cpp shape:
            #   class_specifier / struct_specifier
            #     base_class_clause
            #       access_specifier? ("public"/"protected"/"private")  -- skip
            #       "virtual"?                                          -- skip
            #       type_identifier                                     -- "Base"
            #       qualified_identifier                                -- "ns::Base"
            #       template_type                                       -- "Vec<int>"
            # Multiple bases are siblings separated by ',' tokens.
            if config.ts_module == "tree_sitter_cpp":
                for child in node.children:
                    if child.type != "base_class_clause":
                        continue
                    for sub in child.children:
                        base = ""
                        template_args_node = None
                        if sub.type == "type_identifier":
                            base = _read_text(sub, source)
                        elif sub.type == "qualified_identifier":
                            # Use the unqualified tail so "std::vector" matches
                            # a "vector" node id if one exists in the graph;
                            # fall back to the full qualified text otherwise.
                            tail = sub.child_by_field_name("name")
                            base = _read_text(tail, source) if tail else _read_text(sub, source)
                        elif sub.type == "template_type":
                            tname = sub.child_by_field_name("name")
                            base = _read_text(tname, source) if tname else _read_text(sub, source)
                            # The base's template_argument_list carries generic
                            # type arguments (class Car : public Base<Dep>). The
                            # Java handler (_emit_java_parent_type) emits these as
                            # generic_arg references; C++ dropped them because we
                            # only emitted the `inherits` edge on the base name.
                            template_args_node = sub.child_by_field_name("arguments")
                        else:
                            continue
                        if not base:
                            continue
                        base_nid = ensure_named_node(base, line)
                        add_edge(class_nid, base_nid, "inherits", line)
                        # Emit a generic_arg reference for each type argument on the
                        # base (Base<Dep> -> Car references Dep). _cpp_collect_type_refs
                        # handles nested/qualified args (Base<std::vector<Dep>>) too.
                        if template_args_node is not None:
                            arg_refs: list[tuple[str, str]] = []
                            for arg in template_args_node.children:
                                if arg.is_named:
                                    _cpp_collect_type_refs(arg, source, True, arg_refs)
                            for ref_name, _role in arg_refs:
                                target_nid = ensure_named_node(ref_name, line)
                                if target_nid != class_nid:
                                    add_edge(class_nid, target_nid, "references",
                                             line, context="generic_arg")

            # Find body and recurse. Ruby pushes its scope segments so nested
            # declarations qualify against the enclosing module/class (#2302);
            # ruby_segments is empty for every other language.
            body = _find_body(node, config)
            if body:
                ruby_namespace.extend(ruby_segments)
                if config.ts_module == "tree_sitter_ruby":
                    ruby_lexical_scopes.append(class_name)
                previous_singleton_depth = ruby_singleton_context_depth
                if config.ts_module == "tree_sitter_ruby":
                    ruby_singleton_context_depth = 0
                try:
                    for child in body.children:
                        walk(child, parent_class_nid=class_nid)
                finally:
                    ruby_singleton_context_depth = previous_singleton_depth
                    if config.ts_module == "tree_sitter_ruby":
                        ruby_lexical_scopes.pop()
                    if ruby_segments:
                        del ruby_namespace[-len(ruby_segments):]
            return

        # Event listener property arrays: $listen = [Event::class => [Listener::class]]
        if (t == "property_declaration"
                and parent_class_nid
                and config.event_listener_properties):
            handled_event_listener = False
            for element in node.children:
                if element.type != "property_element":
                    continue
                prop_name: str | None = None
                array_node = None
                for c in element.children:
                    if c.type == "variable_name":
                        for sc in c.children:
                            if sc.type == "name":
                                prop_name = _read_text(sc, source)
                                break
                    elif c.type == "array_creation_expression":
                        array_node = c
                if (prop_name is None
                        or prop_name not in config.event_listener_properties
                        or array_node is None):
                    continue
                handled_event_listener = True
                for entry in array_node.children:
                    if entry.type != "array_element_initializer":
                        continue
                    event_cls: str | None = None
                    listener_arr = None
                    for sub in entry.children:
                        if sub.type == "class_constant_access_expression" and event_cls is None:
                            for sc in sub.children:
                                if sc.is_named and sc.type in ("name", "qualified_name"):
                                    event_cls = _read_text(sc, source)
                                    break
                        elif sub.type == "array_creation_expression":
                            listener_arr = sub
                    if not event_cls or listener_arr is None:
                        continue
                    for listener_entry in listener_arr.children:
                        if listener_entry.type != "array_element_initializer":
                            continue
                        for item in listener_entry.children:
                            if item.type != "class_constant_access_expression":
                                continue
                            for sc in item.children:
                                if sc.is_named and sc.type in ("name", "qualified_name"):
                                    listener_cls = _read_text(sc, source)
                                    line_no = item.start_point[0] + 1
                                    pending_listen_edges.append((event_cls, listener_cls, line_no))
                                    break
                            break
            if handled_event_listener:
                return

        if (config.ts_module == "tree_sitter_c_sharp"
                and t == "field_declaration"
                and parent_class_nid):
            type_node = node.child_by_field_name("type")
            if type_node is None:
                for child in node.children:
                    if child.type == "variable_declaration":
                        type_node = child.child_by_field_name("type")
                        if type_node is not None:
                            break
            type_info = _read_csharp_type_name(type_node, source)
            if type_info:
                type_name, qualified, qualifier = type_info
                csharp_type_params = _csharp_type_parameters_in_scope(
                    type_node if type_node is not None else node, source
                )
                if not type_name or type_name in csharp_type_params:
                    return
                # Record the field's declared type for the method-scoped
                # receiver tables (#2299) — the C# twin of java_field_types.
                # Pascal-case only: primitives never own a resolvable method.
                if type_name[:1].isupper():
                    fields = csharp_field_types.setdefault(parent_class_nid, {})
                    for child in node.children:
                        if child.type != "variable_declaration":
                            continue
                        for declarator in child.children:
                            if declarator.type != "variable_declarator":
                                continue
                            name_node = declarator.child_by_field_name("name") or next(
                                (g for g in declarator.children
                                 if g.type == "identifier"),
                                None,
                            )
                            if name_node is not None:
                                fields[_read_text(name_node, source)] = type_name
                line = node.start_point[0] + 1
                # Walk the whole type expression rather than only its outer name, so
                # `Box<Widget>` yields the Box field ref AND the Widget generic_arg ref.
                # Reading just the outer name left every generic argument in field
                # position unlinked -- `IDbContextFactory<SomeContext>` lost SomeContext,
                # and `Mock<IThing>` lost IThing across entire test suites. The C#
                # property_declaration handler below and the tree_sitter_java
                # field_declaration handler beside it already do exactly this; C# fields
                # were the odd one out.
                refs: list[tuple[str, str, bool, str]] = []
                _csharp_collect_type_refs(
                    type_node, source, False, refs, csharp_type_params
                )
                for ref_name, role, ref_qualified, ref_qualifier in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        metadata = {"ref_token": ref_name}
                        if ref_qualified:
                            metadata["qualified"] = True
                        if ref_qualifier:
                            metadata["ref_qualifier"] = ref_qualifier
                        add_edge(parent_class_nid, target_nid, "references",
                                 line, context=ctx, metadata=metadata)
            return

        if (config.ts_module == "tree_sitter_c_sharp"
                and t == "property_declaration"
                and parent_class_nid):
            # C# auto-properties (`public Widget Main { get; set; }`) are the
            # idiomatic way to declare state, yet only field_declaration was
            # handled — so property types produced no references edge. Unlike a
            # field, a property exposes its type on the node directly (no
            # variable_declaration wrapper), so read it straight off the `type`
            # field. Use _csharp_collect_type_refs (like the Java/PHP/Kotlin
            # siblings) so `List<Widget>` yields both the List field ref and the
            # Widget generic_arg ref.
            # A property becomes a node, the way a C++ data member does. Fields
            # stay out: the id recipe casefolds and strips leading underscores, so
            # `_count` and `Count` normalize to the same id, and emitting both
            # would hand the node to whichever the parser reached first — in
            # practice the private backing field, hiding the public member behind
            # it. See #3006 for the follow-up.
            prop_node_name = node.child_by_field_name("name")
            if prop_node_name is not None:
                property_name = _read_text(prop_node_name, source)
                if property_name:
                    property_line = node.start_point[0] + 1
                    property_nid = _make_id(parent_class_nid, property_name)
                    if property_nid not in seen_ids:
                        add_node(property_nid, property_name, property_line)
                        add_edge(parent_class_nid, property_nid, "defines",
                                 property_line, context="field")
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                # Record the property's declared type for the method-scoped
                # receiver tables (#2299), like a field: `Main.Render()` on a
                # `public Widget Main { get; set; }` types Main as Widget.
                prop_name_node = node.child_by_field_name("name")
                prop_type = _csharp_receiver_type_name(type_node, source)
                if prop_name_node is not None and prop_type:
                    csharp_field_types.setdefault(parent_class_nid, {})[
                        _read_text(prop_name_node, source)
                    ] = prop_type
                line = node.start_point[0] + 1
                refs: list[tuple[str, str, bool, str]] = []
                _csharp_collect_type_refs(type_node, source, False, refs)
                for ref_name, role, qualified, qualifier in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        metadata = {"ref_token": ref_name}
                        if qualified:
                            metadata["qualified"] = True
                        if qualifier:
                            metadata["ref_qualifier"] = qualifier
                        add_edge(parent_class_nid, target_nid, "references",
                                 line, context=ctx, metadata=metadata)
            return

        if (config.ts_module == "tree_sitter_java"
                and t == "field_declaration"
                and parent_class_nid):
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                receiver_type = _java_receiver_type_name(type_node, source)
                if receiver_type:
                    fields = java_field_types.setdefault(parent_class_nid, {})
                    for field_name in _java_declarator_names(node, source):
                        fields[field_name] = receiver_type
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _java_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references",
                                 line, context=ctx)
            return

        if (config.ts_module == "tree_sitter_java"
                and t == "annotation_type_element_declaration"
                and parent_class_nid):
            type_node = node.child_by_field_name("type")
            line = node.start_point[0] + 1
            refs: list[tuple[str, str]] = []
            _java_collect_type_refs(
                type_node, source, False, refs, preserve_qualified=True
            )
            for ref_name, role in refs:
                ctx = "generic_arg" if role == "generic_arg" else "return_type"
                target_nid = ensure_named_node(ref_name, line)
                if target_nid != parent_class_nid:
                    add_edge(parent_class_nid, target_nid, "references",
                             line, context=ctx)
            return

        if (config.ts_module == "tree_sitter_php"
                and t == "property_declaration"
                and parent_class_nid):
            for c in node.children:
                if c.type not in ("named_type", "primitive_type", "nullable_type",
                                   "union_type", "intersection_type", "optional_type"):
                    continue
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _php_collect_type_refs(c, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references", line, context=ctx)
                break
            return

        if (config.ts_module == "tree_sitter_kotlin"
                and t == "property_declaration"):
            # Field-type references stay class-gated: top-level properties keep
            # their pre-#2565 (no-references) behavior unchanged.
            if parent_class_nid:
                type_node = _kotlin_property_type_node(node)
                if type_node is not None:
                    line = node.start_point[0] + 1
                    refs: list[tuple[str, str]] = []
                    _kotlin_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "field"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != parent_class_nid:
                            add_edge(parent_class_nid, target_nid, "references", line, context=ctx)
            # #2565: seed the initializer into initializer_nodes so walk_calls
            # collects its calls (`val repo = createRepo()`), which previously
            # died at the `return` below. Seeding the WHOLE expression (not just
            # call_types) lets walk_calls recurse into nested argument calls
            # (`HttpClient(base())`) and lambda bodies; a literal initializer
            # (`val plain = 5`) contains no call and yields nothing. The
            # explicit type, if any, lives inside variable_declaration BEFORE
            # the `=`, so post-`=` named children are only the initializer.
            # Top-level properties attribute to the file node.
            owner_nid = parent_class_nid or file_nid
            seen_eq = False
            for child in node.children:
                if not child.is_named:
                    seen_eq = seen_eq or child.type == "="
                    continue
                if seen_eq:                              # `= expr` initializer
                    initializer_nodes.append((owner_nid, child))
                elif child.type == "property_delegate":  # `by lazy { ... }` / any delegate
                    for sub in child.children:
                        if sub.is_named:
                            initializer_nodes.append((owner_nid, sub))
            return

        if (config.ts_module == "tree_sitter_swift"
                and t == "property_declaration"
                and parent_class_nid):
            line = node.start_point[0] + 1
            prop_type: str | None = None
            type_anno = _swift_property_type_node(node)
            if type_anno is not None:
                refs: list[tuple[str, str]] = []
                _swift_collect_type_refs(type_anno, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references", line, context=ctx)
                    if prop_type is None and role == "type":
                        prop_type = ref_name
            # #1356 Stage 1: walk the initializer so a constructor call
            # (`let vm = VM()`) produces a calls edge. #1356 Stage 2a: when the
            # property has no type annotation, infer its type from the
            # constructor so `vm.update()` later resolves to VM.
            pending_factory: tuple[str, str] | None = None
            for child in node.children:
                if child.type in config.call_types:
                    initializer_nodes.append((parent_class_nid, child))
                    if prop_type is None:
                        ctor = _swift_constructor_type(child, source)
                        if ctor is not None:
                            prop_type = ctor
                        else:
                            # #2561: `let x = Factory.make()` — no in-file type;
                            # stash the label-only binding for corpus-side
                            # resolution against make's plain return type.
                            pending_factory = _swift_factory_call(child, source)
                # #1604 Stage 2b: `let x = Type.shared` (or any `Type.staticProp`)
                # binds x to Type via a static-member access, which is a
                # navigation_expression, not a constructor call. Infer x's type from
                # the uppercase head so later `x.method()` calls resolve to Type. This
                # is the singleton idiom (`Type.shared`) cached into a local var and
                # called on a subsequent line — extremely common in Swift.
                elif child.type == "navigation_expression" and prop_type is None:
                    head = child.children[0] if child.children else None
                    if head is not None and head.type == "simple_identifier":
                        htext = _read_text(head, source)
                        if htext and htext[:1].isupper():
                            prop_type = htext
            # #2561: `@Environment(Store.self) var store` names the property's
            # type only inside the attribute argument (modifiers > attribute),
            # which the direct-children scan above never reaches. Last resort:
            # annotation and constructor inference keep priority.
            if prop_type is None:
                prop_type = _swift_attribute_type_name(node, source)
            prop_name = _swift_property_name(node, source)
            if prop_name and prop_type:
                type_table[prop_name] = prop_type
            elif (prop_name and pending_factory is not None
                  and prop_name not in swift_factory_bindings):
                swift_factory_bindings[prop_name] = pending_factory
            # #2181: a computed property (`var body: some View { … }`) or an
            # observed one (`willSet`/`didSet`) carries a body that the branches
            # above never emitted — so the property node AND every call inside it
            # were dropped. For SwiftUI this erases the whole view layer, since
            # `body` is a computed property. Emit a function-like member node and
            # defer its body to the call-walk via function_bodies (mirroring how
            # methods register their bodies). Stored properties have no such body
            # child, so their behaviour is unchanged (no regression).
            comp_bodies = [c for c in node.children
                           if c.type in ("computed_property", "willset_didset_block")]
            if comp_bodies and prop_name:
                prop_nid = _make_id(parent_class_nid, prop_name)
                add_node(prop_nid, f".{prop_name}", line)
                add_edge(parent_class_nid, prop_nid, "method", line)
                for body_block in comp_bodies:
                    function_bodies.append((prop_nid, body_block))
            return

        if (config.ts_module == "tree_sitter_scala"
                and t in ("val_definition", "var_definition")
                and parent_class_nid):
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _scala_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references",
                                 line, context=ctx)
            # fall through so any call expressions in the initializer get walked

        # Scala: `self: Logging with Database =>` (or `this: T =>`) declares a
        # structural precondition on the enclosing type, not a mixin/reference.
        # self_type carries no field names, so the type node is found
        # positionally: the binder identifier is named[0], the type (when
        # present) is named[1]. `self =>` binds a name with no type at all, so
        # len(named) < 2 correctly yields no type node rather than misreading
        # the binder as a type. _scala_collect_type_refs already handles every
        # shape a self-type's type position can take (type_identifier,
        # compound_type for `with`, refinement bodies via compound_type) --
        # reused unchanged.
        if (config.ts_module == "tree_sitter_scala"
                and t == "self_type"
                and parent_class_nid):
            named = [c for c in node.children if c.is_named]
            type_node = named[1] if len(named) >= 2 else None
            if type_node is not None:
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _scala_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "requires", line)
            return

        if (config.ts_module == "tree_sitter_cpp"
                and t == "field_declaration"
                and parent_class_nid):
            # Skip method prototypes (field_declaration with a function_declarator
            # is a member-function declaration, not a data member).
            decls = list(node.children_by_field_name("declarator"))
            is_method = any(
                d.type == "function_declarator"
                or (d.type in ("pointer_declarator", "reference_declarator")
                    and any(c.type == "function_declarator" for c in d.children))
                for d in decls
            )
            type_node = node.child_by_field_name("type")
            # A nested type (`class Inner { … };` inside a class body) is a
            # field_declaration whose `type` field IS the class_specifier, so
            # returning from this branch used to drop Inner and everything it
            # declares — silently, with no parse error (#2876). Walk it as a
            # class instead: the engine's existing nested-type handling gives
            # it a `contains` edge from the enclosing type. The declarator loop
            # below still runs, since `class Inner { } inst;` declares a member
            # alongside the type.
            # Only class/struct nested types are recovered here: `enum_specifier`
            # is deliberately not in C++'s `class_types`, so a nested `enum` and
            # its enumerators are still not emitted. That is outside #2876's scope
            # (which is about nested class/struct and C++/CLI) and is left as a
            # known gap rather than widened here.
            is_nested_type = (
                type_node is not None
                and type_node.type in config.class_types
                and type_node.child_by_field_name("body") is not None
            )
            if is_nested_type:
                walk(type_node, parent_class_nid)
            if not is_method and not is_nested_type:
                if type_node is not None:
                    line = node.start_point[0] + 1
                    refs: list[tuple[str, str]] = []
                    _cpp_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "field"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != parent_class_nid:
                            add_edge(parent_class_nid, target_nid, "references",
                                     line, context=ctx)
            # Emit a node for each data member. Use children_by_field_name so we
            # only visit declarator children, not the type node (which would give
            # us the type name, not the field name). Handles int x, y; via
            # multiple declarator fields and static const int MAX = 100; via the
            # init_declarator → field_identifier recursion in _get_cpp_func_name.
            for decl in decls:
                name = _get_cpp_func_name(decl, source)
                if name:
                    line = decl.start_point[0] + 1
                    field_nid = _make_id(parent_class_nid, name)
                    add_node(field_nid, name, line)
                    add_edge(parent_class_nid, field_nid, "defines", line, context="field")
            return

        # Ruby's `class << self` contains ordinary `method` nodes. Keep them
        # attached to the lexical class and record singleton dispatch kind.
        if (
            config.ts_module == "tree_sitter_ruby"
            and t == "singleton_class"
            and parent_class_nid
        ):
            value = node.child_by_field_name("value")
            body = node.child_by_field_name("body")
            if value is not None and _read_text(value, source) == "self" and body:
                if _ruby_body_has_lookup_barrier(body, source):
                    class_node = next(
                        n for n in nodes if n["id"] == parent_class_nid
                    )
                    class_metadata = dict(class_node.get("metadata") or {})
                    class_metadata["ruby_lookup_unsafe"] = True
                    class_node["metadata"] = sanitize_metadata(class_metadata)
                ruby_singleton_context_depth += 1
                try:
                    for child in body.children:
                        walk(child, parent_class_nid=parent_class_nid)
                finally:
                    ruby_singleton_context_depth -= 1
                return

        # Function types
        if t in config.function_types:
            # Swift deinit/subscript have no name field — resolve before generic fallback
            if t == "deinit_declaration":
                func_name: str | None = "deinit"
            elif t == "subscript_declaration":
                func_name = "subscript"
            elif config.resolve_function_name_fn is not None:
                # C/C++ style: use declarator
                declarator = node.child_by_field_name("declarator")
                func_name = None
                if declarator:
                    func_name = config.resolve_function_name_fn(declarator, source)
            else:
                name_node = node.child_by_field_name(config.name_field)
                if name_node is None:
                    for child in node.children:
                        if child.type in config.name_fallback_child_types:
                            name_node = child
                            break
                func_name = _read_text(name_node, source) if name_node else None

            if not func_name:
                return
            sanitized_name = (
                config.sanitize_symbol_name_fn(func_name)
                if config.sanitize_symbol_name_fn is not None
                else func_name
            )
            # A name that normalizes to nothing collapses `_make_id(prefix, name)`
            # onto the (absolute-path-derived) prefix, leaking the scan path and
            # colliding with the file/class node (#1899). No graph signal; skip.
            if not normalize_id(sanitized_name):
                return

            line = node.start_point[0] + 1
            ruby_method_kind = None
            if config.ts_module == "tree_sitter_ruby" and parent_class_nid:
                if t == "singleton_method":
                    singleton_object = node.child_by_field_name("object")
                    if (
                        ruby_singleton_context_depth == 0
                        and singleton_object is not None
                        and _read_text(singleton_object, source) == "self"
                    ):
                        ruby_method_kind = "singleton"
                    else:
                        ruby_method_kind = "ambiguous"
                elif ruby_singleton_context_depth == 1:
                    ruby_method_kind = "singleton"
                elif ruby_singleton_context_depth == 0:
                    ruby_method_kind = "instance"
                else:
                    ruby_method_kind = "ambiguous"
            if parent_class_nid:
                func_nid = _make_id(parent_class_nid, sanitized_name)
                if config.ts_module == "tree_sitter_python":
                    func_nid = _python_underscore_salted_nid(
                        func_nid, sanitized_name, python_underscore_groups, kind="method"
                    )
                elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                    func_nid = _js_member_salted_nid(
                        func_nid, sanitized_name, js_member_groups
                    )
                elif config.ts_module == "tree_sitter_c_sharp":
                    key = (node.start_byte, node.end_byte)
                    if key in csharp_method_info:
                        func_nid = csharp_method_info[key][0]
                ruby_method_metadata = None
                csharp_method_metadata = None
                if config.ts_module == "tree_sitter_c_sharp":
                    key = (node.start_byte, node.end_byte)
                    if key in csharp_method_info:
                        _, arity, is_overload = csharp_method_info[key]
                        csharp_method_metadata = {"arity": arity}
                        if is_overload:
                            csharp_method_metadata["csharp_overload"] = True
                if ruby_method_kind is not None:
                    kinds = ruby_method_kinds.setdefault(func_nid, set())
                    kinds.add(ruby_method_kind)
                    ruby_method_counts[func_nid] = ruby_method_counts.get(func_nid, 0) + 1
                    stored_kind = (
                        next(iter(kinds))
                        if len(kinds) == 1
                        and ruby_method_counts[func_nid] == 1
                        and next(iter(kinds)) in {"instance", "singleton"}
                        else "ambiguous"
                    )
                    ruby_method_metadata = {"ruby_method_kind": stored_kind}
                    existing_method = next(
                        (n for n in nodes if n["id"] == func_nid), None
                    )
                    if existing_method is not None:
                        existing_metadata = dict(existing_method.get("metadata") or {})
                        existing_metadata.update(ruby_method_metadata)
                        existing_method["metadata"] = sanitize_metadata(existing_metadata)
                add_node(
                    func_nid,
                    f".{func_name}()",
                    line,
                    metadata=ruby_method_metadata or csharp_method_metadata,
                )
                add_edge(parent_class_nid, func_nid, "method", line)
            else:
                func_nid = _make_id(stem, sanitized_name)
                if config.ts_module == "tree_sitter_python":
                    func_nid = _python_underscore_salted_nid(
                        func_nid, sanitized_name, python_underscore_groups, kind="function"
                    )
                elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                    func_nid = _js_member_salted_nid(
                        func_nid, sanitized_name, js_member_groups
                    )
                add_node(func_nid, f"{func_name}()", line)
                add_edge(file_nid, func_nid, "contains", line)
            callable_def_nids.add(func_nid)  # function / method def is callable
            if config.ts_module == "tree_sitter_python":
                local_bound_names[func_nid] = _python_local_bound_names(node, source)
            elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                local_bound_names[func_nid] = _js_local_bound_names(node, source)

            if config.ts_module == "tree_sitter_python":
                params_node = node.child_by_field_name("parameters")
                for ref_name, role in _python_collect_param_refs(params_node, source):
                    ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != func_nid:
                        edges.append(
                            _semantic_reference_edge(func_nid, target_nid, ctx, str_path, line)
                        )
                return_type_node = node.child_by_field_name("return_type")
                if return_type_node is not None:
                    return_refs: list[tuple[str, str]] = []
                    _python_collect_type_refs(return_type_node, source, False, return_refs)
                    for ref_name, role in return_refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            edges.append(
                                _semantic_reference_edge(func_nid, target_nid, ctx, str_path, line)
                            )

            if config.ts_module == "tree_sitter_c_sharp":
                csharp_type_params = _csharp_type_parameters_in_scope(node, source)
                params_node = node.child_by_field_name("parameters")
                if params_node is not None:
                    for p in params_node.children:
                        if p.type != "parameter":
                            continue
                        type_node = p.child_by_field_name("type")
                        refs: list[tuple[str, str, bool, str]] = []
                        _csharp_collect_type_refs(
                            type_node, source, False, refs, csharp_type_params
                        )
                        for ref_name, role, qualified, qualifier in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                metadata = {"ref_token": ref_name}
                                if qualified:
                                    metadata["qualified"] = True
                                if qualifier:
                                    metadata["ref_qualifier"] = qualifier
                                add_edge(func_nid, target_nid, "references", line,
                                         context=ctx, metadata=metadata)
                return_node = node.child_by_field_name("returns")
                if return_node is not None:
                    refs: list[tuple[str, str, bool, str]] = []
                    _csharp_collect_type_refs(
                        return_node, source, False, refs, csharp_type_params
                    )
                    for ref_name, role, qualified, qualifier in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            metadata = {"ref_token": ref_name}
                            if qualified:
                                metadata["qualified"] = True
                            if qualifier:
                                metadata["ref_qualifier"] = qualifier
                            add_edge(func_nid, target_nid, "references", line,
                                     context=ctx, metadata=metadata)
                for attr_name, qualified, qualifier in _csharp_attribute_names(node, source):
                    target_nid = ensure_named_node(attr_name, line)
                    if target_nid != func_nid:
                        metadata = {"ref_token": attr_name}
                        if qualified:
                            metadata["qualified"] = True
                        if qualifier:
                            metadata["ref_qualifier"] = qualifier
                        add_edge(func_nid, target_nid, "references", line,
                                 context="attribute", metadata=metadata)

            if config.ts_module == "tree_sitter_java":
                params_node = node.child_by_field_name("parameters")
                if params_node is not None:
                    for p in params_node.children:
                        if p.type != "formal_parameter":
                            continue
                        type_node = p.child_by_field_name("type")
                        refs = []
                        _java_collect_type_refs(type_node, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                add_edge(func_nid, target_nid, "references", line, context=ctx)
                return_node = node.child_by_field_name("type")
                if return_node is not None:
                    refs = []
                    _java_collect_type_refs(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)
                annotation_targets: set[str] = set()
                for anno_name, anno_raw in _java_annotation_names(node, source):
                    # Inline-qualified: keep the dotted name (#2504); see the
                    # class-level annotation handling above.
                    target_nid = ensure_named_node(
                        anno_raw if "." in anno_raw else anno_name, line)
                    if target_nid != func_nid and target_nid not in annotation_targets:
                        add_edge(func_nid, target_nid, "references", line, context="attribute")
                        annotation_targets.add(target_nid)
                for ref_name in _java_annotation_class_literal_refs(node, source):
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != func_nid and target_nid not in annotation_targets:
                        add_edge(func_nid, target_nid, "references", line,
                                 context="attribute")
                        annotation_targets.add(target_nid)

            if config.ts_module == "tree_sitter_php":
                params_container = None
                for c in node.children:
                    if c.type == "formal_parameters":
                        params_container = c
                        break
                if params_container is not None:
                    for p in params_container.children:
                        # PHP 8 constructor property promotion (`__construct(private
                        # Repo $repo)`) parses the promoted param as
                        # property_promotion_parameter, not simple_parameter. Its
                        # type sits in the same direct named child shape, so accept
                        # both here; a promoted param is additionally a class field.
                        if p.type not in ("simple_parameter", "property_promotion_parameter"):
                            continue
                        is_promoted = p.type == "property_promotion_parameter"
                        type_node = None
                        for sub in p.children:
                            if sub.type in ("named_type", "primitive_type", "nullable_type",
                                             "union_type", "intersection_type", "optional_type"):
                                type_node = sub
                                break
                        refs: list[tuple[str, str]] = []
                        _php_collect_type_refs(type_node, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                add_edge(func_nid, target_nid, "references", line, context=ctx)
                            # A promoted param declares a real class field; mirror
                            # the property_declaration field-context edge so the
                            # type is discoverable as a class field too.
                            if is_promoted and parent_class_nid and target_nid != parent_class_nid:
                                fctx = "generic_arg" if role == "generic_arg" else "field"
                                add_edge(parent_class_nid, target_nid, "references",
                                         line, context=fctx)
                return_node = _php_method_return_type_node(node)
                if return_node is not None:
                    refs = []
                    _php_collect_type_refs(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)

            if config.ts_module == "tree_sitter_kotlin":
                params_container = None
                for c in node.children:
                    if c.type == "function_value_parameters":
                        params_container = c
                        break
                if params_container is not None:
                    for p in params_container.children:
                        if p.type != "parameter":
                            continue
                        param_type_node = None
                        for sub in p.children:
                            if sub.type in ("user_type", "nullable_type", "type_reference"):
                                param_type_node = sub
                                break
                        refs: list[tuple[str, str]] = []
                        _kotlin_collect_type_refs(param_type_node, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                add_edge(func_nid, target_nid, "references", line, context=ctx)
                return_type_node = _kotlin_function_return_type_node(node)
                if return_type_node is not None:
                    refs = []
                    _kotlin_collect_type_refs(return_type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)

            if config.ts_module == "tree_sitter_swift":
                for p in node.children:
                    if p.type != "parameter":
                        continue
                    type_node = p.child_by_field_name("type")
                    refs: list[tuple[str, str]] = []
                    _swift_collect_type_refs(type_node, source, False, refs)
                    param_type: str | None = None
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)
                        if param_type is None and role == "type":
                            param_type = ref_name
                    # #1356 Stage 2a: record param name -> type (flat per-file
                    # table; later params with the same name win, which is fine
                    # for the depth-1 member-call resolution we do).
                    if param_type:
                        name_node = p.child_by_field_name("name")
                        pname = _read_text(name_node, source) if name_node else None
                        if pname:
                            type_table[pname] = param_type
                return_node = node.child_by_field_name("return_type")
                if return_node is not None:
                    refs = []
                    _swift_collect_type_refs(return_node, source, False, refs)
                    # #2561: a plain concrete return (`-> Type`, node type
                    # user_type — NOT `some P`/`[T]`/`T?`, which parse as
                    # opaque_type/array_type/optional_type) with exactly one
                    # role=="type" ref is marked so the factory-receiver pass
                    # can read the method's return label corpus-side.
                    plain_return = (return_node.type == "user_type"
                                    and sum(1 for _, r in refs if r == "type") == 1)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line,
                                     context=ctx,
                                     metadata={"swift_plain_return": True}
                                     if plain_return and role == "type" else None)

            if (config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
                    and func_name == "constructor"):
                params_node = node.child_by_field_name("parameters")
                if params_node is not None:
                    for p in params_node.children:
                        if p.type != "required_parameter":
                            continue
                        has_modifier = any(
                            c.type in ("accessibility_modifier", "readonly")
                            for c in p.children
                        )
                        if not has_modifier:
                            continue
                        name_n = p.child_by_field_name("pattern")
                        type_n = p.child_by_field_name("type")
                        if name_n is None or type_n is None:
                            continue
                        pname = _read_text(name_n, source)
                        for tc in type_n.children:
                            if tc.type == "type_identifier":
                                ptype = _read_text(tc, source)
                                if pname and ptype:
                                    type_table[pname] = ptype
                                break

            if config.ts_module in ("tree_sitter_c", "tree_sitter_cpp"):
                collect = (_cpp_collect_type_refs if config.ts_module == "tree_sitter_cpp"
                           else _c_collect_type_refs)
                return_node = node.child_by_field_name("type")
                if return_node is not None:
                    refs: list[tuple[str, str]] = []
                    collect(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)
                # function_declarator may be wrapped in pointer/reference declarators
                decl = node.child_by_field_name("declarator")
                while decl is not None and decl.type in (
                        "pointer_declarator", "reference_declarator"):
                    decl = decl.child_by_field_name("declarator")
                if decl is not None and decl.type == "function_declarator":
                    params_node = decl.child_by_field_name("parameters")
                    if params_node is not None:
                        for p in params_node.children:
                            if p.type != "parameter_declaration":
                                continue
                            ptype = p.child_by_field_name("type")
                            if ptype is None:
                                continue
                            refs = []
                            collect(ptype, source, False, refs)
                            for ref_name, role in refs:
                                ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                                target_nid = ensure_named_node(ref_name, line)
                                if target_nid != func_nid:
                                    add_edge(func_nid, target_nid, "references",
                                             line, context=ctx)

            if config.ts_module == "tree_sitter_scala":
                params_node = None
                for c in node.children:
                    if c.type == "parameters":
                        params_node = c
                        break
                if params_node is not None:
                    for p in params_node.children:
                        if p.type != "parameter":
                            continue
                        ptype = p.child_by_field_name("type")
                        if ptype is None:
                            continue
                        refs: list[tuple[str, str]] = []
                        _scala_collect_type_refs(ptype, source, False, refs)
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                            target_nid = ensure_named_node(ref_name, line)
                            if target_nid != func_nid:
                                add_edge(func_nid, target_nid, "references",
                                         line, context=ctx)
                return_node = node.child_by_field_name("return_type")
                if return_node is not None:
                    refs = []
                    _scala_collect_type_refs(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references",
                                     line, context=ctx)

            body = _find_body(node, config)
            # JS/TS: capture callable members assigned directly in a function
            # body. Besides constructor-style `this.X = fn`, factories commonly
            # create an object literal and assign its public surface with
            # `api.X = fn`. These statements otherwise live only in a body that
            # is walked for calls, so their symbols vanish from the graph.
            if body is not None and config.ts_module in (
                "tree_sitter_javascript", "tree_sitter_typescript"
            ):
                function_owner_nid = parent_class_nid if parent_class_nid else func_nid
                _js_scan_member_assignments(
                    body, function_owner_nid, source, this_as_method=True,
                    add_node_fn=add_node, add_edge_fn=add_edge,
                    function_bodies=function_bodies,
                )
            if body:
                if config.ts_module == "tree_sitter_java" and parent_class_nid:
                    java_method_scopes[id(body)] = (node, parent_class_nid)
                if config.ts_module == "tree_sitter_c_sharp" and parent_class_nid:
                    csharp_method_scopes[id(body)] = (node, parent_class_nid)
                    csharp_caller_class[func_nid] = parent_class_nid
                function_bodies.append((func_nid, body))
                if config.ts_module in (
                    "tree_sitter_javascript", "tree_sitter_typescript"
                ):
                    _scan_js_nested_function_declarations(
                        body, func_nid, source=source, config=config,
                        add_node=add_node, add_edge=add_edge,
                        callable_def_nids=callable_def_nids,
                        local_bound_names=local_bound_names,
                        function_bodies=function_bodies,
                        scope_parents=scope_parents,
                        lexical_nids_by_scope=lexical_nids_by_scope,
                    )
                if config.ts_module == "tree_sitter_python":
                    _scan_python_nested_function_declarations(
                        body, func_nid, source=source, config=config,
                        add_node=add_node, add_edge=add_edge,
                        callable_def_nids=callable_def_nids,
                        local_bound_names=local_bound_names,
                        function_bodies=function_bodies,
                        scope_parents=scope_parents,
                        lexical_nids_by_scope=lexical_nids_by_scope,
                    )
                if config.ts_module == "tree_sitter_kotlin":
                    # #2347: Kotlin anonymous objects (`object : Foo { … }`,
                    # node type `object_literal`). The function branch never
                    # recurses into bodies and object_literal is not a
                    # class_type, so the literal's members (and every call
                    # inside them) got no nodes at all. Scan this body for
                    # object_literal descendants — without crossing a nested
                    # function_declaration boundary (a local fun's literals
                    # are not this function's) and without descending into a
                    # found literal — then emit an owner node per literal and
                    # walk its class_body exactly like the class branch, so
                    # members and their calls flow through the normal
                    # machinery (walk_calls' function_boundary_types already
                    # keep the enclosing function from absorbing them).
                    _kt_literals = []
                    _kt_stack = list(body.children)
                    while _kt_stack:
                        _kt_node = _kt_stack.pop()
                        if _kt_node.type == "function_declaration":
                            continue
                        if _kt_node.type == "object_literal":
                            _kt_literals.append(_kt_node)
                            continue
                        _kt_stack.extend(_kt_node.children)
                    _kt_literals.sort(key=lambda n: n.start_byte)
                    for lit in _kt_literals:
                        lit_line = lit.start_point[0] + 1
                        # Supertypes from the literal's delegation_specifiers,
                        # shaped like the Kotlin class-branch handling:
                        # constructor_invocation -> inherits, bare user_type
                        # (or explicit_delegation) -> implements.
                        lit_bases: list[tuple[str, str]] = []
                        for dchild in lit.children:
                            if dchild.type != "delegation_specifiers":
                                continue
                            for spec in dchild.children:
                                if spec.type != "delegation_specifier":
                                    continue
                                relation = "implements"
                                user_type_node = None
                                for sub in spec.children:
                                    if sub.type == "constructor_invocation":
                                        relation = "inherits"
                                        for inner in sub.children:
                                            if inner.type == "user_type":
                                                user_type_node = inner
                                                break
                                        break
                                    if sub.type == "user_type":
                                        user_type_node = sub
                                        break
                                    if sub.type == "explicit_delegation":
                                        for inner in sub.children:
                                            if inner.type == "user_type":
                                                user_type_node = inner
                                                break
                                        break
                                base = _kotlin_user_type_name(
                                    user_type_node, source
                                )
                                if base:
                                    lit_bases.append((base, relation))
                        obj_label = (
                            lit_bases[0][0] if lit_bases
                            else f"object@L{lit_line}"
                        )
                        obj_nid = _make_id(
                            func_nid, f"object:{obj_label}", f"L{lit_line}"
                        )
                        add_node(obj_nid, obj_label, lit_line)
                        add_edge(func_nid, obj_nid, "contains", lit_line)
                        callable_def_nids.add(obj_nid)
                        callable_class_nids.add(obj_nid)
                        for base, relation in lit_bases:
                            base_nid = ensure_named_node(base, lit_line)
                            if base_nid != obj_nid:
                                add_edge(obj_nid, base_nid, relation, lit_line)
                        lit_body = next(
                            (c for c in lit.children if c.type == "class_body"),
                            None,
                        )
                        if lit_body is not None:
                            for child in lit_body.children:
                                walk(child, parent_class_nid=obj_nid)
            return

        # JS/TS arrow functions and C# namespaces — language-specific extra handling
        if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
            if _js_extra_walk(node, source, file_nid, stem, str_path,
                              nodes, edges, seen_ids, function_bodies,
                              parent_class_nid, add_node, add_edge,
                              callable_def_nids, local_bound_names,
                              closure_locals_by_body, config=config,
                              scope_parents=scope_parents,
                              lexical_nids_by_scope=lexical_nids_by_scope):
                return

        # TS enum members, and namespace / module containers
        if config.ts_module == "tree_sitter_typescript":
            if _ts_extra_walk(node, source, file_nid, stem, str_path,
                              nodes, edges, seen_ids, function_bodies,
                              parent_class_nid, add_node, add_edge, walk):
                return

        if config.ts_module == "tree_sitter_c_sharp":
            if _csharp_extra_walk(node, source, file_nid, stem, str_path,
                                   nodes, edges, seen_ids, function_bodies,
                                   parent_class_nid, add_node, add_edge, walk,
                                   namespace_stack, scope_stack):
                return

        if config.ts_module == "tree_sitter_swift":
            if _swift_extra_walk(node, source, file_nid, stem, str_path,
                                  nodes, edges, seen_ids, function_bodies,
                                  parent_class_nid, add_node, add_edge,
                                  ensure_named_node):
                return

        if config.ts_module == "tree_sitter_java":
            if _java_extra_walk(node, source, file_nid, stem, str_path,
                                nodes, edges, seen_ids, function_bodies,
                                parent_class_nid, add_node, add_edge, walk):
                return

        if config.ts_module == "tree_sitter_kotlin":
            if _kotlin_extra_walk(node, source, file_nid, stem, str_path,
                                  nodes, edges, seen_ids, function_bodies,
                                  parent_class_nid, add_node, add_edge, walk):
                return

        if config.ts_module == "tree_sitter_ruby":
            if _ruby_extra_walk(node, source, file_nid, stem, str_path,
                                nodes, edges, seen_ids, function_bodies,
                                parent_class_nid, add_node, add_edge, walk,
                                callable_def_nids, callable_class_nids,
                                ruby_namespace, ruby_lexical_scopes):
                return

        # Python's `@property` / `@staticmethod` / `@classmethod` wrap the
        # inner function_definition in a `decorated_definition` node. The
        # default recurse below clears parent_class_nid, which would cause the
        # inner method to be emitted with a class-unqualified node id (e.g.
        # `file_baz` instead of `file_bar_baz`). That diverges from the
        # class-qualified id the rationale walker uses for the same method's
        # docstring, leaving the rationale edge dangling and the docstring
        # node orphaned (#1050). Treat decorated_definition as a transparent
        # wrapper so parent_class_nid propagates to the real function node.
        if t == "decorated_definition":
            # Applying a decorator emitted no edge to the decorator symbol, so
            # `affected <decorator>` reported nothing for the functions it wraps
            # (#2154). Emit the same shape TS/JS already emits in
            # `_ts_emit_decorator_edges`: a `references` edge (context=
            # "decorator") from the decorated function/class to each decorator,
            # resolved via ensure_named_node so an imported decorator becomes a
            # sourceless stub the corpus rewire collapses onto its definition.
            # The owner ids mirror the definition branches below/above verbatim,
            # so the edge lands on the node the walk is about to create.
            if config.ts_module == "tree_sitter_python":
                inner = node.child_by_field_name("definition")
                inner_name = None
                if inner is not None:
                    name_node = inner.child_by_field_name("name")
                    inner_name = _read_text(name_node, source) if name_node else None
                # A name that normalizes to nothing is skipped by the definition
                # branches (#1899), so an edge to it would dangle.
                if inner_name and normalize_id(inner_name):
                    if inner.type in config.class_types:
                        owner_nid = _make_id(stem, ".".join(namespace_stack), inner_name)
                    elif parent_class_nid:
                        owner_nid = _make_id(parent_class_nid, inner_name)
                    else:
                        owner_nid = _make_id(stem, inner_name)
                    for child in node.children:
                        if child.type != "decorator":
                            continue
                        deco_name = _python_decorator_name(child, source)
                        # Builtin/stdlib decorators are noise: no stub nodes,
                        # no false rewires onto same-named local definitions.
                        if not deco_name or deco_name in _PYTHON_DECORATOR_NOISE:
                            continue
                        deco_line = child.start_point[0] + 1
                        target = ensure_named_node(deco_name, deco_line)
                        if target != owner_nid:
                            add_edge(owner_nid, target, "references", deco_line,
                                     context="decorator")
            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return

        # #2565: a `companion object` is not an attribution scope of its own —
        # its members belong to the enclosing class in Kotlin. The default
        # recurse below would strip parent_class_nid, orphaning companion
        # property initializers (and leaving companion `fun`s file-level).
        # Recurse transparently, entering the class_body's children directly
        # since a bare class_body would itself default-recurse and drop the
        # parent link. Companion `fun`s thereby become class-attributed methods.
        if config.ts_module == "tree_sitter_kotlin" and t == "companion_object":
            for child in node.children:
                if child.type == "class_body":
                    for member in child.children:
                        walk(member, parent_class_nid=parent_class_nid)
                else:
                    walk(child, parent_class_nid=parent_class_nid)
            return

        # #2551: tree-sitter ERROR recovery can wrap declarations that plainly
        # sit inside a class body (e.g. the Kotlin grammar choking on a one-line
        # sibling member). The default recurse below deliberately drops
        # parent_class_nid (an unknown wrapper usually IS a scope boundary), but
        # an ERROR node is a parse artifact, not a scope — keep the enclosing
        # class linkage for whatever declarations were recovered inside it.
        if t == "ERROR":
            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return

        # Default: recurse
        for child in node.children:
            walk(child, parent_class_nid=None)

    walk(root)

    # ── Call-graph pass ───────────────────────────────────────────────────────
    label_to_nid: dict[str, str] = {}     # case-sensitive (Ruby, C#, Java, Kotlin, etc.)
    label_to_nid_ci: dict[str, str] = {}  # case-insensitive (PHP functions/classes)
    # nid -> source_file, so the indirect-dispatch guard can tell a genuine local
    # non-callable (reject) from an import-resolved foreign symbol whose definition
    # lives in another file (defer to the cross-file resolver). JS/TS named imports
    # surface the imported symbol's REAL node into this file's label map.
    nid_to_sf: dict[str, str] = {}
    for n in nodes:
        nid_to_sf[n["id"]] = str(n.get("source_file") or "")
        if n.get("type") == "namespace":
            continue
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        # For languages with lexical nesting (Python), nested functions should not overwrite
        # module-level definitions in the module/file-level label_to_nid map (#3405).
        if n["id"] not in scope_parents:
            label_to_nid[normalised] = n["id"]
            label_to_nid_ci[normalised.lower()] = n["id"]
        else:
            label_to_nid.setdefault(normalised, n["id"])
            label_to_nid_ci.setdefault(normalised.lower(), n["id"])

    seen_call_pairs: set[tuple[str, str]] = set()
    seen_indirect_pairs: set[tuple[str, str]] = set()  # Python indirect_call dedup
    seen_dyn_import_pairs: set[tuple[str, str]] = set()
    seen_static_ref_pairs: set[tuple[str, str, str]] = set()
    seen_helper_ref_pairs: set[tuple[str, str, str]] = set()
    seen_bind_pairs: set[tuple[str, str, str]] = set()
    raw_calls: list[dict] = []  # unresolved calls for cross-file resolution in extract()
    # Ruby: per-method `var -> ClassName` table from `var = Const.new` bindings,
    # populated before walk_calls runs. Lets member-call raw_calls carry a
    # receiver_type so the cross-file pass resolves `var.method` by type (#ruby).
    ruby_var_types: dict[str, dict[str, str | None]] = {}
    # Fields declared on a SUPERCLASS type receivers in a subclass too (#3151):
    # fold each class's table with its ancestors', nearest declaration winning.
    # Local `inherits` edges only - the cross-file half lives in the corpus
    # member-call resolvers, which see the whole graph.
    _local_bases: dict[str, list[str]] = {}
    for _e in edges:
        if _e.get("relation") == "inherits":
            _local_bases.setdefault(_e["source"], []).append(_e["target"])

    def _fields_up_chain(tables: dict, class_nid) -> dict:
        if not class_nid:
            return {}
        merged: dict = {}
        seen: set = set()
        queue = [class_nid]
        while queue:
            cls = queue.pop(0)
            if cls in seen:
                continue
            seen.add(cls)
            for _name, _tname in tables.get(cls, {}).items():
                merged.setdefault(_name, _tname)
            queue.extend(_local_bases.get(cls, []))
        return merged

    java_receiver_types = {
        body_id: _java_method_receiver_types(
            method_node,
            source,
            _fields_up_chain(java_field_types, class_nid),
        )
        for body_id, (method_node, class_nid) in java_method_scopes.items()
    }
    csharp_receiver_types = {
        body_id: _csharp_method_receiver_types(
            method_node,
            source,
            _fields_up_chain(csharp_field_types, class_nid),
        )
        for body_id, (method_node, class_nid) in csharp_method_scopes.items()
    }

    def _emit_indirect_by_name(ident_name: str, loc_node, scope_nid: str,
                               context: str) -> None:
        """Resolve a name that is referenced AS A VALUE to a real callable def and emit
        one INFERRED ``indirect_call`` edge — deferring an unknown / foreign name to the
        cross-file resolver, which applies the single-definition god-node guard and the
        GLOBAL callable-target check. The name is already extracted; scope filtering is
        the CALLER's job: an identifier reference must reject param/local shadows (a bare
        name IS a binding — see ``_emit_indirect_ref``), whereas a ``getattr(obj, "x")``
        string names an ATTRIBUTE and is never shadowed by a local, so that path passes
        the name straight through. ``loc_node`` supplies the source line.
        """
        ref_nid = label_to_nid.get(ident_name)
        # Defer to the cross-file resolver when the name is not defined in this file
        # (`from .h import fn`), or resolves to an import-surfaced FOREIGN symbol whose
        # definition (and callability) lives in another file (JS/TS named imports map
        # the real node into this file's label map). The cross-file pass applies the
        # single-definition god-node guard plus the GLOBAL callable-target check, so a
        # foreign non-callable (an imported data const) still produces no edge.
        if ref_nid is None or (
            ref_nid not in callable_def_nids and nid_to_sf.get(ref_nid, "") != str_path
        ):
            raw_calls.append({
                "caller_nid": scope_nid,
                "callee": ident_name,
                "is_member_call": False,
                "indirect": True,
                "context": context,
                "source_file": str_path,
                "source_location": f"L{loc_node.start_point[0] + 1}",
            })
            return
        if ref_nid == scope_nid or ref_nid not in callable_def_nids:
            return  # self-ref, or a same-named LOCAL non-callable data node — no edge
        if ref_nid in callable_class_nids:
            # A class referenced as a value (`select(Model)`, `db.get(Model, id)`,
            # an exception tuple) is a descriptor, not an invocation — no edge (#2137).
            return
        if (scope_nid, ref_nid) in seen_call_pairs:
            return  # already a direct call to this target
        if (scope_nid, ref_nid) in seen_indirect_pairs:
            return
        seen_indirect_pairs.add((scope_nid, ref_nid))
        edges.append({
            "source": scope_nid,
            "target": ref_nid,
            "relation": "indirect_call",
            "context": context,
            "confidence": "INFERRED",
            # 0.85 = "strong inference" on the extraction-spec rubric. The symbol
            # link is direct — the function is named right here — but that it is
            # ever INVOKED is the inference, which is why this is not the 0.95
            # tier. Previously no score was emitted at all and the edge inherited
            # the 0.5 default the rubric forbids (#2813).
            "confidence_score": 0.85,
            "source_file": str_path,
            "source_location": f"L{loc_node.start_point[0] + 1}",
            "weight": 1.0,
        })

    def _emit_indirect_ref(ident, scope_nid: str, enclosing_locals, context: str) -> None:
        """A function referenced BY NAME — passed as a call argument, or listed as a
        value in a dispatch table — is an indirect dependency of ``scope_nid``. Emit
        it as a distinct INFERRED ``indirect_call`` (kept out of the precise ``calls``
        relation) only when the name resolves to a real callable and is NOT shadowed
        by a parameter / local binding. A callback defined in another file is deferred
        to the cross-file resolver via an ``indirect`` raw_call carrying its context.
        Language-agnostic; shared by the call-argument and dispatch-table capture
        paths for Python and JS/TS (#1565, #1566).
        """
        if ident is None or ident.type not in ("identifier", "shorthand_property_identifier"):
            return
        ident_name = _read_text(ident, source)
        # shadowing: a param / local binding names a local value, not the module fn
        if ident_name in enclosing_locals or ident_name in ("self", "cls"):
            return
        # An import from outside the corpus binds the name for the whole module, so
        # it shadows in every scope — no unique same-named definition elsewhere in
        # the corpus is what this identifier refers to.
        if ident_name in js_external_imports:
            return
        _emit_indirect_by_name(ident_name, ident, scope_nid, context)

    def _python_dispatch_value_idents(coll_node):
        """Yield the identifier value-nodes of a dict/list/set/tuple literal that are
        function-reference candidates: dict VALUES (never keys), and the elements of a
        list/set/tuple. Nested collections are reached by the caller's own recursion."""
        if coll_node.type == "dictionary":
            for pair in coll_node.children:
                if pair.type == "pair":
                    val = pair.child_by_field_name("value")
                    if val is not None and val.type == "identifier":
                        yield val
        else:  # list / set / tuple
            for el in coll_node.children:
                if el.type == "identifier":
                    yield el

    def _python_ref_value_idents(value_node):
        """Identifiers on the VALUE side of an assignment RHS or a return: a bare name
        (`cb = handler`, `return handler`) or the elements of a bare unpack
        (`a, b = f, g`). A collection LITERAL on the RHS (`cb = [f]`, `cb = (f, g)`) is a
        dispatch table reached by the normal recursion, so it is not handled here."""
        if value_node is None:
            return
        if value_node.type == "identifier":
            yield value_node
        elif value_node.type == "expression_list":
            for ch in value_node.children:
                if ch.type == "identifier":
                    yield ch

    def _getattr_ref_name(call_node):
        """If ``call_node`` is a builtin ``getattr(obj, "name"[, default])`` whose name
        argument is a PLAIN string literal, return ``(name, string_node)``: the string
        names an attribute looked up by that exact name, so it resolves to a callable
        def of the same label. A dynamic name — a variable, an f-string, a concatenation,
        any expression — is not statically resolvable and yields ``None`` (no edge is
        manufactured), as do the 1-arg form and ``obj.getattr(...)`` (a method, not the
        builtin). Unlike an identifier, a string is an attribute name and is never
        shadowed by a param/local, so callers resolve it without the shadow guard.
        """
        fn = call_node.child_by_field_name("function")
        if fn is None or fn.type != "identifier" or _read_text(fn, source) != "getattr":
            return None
        args = call_node.child_by_field_name("arguments")
        if args is None:
            return None
        positional = [c for c in args.children
                      if c.is_named and c.type not in ("keyword_argument", "comment")]
        if len(positional) < 2:
            return None
        name_node = positional[1]
        if name_node.type != "string" or any(
            ch.type == "interpolation" for ch in name_node.children
        ):
            return None  # variable, f-string, concatenation, or expression — dynamic
        content = next(
            (ch for ch in name_node.children if ch.type == "string_content"), None)
        if content is None:
            return None  # empty string "" — no attribute name
        return _read_text(content, source), name_node

    def _php_class_const_scope(n) -> str | None:
        scope = n.child_by_field_name("scope")
        if scope is None:
            for c in n.children:
                if c.is_named and c.type in ("name", "qualified_name", "identifier"):
                    scope = c
                    break
        if scope is None:
            return None
        return _read_text(scope, source)

    _tracked_body_ids: set[object] = set()
    _JS_CLOSURE_TYPES = ("arrow_function", "function_expression")
    # #2575: nested NAMED functions get the same descent as closures. walk()
    # appends only the OUTER declaration's body to function_bodies and never
    # recurses into it, so `function outer(){ function inner(){ helper() } }`
    # hit this boundary and dropped every call (and dynamic import) inside
    # inner. Nested declarations are never in function_bodies, so the
    # _tracked_body_ids guard below still prevents double-walking the
    # top-level ones (those are entered via their own function_bodies entry).
    _JS_DESCEND_TYPES = _JS_CLOSURE_TYPES + (
        "function_declaration", "generator_function_declaration",
        "generator_function")

    def walk_calls(
        node,
        caller_nid: str,
        # Java: flat name -> type. C#: the (scoped bindings, field base) pair
        # from _csharp_method_receiver_types, resolved positionally (#2472).
        receiver_types: dict[str, str] | tuple | None = None,
        extra_locals: frozenset[str] = frozenset(),
    ) -> None:
        if node.type in config.function_boundary_types:
            # JS/TS: an inline/returned closure not separately tracked in
            # function_bodies would otherwise drop its calls at this boundary.
            # Descend into it with the enclosing caller so `return () =>
            # svc.doThing()` links to the caller (#1630). Tracked closures
            # (const-assigned arrows) are walked with their own nid — skip to
            # avoid double-counting.
            if (config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
                    and node.type in _JS_DESCEND_TYPES):
                body = node.child_by_field_name("body")
                if body is not None and body not in _tracked_body_ids:
                    # This closure's own params/locals (`(r) => c.get(r)`) are
                    # scoped to it, not to the enclosing caller_nid — but its
                    # calls ARE attributed to caller_nid right here, so a bare
                    # reference to one of them (e.g. passed on as a call
                    # argument) must still be recognized as local, not resolved
                    # against an unrelated same-named definition elsewhere in
                    # the corpus (#2241). Fold this closure's own bindings into
                    # extra_locals for its subtree only; deeper untracked
                    # closures compound the same way on their own recursion.
                    closure_locals = extra_locals | _js_local_bound_names(node, source)
                    for child in node.children:
                        walk_calls(child, caller_nid, receiver_types, closure_locals)
            return

        # CommonJS imports are valid at any lexical depth.  The module-level
        # pass records top-level require() declarations; this pass owns function
        # bodies, so detect lazy/cycle-breaking requires here and attribute the
        # dependency to the enclosing callable rather than silently dropping it.
        if (config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
                and node.type in ("lexical_declaration", "variable_declaration")):
            _require_imports_js(node, source, caller_nid, stem, edges, str_path)

        if node.type in config.call_types:
            # JS/TS dynamic imports: await import('./foo.js')
            if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                if _dynamic_import_js(node, source, caller_nid, str_path,
                                      edges, seen_dyn_import_pairs):
                    # Still recurse into children (import().then(...) may have calls)
                    for child in node.children:
                        walk_calls(child, caller_nid, receiver_types, extra_locals)
                    return

            callee_name: str | None = None
            is_member_call: bool = False
            is_this_field_call: bool = False
            swift_receiver: str | None = None
            member_receiver: str | None = None
            kotlin_qualified_prefix: str | None = None
            csharp_qualified_prefix: str | None = None
            csharp_arg_count: int | None = None

            # Special handling per language
            if config.ts_module == "tree_sitter_swift":
                # Swift: first child may be simple_identifier or navigation_expression
                first = node.children[0] if node.children else None
                if first:
                    if first.type == "simple_identifier":
                        callee_name = _read_text(first, source)
                    elif first.type == "navigation_expression":
                        is_member_call = True
                        for child in first.children:
                            if child.type == "navigation_suffix":
                                for sc in child.children:
                                    if sc.type == "simple_identifier":
                                        callee_name = _read_text(sc, source)
                        # #1356: capture the receiver so the cross-file pass can
                        # resolve it through the file's type table.
                        recv_node = first.children[0] if first.children else None
                        swift_receiver = _swift_receiver_name(recv_node, source)
            elif config.ts_module == "tree_sitter_kotlin":
                # Kotlin: first child may be simple_identifier/identifier or
                # navigation_expression. PyPI's `tree_sitter_kotlin` produces
                # `identifier` for plain identifier nodes; older grammar
                # versions (including the JVM `io.github.bonede:tree-sitter-kotlin`
                # binding) produce `simple_identifier`. Accept both.
                first = node.children[0] if node.children else None
                if first:
                    if first.type in ("simple_identifier", "identifier"):
                        callee_name = _read_text(first, source)
                    elif first.type == "navigation_expression":
                        is_member_call = True
                        for child in reversed(first.children):
                            if child.type in ("simple_identifier", "identifier"):
                                callee_name = _read_text(child, source)
                                break
                        # #2550: `com.example.Foo.bar()` is a NESTED
                        # navigation_expression chain; the last identifier alone
                        # (`bar`) rarely matches in-file, so the call was dropped
                        # (the shared cross-file pass skips member calls). When
                        # EVERY chain segment is a plain identifier and there are
                        # >= 3 (a real dotted FQN, not `recv.method()`), stamp the
                        # dotted prefix for _resolve_kotlin_qualified_calls.
                        # member_receiver is deliberately NOT set: an uppercase
                        # receiver would trip the capitalized-receiver deferral
                        # below and regress in-file `Foo.bar()` resolution.
                        segments = _kotlin_nav_identifier_segments(first, source)
                        if segments is not None and len(segments) >= 3:
                            kotlin_qualified_prefix = ".".join(segments[:-1])
            elif config.ts_module == "tree_sitter_scala":
                # Scala: first child
                first = node.children[0] if node.children else None
                if first:
                    if first.type == "identifier":
                        callee_name = _read_text(first, source)
                    elif first.type == "field_expression":
                        is_member_call = True
                        field = first.child_by_field_name("field")
                        if field:
                            callee_name = _read_text(field, source)
                        else:
                            for child in reversed(first.children):
                                if child.type == "identifier":
                                    callee_name = _read_text(child, source)
                                    break
            elif config.ts_module == "tree_sitter_php" and node.type == "object_creation_expression":
                # PHP `new Foo(...)` keeps the class in a bare name/qualified_name
                # child (no field), so the generic call path never names it and a
                # class a method only constructs stayed unlinked - the PHP twin of
                # Java's #1373 and C#'s #2997. The types this loses are the ones
                # handed straight to something else (`$bus->dispatch(new Cmd(...))`).
                # A qualified `new \App\Bar()` names the last segment, matching how
                # the PHP namespace pass keys classes; `new $cls()` is dynamic and
                # `new self()`/`static`/`parent` name no other class - all stay
                # unnamed rather than minting junk edges.
                for _oce_child in node.children:
                    if _oce_child.type in ("name", "qualified_name"):
                        _oce_text = _read_text(_oce_child, source).rsplit("\\", 1)[-1]
                        if _oce_text.lower() not in ("self", "static", "parent"):
                            callee_name = _oce_text
                        break
            elif config.ts_module == "tree_sitter_c_sharp" and node.type == "object_creation_expression":
                # `new Foo(...)` keeps the constructed type in the `type` field, so
                # the invocation path below never sees it and a type a method only
                # constructs stays unlinked — the C# twin of the Java gap in #1373.
                # Types reached solely through a method body are exactly the ones
                # this misses: message classes handed straight to a bus
                # (`Send(new OrderPlaced { ... })`) and locally built collaborators.
                # `_read_csharp_type_name` drops the generic arguments and the
                # namespace qualifier, so `new A.B.Cache<string>()` names `Cache`.
                # Target-typed `new()` parses as `implicit_object_creation_expression`
                # and stays out of `call_types`: naming it needs the declared type of
                # whatever it is being assigned to, which is a separate problem.
                # A qualifier written in source is kept for
                # `_resolve_csharp_qualified_calls`, so `new A.B.Cache()` can still
                # pick one of several `Cache` classes instead of hitting the
                # ambiguity guard on the bare name.
                type_info = _read_csharp_type_name(node.child_by_field_name("type"), source)
                if type_info and type_info[0]:
                    callee_name = type_info[0]
                    if type_info[1] and type_info[2]:
                        csharp_qualified_prefix = type_info[2]
                args_node = node.child_by_field_name("arguments")
                if args_node is not None:
                    csharp_arg_count = sum(1 for c in args_node.children if c.type == "argument")
            elif config.ts_module == "tree_sitter_c_sharp" and node.type == "invocation_expression":
                args_node = node.child_by_field_name("arguments")
                if args_node is not None:
                    csharp_arg_count = sum(1 for c in args_node.children if c.type == "argument")
                # C#: the invoked function is the `function` field. A member call
                # `recv.Method(...)` is a member_access_expression (receiver in its
                # `expression` field, method in `name`). Capture a simple-identifier
                # or `this` receiver + set is_member_call so the receiver-typed
                # resolver (_resolve_csharp_member_calls) can bind it to the
                # receiver's declared type. Without this the bare method name matched
                # any same-named method in the corpus, silently mis-resolving
                # `_server.Save()` to an unrelated `Cache.Save()` (#1609).
                fn_node = node.child_by_field_name("function")
                if fn_node is not None and fn_node.type == "member_access_expression":
                    mname = fn_node.child_by_field_name("name")
                    recv = fn_node.child_by_field_name("expression")
                    if mname is not None:
                        # `recv.Get<int>(...)`: the name field is a
                        # generic_name; its raw text carries the type-argument
                        # list, which is not part of the method's identity
                        # (#3406) — read the bare identifier instead.
                        callee_name = _csharp_bare_call_name(mname, source)
                        is_member_call = True
                        if recv is not None and recv.type == "identifier":
                            member_receiver = _read_text(recv, source)
                        elif recv is not None and recv.type in ("this", "this_expression"):
                            member_receiver = "this"
                        elif recv is not None and recv.type in ("base", "base_expression"):
                            # base.M(): resolved against the caller's single
                            # resolvable base class in the cross-file pass.
                            member_receiver = "base"
                        elif recv is not None and recv.type == "member_access_expression":
                            # this.field.M(): the explicit-`this` field access is
                            # typed exactly like a bare `field.M()` via the file
                            # table; any other chained receiver stays untyped
                            # (the resolver bails rather than guessing).
                            inner = recv.child_by_field_name("expression")
                            fname = recv.child_by_field_name("name")
                            if (
                                inner is not None
                                and inner.type in ("this", "this_expression")
                                and fname is not None
                                and fname.type == "identifier"
                            ):
                                member_receiver = _read_text(fname, source)
                elif fn_node is not None and fn_node.type == "identifier":
                    callee_name = _read_text(fn_node, source)
                elif fn_node is not None and fn_node.type == "generic_name":
                    # Unqualified generic call `Get<int>(...)` / `Make<T>()`:
                    # without this arm the raw-text fallback captured
                    # `Get<int>` verbatim, so the call never matched the
                    # `.Get()` member and the edge was silently dropped —
                    # while the non-generic spelling resolved fine (#3406).
                    callee_name = _csharp_bare_call_name(fn_node, source)
                else:
                    # Fallback: original name-field / first-named-child scan.
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        callee_name = _read_text(name_node, source)
                    else:
                        for child in node.children:
                            if child.is_named:
                                raw = _read_text(child, source)
                                if "." in raw:
                                    callee_name = raw.split(".")[-1]
                                    is_member_call = True
                                    parts = raw.split(".")
                                    if len(parts) == 2 and parts[0]:
                                        member_receiver = parts[0]
                                else:
                                    callee_name = raw
                                break
                # C#: emit a `references[generic_arg]` edge for every type
                # argument at the call site (`recv.Do<T>()`, the
                # `services.AddScoped<ISvc, Impl>()` DI shape, static
                # `Foo<IBar>()`). The property/return/parameter branches
                # already walk their declared type for the same reason; the
                # call-site branch didn't, so the type arguments never
                # became nodes and dependency edges were silently erased
                # (#2911). The C# class_declaration's field_declaration and
                # property_declaration branches above are the direct
                # analogue. The call-site function carries its type-arg list
                # either as a `type_argument_list` child on a `generic_name`
                # (static call) or as the same child on the
                # `member_access_expression`'s `name` `generic_name` (member
                # call); the fallback path uses raw text and never sees the
                # structured type-arg list. The class declaration's
                # field_declaration case is closed by the parallel fix in
                # #2913; this branch covers what that PR deliberately left
                # out.
                if fn_node is not None:
                    call_tal = None
                    if fn_node.type == "member_access_expression":
                        ma_name = fn_node.child_by_field_name("name")
                        if ma_name is not None and ma_name.type == "generic_name":
                            for tal_child in ma_name.children:
                                if tal_child.type == "type_argument_list":
                                    call_tal = tal_child
                                    break
                    elif fn_node.type == "generic_name":
                        for tal_child in fn_node.children:
                            if tal_child.type == "type_argument_list":
                                call_tal = tal_child
                                break
                    if call_tal is not None:
                        call_type_params = _csharp_type_parameters_in_scope(node, source)
                        call_line = node.start_point[0] + 1
                        for call_arg in call_tal.children:
                            if not call_arg.is_named:
                                continue
                            call_refs: list[tuple[str, str, bool, str]] = []
                            _csharp_collect_type_refs(
                                call_arg, source, True, call_refs, call_type_params
                            )
                            for call_ref_name, _call_role, call_qualified, call_qualifier in call_refs:
                                call_target = ensure_named_node(call_ref_name, call_line)
                                if call_target == caller_nid:
                                    continue
                                call_meta = {"ref_token": call_ref_name}
                                if call_qualified:
                                    call_meta["qualified"] = True
                                if call_qualifier:
                                    call_meta["ref_qualifier"] = call_qualifier
                                add_edge(caller_nid, call_target, "references",
                                         call_line, context="generic_arg",
                                         metadata=call_meta)
            elif config.ts_module == "tree_sitter_php":
                # PHP: distinguish call expression subtypes
                if node.type == "function_call_expression":
                    func_node = node.child_by_field_name("function")
                    if func_node:
                        callee_name = _read_text(func_node, source)
                elif node.type == "scoped_call_expression":
                    # Static method call: Helper::format() → callee = "Helper"
                    scope_node = node.child_by_field_name("scope")
                    if scope_node:
                        callee_name = _read_text(scope_node, source)
                else:
                    # member_call_expression: $obj->method()
                    is_member_call = True
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        callee_name = _read_text(name_node, source)
            elif config.ts_module == "tree_sitter_cpp":
                # C++: function field, then field_expression/qualified_identifier
                func_node = node.child_by_field_name(config.call_function_field) if config.call_function_field else None
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = _read_text(func_node, source)
                    elif func_node.type == "field_expression":
                        # `f.bar()` / `f->bar()` / `this->bar()`: receiver is the
                        # `argument` (object) field, callee is the `field` (#1547).
                        # Capture a simple-identifier (or `this`) receiver so the
                        # cross-file pass can resolve it through the file's type
                        # table; chained receivers (`a.b.method()`) are left to bail.
                        is_member_call = True
                        name = func_node.child_by_field_name("field")
                        if name:
                            callee_name = _read_text(name, source)
                        obj = func_node.child_by_field_name("argument")
                        if obj is not None and obj.type == "identifier":
                            member_receiver = _read_text(obj, source)
                        elif obj is not None and obj.type == "this":
                            member_receiver = "this"
                    elif func_node.type == "qualified_identifier":
                        # `Foo::bar()`: the scope (`Foo`) is the receiver type named
                        # explicitly in source (EXTRACTED), the name is the callee.
                        is_member_call = True
                        name = func_node.child_by_field_name("name")
                        if name:
                            callee_name = _read_text(name, source)
                        scope = func_node.child_by_field_name("scope")
                        if scope is not None:
                            member_receiver = _read_text(scope, source)
            elif config.ts_module == "tree_sitter_java":
                if node.type == "object_creation_expression":
                    # `new Foo(...)` — the constructed type is in the `type` field,
                    # not `name`, so the generic path misses it (#1373).
                    type_node = node.child_by_field_name("type")
                    if type_node is not None:
                        raw = _read_text(type_node, source).split("<", 1)[0].strip()
                        if raw:
                            callee_name = raw.rsplit(".", 1)[-1]
                elif node.type == "method_invocation":
                    name_node = node.child_by_field_name("name")
                    if name_node is not None:
                        callee_name = _read_text(name_node, source)
                    receiver = node.child_by_field_name("object")
                    if receiver is not None:
                        is_member_call = True
                        if receiver.type == "identifier":
                            member_receiver = _read_text(receiver, source)
                        elif receiver.type == "this":
                            member_receiver = "this"
                        elif receiver.type == "field_access":
                            owner = receiver.child_by_field_name("object")
                            field = receiver.child_by_field_name("field")
                            if owner is not None and owner.type == "this" and field is not None:
                                member_receiver = f"this.{_read_text(field, source)}"
                                is_this_field_call = True
            elif config.ts_module == "tree_sitter_ruby":
                # Ruby's `call` node carries `receiver` and `method` as direct
                # fields (no intermediate accessor node), so the generic accessor
                # model doesn't apply. Read them directly and capture a simple
                # receiver (`p` in `p.run`, `Processor` in `Processor.new`) so the
                # cross-file pass can resolve member calls by the receiver's type.
                meth = node.child_by_field_name("method")
                if meth is not None:
                    callee_name = _read_text(meth, source)
                recv = node.child_by_field_name("receiver")
                if recv is not None:
                    is_member_call = True
                    if recv.type in ("identifier", "constant"):
                        member_receiver = _read_text(recv, source)
                    elif recv.type == "scope_resolution":
                        # Namespaced receiver `Billing::Processor.call` — keep the whole
                        # constant path. Truncating to the last segment discarded the
                        # namespace, so `ActiveRecord::Base.transaction` bound to
                        # whatever single class named `Base` the corpus defined: the
                        # god-node guard only catches an ambiguous match, not a
                        # unique-but-wrong one (#3078).
                        member_receiver = _ruby_const_full_name(recv, source) or None
            else:
                # Generic: get callee from call_function_field (or constructor on new_expression)
                func_node = node.child_by_field_name(config.call_function_field) if config.call_function_field else None
                if func_node is None and node.type == "new_expression":
                    func_node = node.child_by_field_name("constructor")
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = _read_text(func_node, source)
                    elif func_node.type in config.call_accessor_node_types:
                        is_member_call = True
                        if config.call_accessor_field:
                            attr = func_node.child_by_field_name(config.call_accessor_field)
                            if attr:
                                callee_name = _read_text(attr, source)
                        if config.call_accessor_object_field:
                            # Capture a simple-identifier receiver (e.g. `ClassName`
                            # in `ClassName.method()`) so cross-file member-call
                            # resolution can resolve qualified class-method calls
                            # (#1446). Chained receivers (`a.b.method()`) are skipped
                            # UNLESS the chain is `this.field.method()` (#1316).
                            obj = func_node.child_by_field_name(config.call_accessor_object_field)
                            if obj is not None and obj.type == "identifier":
                                member_receiver = _read_text(obj, source)
                            elif (
                                config.ts_module == "tree_sitter_python"
                                and obj is not None
                                and obj.type == "call"
                            ):
                                # ``super().method()`` has a call node as its
                                # receiver. Preserve it as a known intra-class
                                # receiver instead of treating it as unresolved.
                                receiver_func = obj.child_by_field_name("function")
                                if (
                                    receiver_func is not None
                                    and receiver_func.type == "identifier"
                                    and _read_text(receiver_func, source) == "super"
                                ):
                                    member_receiver = "super"
                            elif (obj is not None
                                  and obj.type in config.call_accessor_node_types
                                  and config.call_accessor_object_field):
                                inner_obj = obj.child_by_field_name(config.call_accessor_object_field)
                                if inner_obj is not None and inner_obj.type == "this":
                                    inner_prop = obj.child_by_field_name(config.call_accessor_field)
                                    if inner_prop is not None:
                                        member_receiver = _read_text(inner_prop, source)
                                        is_this_field_call = True
                    else:
                        # Try reading the node directly (e.g. Java name field is the callee)
                        callee_name = _read_text(func_node, source)

            # _LANGUAGE_BUILTIN_GLOBALS is one union across every language, right for
            # a BARE call (String(x) really would become a god node) but wrong for a
            # MEMBER call: `open` is a Python builtin and `Set` a JavaScript one, so
            # session.open() in Swift or _server.Set() in C# named after another
            # language's builtin was silently discarded outright -- no same-file
            # edge, but also no raw_calls entry, so cross-file resolution never even
            # got a chance to try (#3381). A member call carries a receiver, so it
            # is not the ambiguous case the union guards against; letting it through
            # here while forcing tgt_nid = None below (same as the existing
            # receiver-typed defers just past this comment) means it can only ever
            # reach an edge through a guarded, receiver-typed resolver, never the
            # unguarded bare-name path a real god node would need.
            _builtin_member_call = is_member_call and callee_name in _LANGUAGE_BUILTIN_GLOBALS
            if callee_name and (
                callee_name not in _LANGUAGE_BUILTIN_GLOBALS or _builtin_member_call
            ):
                # Python member calls defer to receiver-based resolution unless the
                # receiver is known to stay in the current class. Falling back to a
                # bare method name for an unresolved/lowercase receiver (`d.get()` or
                # `self.store.get()`) can bind to an unrelated module function and
                # inflate it into a god node (#2417). Qualified class/module calls are
                # recovered later by _resolve_python_member_calls when the receiver
                # supplies enough evidence (#1446/#1883). Known recall trade (#2586):
                # a same-file `x = Thing(); x.method()` no longer gets an edge — it
                # came from the same evidence-free bare-name map and could bind wrong
                # under label collision; local-instantiation receiver typing is a
                # separate follow-up.
                # C#: ANY member call with a captured receiver defers to the
                # receiver-typed resolver — a bare method-name match ignores the
                # receiver's declared type and mis-binds to an unrelated same-named
                # method (#1609). The receiver may be lowercase (`_server.Save()`),
                # so this is broader than the capitalized/this-field Python rule.
                _csharp_defer = (
                    config.ts_module == "tree_sitter_c_sharp"
                    and is_member_call and member_receiver
                )
                _python_defer = (
                    config.ts_module == "tree_sitter_python"
                    and is_member_call
                    and member_receiver not in {"self", "cls", "super"}
                )
                _java_defer = (
                    config.ts_module == "tree_sitter_java" and is_member_call
                )
                if _python_defer or _java_defer or _builtin_member_call or (
                    is_member_call
                    and member_receiver
                    and (
                        member_receiver[:1].isupper()
                        or is_this_field_call
                        or _csharp_defer
                    )
                ):
                    tgt_nid = None
                else:
                    if config.ts_module in (
                        "tree_sitter_python", "tree_sitter_javascript", "tree_sitter_typescript"
                    ) and not is_member_call:
                        curr_scope = caller_nid
                        tgt_nid = None
                        while curr_scope:
                            if curr_scope in lexical_nids_by_scope and callee_name in lexical_nids_by_scope[curr_scope]:
                                tgt_nid = lexical_nids_by_scope[curr_scope][callee_name]
                                break
                            curr_scope = scope_parents.get(curr_scope)
                        if not tgt_nid:
                            tgt_nid = label_to_nid.get(callee_name)
                    else:
                        if config.ts_module == "tree_sitter_c_sharp":
                            overload_tgt = None
                            caller_class = csharp_caller_class.get(caller_nid)
                            if caller_class and (caller_class, callee_name) in csharp_class_methods:
                                candidates = csharp_class_methods[(caller_class, callee_name)]
                                if csharp_arg_count is not None:
                                    matched = [c_nid for c_nid, c_ar in candidates if c_ar == csharp_arg_count]
                                    if len(matched) == 1:
                                        overload_tgt = matched[0]
                                if not overload_tgt and len(candidates) == 1:
                                    overload_tgt = candidates[0][0]
                            tgt_nid = overload_tgt or label_to_nid.get(callee_name)
                        else:
                            tgt_nid = label_to_nid.get(callee_name)
                    # A qualified `new A.B.Foo()` whose bare name matches only a
                    # sourceless stub in this file would bind the call to the stub
                    # and never reach _resolve_csharp_qualified_calls, the one pass
                    # that can honour the namespace. Defer so it can.
                    if (
                        csharp_qualified_prefix
                        and tgt_nid
                        and not nid_to_sf.get(tgt_nid)
                    ):
                        tgt_nid = None
                # A name-only reference can leave a source-less stub behind when
                # the target is outside the scanned corpus (for example,
                # ``HTTPException`` imported from FastAPI). It is useful for the
                # type/reference edge, but it is not a project-defined callable
                # and must not become a calls hub. Real definitions have a
                # source_file and continue through the normal call path.
                external_stub_target = bool(tgt_nid and not nid_to_sf.get(tgt_nid))
                if tgt_nid and not external_stub_target:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "context": "call",
                            "confidence": "EXTRACTED",
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })
                elif callee_name and not tgt_nid:
                    # In Python, if an unqualified call names a local non-callable variable or parameter,
                    # do NOT append it to raw_calls (#3405 Part 3/4).
                    is_py_local_data = (
                        config.ts_module == "tree_sitter_python"
                        and not is_member_call
                        and callee_name in (local_bound_names.get(caller_nid, frozenset()) | extra_locals)
                    )
                    if not is_py_local_data:
                        # Callee not in this file — save for cross-file resolution in extract()
                        rc_entry = {
                            "caller_nid": caller_nid,
                            "callee": callee_name,
                            "is_member_call": is_member_call,
                            "source_file": str_path,
                            "source_location": f"L{node.start_point[0] + 1}",
                            "receiver": swift_receiver or member_receiver,
                        }
                        # Ruby: attach the receiver's inferred type from the method's
                        # local `var = Const.new` bindings, when unambiguously known.
                        if member_receiver and config.ts_module == "tree_sitter_ruby":
                            rc_entry["receiver_type"] = ruby_var_types.get(
                                caller_nid, {}
                            ).get(member_receiver)
                        # Tag the C++ raw_call's language so the cross-file C++ resolver
                        # claims it unambiguously: a `.h` file routes to extract_cpp or
                        # extract_objc by content, and both resolvers see `.h` in their
                        # suffix sets, so a source_file suffix alone can't separate them.
                        if config.ts_module == "tree_sitter_cpp":
                            rc_entry["lang"] = "cpp"
                        # C#: tag the raw_call so _resolve_csharp_member_calls claims
                        # it, and stamp the receiver's type from the method's SCOPED
                        # bindings by the call's byte offset (#1609, per-method since
                        # #2299, position-aware since #2472). `this.field.M()` is
                        # covered too: member_receiver is the bare field name, and
                        # class fields/properties are the base scope.
                        if config.ts_module == "tree_sitter_c_sharp":
                            rc_entry["lang"] = "csharp"
                            if csharp_arg_count is not None:
                                rc_entry["arg_count"] = csharp_arg_count
                            if csharp_qualified_prefix:
                                rc_entry["qualified_prefix"] = csharp_qualified_prefix
                            receiver_type = _csharp_scoped_receiver_type(
                                receiver_types, member_receiver, node.start_byte
                            )
                            if receiver_type:
                                rc_entry["receiver_type"] = receiver_type
                        if config.ts_module == "tree_sitter_java":
                            rc_entry["lang"] = "java"
                            receiver_type = (receiver_types or {}).get(member_receiver or "")
                            if receiver_type:
                                rc_entry["receiver_type"] = receiver_type
                        # Kotlin fully-qualified call (#2550): the dotted prefix +
                        # lang tag let _resolve_kotlin_qualified_calls claim it.
                        if kotlin_qualified_prefix:
                            rc_entry["lang"] = "kotlin"
                            rc_entry["qualified_prefix"] = kotlin_qualified_prefix
                        raw_calls.append(rc_entry)

            # Indirect dispatch: a function passed BY NAME as a call argument
            # (executor.submit(fn), Thread(target=fn), map(fn, xs)) is a real dependency
            # the callee-only scan above can't see. Emit it as a distinct `indirect_call`
            # relation so strict `calls` queries stay precise while affected/blast-radius
            # picks up the edge. Python only for now; dispatch via dict literals, getattr
            # or decorators lives in other AST nodes and is left to a follow-up.
            #
            # Emission is general across call targets (no submit/map/Thread allow-list):
            # the value is catching a callback passed to ANY function. Two guards keep
            # it sound — without them an identifier merely matching a node label produced
            # false edges for the idiomatic shadow case and for plain data variables:
            #   1. SHADOWING — skip an argument that is a parameter or local binding of
            #      the enclosing function; it names a local value, not the module fn.
            #   2. CALLABLE TARGET — resolve only to a function / method / class def, so
            #      `process(config)` can't point at a same-named non-callable node.
            if config.ts_module == "tree_sitter_python":
                args_node = node.child_by_field_name("arguments")
                if args_node is not None:
                    enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
                    for arg in args_node.children:
                        if arg.type == "identifier":
                            _emit_indirect_ref(arg, caller_nid, enclosing_locals, "argument")
                        elif arg.type == "keyword_argument":
                            _emit_indirect_ref(
                                arg.child_by_field_name("value"),
                                caller_nid, enclosing_locals, "argument")
                # Reflective dispatch: getattr(obj, "handler") names a callable by
                # string literal (#1566 slice 3). The string is an ATTRIBUTE name, not
                # an identifier binding, so it is never shadowed by a param/local — it
                # resolves straight to the callable, bypassing the identifier shadow
                # guard. A dynamic name (getattr(obj, name)) is unresolvable → no edge.
                getattr_ref = _getattr_ref_name(node)
                if getattr_ref is not None:
                    ref_name, loc = getattr_ref
                    _emit_indirect_by_name(ref_name, loc, caller_nid, "getattr")
            elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                # JS/TS: a callback passed by name (`arr.map(fn)`, `setTimeout(fn)`,
                # `el.addEventListener("x", fn)`). Positional identifier args only —
                # inline arrows/function expressions are direct definitions, not a
                # by-name reference. No keyword args in JS (named args are objects,
                # handled by the collection pass).
                args_node = node.child_by_field_name("arguments")
                if args_node is not None:
                    enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
                    for arg in args_node.children:
                        if arg.type == "identifier":
                            _emit_indirect_ref(arg, caller_nid, enclosing_locals, "argument")

            # Helper function calls: config('foo.bar') → uses_config edge to "foo"
            if (callee_name and callee_name in config.helper_fn_names):
                args_node = node.child_by_field_name("arguments")
                first_key: str | None = None
                if args_node:
                    for arg in args_node.children:
                        if arg.type != "argument":
                            continue
                        for inner in arg.children:
                            if inner.type == "string":
                                for sc in inner.children:
                                    if sc.type == "string_content":
                                        first_key = _read_text(sc, source)
                                        break
                                break
                        if first_key:
                            break
                if first_key:
                    segment = first_key.split(".")[0]
                    tgt_nid = (label_to_nid_ci.get(segment.lower())
                               or label_to_nid_ci.get(f"{segment}.php".lower()))
                    if tgt_nid and tgt_nid != caller_nid:
                        relation = f"uses_{callee_name}"
                        pair3 = (caller_nid, tgt_nid, relation)
                        if pair3 not in seen_helper_ref_pairs:
                            seen_helper_ref_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append({
                                "source": caller_nid,
                                "target": tgt_nid,
                                "relation": relation,
                                "confidence": "EXTRACTED",
                                "confidence_score": 1.0,
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 1.0,
                            })

            # Service container bindings: $this->app->bind(Foo::class, Bar::class)
            if (node.type == "member_call_expression"
                    and callee_name
                    and callee_name in config.container_bind_methods):
                args_node = node.child_by_field_name("arguments")
                class_args: list[str] = []
                if args_node:
                    for arg in args_node.children:
                        if arg.type != "argument":
                            continue
                        for inner in arg.children:
                            if inner.type == "class_constant_access_expression":
                                cls = _php_class_const_scope(inner)
                                if cls:
                                    class_args.append(cls)
                                break
                        if len(class_args) >= 2:
                            break
                if len(class_args) == 2:
                    contract_name, impl_name = class_args
                    contract_nid = label_to_nid_ci.get(contract_name.lower())
                    impl_nid = label_to_nid_ci.get(impl_name.lower())
                    if contract_nid and impl_nid and contract_nid != impl_nid:
                        pair3 = (contract_nid, impl_nid, "bound_to")
                        if pair3 not in seen_bind_pairs:
                            seen_bind_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append({
                                "source": contract_nid,
                                "target": impl_nid,
                                "relation": "bound_to",
                                "confidence": "EXTRACTED",
                                "confidence_score": 1.0,
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 1.0,
                            })

        # Static property access: Foo::$bar → uses_static_prop edge
        if node.type in config.static_prop_types:
            scope_node = node.child_by_field_name("scope")
            if scope_node is None:
                for child in node.children:
                    if child.is_named and child.type in ("name", "qualified_name", "identifier"):
                        scope_node = child
                        break
            if scope_node is not None:
                class_name = _read_text(scope_node, source)
                tgt_nid = label_to_nid_ci.get(class_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, "uses_static_prop")
                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "uses_static_prop",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })

        # PHP class constant access: Foo::BAR → references_constant edge
        if config.ts_module == "tree_sitter_php" and node.type == "class_constant_access_expression":
            class_name = _php_class_const_scope(node)
            if class_name:
                tgt_nid = label_to_nid_ci.get(class_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, "references_constant")
                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "references_constant",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })

        # Dispatch tables (#1566): a function listed as a value in a dict/list/set/
        # tuple literal inside this body is an indirect dependency of the enclosing
        # function. Reuses the shared resolve-and-emit guard (callable-target-only,
        # not shadowed by a param/local, cross-file deferral).
        if config.ts_module == "tree_sitter_python" and node.type in (
            "dictionary", "list", "set", "tuple"
        ):
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            for ident in _python_dispatch_value_idents(node):
                _emit_indirect_ref(ident, caller_nid, enclosing_locals, "collection")
        elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript") \
                and node.type in ("object", "array"):
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            for ident in _js_dispatch_value_idents(node):
                _emit_indirect_ref(ident, caller_nid, enclosing_locals, "collection")

        # Assignment / return references (#1566 slice 2): a function bound to a name
        # (cb = handler) or returned from a factory (return handler) is an indirect
        # dependency of the enclosing function. The VALUE side only -- the assignment
        # TARGET is a new local binding, not a reference -- so the shared shadow guard
        # still holds (a param/local named on the RHS is the local, not the module fn).
        if config.ts_module == "tree_sitter_python" and node.type == "assignment":
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            for ident in _python_ref_value_idents(node.child_by_field_name("right")):
                _emit_indirect_ref(ident, caller_nid, enclosing_locals, "assignment")
        elif config.ts_module == "tree_sitter_python" and node.type == "return_statement":
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            value = next((c for c in node.children if c.is_named), None)
            for ident in _python_ref_value_idents(value):
                _emit_indirect_ref(ident, caller_nid, enclosing_locals, "return")

        # `catch (e)` binds through the clause's own `parameter` field, never a
        # variable_declarator, so `_js_local_bound_names` never sees it: a one-letter
        # binding passed on as a call argument in the handler read as a by-name
        # reference to a same-named callable elsewhere in the corpus (minified bundles
        # supply one for nearly every letter). The binding is scoped to the clause, so
        # fold it into extra_locals for that subtree only — same shape as the untracked
        # closure fold above (#2241) — leaving references outside the block resolvable.
        if (
            config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript")
            and node.type == "catch_clause"
        ):
            param = node.child_by_field_name("parameter")  # absent for ES2019 `catch {}`
            if param is not None:
                caught: set[str] = set()
                _js_collect_pattern_idents(param, source, caught)
                extra_locals = extra_locals | frozenset(caught)

        for child in node.children:
            walk_calls(child, caller_nid, receiver_types, extra_locals)

    if config.ts_module == "tree_sitter_ruby":
        for caller_nid, body_node in function_bodies:
            ruby_var_types[caller_nid] = _ruby_local_class_bindings(body_node, source)

    # C++: build the per-file `var -> ClassName` table from local declarations in
    # every function body so the cross-file member-call pass can type a receiver
    # (#1547). File-scoped (not per-body): a later body's `Foo f;` doesn't clobber
    # an earlier binding (`var not in table`), keeping resolution conservative.
    if config.ts_module == "tree_sitter_cpp":
        for _caller_nid, body_node in function_bodies:
            _cpp_local_var_types(body_node, source, type_table)

    # Swift: type local `let x = Type()` / `let x = Type.shared` bindings inside
    # method bodies so `x.method()` on a later line resolves — class-level
    # properties are typed in the walk, but method-body locals were not (#1604).
    if config.ts_module == "tree_sitter_swift":
        for _caller_nid, body_node in function_bodies:
            _swift_local_var_types(body_node, source, type_table,
                                   factory=swift_factory_bindings)

    # JS/TS: bodies already walked with their own caller_nid (const-assigned
    # arrows, methods). An INLINE/returned arrow or function-expression that is
    # NOT separately tracked (e.g. `return () => svc.doThing()`) is otherwise
    # skipped at the arrow boundary in walk_calls, losing its calls — so let
    # walk_calls descend into such untracked closures with the enclosing caller
    # (#1630 Pattern B). Guarding on the tracked set prevents double-walking.
    _tracked_body_ids.update(b for _, b in function_bodies)

    # Body ids are unique (one language per file), so the Java (flat) and C#
    # (scoped, #2472) per-method receiver tables merge without collision — the
    # stamp site branches on language to read the matching shape.
    receiver_types_by_body = {**java_receiver_types, **csharp_receiver_types}
    for caller_nid, body_node in function_bodies:
        walk_calls(
            body_node,
            caller_nid,
            receiver_types_by_body.get(id(body_node)),
            frozenset(closure_locals_by_body.get(id(body_node), ())),
        )

    # #1356: walk property/field initializers (collected above). walk_calls
    # self-guards against re-entering function bodies and dedups via
    # seen_call_pairs, so a closure inside an initializer is not double-walked.
    for owner_nid, init_node in initializer_nodes:
        walk_calls(init_node, owner_nid)

    # ── Event listener pass ───────────────────────────────────────────────────
    seen_listen_pairs: set[tuple[str, str]] = set()
    for event_name, listener_name, line in pending_listen_edges:
        event_nid = label_to_nid_ci.get(event_name.lower())
        listener_nid = label_to_nid_ci.get(listener_name.lower())
        if not event_nid or not listener_nid or event_nid == listener_nid:
            continue
        pair2 = (event_nid, listener_nid)
        if pair2 in seen_listen_pairs:
            continue
        seen_listen_pairs.add(pair2)
        edges.append({
            "source": event_nid,
            "target": listener_nid,
            "relation": "listened_by",
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    # ── Module-level dispatch tables (#1566) ──────────────────────────────────
    # A function listed as a value in a TOP-LEVEL dict/list/set/tuple literal (a
    # route / handler registry) is an indirect dependency of the file. Attributed
    # to the file node. Function and class bodies are walked above, so this scan
    # stops at their boundaries — it must not re-attribute a method's local table
    # to the file, and class-attribute tables are a later refinement.
    if config.ts_module == "tree_sitter_python":
        module_bound = _python_module_bound_names(root, source)

        def _scan_module_dispatch(n) -> None:
            if n.type in ("function_definition", "class_definition"):
                return
            if n.type in ("dictionary", "list", "set", "tuple"):
                for ident in _python_dispatch_value_idents(n):
                    _emit_indirect_ref(ident, file_nid, module_bound, "collection")
            elif n.type == "assignment":
                # Module-level alias / re-export: CALLBACK = handler
                for ident in _python_ref_value_idents(n.child_by_field_name("right")):
                    _emit_indirect_ref(ident, file_nid, module_bound, "assignment")
            elif n.type == "call":
                # Module-level reflective dispatch: HANDLER = getattr(mod, "handler")
                # (#1566 slice 3). Attributed to the file node, like a module table.
                getattr_ref = _getattr_ref_name(n)
                if getattr_ref is not None:
                    ref_name, loc = getattr_ref
                    _emit_indirect_by_name(ref_name, loc, file_nid, "getattr")
            for c in n.children:
                _scan_module_dispatch(c)

        _scan_module_dispatch(root)
    elif config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
        js_module_bound = _js_module_bound_names(root, source)

        def _scan_js_module_dispatch(n) -> None:
            if n.type in _JS_SCOPE_BOUNDARY:
                return  # function / class bodies are walked separately
            if n.type in ("object", "array"):
                for ident in _js_dispatch_value_idents(n):
                    _emit_indirect_ref(ident, file_nid, js_module_bound, "collection")
            elif n.type in ("call_expression", "new_expression"):
                # Module-level callback registration is idiomatic in JS — Express
                # routes (`app.get("/", handler)`), event wiring (`emitter.on("e",
                # handler)`), `setTimeout(fn)`. Capture identifier args as indirect
                # refs of the file (inline arrows are direct defs, not by-name refs).
                margs = n.child_by_field_name("arguments")
                if margs is not None:
                    for marg in margs.children:
                        if marg.type == "identifier":
                            _emit_indirect_ref(marg, file_nid, js_module_bound, "argument")
            for c in n.children:
                _scan_js_module_dispatch(c)

        _scan_js_module_dispatch(root)

    # ── Clean edges ───────────────────────────────────────────────────────────
    valid_ids = seen_ids
    clean_edges = []
    # Byte-identical duplicates collapse to one edge (#3251): a signature that
    # annotates the same type twice (``def f(a: Path, b: Path)``) is ONE
    # reference relationship at one location, but the per-occurrence emission
    # loops above append it once per annotation — in every language block, since
    # neither add_edge nor the raw appends de-duplicate. The copies carry zero
    # information (build's dedup drops them anyway) and their only observable
    # effect is tripping diagnose_extraction's exact_duplicate_edges health
    # warning. Only edges whose ENTIRE payload is identical collapse; any
    # differing field (source_location, context, metadata, …) keeps both.
    _seen_edge_payloads: set[str] = set()
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (tgt in valid_ids or edge["relation"] in ("imports", "imports_from", "re_exports")):
            payload = json.dumps(edge, sort_keys=True, default=str)
            if payload in _seen_edge_payloads:
                continue
            _seen_edge_payloads.add(payload)
            clean_edges.append(edge)

    # Ruby mixins were collected during the node walk (before raw_calls existed);
    # fold them in so the cross-file resolver sees them (#1668).
    if _ruby_mixin_calls:
        raw_calls.extend(_ruby_mixin_calls)
    result = {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}
    # Export the per-file field->type tables for the corpus member-call
    # resolvers (#3151): a field declared on a superclass in ANOTHER file can
    # only be typed once the whole graph is visible. Keyed by class label +
    # source_file, never by node id - ids are rewritten by the #1529 remap
    # passes and the cache portability rewrite, which is exactly how the
    # id-keyed ObjC table went stale (#3150).
    _field_table_export = [
        {"lang": _lang, "class_label": _n.get("label"),
         "source_file": _n.get("source_file"), "fields": dict(_tbl)}
        for _lang, _tables in (("java", java_field_types), ("csharp", csharp_field_types))
        for _cls, _tbl in _tables.items()
        if _tbl
        for _n in (next((x for x in nodes if x["id"] == _cls), None),)
        if _n is not None and _n.get("label")
    ]
    if _field_table_export:
        result["member_field_tables"] = _field_table_export
    # #2551: the parser recovered from syntax errors, so extraction may be
    # partial (in the worst case, nothing but the file node). Record the first
    # error's line so extract() can warn instead of reporting silent success.
    # Rides on the result dict, so it survives the per-file AST cache.
    if root.has_error:
        result["parse_errors"] = {
            "first_error_line": _first_parse_error_line(root),
            "multiline_error": _has_multiline_error(root),
        }
    # Kotlin (#2526/#2550): the declared package qualifies every node in the
    # file; the import-target and qualified-call resolvers key their per-package
    # symbol indexes off it.
    if config.ts_module == "tree_sitter_kotlin":
        _pkg = _kotlin_package_name(root, source)
        if _pkg:
            result["kotlin_package"] = _pkg
    if callable_def_nids:
        # Mark function / method / class defs with a `_callable` attribute so the
        # cross-file indirect_call pass can resolve a by-name callback only to a real
        # callable (never a same-named data symbol). A marker rides on the node dict
        # and survives the id-remap / disambiguation passes in extract(); a pre-remap
        # id set would go stale and silently drop every cross-file indirect edge when
        # ids are relativized (#1566 regression). Stripped before output, like origin_file.
        for n in nodes:
            if n["id"] in callable_def_nids:
                n["_callable"] = True
                if n["id"] in callable_class_nids:
                    # Class def: callable only via constructor. The indirect_call
                    # guard excludes these to avoid false edges (#2137).
                    n["_callable_class"] = True
    if swift_extensions:
        result["swift_extensions"] = swift_extensions
    # TS/JS: augment the constructor-injection type table with local `new`
    # bindings and type-annotated parameters, so `const s = new Svc(); s.m()` and
    # a call on a typed param (incl. inside a closure) resolve (#1630). The
    # constructor-injection entries are populated during the walk above and win on
    # a name clash (first-binding-wins in the helper).
    ts_class_tables: dict[str, dict[str, str]] = {}
    if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
        _ts_receiver_type_table(root, source, type_table, ts_class_tables)
    if config.ts_module == "tree_sitter_swift":
        if type_table or swift_factory_bindings:
            result["swift_type_table"] = {"path": str_path, "table": type_table}
            if swift_factory_bindings:
                # Lists, not tuples: the value must round-trip the JSON AST cache.
                result["swift_type_table"]["factory"] = {
                    k: list(v) for k, v in swift_factory_bindings.items()
                }
    elif type_table or ts_class_tables:
        if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
            result["ts_type_table"] = {
                "path": str_path,
                "table": type_table,
                "classes": ts_class_tables,
            }
        elif config.ts_module == "tree_sitter_cpp":
            result["cpp_type_table"] = {"path": str_path, "table": type_table}
    return result
