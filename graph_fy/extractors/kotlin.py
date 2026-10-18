"""Kotlin and Scala AST extractors."""
from __future__ import annotations

from pathlib import Path

from graph_fy.extractors.base import _make_id, _read_text
from graph_fy.extractors.models import LanguageConfig

from graph_fy.security import sanitize_metadata


def _import_kotlin(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    path_node = node.child_by_field_name("path")
    if path_node is None:
        path_node = next(
            (c for c in node.children if c.type == "qualified_identifier"), None
        )
    if path_node is not None:
        raw = _read_text(path_node, source).strip()
    else:
        raw = next(
            (_read_text(c, source).strip() for c in node.children
             if c.type == "identifier"),
            "",
        )
    if not raw:
        return
    # Wildcard (`import a.b.*`): imports a whole package, not a symbol.
    if raw.endswith(".*") or raw == "*" or any(c.type == "*" for c in node.children):
        return
    # Alias (`import a.b.C as D`): the alias is the identifier child after `as`.
    alias = None
    saw_as = False
    for child in node.children:
        if not saw_as:
            saw_as = child.type == "as"
        elif child.type in ("identifier", "simple_identifier"):
            alias = _read_text(child, source).strip() or None
            break
    module_name = raw.split(".")[-1].strip()
    if not module_name:
        return
    edges.append({
        "source": file_nid,
        "target": _make_id(module_name),
        "relation": "imports",
        "context": "import",
        "confidence": "EXTRACTED",
        "source_file": str_path,
        "source_location": f"L{node.start_point[0] + 1}",
        "weight": 1.0,
        "metadata": sanitize_metadata({k: v for k, v in
            {"target_fqn": raw, "alias": alias}.items() if v is not None}),
    })


def _import_scala(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    for child in node.children:
        if child.type in ("stable_id", "identifier"):
            raw = _read_text(child, source)
            module_name = raw.split(".")[-1].strip("{} ")
            if module_name and module_name != "_":
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


_KOTLIN_CONFIG = LanguageConfig(
    ts_module="tree_sitter_kotlin",
    class_types=frozenset({"class_declaration", "object_declaration"}),
    function_types=frozenset({"function_declaration"}),
    import_types=frozenset({"import_header", "import"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"navigation_expression"}),
    call_accessor_field="",
    name_fallback_child_types=("simple_identifier", "identifier"),
    body_fallback_child_types=("function_body", "class_body", "enum_class_body"),
    function_boundary_types=frozenset({"function_declaration"}),
    import_handler=_import_kotlin,
)

_SCALA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_scala",
    class_types=frozenset({"class_definition", "object_definition", "trait_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"field_expression"}),
    call_accessor_field="field",
    name_fallback_child_types=("identifier",),
    body_fallback_child_types=("template_body",),
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_scala,
)


def _kotlin_user_type_name(user_type_node, source: bytes) -> str | None:
    """Return the tail identifier text from a Kotlin user_type node (without generics).

    A qualified supertype like `com.example.Base` lists its segments as flat
    `identifier` children (`com`, `example`, `Base`) separated by `.` tokens, so
    the real type is the LAST segment, not the first — returning the head yielded
    the package root (`com`). Type arguments live in a separate `type_arguments`
    child, so scanning direct children and keeping the last identifier/
    type_identifier segment ignores generics correctly (mirrors the C++ qualified
    base handling, which uses the unqualified tail)."""
    if user_type_node is None:
        return None
    name: str | None = None
    for c in user_type_node.children:
        if c.type in ("type_identifier", "identifier"):
            text = _read_text(c, source)
            if text:
                name = text
        elif c.type == "simple_user_type":
            for sub in c.children:
                if sub.type in ("identifier", "type_identifier"):
                    text = _read_text(sub, source)
                    if text:
                        name = text
                    break
    return name

def _kotlin_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Kotlin type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t in ("integral_literal", "boolean_literal"):
        return
    if t == "user_type":
        for c in node.children:
            if c.type in ("identifier", "type_identifier"):
                text = _read_text(c, source)
                if text and text not in _KOTLIN_BUILTIN_TYPES and text not in _JAVA_BUILTIN_TYPES:
                    out.append((text, "generic_arg" if generic else "type"))
                break
            if c.type == "simple_user_type":
                for sub in c.children:
                    if sub.type in ("identifier", "type_identifier"):
                        text = _read_text(sub, source)
                        if text and text not in _KOTLIN_BUILTIN_TYPES and text not in _JAVA_BUILTIN_TYPES:
                            out.append((text, "generic_arg" if generic else "type"))
                        break
                break
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.type == "type_projection":
                        for sub in arg.children:
                            if sub.is_named:
                                _kotlin_collect_type_refs(sub, source, True, out)
                    elif arg.is_named:
                        _kotlin_collect_type_refs(arg, source, True, out)
        return
    if t in ("identifier", "type_identifier"):
        text = _read_text(node, source)
        if text and text not in _KOTLIN_BUILTIN_TYPES and text not in _JAVA_BUILTIN_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in ("nullable_type", "parenthesized_type", "type_reference"):
        for c in node.children:
            if c.is_named:
                _kotlin_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _kotlin_collect_type_refs(c, source, generic, out)

def _kotlin_property_type_node(property_node):
    """Find the user_type node within a Kotlin property_declaration."""
    for c in property_node.children:
        if c.type == "variable_declaration":
            for sub in c.children:
                if sub.type in ("user_type", "nullable_type", "type_reference"):
                    return sub
        if c.type in ("user_type", "nullable_type", "type_reference"):
            return c
    return None

def _kotlin_function_return_type_node(func_node):
    """Find the return-type node of a Kotlin function_declaration (the type after `: ` post-params)."""
    saw_params = False
    saw_colon = False
    for c in func_node.children:
        if c.type == "function_value_parameters":
            saw_params = True
            continue
        if saw_params and c.type == ":":
            saw_colon = True
            continue
        if saw_colon:
            if c.is_named:
                return c
    return None

def _scala_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Scala type expression; append (name, role) tuples.
    Handles type_identifier, generic_type (List[T]), and common type wrappers."""
    if node is None:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        base = node.child_by_field_name("type")
        if base is None:
            for c in node.children:
                if c.type == "type_identifier":
                    base = c
                    break
        if base is not None and base.type == "type_identifier":
            text = _read_text(base, source)
            if text:
                out.append((text, "generic_arg" if generic else "type"))
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _scala_collect_type_refs(arg, source, True, out)
        return
    if t in ("compound_type", "infix_type", "function_type", "tuple_type",
             "annotated_type", "projected_type"):
        for c in node.children:
            if c.is_named:
                _scala_collect_type_refs(c, source, generic, out)

def _kotlin_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                       nodes: list, edges: list, seen_ids: set, function_bodies: list,
                       parent_class_nid: str | None, add_node_fn, add_edge_fn,
                       walk_fn) -> bool:
    """Handle enum_entry for Kotlin. Returns True if handled (#1700 Kotlin half)."""
    if node.type == "enum_entry" and parent_class_nid:
        name_node = None
        for child in node.children:
            if child.type in ("simple_identifier", "identifier"):
                name_node = child
                break
        if name_node is None:
            return True
        const_name = _read_text(name_node, source)
        line = node.start_point[0] + 1
        const_nid = _make_id(parent_class_nid, const_name)
        add_node_fn(const_nid, const_name, line)
        add_edge_fn(parent_class_nid, const_nid, "case_of", line)
        for child in node.children:
            if child.type == "class_body":
                for member in child.children:
                    walk_fn(member, parent_class_nid=const_nid)
        return True
    return False


def _kotlin_package_name(root, source: bytes) -> str | None:
    """Dotted package FQN from the file's ``package_header``, or None.

    Grammar 1.1.0 puts the path in a ``qualified_identifier`` child; older
    forks use an ``identifier`` that spans the whole dotted text. Either way
    the node's text IS the FQN.
    """
    for child in root.children:
        if child.type != "package_header":
            continue
        for c in child.children:
            if c.type in ("qualified_identifier", "identifier"):
                pkg = _read_text(c, source).strip()
                return pkg or None
        return None
    return None


def _kotlin_nav_identifier_segments(nav, source: bytes) -> list[str] | None:
    """Flatten a Kotlin ``navigation_expression`` chain into its dotted
    identifier segments (``com.example.Foo.bar`` -> [com, example, Foo, bar]).

    Returns None when any segment is not a plain identifier — a receiver that
    is an expression, a call, ``this``, a string literal, etc. must never read
    as a qualified name (#2550). Older grammars with a different navigation
    shape also bail here, preserving their current behavior.
    """
    segments: list[str] = []
    node = nav
    while node is not None and node.type == "navigation_expression":
        named = [c for c in node.children if c.is_named]
        # Grammar 1.1.0 shape: <receiver> "." <identifier> (the dot is unnamed).
        if len(named) != 2:
            return None
        head, tail = named
        if tail.type not in ("simple_identifier", "identifier"):
            return None
        segments.append(_read_text(tail, source))
        node = head
    if node is None or node.type not in ("simple_identifier", "identifier"):
        return None
    segments.append(_read_text(node, source))
    segments.reverse()
    return segments


def extract_kotlin(path: Path) -> dict:
    """Extract classes, objects, functions, and imports from a .kt/.kts file."""
    from graph_fy.extractors.engine import _extract_generic
    return _extract_generic(path, _KOTLIN_CONFIG)


def extract_scala(path: Path) -> dict:
    """Extract classes, objects, functions, and imports from a .scala file."""
    from graph_fy.extractors.engine import _extract_generic
    return _extract_generic(path, _SCALA_CONFIG)
