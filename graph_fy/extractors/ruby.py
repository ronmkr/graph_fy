"""Ruby AST extractor."""
from __future__ import annotations

from pathlib import Path

from graph_fy.extractors.base import _LANGUAGE_BUILTIN_GLOBALS, _file_stem, _make_id, _read_text
from graph_fy.extractors.models import LanguageConfig



def _ruby_sanitize_method_name(name: str) -> str:
    """Encode trailing Ruby method suffixes (!, ?, =) into safe node ID components (#3077)."""
    if not name:
        return name
    if name.endswith("!"):
        return f"{name[:-1]}_bang"
    if name.endswith("?"):
        return f"{name[:-1]}_pred"
    if name.endswith("="):
        return f"{name[:-1]}_eq"
    return name


_RUBY_CONFIG = LanguageConfig(
    ts_module="tree_sitter_ruby",
    class_types=frozenset({"class", "module"}),
    function_types=frozenset({"method", "singleton_method"}),
    import_types=frozenset(),
    call_types=frozenset({"call"}),
    call_function_field="method",
    call_accessor_node_types=frozenset(),
    name_fallback_child_types=("constant", "scope_resolution", "identifier"),
    body_fallback_child_types=("body_statement",),
    function_boundary_types=frozenset({"method", "singleton_method"}),
    sanitize_symbol_name_fn=_ruby_sanitize_method_name,
)


def _ruby_new_class_name(node, source: bytes) -> str | None:
    """Return ``ClassName`` if ``node`` is a ``ClassName.new(...)`` call, else None.

    Only a bare capitalized constant receiver counts (``Processor.new``);
    namespaced (``A::B.new``) and dynamic receivers are intentionally ignored so
    the binding stays unambiguous.
    """
    if node is None or node.type != "call":
        return None
    recv = node.child_by_field_name("receiver")
    meth = node.child_by_field_name("method")
    if recv is None or meth is None:
        return None
    if recv.type != "constant" or _read_text(meth, source) != "new":
        return None
    return _read_text(recv, source)

def _ruby_local_class_bindings(body_node, source: bytes) -> dict[str, str | None]:
    """Map ``local_var -> ClassName`` for ``var = ClassName.new`` within one Ruby
    method body, not descending into nested method definitions.

    100%-confidence contract: a variable assigned more than once, or to anything
    other than a single ``Constant.new``, maps to ``None`` (ambiguous) so callers
    never resolve it. Only the certain single-binding case carries a type.
    """
    bindings: dict[str, str | None] = {}
    boundary = {"method", "singleton_method"}

    def visit(n) -> None:
        for child in n.children:
            if child.type in boundary:
                continue  # nested method has its own scope
            if child.type == "assignment":
                left = child.child_by_field_name("left")
                right = child.child_by_field_name("right")
                if left is not None and left.type == "identifier":
                    var = _read_text(left, source)
                    cls = _ruby_new_class_name(right, source) if right is not None else None
                    if cls is None:
                        # assigned to something we can't type: poison if it was typed
                        if var in bindings:
                            bindings[var] = None
                    elif var in bindings:
                        if bindings[var] != cls:
                            bindings[var] = None  # reassigned to a different class
                    else:
                        bindings[var] = cls
            visit(child)

    visit(body_node)
    return bindings

def _ruby_const_last_name(node, source: bytes) -> str:
    """Last constant of a ``constant`` or ``scope_resolution`` (``A::B::C`` -> ``C``)."""
    if node is None:
        return ""
    if node.type == "constant":
        return _read_text(node, source)
    if node.type == "scope_resolution":
        consts = [c for c in node.children if c.type == "constant"]
        if consts:
            return _read_text(consts[-1], source)
    return ""

def _ruby_const_full_name(node, source: bytes) -> str:
    """Full constant path of a ``constant`` or ``scope_resolution`` (``A::B::C`` kept whole)."""
    if node is None or node.type not in ("constant", "scope_resolution"):
        return ""
    return _read_text(node, source).strip()


_RUBY_LOOKUP_MUTATORS = frozenset(
    {
        "alias_method",
        "attr",
        "attr_accessor",
        "attr_reader",
        "attr_writer",
        "define_method",
        "define_singleton_method",
        "include",
        "extend",
        "module_function",
        "prepend",
        "refine",
        "remove_method",
        "undef_method",
        "using",
    }
)

_RUBY_EXTERNAL_OWNER_MUTATORS = _RUBY_LOOKUP_MUTATORS | frozenset(
    {"class_eval", "class_exec", "module_eval", "module_exec"}
)


