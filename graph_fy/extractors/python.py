"""Python AST and rationale extractor."""
from __future__ import annotations

import hashlib
import re
import textwrap
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _file_stem, _make_id, _read_text, _find_body
from graph_fy.ids import normalize_id
from graph_fy.extractors.models import LanguageConfig
from graph_fy.extractors.resolution import _probe_python_module_candidate

_PYTHON_TYPE_CONTAINERS = frozenset({
    "list", "dict", "set", "tuple", "frozenset", "type",
    "List", "Dict", "Set", "Tuple", "FrozenSet", "Type",
    "Optional", "Union", "Sequence", "Iterable", "Mapping", "MutableMapping",
    "Iterator", "Callable", "Awaitable", "AsyncIterable", "AsyncIterator", "Coroutine",
    "Generator", "AsyncGenerator", "ContextManager", "AsyncContextManager",
    "Annotated", "ClassVar", "Final", "Literal", "Concatenate", "ParamSpec", "TypeVar",
    "None", "Ellipsis",
})

_PYTHON_ANNOTATION_NOISE = frozenset({
    # scalar builtins
    "str", "int", "float", "bool", "bytes", "bytearray", "complex", "object",
    "True", "False", "Self",
    # unittest.mock
    "MagicMock", "Mock", "AsyncMock", "NonCallableMock",
    "NonCallableMagicMock", "PropertyMock", "patch", "sentinel",
})

_PYTHON_DECORATOR_NOISE = frozenset({
    "property", "staticmethod", "classmethod", "abstractmethod",
    "abstractproperty", "cached_property", "wraps", "lru_cache", "cache",
    "singledispatch", "singledispatchmethod", "total_ordering",
    "contextmanager", "asynccontextmanager", "overload", "override",
    "final", "no_type_check", "runtime_checkable", "dataclass",
    "setter", "getter", "deleter",
})


