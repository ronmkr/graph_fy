"""PHP AST extractor."""
from __future__ import annotations

from pathlib import Path

from graph_fy.extractors.base import _make_id, _read_text
from graph_fy.extractors.models import LanguageConfig



def _import_php(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    saw_as = False
    module_name = None
    for child in node.children:
        if child.type == "as":
            saw_as = True
            continue
        if child.type in ("qualified_name", "name", "identifier"):
            bare = _read_text(child, source).split("\\")[-1].strip()
            if saw_as:
                module_name = bare
                break
            if module_name is None:
                module_name = bare
    if module_name:
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


_PHP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_php",
    ts_language_fn="language_php",
    class_types=frozenset({
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "trait_declaration",
    }),
    function_types=frozenset({"function_definition", "method_declaration"}),
    import_types=frozenset({"namespace_use_clause"}),
    call_types=frozenset({"function_call_expression", "member_call_expression", "scoped_call_expression", "class_constant_access_expression", "object_creation_expression"}),
    static_prop_types=frozenset({"scoped_property_access_expression"}),
    helper_fn_names=frozenset({"config"}),
    container_bind_methods=frozenset({"bind", "singleton", "scoped", "instance"}),
    event_listener_properties=frozenset({"listen", "subscribe"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_call_expression"}),
    call_accessor_field="name",
    name_fallback_child_types=("name",),
    body_fallback_child_types=("declaration_list", "compound_statement", "enum_declaration_list"),
    function_boundary_types=frozenset({"function_definition", "method_declaration"}),
    import_handler=_import_php,
)


def _php_name_text(node, source: bytes) -> str | None:
    """Return the unqualified name text from a PHP `name`/`qualified_name` node."""
    if node is None:
        return None
    return _read_text(node, source).rsplit("\\", 1)[-1] or None

def _php_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a PHP type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t == "primitive_type":
        return
    if t == "named_type":
        for c in node.children:
            if c.type in ("name", "qualified_name"):
                text = _php_name_text(c, source)
                if text:
                    out.append((text, "generic_arg" if generic else "type"))
                return
        return
    if t in ("name", "qualified_name"):
        text = _php_name_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in ("nullable_type", "union_type", "intersection_type", "optional_type"):
        for c in node.children:
            if c.is_named:
                _php_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _php_collect_type_refs(c, source, generic, out)

def _php_method_return_type_node(method_node):
    """Return the named_type/primitive_type node sitting after formal_parameters."""
    saw_params = False
    for c in method_node.children:
        if c.type == "formal_parameters":
            saw_params = True
            continue
        if saw_params and c.is_named and c.type not in ("compound_statement",):
            if c.type in ("named_type", "primitive_type", "nullable_type",
                          "union_type", "intersection_type", "optional_type"):
                return c
    return None


def extract_php(path: Path) -> dict:
    """Extract classes, functions, methods, namespace uses, and calls from a .php file."""
    from graph_fy.extractors.engine import _extract_generic
    return _extract_generic(path, _PHP_CONFIG)
