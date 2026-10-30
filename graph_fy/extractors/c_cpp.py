"""C and C++ AST extractors."""
from __future__ import annotations

import re
from pathlib import Path

from graph_fy.extractors.base import _file_stem, _make_id, _read_text
from graph_fy.extractors.models import LanguageConfig

from graph_fy.extractors.resolution import _resolve_c_include_path


def _get_c_func_name(node, source: bytes) -> str | None:
    """Recursively unwrap declarator to find the innermost identifier (C)."""
    if node.type == "identifier":
        return _read_text(node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_c_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


def _cpp_declarator_type_name(declarator, source: bytes) -> str | None:
    """The name a typedef declarator introduces: `Handle`, `*HandlePtr`,
    `(*Fn)(int)`, `Arr[4]` each wrap one `type_identifier`."""
    node = declarator
    while node is not None and node.type != "type_identifier":
        inner = node.child_by_field_name("declarator")
        if inner is None and node.type == "parenthesized_declarator":
            inner = next(iter(node.named_children), None)
        node = inner
    return _read_text(node, source) if node is not None else None


def _cpp_type_last_name(type_node, source: bytes) -> str | None:
    """`Bar` for `ns::Bar` or `ns::Bar<T>`: the name a qualified type ends in."""
    if type_node is not None and type_node.type == "type_descriptor":
        type_node = type_node.child_by_field_name("type")
    if type_node is None or type_node.type != "qualified_identifier":
        return None
    while type_node is not None and type_node.type == "qualified_identifier":
        type_node = type_node.child_by_field_name("name")
    if type_node is not None and type_node.type == "template_type":
        type_node = type_node.child_by_field_name("name")
    return _read_text(type_node, source) if type_node is not None else None


def _cpp_alias_names(node, source: bytes) -> list[str]:
    """The type names a namespace- or file-scope `using X = ...;` or `typedef`
    declares. A member alias (`using value_type = T;` in a class) is not one: it
    sits in every container and C++ member ids are not class-qualified, so they
    would fold into one hub. Neither is the struct a C-style
    `typedef struct Foo {...} Foo;` names (the struct's own node is it), nor a
    same-name re-export (`using Bar = ns::Bar;`), which denotes the original
    type and declares no new name. An include guard or `#if`/`#ifdef` block is
    a conditional wrapper, not a scope, so an alias inside one keeps the scope
    around it."""
    scope = node.parent
    while scope is not None and (
            scope.type == "template_declaration" or scope.type.startswith("preproc_")):
        scope = scope.parent
    if scope is None or scope.type not in ("translation_unit", "declaration_list"):
        return []
    target = node.child_by_field_name("type")
    if node.type == "alias_declaration":
        name_node = node.child_by_field_name("name")
        candidates = [_read_text(name_node, source)] if name_node is not None else []
    else:
        candidates = [_cpp_declarator_type_name(d, source)
                      for d in node.children_by_field_name("declarator")]
    own_names = {_cpp_type_last_name(target, source)}
    if target is not None and target.type in (
            "struct_specifier", "class_specifier", "union_specifier", "enum_specifier"):
        spec_name = target.child_by_field_name("name")
        if spec_name is not None:
            own_names.add(_read_text(spec_name, source))
    # A set, not `in names`: one typedef can declare thousands of names.
    names: list[str] = []
    seen = set(own_names)
    for name in candidates:
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _import_c(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    for child in node.children:
        if child.type in ("string_literal", "system_lib_string", "string"):
            raw = _read_text(child, source).strip('"<> ')
            # Quoted includes: try to resolve to a real file so the target ID
            # matches the node ID _extract_generic creates for that file.
            if child.type != "system_lib_string":
                resolved = _resolve_c_include_path(raw, str_path)
                if resolved is not None:
                    tgt_nid = _make_id(str(resolved))
                    edges.append({
                        "source": file_nid,
                        "target": tgt_nid,
                        "relation": "imports",
                        "context": "import",
                        "confidence": "EXTRACTED",
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                        "weight": 1.0,
                        "target_file": str(resolved),
                    })
                    break
            module_name = raw.split("/")[-1].split(".")[0]
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
            break


_C_PRIMITIVE_TYPE_NODES = frozenset({
    "primitive_type", "sized_type_specifier", "auto", "placeholder_type_specifier",
})

def _c_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a C type expression; append (name, role) tuples for user-defined types.
    Skips primitive types and qualifiers; recognises type_identifier."""
    if node is None or node.type in _C_PRIMITIVE_TYPE_NODES:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in ("pointer_declarator", "reference_declarator", "array_declarator",
             "type_qualifier", "type_descriptor", "abstract_pointer_declarator",
             "abstract_reference_declarator", "abstract_array_declarator"):
        for c in node.children:
            if c.is_named:
                _c_collect_type_refs(c, source, generic, out)

def _cpp_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a C++ type expression; append (name, role) tuples.
    Resolves qualified_identifier tails (std::string → string) and template_type
    base + arguments (std::vector<HttpClient> → vector + HttpClient as generic_arg)."""
    if node is None or node.type in _C_PRIMITIVE_TYPE_NODES:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "qualified_identifier":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            _cpp_collect_type_refs(name_node, source, generic, out)
        return
    if t == "template_type":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            text = _read_text(name_node, source)
            if text:
                out.append((text, "generic_arg" if generic else "type"))
        args_node = node.child_by_field_name("arguments")
        if args_node is not None:
            for c in args_node.children:
                if c.is_named:
                    _cpp_collect_type_refs(c, source, True, out)
        return
    if t in ("type_descriptor", "pointer_declarator", "reference_declarator",
             "array_declarator", "type_qualifier", "abstract_pointer_declarator",
             "abstract_reference_declarator", "abstract_array_declarator"):
        for c in node.children:
            if c.is_named:
                _cpp_collect_type_refs(c, source, generic, out)

def _get_cpp_func_name(node, source: bytes) -> str | None:
    """Recursively unwrap declarator to find the innermost identifier (C++)."""
    if node.type == "identifier":
        return _read_text(node, source)
    if node.type in ("field_identifier", "destructor_name", "operator_name"):
        return _read_text(node, source)
    if node.type == "qualified_identifier":
        # An out-of-class DEFINITION (`void Foo::bar() {}`) carries a
        # qualified_identifier declarator. Retaining the `Foo::` qualifier makes
        # _make_id(stem, "Foo::bar") normalize to the same id as the in-class
        # member _make_id(class_nid, "bar"), so the decl in Foo.h and the def in
        # Foo.cpp resolve to ONE method node instead of two (#1547). The full
        # qualified text also handles nested scopes (`A::B::bar`). Free functions
        # never have a qualified_identifier here, so their bare-name ids are
        # unchanged; only qualified definitions shift onto their owning class.
        return _read_text(node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_cpp_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None

def _cpp_declarator_name(node, source: bytes) -> str | None:
    """Return the bare variable name from a C++ declaration declarator, unwrapping
    pointer/reference/init wrappers (``*f``, ``&r``, ``f = Foo()``). Returns None
    for anything that isn't a plain named local (arrays, function pointers,
    structured bindings) so the type table never records a guessed receiver."""
    t = node.type
    if t == "identifier":
        return _read_text(node, source)
    if t in ("pointer_declarator", "reference_declarator", "init_declarator"):
        inner = node.child_by_field_name("declarator")
        if inner is None:
            for c in node.children:
                if c.type in ("identifier", "pointer_declarator",
                              "reference_declarator"):
                    inner = c
                    break
        if inner is not None:
            return _cpp_declarator_name(inner, source)
    return None

def _cpp_local_var_types(body_node, source: bytes, table: dict[str, str]) -> None:
    """Collect ``var -> ClassName`` from local variable declarations in a C++
    function body, for receiver-type inference in the cross-file member-call pass
    (#1547). Handles ``Foo f;``, ``Foo* f;``, ``Foo *f = ...;``, ``Foo f = Foo();``.

    Only a class-like (``type_identifier``/``qualified_identifier``) type with a
    single named declarator is recorded — PRECISION over recall: a built-in type
    (``int x``), an ambiguous multi-declarator line, or an un-nameable declarator
    contributes nothing rather than a guess. A qualified type ``ns::Foo`` records
    its simple tail ``Foo`` so it keys to the type's definition node label.
    """
    stack = [body_node]
    while stack:
        n = stack.pop()
        if n.type in ("function_definition", "lambda_expression"):
            # Don't descend into a nested function/lambda: its locals are scoped
            # away and would pollute this body's table.
            if n is not body_node:
                continue
        if n.type == "declaration":
            type_node = n.child_by_field_name("type")
            if type_node is not None and type_node.type in (
                "type_identifier", "qualified_identifier"
            ):
                type_name = _read_text(type_node, source).split("::")[-1].strip()
                declarators = [
                    c for c in n.children
                    if c.type in ("identifier", "pointer_declarator",
                                  "reference_declarator", "init_declarator")
                ]
                # A single declarator only: `Foo a, b;` is ambiguous to attribute
                # to one receiver name cleanly, so skip multi-declarator lines.
                if type_name and type_name[:1].isupper() and len(declarators) == 1:
                    var = _cpp_declarator_name(declarators[0], source)
                    if var and var not in table:
                        table[var] = type_name
        for c in n.children:
            stack.append(c)


_C_CONFIG = LanguageConfig(
    ts_module="tree_sitter_c",
    class_types=frozenset(),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"preproc_include"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"field_expression"}),
    call_accessor_field="field",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_c,
    resolve_function_name_fn=_get_c_func_name,
)

_CPP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_cpp",
    class_types=frozenset({"class_specifier", "struct_specifier"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"preproc_include"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"field_expression", "qualified_identifier"}),
    call_accessor_field="field",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_c,
    resolve_function_name_fn=_get_cpp_func_name,
)