def _python_collect_type_refs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    """Walk a Python type annotation; append (name, role) where role is 'type' or 'generic_arg'."""
    if node is None:
        return
    t = node.type
    if t == "type":
        for c in node.children:
            if c.is_named:
                _python_collect_type_refs(c, source, generic, out)
        return
    if t == "identifier":
        name = _read_text(node, source)
        if name and name not in _PYTHON_TYPE_CONTAINERS and name not in _PYTHON_ANNOTATION_NOISE:
            out.append((name, "generic_arg" if generic else "type"))
        return
    if t == "attribute":
        tail = _read_text(node, source).rsplit(".", 1)[-1]
        if tail and tail not in _PYTHON_TYPE_CONTAINERS and tail not in _PYTHON_ANNOTATION_NOISE:
            out.append((tail, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        for c in node.children:
            if c.type == "identifier":
                container = _read_text(c, source)
                if container and container not in _PYTHON_TYPE_CONTAINERS and container not in _PYTHON_ANNOTATION_NOISE:
                    out.append((container, "generic_arg" if generic else "type"))
            elif c.type == "type_parameter":
                for sub in c.children:
                    if sub.is_named:
                        _python_collect_type_refs(sub, source, True, out)
        return
    if t == "subscript":
        value = node.child_by_field_name("value")
        if value is not None:
            _python_collect_type_refs(value, source, generic, out)
        for c in node.children:
            if c is value or not c.is_named:
                continue
            _python_collect_type_refs(c, source, True, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _python_collect_type_refs(c, source, generic, out)


_PY_TYPE_ALIAS_ANNOTATIONS = frozenset({
    "TypeAlias", "typing.TypeAlias", "typing_extensions.TypeAlias",
})
_PY_NEW_TYPE_CALLEES = frozenset({"NewType", "typing.NewType", "typing_extensions.NewType"})


def _python_alias_name(stmt, source: bytes) -> str | None:
    """The type name a module-level statement declares as an explicit alias.

    `type Vector = ...` / `type Pair[T] = ...` (PEP 695), `Matrix: TypeAlias = ...`
    and `UserId = NewType("UserId", int)` each declare one. A bare
    `JsonValue = Union[...]` is an alias by convention only and is not one.
    A same-name re-export (`Field: TypeAlias = core.Field`, `type Field =
    core.Field[int]`, or `Field: TypeAlias = Field` after `from core import
    Field`) denotes the original type and declares no new name, so it
    returns None: references keep resolving to the one real `Field`.
    """
    if stmt.type == "type_alias_statement":
        left, right = stmt.child_by_field_name("left"), stmt.child_by_field_name("right")
        name = _read_text(left, source).split("[", 1)[0].strip() if left is not None else ""
    elif stmt.type == "expression_statement" and len(stmt.named_children) == 1:
        assign = stmt.named_children[0]
        left = assign.child_by_field_name("left") if assign.type == "assignment" else None
        if left is None or left.type != "identifier":
            return None
        name, right = _read_text(left, source), assign.child_by_field_name("right")
        annotation = assign.child_by_field_name("type")
        if annotation is None:
            callee = right.child_by_field_name("function") if right is not None and right.type == "call" else None
            # NewType always mints a new type, even under its own name.
            return name if callee is not None and _read_text(callee, source) in _PY_NEW_TYPE_CALLEES else None
        if _read_text(annotation, source).strip() not in _PY_TYPE_ALIAS_ANNOTATIONS:
            return None
    else:
        return None
    if not name.isidentifier():
        return None
    target = _read_text(right, source).split("[", 1)[0].strip() if right is not None else ""
    if target.rsplit(".", 1)[-1] == name:
        return None
    return name


def _python_pre_scan_underscore_collisions(root_node, source: bytes, stem: str) -> dict[str, Any]:
    """Pre-scan a Python module for name differences and duplicate/kind declarations that collapse to one node id (#42, #3302)."""
    raw_groups: dict[str, list[tuple[str, str]]] = {}

    def _record(plain_nid: str, name: str, kind: str) -> None:
        raw_groups.setdefault(plain_nid, []).append((name, kind))

    def _undecorated(node):
        if node.type == "decorated_definition":
            return node.child_by_field_name("definition") or node
        return node

    classes: list = []
    for child in map(_undecorated, root_node.children):
        alias_name = _python_alias_name(child, source)
        if alias_name:
            _record(_make_id(stem, alias_name), alias_name, "type")
        elif child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                name = _read_text(name_node, source)
                if name:
                    _record(_make_id(stem, name), name, "function")
        elif child.type == "class_definition":
            class_name_node = child.child_by_field_name("name")
            body = child.child_by_field_name("body")
            if class_name_node is None or body is None:
                continue
            class_name = _read_text(class_name_node, source)
            if not class_name:
                continue
            _record(_make_id(stem, class_name), class_name, "class")
            classes.append((class_name, body))

    # Pre-compute class IDs for class member scanning
    temp_module_groups: dict[str, Any] = {}
    for plain_nid, decl_list in raw_groups.items():
        if len(decl_list) >= 2:
            temp_module_groups[plain_nid] = _build_collision_entry(plain_nid, decl_list)

    for class_name, body in classes:
        plain_class_nid = _make_id(stem, class_name)
        class_nid = _python_underscore_salted_nid(plain_class_nid, class_name, temp_module_groups, kind="class")
        for member in map(_undecorated, body.children):
            if member.type == "function_definition":
                name_node = member.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _read_text(name_node, source)
                if name:
                    _record(_make_id(class_nid, name), name, "method")
            elif member.type == "class_definition":
                name_node = member.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _read_text(name_node, source)
                if name:
                    _record(_make_id(class_nid, name), name, "class")

    final_groups: dict[str, Any] = {}
    for plain_nid, decl_list in raw_groups.items():
        if len(decl_list) >= 2:
            final_groups[plain_nid] = _build_collision_entry(plain_nid, decl_list)

    return final_groups


def _build_collision_entry(plain_nid: str, decl_list: list[tuple[str, str]]) -> dict[str, Any]:
    """Compute deterministic IDs and canonical reference targets for colliding declarations."""
    names = {d[0] for d in decl_list}
    public_names = [n for n in names if not n.startswith("_")]
    canonical_name = public_names[0] if len(public_names) == 1 else None

    canonical_lookup: dict[str, str] = {}
    assignments: list[tuple[str, str, str]] = []

    for name in names:
        subdecls = [d for d in decl_list if d[0] == name]
        if name == canonical_name:
            base_nid = plain_nid
        else:
            salt = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
            base_nid = _make_id(plain_nid, salt)

        if len(subdecls) == 1:
            assignments.append((name, subdecls[0][1], base_nid))
            canonical_lookup[name] = base_nid
        else:
            has_class = any(d[1] == "class" for d in subdecls)
            fn_count = 0
            for name_val, kind in subdecls:
                if kind == "class" and has_class:
                    assigned = base_nid
                    canonical_lookup[name] = base_nid
                elif not has_class and fn_count == 0:
                    assigned = base_nid
                    canonical_lookup[name] = base_nid
                    fn_count += 1
                else:
                    if has_class and len(subdecls) == 2:
                        fn_salt = hashlib.sha1(f"{kind}:{name}".encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
                    else:
                        fn_salt = hashlib.sha1(f"{kind}:{fn_count}:{name}".encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
                    assigned = _make_id(plain_nid, fn_salt)
                    fn_count += 1
                assignments.append((name_val, kind, assigned))

    return {
        "raw_names": names,
        "assignments": assignments,
        "canonical": canonical_lookup,
        "cursors": {},
    }


def _python_underscore_salted_nid(
    plain_nid: str,
    name: str,
    groups: dict[str, Any],
    *,
    kind: str | None = None,
) -> str:
    """Resolve a Python function/method's real node id against the collision map."""
    entry = groups.get(plain_nid)
    if not entry:
        return plain_nid

    # Legacy support if called with a plain set of names
    if isinstance(entry, set):
        if len(entry) < 2:
            return plain_nid
        public = [n for n in entry if not n.startswith("_")]
        if len(public) == 1 and name == public[0]:
            return plain_nid
        salt = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
        return _make_id(plain_nid, salt)

    if kind is None:
        return entry.get("canonical", {}).get(name, plain_nid)

    assignments = entry.get("assignments", [])
    cursors = entry.setdefault("cursors", {})
    cur_idx = cursors.get((name, kind), 0)
    for i in range(cur_idx, len(assignments)):
        aname, akind, anid = assignments[i]
        if aname == name and akind == kind:
            cursors[(name, kind)] = i + 1
            return anid
    return entry.get("canonical", {}).get(name, plain_nid)


def _python_collect_param_refs(params_node, source: bytes) -> list[tuple[str, str]]:
    """Collect type refs from each typed parameter under a `parameters` node."""
    out: list[tuple[str, str]] = []
    if params_node is None:
        return out
    for child in params_node.children:
        if child.type in ("typed_parameter", "typed_default_parameter"):
            type_node = child.child_by_field_name("type")
            _python_collect_type_refs(type_node, source, False, out)
    return out


def _python_param_names(params_node, source: bytes) -> set[str]:
    """Plain parameter identifiers declared on a Python `parameters` node."""
    out: set[str] = set()
    if params_node is None:
        return out
    for child in params_node.children:
        if child.type == "identifier":
            out.add(_read_text(child, source))
        elif child.type in (
            "typed_parameter",
            "default_parameter",
            "typed_default_parameter",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        ):
            name_n = child.child_by_field_name("name")
            if name_n is None:
                name_n = next((c for c in child.children if c.type == "identifier"), None)
            if name_n is not None:
                out.add(_read_text(name_n, source))
    return out


def _python_collect_assignment_targets(node, source: bytes, out: set[str]) -> None:
    """Identifiers bound as `pattern` targets under a Python AST subtree."""
    if node is None:
        return
    if node.type == "identifier":
        out.add(_read_text(node, source))
        return
    if node.type in ("pattern_list", "tuple_pattern", "list_pattern"):
        for c in node.children:
            _python_collect_assignment_targets(c, source, out)


def _python_local_bound_names(func_def_node, source: bytes) -> set[str]:
    """Names bound LOCALLY inside a Python function."""
    bound: set[str] = set()
    bound |= _python_param_names(func_def_node.child_by_field_name("parameters"), source)

    def walk(n) -> None:
        for child in n.children:
            t = child.type
            if t in ("function_definition", "class_definition", "lambda"):
                continue
            if t == "assignment":
                _python_collect_assignment_targets(child.child_by_field_name("left"), source, bound)
            elif t in ("for_statement", "for_in_clause"):
                _python_collect_assignment_targets(child.child_by_field_name("left"), source, bound)
            elif t == "with_statement":
                for item in child.children:
                    if item.type == "with_clause":
                        for wi in item.children:
                            if wi.type == "with_item":
                                alias = wi.child_by_field_name("alias")
                                _python_collect_assignment_targets(alias, source, bound)
            elif t == "named_expression":
                _python_collect_assignment_targets(child.child_by_field_name("name"), source, bound)
            walk(child)

    body = func_def_node.child_by_field_name("body")
    if body is not None:
        walk(body)
    return bound


def _python_module_bound_names(root, source: bytes) -> set[str]:
    """Names rebound by assignment at MODULE scope."""
    bound: set[str] = set()

    def walk(n) -> None:
        for child in n.children:
            t = child.type
            if t in ("function_definition", "class_definition", "lambda"):
                continue
            if t == "assignment":
                _python_collect_assignment_targets(child.child_by_field_name("left"), source, bound)
            elif t in ("for_statement", "for_in_clause"):
                _python_collect_assignment_targets(child.child_by_field_name("left"), source, bound)
            elif t == "with_statement":
                for item in child.children:
                    if item.type == "with_clause":
                        for wi in item.children:
                            if wi.type == "with_item":
                                alias = wi.child_by_field_name("alias")
                                _python_collect_assignment_targets(alias, source, bound)
            elif t == "named_expression":
                _python_collect_assignment_targets(child.child_by_field_name("name"), source, bound)
            walk(child)

    walk(root)
    return bound


def _python_decorator_name(deco_node, source: bytes) -> str | None:
    """Return the head symbol of a Python `decorator` node."""
    for child in deco_node.children:
        if not child.is_named:
            continue
        target = child
        if target.type == "call":
            target = target.child_by_field_name("function") or target
        if target.type == "attribute":
            attr = target.child_by_field_name("attribute")
            return _read_text(attr, source) if attr else None
        if target.type == "identifier":
            return _read_text(target, source)
        return None
    return None


def _shorten_rationale_label(text: str, width: int = 80) -> str:
    """Collapse whitespace and truncate ``text`` to ``width`` chars for a
    rationale node label, cutting on a word boundary rather than mid-word.
    Shared by the Python and JS/TS rationale extractors (#2206).

    ``textwrap.shorten`` collapses to just the placeholder when the first
    "word" alone exceeds ``width`` (e.g. a docstring/comment that opens with
    an unbroken URL) -- that would emit a content-free label, so fall back to
    a plain character truncation of the normalized text in that case.
    """
    label = textwrap.shorten(text, width=width, placeholder="…")
    if label in ("", "…"):
        flat = " ".join(text.split())
        label = flat if len(flat) <= width else flat[: width - 1] + "…"
    return label


def _import_python(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    t = node.type
    if t == "import_statement":
        for child in node.children:
            if child.type in ("dotted_name", "aliased_import"):
                raw = _read_text(child, source)
                raw_module, _, raw_alias = raw.partition(" as ")
                module_name = raw_module.strip().lstrip(".")
                tgt_nid = _make_id(module_name)
                edge = {
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                }
                if raw_alias:
                    # `import pkg.mod as alias` binds the local name `alias`, not
                    # `mod`'s own stem, to the module -- stash it so the cross-file
                    # member-call resolver can match `alias.func()` against this
                    # edge instead of dropping it (#2082).
                    edge["local_alias"] = raw_alias.strip()
                edges.append(edge)
    elif t == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        if module_node:
            raw = _read_text(module_node, source)
            target_path: "Path | None" = None
            if raw.startswith("."):
                # Relative import - resolve to full path so IDs match file node IDs
                dots = len(raw) - len(raw.lstrip("."))
                module_name = raw.lstrip(".")
                base = Path(str_path).parent
                for _ in range(dots - 1):
                    base = base.parent
                # A relative import can name a subpackage (a directory with an
                # __init__.py), not a module file. Probing the candidate on disk
                # (mirroring the companion `imports` edge's
                # _resolve_python_module_path) resolves `graphs` -> graphs/__init__.py
                # instead of a nonexistent graphs.py: without it the target keeps an
                # absolute-path-derived slug that the target_file stamp below can't
                # heal, so it dangles per-checkout (#2455).
                candidate = base / module_name.replace(".", "/") if module_name else base
                resolved = _probe_python_module_candidate(candidate)
                if resolved is not None:
                    target_path = resolved
                else:
                    rel = (module_name.replace(".", "/") + ".py") if module_name else "__init__.py"
                    target_path = base / rel
                tgt_nid = _make_id(str(target_path))
            else:
                tgt_nid = _make_id(raw)
            edge = {
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports_from",
                "context": "import",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            }
            # Stamp the resolved target file (mirroring _import_js, #1814) so
            # the #2169 remap pass can canonicalize this edge's target on an
            # incremental run where the target file itself is not in the
            # batch — without it the target keeps an absolute-path-derived id
            # that matches no node in the merged graph and dangles (#2213).
            # Existence-gated: a speculative import of a nonexistent sibling
            # must stay dangling, exactly as before. The stamp is transient
            # and popped before graph.json ships.
            if target_path is not None:
                try:
                    if target_path.is_file():
                        edge["target_file"] = str(target_path)
                except OSError:
                    pass
            edges.append(edge)



_PYTHON_CONFIG = LanguageConfig(
    ts_module="tree_sitter_python",
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_statement", "import_from_statement"}),
    call_types=frozenset({"call"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"attribute"}),
    call_accessor_field="attribute",
    call_accessor_object_field="object",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_python,
)


_RATIONALE_PREFIXES = ("# NOTE:", "# IMPORTANT:", "# HACK:", "# WHY:", "# RATIONALE:", "# TODO:", "# FIXME:")


def _is_autogenerated_python(source: bytes) -> bool:
    """Return True if this Python file is auto-generated and its module docstring is noise.

    Covers: Alembic/Flask-Migrate revisions, Django migrations, protobuf/gRPC/OpenAPI stubs.
    Module docstrings in these files are change annotations or boilerplate, not rationale.
    """
    head = source[:2048].decode("utf-8", errors="replace")
    # Generic generated-file markers (protobuf, gRPC, OpenAPI codegen, etc.)
    if any(m in head for m in ("DO NOT EDIT", "@generated", "Generated by the protocol buffer")):
        return True
    # Alembic / Flask-Migrate revision files
    if (re.search(r"^revision\s*[:=]", head, re.MULTILINE)
            and "def upgrade(" in head
            and "down_revision" in head):
        return True
    # Django migrations
    if "class Migration(migrations.Migration)" in head and "operations" in head:
        return True
    return False


def _extract_python_rationale(path: Path, result: dict) -> None:
    """Post-pass: extract docstrings and rationale comments from Python source.
    Mutates result in-place by appending to result['nodes'] and result['edges'].
    """
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
        language = Language(tspython.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception:
        return

    stem = _file_stem(path)
    str_path = str(path)
    nodes = result["nodes"]
    edges = result["edges"]
    seen_ids = {n["id"] for n in nodes}
    file_nid = _make_id(str(path))

    def _get_docstring(body_node) -> tuple[str, int] | None:
        """A docstring is the first STATEMENT in a module/class/function body.

        A leading `comment` node — the shebang line essentially every
        executable script starts with, a coding-declaration or license
        header, or any ordinary comment — is not a statement: tree-sitter
        still parses it as a sibling child of the body, but Python's own
        docstring rule skips right over it. The old unconditional `break`
        after the first loop iteration stopped at that comment instead of
        looking past it, so a module docstring behind a shebang (or any
        leading comment) was silently never found (#3312).
        """
        if not body_node:
            return None
        for child in body_node.children:
            if child.type == "comment":
                continue
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type in ("string", "concatenated_string"):
                        text = source[sub.start_byte:sub.end_byte].decode("utf-8", errors="replace")
                        text = text.strip("\"'").strip('"""').strip("'''").strip()
                        if len(text) > 20:
                            return text, child.start_point[0] + 1
            break
        return None

    def _add_rationale(text: str, line: int, parent_nid: str) -> None:
        label = _shorten_rationale_label(text)
        rid = _make_id(stem, "rationale", str(line))
        if rid not in seen_ids:
            seen_ids.add(rid)
            nodes.append({
                "id": rid,
                "label": label,
                "file_type": "rationale",
                "source_file": str_path,
                "source_location": f"L{line}",
            })
        edges.append({
            "source": rid,
            "target": parent_nid,
            "relation": "rationale_for",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    # Module-level docstring — skip for auto-generated files (Alembic, Django
    # migrations, protobuf stubs, etc.) whose module docstrings are revision
    # annotations, not architectural rationale.
    if not _is_autogenerated_python(source):
        ds = _get_docstring(root)
        if ds:
            _add_rationale(ds[0], ds[1], file_nid)

    # Class and function docstrings
    def walk_docstrings(node, parent_nid: str) -> None:
        t = node.type
        if t == "class_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node and body:
                class_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                nid = _make_id(stem, class_name)
                ds = _get_docstring(body)
                if ds:
                    _add_rationale(ds[0], ds[1], nid)
                for child in body.children:
                    walk_docstrings(child, nid)
            return
        if t == "function_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node and body:
                func_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                nid = _make_id(parent_nid, func_name) if parent_nid != file_nid else _make_id(stem, func_name)
                ds = _get_docstring(body)
                if ds:
                    _add_rationale(ds[0], ds[1], nid)
            return
        for child in node.children:
            walk_docstrings(child, parent_nid)

    walk_docstrings(root, file_nid)

    # Rationale comments (# NOTE:, # IMPORTANT:, etc.)
    source_text = source.decode("utf-8", errors="replace")
    for lineno, line_text in enumerate(source_text.splitlines(), start=1):
        stripped = line_text.strip()
        if any(stripped.startswith(p) for p in _RATIONALE_PREFIXES):
            _add_rationale(stripped, lineno, file_nid)


def extract_python(path: Path) -> dict:
    """Extract classes, functions, and imports from a .py file via tree-sitter AST."""
    from graph_fy.extractors.engine import _extract_generic
    result = _extract_generic(path, _PYTHON_CONFIG)
    if "error" not in result:
        _extract_python_rationale(path, result)
        try:
            from graph_fy.extractors.frameworks import extract_python_frameworks
            source_text = path.read_text(encoding="utf-8", errors="ignore")
            extract_python_frameworks(path, source_text, result)
        except Exception:
            pass
        try:
            from graph_fy.extractors.config_keys import extract_config_keys_from_source
            source_text = path.read_text(encoding="utf-8", errors="ignore")
            extract_config_keys_from_source(path, source_text, result)
        except Exception:
            pass
    return result


def _python_collect_param_refs(params_node, source: bytes) -> list[tuple[str, str]]:
    """Collect type refs from each typed parameter under a `parameters` node."""
    out: list[tuple[str, str]] = []
    if params_node is None:
        return out
    for child in params_node.children:
        if child.type in ("typed_parameter", "typed_default_parameter"):
            type_node = child.child_by_field_name("type")
            _python_collect_type_refs(type_node, source, False, out)
    return out

def _python_param_names(params_node, source: bytes) -> set[str]:
    """Plain parameter identifiers declared on a Python `parameters` node.

    Covers positional/keyword params plus `*args` / `**kwargs` and typed or
    default forms — anything that binds a local name the function body can shadow
    a module-level definition with.
    """
    out: set[str] = set()
    if params_node is None:
        return out
    for child in params_node.children:
        if child.type == "identifier":
            out.add(_read_text(child, source))
        elif child.type in (
            "typed_parameter",
            "default_parameter",
            "typed_default_parameter",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        ):
            # The bound name is the first identifier child (the rest is type/default).
            name_n = child.child_by_field_name("name")
            if name_n is None:
                name_n = next(
                    (c for c in child.children if c.type == "identifier"), None
                )
            if name_n is not None:
                out.add(_read_text(name_n, source))
    return out

def _python_collect_assignment_targets(node, source: bytes, out: set[str]) -> None:
    """Identifiers bound as `pattern` targets under a Python AST subtree.

    Recurses through `pattern_list` / `tuple_pattern` / `list_pattern` so tuple
    unpacking (`a, b = ...`, `for a, b in ...`) contributes every bound name.
    """
    if node is None:
        return
    if node.type == "identifier":
        out.add(_read_text(node, source))
        return
    if node.type in ("pattern_list", "tuple_pattern", "list_pattern"):
        for c in node.children:
            _python_collect_assignment_targets(c, source, out)

def _python_local_bound_names(func_def_node, source: bytes) -> set[str]:
    """Names bound LOCALLY inside a Python function: parameters plus assignment,
    `for`, `with ... as`, and comprehension targets.

    Used by the indirect-dispatch guard to reject a call-argument identifier that
    is a parameter or a local binding — it names a local value, not the module-
    level function/class that happens to share the name. Nested `function_definition`
    and `class_definition` subtrees are NOT descended into: their bindings belong
    to a different scope.
    """
    bound: set[str] = set()
    bound |= _python_param_names(func_def_node.child_by_field_name("parameters"), source)

    def walk(n) -> None:
        for child in n.children:
            t = child.type
            if t in ("function_definition", "class_definition", "lambda"):
                continue  # inner scope — its bindings are not this function's locals
            if t == "assignment":
                _python_collect_assignment_targets(
                    child.child_by_field_name("left"), source, bound
                )
            elif t in ("for_statement", "for_in_clause"):
                _python_collect_assignment_targets(
                    child.child_by_field_name("left"), source, bound
                )
            elif t == "with_statement":
                for item in child.children:
                    if item.type == "with_clause":
                        for wi in item.children:
                            if wi.type == "with_item":
                                alias = wi.child_by_field_name("alias")
                                _python_collect_assignment_targets(alias, source, bound)
            elif t == "named_expression":  # walrus :=
                _python_collect_assignment_targets(
                    child.child_by_field_name("name"), source, bound
                )
            walk(child)

    body = func_def_node.child_by_field_name("body")
    if body is not None:
        walk(body)
    return bound

def _python_module_bound_names(root, source: bytes) -> set[str]:
    """Names rebound by assignment at MODULE scope (top-level `x = ...`, `for`, walrus).

    The module-scope analogue of the per-function shadow set: a dispatch-table value
    whose name is reassigned to data at module level (`handler = build()`) names that
    value, not a same-named function, so it must not manufacture an indirect edge.
    Function and class bodies are not descended into — their bindings are local.
    """
    bound: set[str] = set()

    def walk(n) -> None:
        for child in n.children:
            t = child.type
            if t in ("function_definition", "class_definition", "lambda"):
                continue  # inner scope — not a module-level binding
            if t == "assignment":
                _python_collect_assignment_targets(
                    child.child_by_field_name("left"), source, bound
                )
            elif t in ("for_statement", "for_in_clause"):
                _python_collect_assignment_targets(
                    child.child_by_field_name("left"), source, bound
                )
            elif t == "named_expression":  # walrus :=
                _python_collect_assignment_targets(
                    child.child_by_field_name("name"), source, bound
                )
            walk(child)

    walk(root)
    return bound


def _scan_python_nested_function_declarations(
    container_node, parent_nid: str, *, source: bytes, config,
    add_node, add_edge, callable_def_nids: set | None,
    local_bound_names: dict | None, function_bodies: list,
    scope_parents: dict[str, str] | None = None,
    lexical_nids_by_scope: dict[str, dict[str, str]] | None = None,
) -> None:
    """Emit a node + `contains` edge for every function lexically nested inside
    *container_node*, scoped under *parent_nid*, track its body, and record lexical
    scope hierarchy so calls resolve to local definitions instead of falling through
    to corpus-wide resolution (#3405).

    Recurses through Python statements (including blocks, if/while/for/try/with),
    unwrapping `decorated_definition` when it wraps a `function_definition`.
    """
    if container_node is None:
        return
    for child in container_node.children:
        target = child
        if target.type == "decorated_definition":
            inner = target.child_by_field_name("definition")
            if inner is not None:
                target = inner
        if target.type == "function_definition":
            name_node = target.child_by_field_name(config.name_field)
            if name_node is None:
                for c in target.children:
                    if c.type in config.name_fallback_child_types:
                        name_node = c
                        break
            func_name = _read_text(name_node, source) if name_node else None
            if func_name and normalize_id(func_name):
                line = target.start_point[0] + 1
                nested_nid = _make_id(parent_nid, func_name)
                add_node(nested_nid, f"{func_name}()", line)
                add_edge(parent_nid, nested_nid, "contains", line)
                if callable_def_nids is not None:
                    callable_def_nids.add(nested_nid)
                if local_bound_names is not None:
                    local_bound_names[nested_nid] = _python_local_bound_names(target, source)
                if scope_parents is not None:
                    scope_parents[nested_nid] = parent_nid
                if lexical_nids_by_scope is not None:
                    lexical_nids_by_scope.setdefault(parent_nid, {})[func_name] = nested_nid
                    lexical_nids_by_scope.setdefault(nested_nid, {})[func_name] = nested_nid
                nested_body = _find_body(target, config)
                if nested_body:
                    function_bodies.append((nested_nid, nested_body))
                    _scan_python_nested_function_declarations(
                        nested_body, nested_nid, source=source, config=config,
                        add_node=add_node, add_edge=add_edge,
                        callable_def_nids=callable_def_nids,
                        local_bound_names=local_bound_names,
                        function_bodies=function_bodies,
                        scope_parents=scope_parents,
                        lexical_nids_by_scope=lexical_nids_by_scope,
                    )
        else:
            _scan_python_nested_function_declarations(
                child, parent_nid, source=source, config=config,
                add_node=add_node, add_edge=add_edge,
                callable_def_nids=callable_def_nids,
                local_bound_names=local_bound_names,
                function_bodies=function_bodies,
                scope_parents=scope_parents,
                lexical_nids_by_scope=lexical_nids_by_scope,
            )


def _python_decorator_name(deco_node, source: bytes) -> str | None:
    """Return the head symbol of a Python `decorator` node.

    The Python twin of `_ts_decorator_name`, differing only in grammar node
    names: `@traced` -> the identifier; `@retry(times=3)` -> the `function` of
    the `call`; `@app.route("/")` / `@mod.deco` -> the `attribute` (the symbol
    itself, not the module alias it is reached through).
    """
    for child in deco_node.children:
        if not child.is_named:
            continue
        target = child
        if target.type == "call":
            target = target.child_by_field_name("function") or target
        if target.type == "attribute":
            attr = target.child_by_field_name("attribute")
            return _read_text(attr, source) if attr else None
        if target.type == "identifier":
            return _read_text(target, source)
        return None
    return None