def _ruby_body_has_lookup_barrier(body_node, source: bytes) -> bool:
    """Whether a lexical body can alter Ruby method lookup dynamically."""
    method_scopes = {"method", "singleton_method"}

    def visit(node, *, direct: bool = False) -> bool:
        for child in node.children:
            if child.type in {"class", "module"}:
                continue
            if child.type == "singleton_class":
                if not direct:
                    return True
                continue
            if child.type in method_scopes:
                # A normal direct method declaration is modelled explicitly.
                # A conditional/nested declaration is not guaranteed to exist.
                if not direct:
                    return True
                continue
            if child.type in {"alias", "undef"}:
                return True
            if child.type == "call":
                receiver = child.child_by_field_name("receiver")
                method = child.child_by_field_name("method")
                if (
                    (receiver is None or _read_text(receiver, source) == "self")
                    and method is not None
                    and _read_text(method, source) in _RUBY_LOOKUP_MUTATORS
                ):
                    return True
            if visit(child):
                return True
        return False

    return visit(body_node, direct=True)


def _ruby_file_has_refinement_barrier(root_node, source: bytes) -> bool:
    """Whether any lexical scope in a file activates or defines a refinement.

    ``using`` affects method lookup for definitions in its lexical scope, while
    ``refine`` can hide method bodies behind a dynamic lookup layer.  The
    extractor does not model that lexical activation precisely, so inherited
    implicit-self promotion fails closed for the whole file.
    """

    def visit(node) -> bool:
        if node.type == "call":
            method = node.child_by_field_name("method")
            if method is not None and _read_text(method, source) in {"refine", "using"}:
                return True
        return any(visit(child) for child in node.children)

    return visit(root_node)


def _ruby_external_method_owners(root_node, source: bytes) -> list[str]:
    """Return explicit constant owners modified outside their class body."""
    owners: set[str] = set()

    def constant_refs(node) -> set[str]:
        if node.type in {"constant", "scope_resolution"}:
            raw = _ruby_const_full_name(node, source)
            return {raw} if raw else set()
        if node.type not in {
            "left_assignment_list",
            "parenthesized_statements",
            "splat_argument",
        }:
            # Attribute/index writes such as `Namespace.config = value`
            # contain a constant receiver but do not rebind that constant.
            return set()
        refs: set[str] = set()
        for child in node.children:
            if child.is_named:
                refs.update(constant_refs(child))
        return refs

    def alias_rhs_refs(node, *, allow_array: bool = False) -> set[str]:
        if node.type in {"constant", "scope_resolution"}:
            raw = _ruby_const_full_name(node, source)
            return {raw} if raw else set()
        if node.type in {
            "parenthesized_statements",
            "right_assignment_list",
        } or (allow_array and node.type == "array"):
            refs: set[str] = set()
            for child in node.children:
                if child.is_named:
                    refs.update(alias_rhs_refs(child, allow_array=allow_array))
            return refs
        return set()

    def prefix_marker(raw: str) -> str:
        return f"{raw}::*"

    def visit(node) -> None:
        if node.type in {"assignment", "operator_assignment"}:
            left = node.child_by_field_name("left")
            lhs_refs = constant_refs(left) if left is not None else set()
            if lhs_refs:
                # Any write can change which superclass a constant reference
                # denotes, including dynamic RHS and multiple assignment.
                owners.update(prefix_marker(raw) for raw in lhs_refs)
            right = node.child_by_field_name("right")
            if node.type == "assignment" and lhs_refs and right is not None:
                # Treat both ends of a potential constant alias as unsafe.  A
                # later mutation through the alias affects descendant owners
                # too.  Only direct/parenthesized constant RHS forms qualify;
                # ordinary values containing constant references do not.
                owners.update(
                    prefix_marker(raw)
                    for raw in alias_rhs_refs(
                        right,
                        allow_array=left is not None
                        and left.type == "left_assignment_list",
                    )
                )
        if node.type in {"class", "module"}:
            name = node.child_by_field_name("name")
            if name is not None and name.type == "scope_resolution":
                raw = _ruby_const_full_name(name, source)
                if raw:
                    # Compact declarations resolve a constant path rather than
                    # introducing every segment lexically.  Until that lookup
                    # is modelled, guard every matching real owner.
                    owners.add(raw)
        if node.type == "singleton_method":
            owner = node.child_by_field_name("object")
            if owner is not None and _read_text(owner, source) != "self":
                raw = _ruby_const_full_name(owner, source)
                if raw:
                    owners.add(raw)
            return
        if node.type == "singleton_class":
            owner = node.child_by_field_name("value")
            if owner is not None and _read_text(owner, source) != "self":
                raw = _ruby_const_full_name(owner, source)
                if raw:
                    owners.add(raw)
        if node.type == "call":
            receiver = node.child_by_field_name("receiver")
            method = node.child_by_field_name("method")
            if (
                receiver is not None
                and receiver.type in {"constant", "scope_resolution"}
                and method is not None
                and _read_text(method, source) in _RUBY_EXTERNAL_OWNER_MUTATORS
            ):
                raw = _ruby_const_full_name(receiver, source)
                if raw:
                    owners.add(raw)
        if node.type == "method":
            return
        for child in node.children:
            visit(child)

    visit(root_node)
    return sorted(owners)