def extract_c(path: Path) -> dict:
    """Extract functions and includes from a .c/.h file."""
    from graph_fy.extractors.engine import _extract_generic
    return _extract_generic(path, _C_CONFIG)


_CPP_STRING_TEST_MACROS = (
    "TEST_CASE", "TEST_CASE_TEMPLATE", "SCENARIO",
)
_CPP_STRING_TEST_RE = re.compile(
    r'^[ \t]*(?:' + "|".join(_CPP_STRING_TEST_MACROS) + r')\s*\(\s*"((?:[^"\\]|\\.)+)"',
    re.MULTILINE,
)


def _augment_cpp_string_tests(path: Path, result: dict) -> dict:
    """Append callable nodes for doctest/Catch2 string-named test cases that
    tree-sitter-cpp drops as ERROR nodes (issue #2594).
    """
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return result
    matches = list(_CPP_STRING_TEST_RE.finditer(source))
    if not matches:
        return result

    str_path = str(path)
    stem = _file_stem(path)
    file_nid = _make_id(str_path)
    stem_collapse_id = _make_id(stem)
    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    seen_ids = {n.get("id") for n in nodes}

    for m in matches:
        test_name = m.group(1)
        line = source.count("\n", 0, m.start()) + 1
        test_nid = _make_id(stem, test_name)
        if test_nid == stem_collapse_id:
            test_nid = _make_id(stem, "test", f"L{line}")
        if test_nid in seen_ids:
            continue
        seen_ids.add(test_nid)
        nodes.append({
            "id": test_nid,
            "label": f'"{test_name}"',
            "file_type": "code",
            "source_file": str_path,
            "source_location": f"L{line}",
        })
        edges.append({
            "source": file_nid,
            "target": test_nid,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })
    return result


