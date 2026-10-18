"""Swift AST extractor."""
from __future__ import annotations

from pathlib import Path

from graph_fy.extractors.base import _make_id, _read_text
from graph_fy.extractors.models import LanguageConfig



def _import_swift(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> list[tuple[str, str]]:
    """Emit module-level ``imports`` edges and report the imported modules."""
    modules: list[tuple[str, str]] = []
    for child in node.children:
        if child.type == "identifier":
            raw = _read_text(child, source)
            tgt_nid = _make_id(raw)
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
            modules.append((tgt_nid, raw))
            break
    return modules


_SWIFT_CONFIG = LanguageConfig(
    ts_module="tree_sitter_swift",
    class_types=frozenset({"class_declaration", "protocol_declaration"}),
    function_types=frozenset({"function_declaration", "init_declaration", "deinit_declaration", "subscript_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"navigation_expression"}),
    call_accessor_field="",
    name_fallback_child_types=("simple_identifier", "type_identifier", "user_type"),
    body_fallback_child_types=("class_body", "protocol_body", "function_body", "enum_class_body"),
    function_boundary_types=frozenset({"function_declaration", "init_declaration", "deinit_declaration", "subscript_declaration"}),
    import_handler=_import_swift,
)


def _swift_declaration_keyword(node) -> str | None:
    """Return the leading kind token for a Swift class_declaration: class/struct/enum/extension/actor."""
    for c in node.children:
        if not c.is_named and c.type in ("class", "struct", "enum", "extension", "actor"):
            return c.type
    return None


def _swift_pre_scan(root_node, source: bytes) -> tuple[set[str], set[str]]:
    """Pre-scan a Swift compilation unit and return (protocol_names, class_like_names)."""
    protocols: set[str] = set()
    classes: set[str] = set()
    stack = [root_node]
    while stack:
        n = stack.pop()
        if n.type == "protocol_declaration":
            name_node = n.child_by_field_name("name")
            if name_node is None:
                for c in n.children:
                    if c.type == "type_identifier":
                        name_node = c
                        break
            if name_node is not None:
                text = _read_text(name_node, source)
                if text:
                    protocols.add(text)
        elif n.type == "class_declaration":
            kw = _swift_declaration_keyword(n)
            if kw in ("class", "struct", "enum", "actor"):
                name_node = n.child_by_field_name("name")
                if name_node is not None:
                    text = _read_text(name_node, source)
                    if text:
                        classes.add(text)
        stack.extend(n.children)
    return protocols, classes

def _swift_classify_base(name: str, kind: str | None, is_first: bool,
                          protocols: set[str], classes: set[str]) -> str:
    """Classify a Swift inheritance_specifier entry as `inherits` or `implements`."""
    if name in protocols:
        return "implements"
    if name in classes:
        return "inherits"
    # struct/enum/extension/actor cannot inherit a class — all conformances are protocols.
    if kind in ("struct", "enum", "extension", "actor"):
        return "implements"
    # `class`: first entry is conventionally the base class; subsequent are protocols.
    return "inherits" if is_first else "implements"

def _swift_user_type_name(user_type_node, source: bytes) -> str | None:
    """Return the head type_identifier text from a Swift user_type node (without generics)."""
    if user_type_node is None:
        return None
    for c in user_type_node.children:
        if c.type == "type_identifier":
            text = _read_text(c, source)
            return text or None
    return None

def _swift_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Swift type expression; append (name, role) tuples (role 'type' or 'generic_arg')."""
    if node is None:
        return
    t = node.type
    if t == "type_annotation":
        for c in node.children:
            if c.is_named:
                _swift_collect_type_refs(c, source, generic, out)
        return
    if t == "user_type":
        for c in node.children:
            if c.type == "type_identifier":
                text = _read_text(c, source)
                if text:
                    out.append((text, "generic_arg" if generic else "type"))
                break
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _swift_collect_type_refs(arg, source, True, out)
        return
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in ("optional_type", "implicitly_unwrapped_optional_type", "array_type",
             "dictionary_type", "tuple_type"):
        for c in node.children:
            if c.is_named:
                _swift_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _swift_collect_type_refs(c, source, generic, out)

def _swift_property_type_node(property_node):
    """Return the type_annotation child of a Swift property_declaration, if any."""
    for c in property_node.children:
        if c.type == "type_annotation":
            return c
    return None

def _swift_attribute_type_name(property_node, source: bytes) -> str | None:
    """Return the type named by an ``@Environment(Type.self)`` attribute argument.

    Structural, whitelist-gated (#2561): only the ``Environment`` wrapper names
    the property's OWN type in its argument — ``@Query(Item.self)`` properties
    hold a *collection* of the argument type, so typing them as the element type
    fabricates member-call edges (measured false edge in the report). The
    argument must be a navigation_expression of exactly
    ``[simple_identifier (uppercase), navigation_suffix ".self"]``; the keypath
    form (``@Environment(\\.dismiss)``, key_path_expression head) and the
    module-dotted form (``@Environment(MyModule.Store.self)``, nested
    navigation_expression head) are skipped — a missed edge, never a wrong one.
    """
    for c in property_node.children:
        if c.type != "modifiers":
            continue
        for attr in c.children:
            if attr.type != "attribute":
                continue
            head = next((a for a in attr.children if a.type == "user_type"), None)
            if head is None or _read_text(head, source) != "Environment":
                continue
            arg = next((a for a in attr.children
                        if a.type == "navigation_expression"), None)
            if arg is None:
                continue
            named = [a for a in arg.children if a.is_named]
            if len(named) != 2:
                continue
            ident, suffix = named
            if ident.type != "simple_identifier" or suffix.type != "navigation_suffix":
                continue
            if _read_text(suffix, source) != ".self":
                continue
            name = _read_text(ident, source)
            if name and name[:1].isupper():
                return name
    return None

def _swift_factory_call(call_node, source: bytes) -> tuple[str, str] | None:
    """If a Swift call expression is a static factory call (``Factory.make()``),
    return ``(factory_type, method_name)``; else None (#2561).

    Only the exact depth-1 shape is accepted: a navigation_expression of
    ``[simple_identifier (uppercase), navigation_suffix]``. Deeper chains
    (``A.B.make()``, ``Singleton.shared.make()``) stay untyped — the resolver
    would have to guess the intermediate hop.
    """
    first = call_node.children[0] if call_node.children else None
    if first is None or first.type != "navigation_expression":
        return None
    named = [c for c in first.children if c.is_named]
    if len(named) != 2:
        return None
    head, suffix = named
    if head.type != "simple_identifier" or suffix.type != "navigation_suffix":
        return None
    htext = _read_text(head, source)
    if not htext or not htext[:1].isupper():
        return None
    mname = next((_read_text(sc, source) for sc in suffix.children
                  if sc.type == "simple_identifier"), None)
    if not mname:
        return None
    return htext, mname

def _swift_property_name(property_node, source: bytes) -> str | None:
    """Return the bound name of a Swift property (``let x``/``var x = ...``)."""
    for c in property_node.children:
        if c.type == "pattern":
            for sc in c.children:
                if sc.type == "simple_identifier":
                    return _read_text(sc, source)
        if c.type == "simple_identifier":
            return _read_text(c, source)
    return None

def _swift_constructor_type(call_node, source: bytes) -> str | None:
    """If a Swift call expression is a constructor (``Foo()``), return the type name.

    Only upper-cased callees are treated as types so a free-function call like
    ``configure()`` in an initializer is not mistaken for a constructor.
    """
    first = call_node.children[0] if call_node.children else None
    if first is not None and first.type == "simple_identifier":
        text = _read_text(first, source)
        if text and text[:1].isupper():
            return text
    return None

def _swift_receiver_name(recv_node, source: bytes) -> str | None:
    """Return the depth-1 receiver name of a Swift member call (``recv.method()``).

    ``vm.update()`` -> ``vm``; ``Type.staticMethod()`` -> ``Type``;
    ``Singleton.shared.method()`` -> ``Singleton`` (head of the chain);
    ``self.svc.fetch()`` -> ``svc`` (the property the call is reached through).
    Returns None for anything deeper, so resolution stays depth-1.
    """
    if recv_node is None:
        return None
    if recv_node.type == "simple_identifier":
        return _read_text(recv_node, source)
    if recv_node.type == "navigation_expression":
        head = recv_node.children[0] if recv_node.children else None
        if head is not None and head.type == "simple_identifier":
            return _read_text(head, source)
        if head is not None and head.type == "self_expression":
            for child in recv_node.children:
                if child.type == "navigation_suffix":
                    for sc in child.children:
                        if sc.type == "simple_identifier":
                            return _read_text(sc, source)
    return None


def _swift_local_var_types(body_node, source: bytes, table: dict[str, str],
                           factory: dict[str, tuple[str, str]] | None = None) -> None:
    """Collect ``var -> Type`` from local ``let``/``var`` bindings in a Swift
    function body, so a member call on the local (``x.method()``) resolves to Type
    in the cross-file member-call pass (#1604).

    Two initializer shapes are recorded, PRECISION over recall:
      - a constructor call ``let x = Type()`` (``_swift_constructor_type``);
      - a static-member access ``let x = Type.shared`` (a navigation_expression
        with an upper-cased head) — the singleton-cached-into-a-local idiom, one
        of the most common Swift call patterns and previously resolved to nothing.
    A factory call (``let x = Factory.make()``) has no in-file type; when
    ``factory`` is given, the pending ``name -> (Factory, method)`` binding is
    stashed there (label-only) for corpus-side resolution against the factory
    method's plain return type (#2561).
    Nested function declarations are not descended into (their locals are scoped
    away); the first binding for a name wins, so a class property of the same name
    already in the table is not overwritten.
    """
    stack = [body_node]
    while stack:
        n = stack.pop()
        if n.type == "function_declaration" and n is not body_node:
            continue
        if n.type == "property_declaration":
            prop_type: str | None = None
            factory_bind: tuple[str, str] | None = None
            for child in n.children:
                if child.type == "call_expression":
                    prop_type = _swift_constructor_type(child, source)
                    if prop_type is None:
                        factory_bind = _swift_factory_call(child, source)
                    break
                if child.type == "navigation_expression":
                    head = child.children[0] if child.children else None
                    if head is not None and head.type == "simple_identifier":
                        htext = _read_text(head, source)
                        if htext and htext[:1].isupper():
                            prop_type = htext
                    break
            name = _swift_property_name(n, source)
            if name and prop_type and name not in table:
                table[name] = prop_type
            elif (name and factory_bind is not None and factory is not None
                  and name not in table and name not in factory):
                factory[name] = factory_bind
        for c in n.children:
            stack.append(c)


def _swift_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                      nodes: list, edges: list, seen_ids: set, function_bodies: list,
                      parent_class_nid: str | None, add_node_fn, add_edge_fn,
                      ensure_named_node_fn) -> bool:
    """Handle enum_entry for Swift. Returns True if handled."""
    if node.type == "enum_entry" and parent_class_nid:
        line = node.start_point[0] + 1
        for child in node.children:
            if child.type == "simple_identifier":
                case_name = _read_text(child, source)
                case_nid = _make_id(parent_class_nid, case_name)
                add_node_fn(case_nid, case_name, line)
                add_edge_fn(parent_class_nid, case_nid, "case_of", line)
        # Associated-value types nest as `enum_type_parameters -> user_type ->
        # type_identifier` (a sibling of the case-name simple_identifier). The
        # case-name loop above never descends into them, so `case started(Session)`
        # used to drop the Event -> Session reference entirely. Mirror the Swift
        # property/parameter emit style: collect the type refs and emit a
        # `references` edge from the ENUM node to each collected type.
        for child in node.children:
            if child.type != "enum_type_parameters":
                continue
            for grand in child.children:
                if not grand.is_named:
                    continue
                refs: list[tuple[str, str]] = []
                _swift_collect_type_refs(grand, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "type"
                    target_nid = ensure_named_node_fn(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge_fn(parent_class_nid, target_nid, "references",
                                    line, context=ctx)
        return True
    return False


def extract_swift(path: Path) -> dict:
    """Extract classes, structs, protocols, functions, imports, and calls from a .swift file."""
    from graph_fy.extractors.engine import _extract_generic
    return _extract_generic(path, _SWIFT_CONFIG)