_RUBY_CLASS_FACTORIES = frozenset({("Struct", "new"), ("Class", "new"), ("Data", "define")})

def _ruby_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                     nodes: list, edges: list, seen_ids: set, function_bodies: list,
                     parent_class_nid: str | None, add_node, add_edge, walk,
                     callable_def_nids: set, callable_class_nids: set,
                     ruby_namespace: list, ruby_lexical_scopes: list) -> bool:
    """Ruby: a constant assignment whose RHS is ``Struct.new(...)``,
    ``Class.new(Super)`` or ``Data.define(...)`` defines a class named after the
    constant (#1640). Synthesize the class node, attach block-defined methods via
    ``method`` (by recursing the block with the new node as parent), and emit an
    ``inherits`` edge for ``Class.new(Super)``. Returns True if handled.
    """
    if node.type != "assignment":
        return False
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None or left.type != "constant" or right.type != "call":
        return False
    recv = right.child_by_field_name("receiver")
    meth = right.child_by_field_name("method")
    if recv is None or meth is None or recv.type != "constant":
        return False
    if (_read_text(recv, source), _read_text(meth, source)) not in _RUBY_CLASS_FACTORIES:
        return False

    const_name = _read_text(left, source)
    if not const_name:
        return False
    # Qualify the factory-defined const against the enclosing scope, mirroring
    # the generic class branch (#2302): `module Billing; Invoice = Struct.new`
    # labels `Billing::Invoice`.
    const_segments = const_name.split("::")
    const_name = "::".join(ruby_namespace + const_segments)
    line = node.start_point[0] + 1
    class_nid = _make_id(stem, const_name)
    # Class factories can install methods through block/runtime behavior that
    # the narrow inherited-call proof does not model.
    add_node(
        class_nid,
        const_name,
        line,
        metadata={"ruby_lookup_unsafe": True},
    )
    callable_def_nids.add(class_nid)  # a class is callable (its constructor)
    callable_class_nids.add(class_nid)  # ...but only via its constructor (#2137)
    # Mirror the generic class branch: containment always hangs off the file node.
    add_edge(file_nid, class_nid, "contains", line)

    # `Class.new(Super)` — the first positional constant argument is the superclass.
    if _read_text(recv, source) == "Class":
        args = next((c for c in right.children if c.type == "argument_list"), None)
        if args is not None:
            for arg in args.children:
                if arg.type in ("constant", "scope_resolution"):
                    base = _ruby_const_last_name(arg, source)
                    raw_base = _ruby_const_full_name(arg, source)
                    if base:
                        base_nid = _make_id(stem, base)
                        if base_nid not in seen_ids:
                            base_nid = _make_id(base)
                            if base_nid not in seen_ids:
                                # origin_file lets _disambiguate_colliding_node_ids
                                # tell this file's unresolved reference apart from
                                # another file's same-named one, instead of every
                                # file's stub collapsing onto one shared bare id
                                # (see ensure_named_node(), which sets the same
                                # field for this exact reason).
                                nodes.append({
                                    "id": base_nid, "label": base,
                                    "file_type": "code", "source_file": "",
                                    "source_location": "", "origin_file": str_path,
                                })
                                seen_ids.add(base_nid)
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
                    break

    # Recurse the do/brace block so block-defined methods attach to the class.
    # The block wraps its statements in a `body_statement` (like a class body);
    # descend into it so the method handler sees parent_class_nid — otherwise the
    # default recurse resets the parent to None and the method hangs off the file
    # with a dot-less label.
    block = next((c for c in right.children if c.type in ("do_block", "block")), None)
    if block is not None:
        body = next((c for c in block.children if c.type == "body_statement"), block)
        ruby_namespace.extend(const_segments)
        ruby_lexical_scopes.append(const_name)
        try:
            for child in body.children:
                walk(child, parent_class_nid=class_nid)
        finally:
            ruby_lexical_scopes.pop()
            del ruby_namespace[-len(const_segments):]
    return True


def extract_ruby(path: Path) -> dict:
    """Extract classes, methods, singleton methods, and calls from a .rb file."""
    from graph_fy.extractors.engine import _extract_generic
    return _extract_generic(path, _RUBY_CONFIG)