_CPP_CLI_MARKER_RE = re.compile(
    rb"\b(?:ref|value)\s+(?:class|struct)\b"
    rb"|\binterface\s+class\b"
    rb"|\bgcnew\b"
    rb"|\[\s*(?:assembly|module)\s*:"
)

_CPP_CLI_CLASS_RE = re.compile(
    rb"(?:\b(?:public|private|protected)\s+)?\b(?:ref|value)\s+(?=(?:class|struct)\b)"
    rb"|(?:\b(?:public|private|protected)\s+)?\binterface\s+(?=class\b)"
)

_CPP_CLI_SUFFIX_UNAMBIGUOUS_RE = re.compile(rb"(?<=[A-Za-z0-9_>])[\^%](?=\s*[,)\]>;])")

_CPP_CLI_SUFFIX_DECL_RE = re.compile(
    rb"(\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+"   # System::String
    rb"|\b[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*"                   # String, DataTable
    rb"|\b[A-Z](?=[\^%])"                                        # T
    rb"|\b(?:bool|char|wchar_t|short|int|long|float|double|unsigned|signed)"
    rb"|>)"                                                      # List<int>^
    rb"[\^%](?=\s+[A-Za-z_])"
)

_CPP_CLI_ATTR_RE = re.compile(rb"\[\s*(?:assembly|module)\s*:[^\[\]]*\]", re.S)


def _blank_keeping_newlines(m: "re.Match[bytes]") -> bytes:
    """Replace a match with spaces, but keep its line breaks."""
    return re.sub(rb"[^\r\n]", b" ", m.group(0))


def _normalize_cpp_cli(source: bytes) -> bytes | None:
    """Rewrite C++/CLI spellings to standard C++ ones, or None if not C++/CLI."""
    if not _CPP_CLI_MARKER_RE.search(source):
        return None
    out = _CPP_CLI_CLASS_RE.sub(_blank_keeping_newlines, source)
    out = re.sub(rb"\bgcnew\b", b"new  ", out)
    out = _CPP_CLI_SUFFIX_UNAMBIGUOUS_RE.sub(b" ", out)
    out = _CPP_CLI_SUFFIX_DECL_RE.sub(rb"\1 ", out)
    return _CPP_CLI_ATTR_RE.sub(_blank_keeping_newlines, out)


_QT_CONNECT_CLASSIC_RE = re.compile(
    r"""(?:QObject::)?connect\s*\(\s*([^,]+)\s*,\s*SIGNAL\s*\(\s*([A-Za-z0-9_]+(?:\s*\([^)]*\))?)\s*\)\s*,\s*([^,]+)\s*,\s*SLOT\s*\(\s*([A-Za-z0-9_]+(?:\s*\([^)]*\))?)\s*\)\s*\)"""
)

_QT_CONNECT_PMF_RE = re.compile(
    r"""(?:QObject::)?connect\s*\(\s*([^,]+)\s*,\s*&([A-Za-z0-9_:]+)\s*,\s*([^,]+)\s*,\s*&([A-Za-z0-9_:]+)\s*\)"""
)

_QT_CONNECT_LAMBDA_RE = re.compile(
    r"""(?:QObject::)?connect\s*\(\s*([^,]+)\s*,\s*&([A-Za-z0-9_:]+)\s*,\s*\["""
)


def _augment_qt_signals_slots(path: Path, result: dict) -> dict:
    """Extract Qt QObject::connect() signal-to-slot event bindings as connects_signal_slot edges."""
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return result

    if "connect" not in source:
        return result

    str_path = str(path.resolve())
    stem = _file_stem(path)
    file_nid = _make_id(str_path)
    edges = result.setdefault("edges", [])

    # 1. Classic Qt syntax: connect(sender, SIGNAL(sig()), receiver, SLOT(slt()))
    for m in _QT_CONNECT_CLASSIC_RE.finditer(source):
        sender = m.group(1).strip()
        signal = m.group(2).strip()
        receiver = m.group(3).strip()
        slot = m.group(4).strip()
        line = source.count("\n", 0, m.start()) + 1

        sender_nid = _make_id(stem, sender) if sender and sender != "this" else file_nid
        slot_name = slot.split("(")[0].strip()
        receiver_nid = _make_id(stem, slot_name) if slot_name else file_nid

        edges.append({
            "source": sender_nid,
            "target": receiver_nid,
            "relation": "connects_signal_slot",
            "signal": signal,
            "slot": slot,
            "sender": sender,
            "receiver": receiver,
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": f"L{line}",
        })

    # 2. Modern PMF syntax: connect(sender, &SenderClass::signal, receiver, &ReceiverClass::slot)
    for m in _QT_CONNECT_PMF_RE.finditer(source):
        sender = m.group(1).strip()
        signal_full = m.group(2).strip()
        receiver = m.group(3).strip()
        slot_full = m.group(4).strip()
        line = source.count("\n", 0, m.start()) + 1

        sender_nid = _make_id(stem, sender) if sender and sender != "this" else file_nid
        slot_tail = slot_full.split("::")[-1].strip()
        slot_nid = _make_id(stem, slot_tail) if slot_tail else file_nid

        edges.append({
            "source": sender_nid,
            "target": slot_nid,
            "relation": "connects_signal_slot",
            "signal": signal_full,
            "slot": slot_full,
            "sender": sender,
            "receiver": receiver,
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": f"L{line}",
        })

    # 3. Lambda syntax: connect(sender, &SenderClass::signal, [=]() { ... })
    for m in _QT_CONNECT_LAMBDA_RE.finditer(source):
        sender = m.group(1).strip()
        signal_full = m.group(2).strip()
        line = source.count("\n", 0, m.start()) + 1

        sender_nid = _make_id(stem, sender) if sender and sender != "this" else file_nid
        edges.append({
            "source": sender_nid,
            "target": file_nid,
            "relation": "connects_signal_slot",
            "signal": signal_full,
            "slot": "<lambda>",
            "sender": sender,
            "receiver": "lambda",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": f"L{line}",
        })

    return result


def extract_cpp(path: Path) -> dict:
    """Extract functions, classes, and includes from a .cpp/.cc/.cxx/.hpp file."""
    from graph_fy.extractors.engine import _extract_generic
    try:
        source = path.read_bytes()
    except OSError:
        res = _augment_cpp_string_tests(path, _extract_generic(path, _CPP_CONFIG))
        return _augment_qt_signals_slots(path, res)
    result = _extract_generic(
        path, _CPP_CONFIG, source_override=_normalize_cpp_cli(source) or source
    )
    res = _augment_cpp_string_tests(path, result)
    return _augment_qt_signals_slots(path, res)

