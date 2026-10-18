"""C# cross-file resolution.

The config-driven C# *extractor* (``extract_csharp`` → ``_extract_generic``)
still lives in ``graph_fy/extract.py``; per ``extractors/MIGRATION.md`` the
config-driven languages cannot be ported one-by-one until the shared
``_extract_generic`` core moves as its own coordinated batch. This module is
the C# home for the parts that *are* cleanly separable — today, the cross-file
type-reference resolver below — and is where ``extract_csharp`` will land when
the core migration happens.
"""
from __future__ import annotations

import hashlib
import html
from pathlib import Path

from graph_fy.extractors.base import (
    _LANGUAGE_BUILTIN_GLOBALS,
    _file_stem,
    _make_id,
    _read_text,
    _semantic_reference_edge,
    _source_location,
    REFERENCE_CONTEXTS,
)
from graph_fy.extractors.models import LanguageConfig
from graph_fy.security import sanitize_metadata


def _csharp_namespace_id(dotted_name: str) -> str:
    digest = hashlib.sha1(dotted_name.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return f"csharp_namespace:{digest}"


def _csharp_pre_scan_interfaces(root_node, source: bytes) -> set[str]:
    """Return names declared as `interface` in this C# compilation unit."""
    out: set[str] = set()
    stack = [root_node]
    while stack:
        n = stack.pop()
        if n.type == "interface_declaration":
            name_node = n.child_by_field_name("name")
            if name_node is not None:
                text = _read_text(name_node, source)
                if text:
                    out.add(text)
        stack.extend(n.children)
    return out


def _csharp_classify_base(name: str, interface_names: set[str]) -> str:
    """`implements` if the base name is an interface (declared or by I-prefix convention), else `inherits`."""
    if name in interface_names:
        return "implements"
    if len(name) >= 2 and name[0] == "I" and name[1].isupper():
        return "implements"
    return "inherits"


_CSHARP_TYPE_PARAMETER_SCOPE_DECLARATIONS = frozenset({
    "class_declaration",
    "interface_declaration",
    "record_declaration",
    "struct_declaration",
    "method_declaration",
})


def _csharp_type_parameters_in_scope(node, source: bytes) -> frozenset[str]:
    """Return C# type-parameter names visible from ``node``."""
    names: set[str] = set()
    scope = node
    while scope is not None:
        if scope.type in _CSHARP_TYPE_PARAMETER_SCOPE_DECLARATIONS:
            for child in scope.children:
                if child.type != "type_parameter_list":
                    continue
                for param in child.children:
                    if param.type == "type_parameter":
                        name_node = next(
                            (sub for sub in param.children if sub.type == "identifier"),
                            None,
                        )
                        if name_node is not None:
                            name = _read_text(name_node, source)
                            if name:
                                names.add(name)
                    elif param.type == "identifier":
                        name = _read_text(param, source)
                        if name:
                            names.add(name)
        scope = scope.parent
    return frozenset(names)


def _csharp_collect_type_refs(
    node,
    source: bytes,
    generic: bool,
    out: list[tuple[str, str, bool, str]],
    skip: frozenset[str] | None = None,
) -> None:
    """Walk a C# type expression; append (name, role, qualified, qualifier) tuples."""
    if node is None:
        return
    if skip is None:
        skip = _csharp_type_parameters_in_scope(node, source)
    t = node.type
    if t == "predefined_type":
        return
    if t == "identifier":
        name = _read_text(node, source)
        if name and name not in skip:
            out.append((name, "generic_arg" if generic else "type", False, ""))
        return
    if t == "qualified_name":
        prefix, _, text = _read_text(node, source).rpartition(".")
        text = text.split("<", 1)[0]
        if text and text not in skip:
            out.append((text, "generic_arg" if generic else "type", True, prefix))
        return
    if t == "generic_name":
        name_child = node.child_by_field_name("name")
        if name_child is None:
            for sub in node.children:
                if sub.type == "identifier":
                    name_child = sub
                    break
        if name_child is not None:
            qualified = name_child.type == "qualified_name"
            prefix, _, name = _read_text(name_child, source).rpartition(".")
            if name and name not in skip:
                out.append((name, "generic_arg" if generic else "type", qualified, prefix if qualified else ""))
        for sub in node.children:
            if sub.type == "type_argument_list":
                for arg in sub.children:
                    if arg.is_named:
                        _csharp_collect_type_refs(arg, source, True, out, skip)
        return
    if t in ("nullable_type", "array_type", "pointer_type", "ref_type"):
        for c in node.children:
            if c.is_named:
                _csharp_collect_type_refs(c, source, generic, out, skip)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _csharp_collect_type_refs(c, source, generic, out, skip)


def _csharp_attribute_names(method_node, source: bytes) -> list[tuple[str, bool, str]]:
    """Collect attribute names from a C# method/declaration's attribute_list children."""
    names: list[tuple[str, bool, str]] = []
    skip = _csharp_type_parameters_in_scope(method_node, source)
    for child in method_node.children:
        if child.type != "attribute_list":
            continue
        for attr in child.children:
            if attr.type != "attribute":
                continue
            name_node = attr.child_by_field_name("name")
            if name_node is None:
                for sub in attr.children:
                    if sub.type in ("identifier", "qualified_name"):
                        name_node = sub
                        break
            if name_node is not None:
                qualified = name_node.type == "qualified_name"
                prefix, _, text = _read_text(name_node, source).rpartition(".")
                if text and text not in skip:
                    names.append((text, qualified, prefix if qualified else ""))
    return names


def _read_csharp_type_name(node, source: bytes) -> tuple[str, bool, str] | None:
    """Resolve a C# type name, whether it was qualified, and its qualifier prefix."""
    if node is None:
        return None
    if node.type in ("identifier", "predefined_type"):
        return (_read_text(node, source), False, "")
    if node.type == "qualified_name":
        prefix, _, tail = _read_text(node, source).rpartition(".")
        tail = tail.split("<", 1)[0]
        return (tail, True, prefix)
    if node.type == "generic_name":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            qualified = name_node.type == "qualified_name"
            prefix, _, tail = _read_text(name_node, source).rpartition(".")
            return (tail, qualified, prefix if qualified else "")
    for child in node.children:
        if not child.is_named:
            continue
        result = _read_csharp_type_name(child, source)
        if result:
            return result
    return None


def _csharp_bare_call_name(name_node, source: bytes) -> str:
    """The callee identifier of a C# call-site name node."""
    if name_node.type == "generic_name":
        for child in name_node.children:
            if child.type == "identifier":
                return _read_text(child, source)
    return _read_text(name_node, source)


def _csharp_receiver_type_name(type_node, source: bytes) -> str | None:
    info = _read_csharp_type_name(type_node, source)
    if not info:
        return None
    name = info[0]
    return name if name and name[:1].isupper() else None


def _csharp_method_receiver_types(
    method_node,
    source: bytes,
    field_types: dict[str, str],
) -> tuple[dict[str, list[tuple[int, int, str | None]]], dict[str, str]]:
    bindings: dict[str, list[tuple[int, int, str | None]]] = {}
    field_poisoned: set[str] = set()

    def bind(name: str | None, type_name: str | None, scope_node) -> None:
        if not name or scope_node is None:
            return
        if field_types.get(name) not in (None, type_name):
            field_poisoned.add(name)
        bindings.setdefault(name, []).append(
            (scope_node.start_byte, scope_node.end_byte, type_name)
        )

    def bind_parameter(param, scope_node) -> None:
        name_node = param.child_by_field_name("name")
        if name_node is not None:
            bind(
                _read_text(name_node, source),
                _csharp_receiver_type_name(param.child_by_field_name("type"), source),
                scope_node,
            )

    body = method_node.child_by_field_name("body")
    param_scope = body if body is not None else method_node
    params = method_node.child_by_field_name("parameters")
    if params is not None:
        for param in params.children:
            if param.type == "parameter":
                bind_parameter(param, param_scope)

    stack = (
        [(child, param_scope) for child in body.children]
        if body is not None
        else []
    )
    while stack:
        node, scope = stack.pop()
        if node.type in (
            "class_declaration",
            "struct_declaration",
            "interface_declaration",
            "record_declaration",
            "enum_declaration",
        ):
            continue
        if node.type == "lambda_expression":
            lam_params = node.child_by_field_name("parameters")
            if lam_params is not None:
                if lam_params.type == "implicit_parameter":
                    bind(_read_text(lam_params, source), None, node)
                else:
                    for param in lam_params.children:
                        if param.type == "parameter":
                            bind_parameter(param, node)
                        elif param.type == "implicit_parameter":
                            bind(_read_text(param, source), None, node)
        elif node.type == "local_function_statement":
            lf_params = node.child_by_field_name("parameters")
            if lf_params is not None:
                for param in lf_params.children:
                    if param.type == "parameter":
                        bind_parameter(param, node)
        elif node.type == "local_declaration_statement":
            vd = next(
                (c for c in node.children if c.type == "variable_declaration"), None
            )
            if vd is not None:
                declared = _csharp_receiver_type_name(
                    vd.child_by_field_name("type"), source
                )
                for declarator in vd.children:
                    if declarator.type != "variable_declarator":
                        continue
                    name_node = declarator.child_by_field_name("name") or next(
                        (g for g in declarator.children if g.type == "identifier"),
                        None,
                    )
                    if name_node is None:
                        continue
                    type_name = declared
                    if type_name is None:
                        for g in declarator.children:
                            if g.type == "object_creation_expression":
                                type_name = _csharp_receiver_type_name(
                                    g.child_by_field_name("type"), source
                                )
                                break
                    bind(_read_text(name_node, source), type_name, scope)
        elif node.type in ("declaration_expression", "declaration_pattern"):
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.type == "identifier":
                bind(
                    _read_text(name_node, source),
                    _csharp_receiver_type_name(
                        node.child_by_field_name("type"), source
                    ),
                    scope,
                )
        child_scope = (
            node
            if node.type in (
                "block", "lambda_expression", "local_function_statement"
            )
            else scope
        )
        stack.extend((child, child_scope) for child in node.children)

    base = {
        name: type_name
        for name, type_name in field_types.items()
        if name not in field_poisoned
    }
    for name in field_poisoned:
        bindings.pop(name, None)
    return bindings, base


def _csharp_scoped_receiver_type(
    table: tuple[dict[str, list[tuple[int, int, str | None]]], dict[str, str]] | None,
    name: str | None,
    call_byte: int,
) -> str | None:
    if not table or not name:
        return None
    bindings, base = table
    candidates = [
        b for b in bindings.get(name, ())
        if b[0] <= call_byte < b[1]
    ]
    if not candidates:
        return base.get(name)
    innermost = min(end - start for start, end, _ in candidates)
    inner = [b for b in candidates if b[1] - b[0] == innermost]
    if len(inner) == 1:
        return inner[0][2]
    return None


def _csharp_namespace_name(node, source: bytes) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _read_text(name_node, source).strip()
    for child in node.children:
        if child.type in ("identifier", "qualified_name"):
            return _read_text(child, source).strip()
    return ""


def _csharp_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                       nodes: list, edges: list, seen_ids: set, function_bodies: list,
                       parent_class_nid: str | None, add_node_fn, add_edge_fn,
                       walk_fn, namespace_stack: list[str], scope_stack: list[str]) -> bool:
    if node.type == "enum_member_declaration" and parent_class_nid:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return True
        member_name = _read_text(name_node, source)
        if not member_name:
            return True
        line = node.start_point[0] + 1
        member_nid = _make_id(parent_class_nid, member_name)
        if member_nid not in seen_ids:
            add_node_fn(member_nid, member_name, line)
            add_edge_fn(parent_class_nid, member_nid, "case_of", line)
        return True
    if node.type == "namespace_declaration":
        ns_name = _csharp_namespace_name(node, source)
        pushed = False
        if ns_name:
            namespace_stack.append(ns_name)
            scope_stack.append(f"s{node.start_byte}")
            pushed = True
            ns_label = ".".join(namespace_stack)
            ns_nid = _csharp_namespace_id(ns_label)
            line = node.start_point[0] + 1
            add_node_fn(ns_nid, ns_label, line, node_type="namespace", metadata={"kind": "csharp_namespace"})
            add_edge_fn(file_nid, ns_nid, "contains", line)
        body = node.child_by_field_name("body")
        if body:
            try:
                for child in body.children:
                    walk_fn(child, parent_class_nid)
            finally:
                if pushed:
                    namespace_stack.pop()
                    scope_stack.pop()
        elif pushed:
            namespace_stack.pop()
            scope_stack.pop()
        return True
    if node.type == "file_scoped_namespace_declaration":
        ns_name = _csharp_namespace_name(node, source)
        if ns_name:
            namespace_stack.append(ns_name)
            scope_stack.append(f"s{node.start_byte}")
            ns_label = ".".join(namespace_stack)
            ns_nid = _csharp_namespace_id(ns_label)
            line = node.start_point[0] + 1
            add_node_fn(ns_nid, ns_label, line, node_type="namespace", metadata={"kind": "csharp_namespace"})
            add_edge_fn(file_nid, ns_nid, "contains", line)
        return True
    if parent_class_nid and node.type.startswith("preproc_"):
        for child in node.children:
            walk_fn(child, parent_class_nid)
        return True
    return False



def _build_csharp_type_def_index(all_nodes: list[dict]) -> dict[tuple[str, str], str]:
    """Return deterministic ``(namespace, name) -> node_id`` C# type definitions."""
    candidates: dict[tuple[str, str], list[dict]] = {}
    for node in all_nodes:
        if node.get("type") == "namespace":
            continue
        metadata = node.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("is_nested_type"):
            continue
        nid = node.get("id")
        label = node.get("label")
        if not (isinstance(nid, str) and nid and isinstance(label, str) and label):
            continue
        source_file = node.get("source_file")
        if (
            not isinstance(source_file, str)
            or not source_file.endswith(".cs")
            or node.get("file_type") != "code"
        ):
            continue
        if label.endswith(")") or label.startswith(".") or "." in label:
            continue
        namespace = metadata.get("namespace", "")
        if not isinstance(namespace, str):
            namespace = ""
        candidates.setdefault((namespace, label), []).append(node)

    return {
        key: sorted(
            nodes,
            key=lambda node: (
                str(node.get("source_file") or ""),
                str(node.get("source_location") or ""),
                str(node.get("id") or ""),
            ),
        )[0]["id"]
        for key, nodes in candidates.items()
    }


def _strip_trailing_csharp_generic_args(target_fqn: str) -> str:
    target_fqn = target_fqn.strip()
    if not target_fqn.endswith(">"):
        return target_fqn
    depth = 0
    for index in range(len(target_fqn) - 1, -1, -1):
        char = target_fqn[index]
        if char == ">":
            depth += 1
        elif char == "<":
            depth -= 1
            if depth == 0:
                return target_fqn[:index].strip()
    return target_fqn


def _resolve_cross_file_csharp_imports(
    per_file: list[dict],
    paths: list[Path],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Re-point resolvable C# ``using`` import edges to canonical internal nodes.

    Namespace imports resolve only to canonical C# namespace nodes. Alias imports
    resolve only when the alias target's prefix is a known canonical namespace and
    the simple type name exists in the shared C# type-definition index. ``using
    static`` and nested type aliases remain deliberate gaps because they need
    member/nested-type modeling beyond this import pass.
    """
    _ = (per_file, paths)
    namespace_id_by_label: dict[str, str] = {}
    for node in sorted(
        all_nodes,
        key=lambda node: (
            str(node.get("source_file") or ""),
            str(node.get("source_location") or ""),
            str(node.get("id") or ""),
        ),
    ):
        if node.get("type") != "namespace":
            continue
        label = node.get("label")
        nid = node.get("id")
        if isinstance(label, str) and label and isinstance(nid, str) and nid:
            namespace_id_by_label.setdefault(label, nid)

    type_def_index = _build_csharp_type_def_index(all_nodes)
    if not namespace_id_by_label and not type_def_index:
        return

    repointed_from: set[str] = set()
    for edge in all_edges:
        if edge.get("relation") != "imports":
            continue
        metadata = edge.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        using_kind = metadata.get("using_kind")
        target_fqn = metadata.get("target_fqn")
        if not using_kind or not isinstance(target_fqn, str) or not target_fqn:
            continue

        resolved = None
        if using_kind == "namespace":
            resolved = namespace_id_by_label.get(target_fqn)
        elif using_kind == "alias":
            base_fqn = _strip_trailing_csharp_generic_args(html.unescape(target_fqn))
            prefix, sep, name = base_fqn.rpartition(".")
            if sep and prefix in namespace_id_by_label:
                resolved = type_def_index.get((prefix, name))

        old_target = edge.get("target")
        if resolved and resolved != old_target:
            edge["target"] = resolved
            if isinstance(old_target, str) and old_target:
                repointed_from.add(old_target)

    if not repointed_from:
        return

    still_referenced: set[str] = set()
    for edge in all_edges:
        still_referenced.add(edge.get("source"))
        still_referenced.add(edge.get("target"))
    all_nodes[:] = [
        node for node in all_nodes
        if node.get("id") not in repointed_from or node.get("id") in still_referenced
    ]


_DOTNET_SOURCE_EXTS = (".cs", ".razor", ".cshtml")


def _is_cs_file(value: object) -> bool:
    return isinstance(value, str) and value.endswith(".cs")


def _is_dotnet_source_file(value: object) -> bool:
    return isinstance(value, str) and value.endswith(_DOTNET_SOURCE_EXTS)


def _metadata(value: object) -> dict:
    return value if isinstance(value, dict) else {}


class CsharpNameResolver:
    """Namespace/using/alias-aware C# simple-name resolution.

    Factored out of ``_resolve_csharp_type_references`` so the member-call pass
    (``_resolve_csharp_member_calls`` in ``graph_fy/extract.py``) can type a
    receiver with the same scoping rules the type-reference side already applies
    (the core of #1620), instead of bailing on any corpus-wide bare-name clash.

    Built from graph-stamped facts only: the ``(namespace, name)`` type-definition
    index plus the per-file ``using``/alias import edges (each carrying its lexical
    scope — ``("file", None)`` applies file-wide; ``("namespace", scope_id)``
    applies only where ``scope_id`` is in the referencing node's ``scope_chain``).
    """

    def __init__(self, all_nodes: list[dict], all_edges: list[dict]) -> None:
        self.node_by_id: dict[str, dict] = {
            node["id"]: node
            for node in all_nodes
            if isinstance(node.get("id"), str) and node.get("id")
        }
        self.type_def_index = _build_csharp_type_def_index(all_nodes)
        self.known_namespaces = {
            node.get("label")
            for node in all_nodes
            if node.get("type") == "namespace" and isinstance(node.get("label"), str)
        }

        self.namespace_usings_by_file: dict[str, list[tuple[str, str, str | None]]] = {}
        self.aliases_by_file: dict[str, dict[str, list[tuple[str, str, str | None]]]] = {}

        for edge in all_edges:
            if edge.get("relation") != "imports":
                continue
            source_node = self.node_by_id.get(edge.get("source"))
            if not (
                source_node
                and isinstance(source_node.get("label"), str)
                and source_node.get("label", "").endswith(_DOTNET_SOURCE_EXTS)
            ):
                continue
            source_file = source_node.get("source_file")
            if not _is_dotnet_source_file(source_file):
                continue
            metadata = _metadata(edge.get("metadata"))
            target_fqn = metadata.get("target_fqn")
            if not isinstance(target_fqn, str) or not target_fqn:
                continue
            scope_kind = metadata.get("scope_kind") or "file"
            scope_id = metadata.get("scope_id")
            using_kind = metadata.get("using_kind")
            if using_kind == "namespace":
                entry = (target_fqn, scope_kind, scope_id)
                bucket = self.namespace_usings_by_file.setdefault(source_file, [])
                if entry not in bucket:
                    bucket.append(entry)
            elif using_kind == "alias":
                alias = metadata.get("alias")
                if isinstance(alias, str) and alias:
                    entry = (target_fqn, scope_kind, scope_id)
                    bucket = self.aliases_by_file.setdefault(source_file, {}).setdefault(alias, [])
                    if entry not in bucket:
                        bucket.append(entry)

    @staticmethod
    def _namespace(node: dict | None) -> str:
        metadata = _metadata((node or {}).get("metadata"))
        namespace = metadata.get("namespace", "")
        return namespace if isinstance(namespace, str) else ""

    @staticmethod
    def _scope_chain(node: dict) -> list[str]:
        chain = _metadata(node.get("metadata")).get("scope_chain")
        return chain if isinstance(chain, list) else []

    def _using_in_scope(self, scope_kind: str, scope_id: str | None, source_node: dict) -> bool:
        if scope_kind == "file":
            return True
        return scope_id is not None and scope_id in self._scope_chain(source_node)

    def _scopes_for(self, source_node: dict, source_file: str) -> list[str]:
        def _append_unique(items: list[str], value: str) -> None:
            if value not in items:
                items.append(value)

        scopes: list[str] = []
        _append_unique(scopes, self._namespace(source_node))
        _append_unique(scopes, "")
        for namespace, scope_kind, scope_id in self.namespace_usings_by_file.get(source_file, []):
            if self._using_in_scope(scope_kind, scope_id, source_node):
                _append_unique(scopes, namespace)
        return scopes

    def _resolve_alias(self, label: str, source_node: dict, source_file: str) -> str | None:
        hits = set()
        for target_fqn, scope_kind, scope_id in self.aliases_by_file.get(source_file, {}).get(label, []):
            if not self._using_in_scope(scope_kind, scope_id, source_node):
                continue
            base_fqn = _strip_trailing_csharp_generic_args(html.unescape(target_fqn))
            namespace, sep, simple_name = base_fqn.rpartition(".")
            if not sep:
                simple_name = namespace
                namespace = ""
            if not simple_name:
                continue
            hit = self.type_def_index.get((namespace, simple_name))
            if hit:
                hits.add(hit)
        return next(iter(hits)) if len(hits) == 1 else None

    def resolve_type_name(
        self, label: str, source_node: dict, source_file: str
    ) -> tuple[str | None, bool]:
        """Resolve a simple type name to a definition node id, with a verdict.

        Returns ``(node_id, decisive)``:
          * ``(nid, True)`` — exactly one in-scope definition (or alias target).
          * ``(None, True)`` — the name is claimed by this file's scoping (an
            alias, or >1 in-scope candidates): genuinely ambiguous/unresolvable,
            the caller must NOT fall back to a looser corpus-wide match.
          * ``(None, False)`` — scoping knows nothing about the name; a caller
            may fall back (e.g. to the corpus-wide unique bare-name match).
        """
        if label in self.aliases_by_file.get(source_file, {}):
            return self._resolve_alias(label, source_node, source_file), True
        candidates: list[str] = []
        for namespace in self._scopes_for(source_node, source_file):
            hit = self.type_def_index.get((namespace, label))
            if hit and hit not in candidates:
                candidates.append(hit)
        if len(candidates) == 1:
            return candidates[0], True
        return None, bool(candidates)

    def resolve_label(self, label: str, source_node: dict, source_file: str) -> str | None:
        resolved, _decisive = self.resolve_type_name(label, source_node, source_file)
        return resolved

    def resolve_qualified(
        self, label: str, qualifier: object, source_node: dict, source_file: str
    ) -> str | None:
        # Sound qualified resolution: an in-scope alias for Q shadows the namespace Q. For a qualified
        # ref Q.label, look up (alias_target_namespace, label). If no in-scope alias, fall through to an
        # exact known namespace. Dangle on ambiguity / no hit / unknown qualifier.
        if not isinstance(qualifier, str) or not qualifier:
            return None
        in_scope = [
            entry for entry in self.aliases_by_file.get(source_file, {}).get(qualifier, [])
            if self._using_in_scope(entry[1], entry[2], source_node)
        ]
        if in_scope:
            hits = set()
            for target_fqn, _scope_kind, _scope_id in in_scope:
                alias_ns = _strip_trailing_csharp_generic_args(html.unescape(target_fqn))
                hit = self.type_def_index.get((alias_ns, label))
                if hit:
                    hits.add(hit)
            return next(iter(hits)) if len(hits) == 1 else None
        if qualifier in self.known_namespaces:
            return self.type_def_index.get((qualifier, label))
        return None


def _resolve_csharp_type_references(
    per_file: list[dict],
    paths: list[Path],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Arbitrate all C# ``inherits``/``implements``/``references`` targets.

    The extractor emits provisional same-file bindings and sourceless stubs. This
    pass is the single soundness gate: it uses only graph-stamped namespace/import
    facts, keeps a binding only when the referenced simple name resolves to one
    in-scope real type definition, and otherwise leaves the edge on a dangling stub.
    """
    _ = (per_file, paths)

    resolver = CsharpNameResolver(all_nodes, all_edges)
    node_by_id = resolver.node_by_id
    aliases_by_file = resolver.aliases_by_file

    def _resolve_label(label: str, source_node: dict, source_file: str) -> str | None:
        return resolver.resolve_label(label, source_node, source_file)

    def _resolve_qualified(label: str, qualifier: object, source_node: dict, source_file: str) -> str | None:
        return resolver.resolve_qualified(label, qualifier, source_node, source_file)

    def _is_placeholder(node: dict | None) -> bool:
        return bool(node) and not node.get("source_file")

    def _is_csharp_relevant_target(node: dict) -> bool:
        if node.get("type") == "namespace":
            return True
        source_file = node.get("source_file")
        return not source_file or _is_cs_file(source_file)

    def _label_for_type_ref_target(target_node: dict, source_file: str) -> str | None:
        label = target_node.get("label")
        if not isinstance(label, str) or not label:
            return None
        if not label.endswith(".cs"):
            return label

        stem = label[:-3]
        for alias in aliases_by_file.get(source_file, {}):
            if alias.lower() == stem.lower() or _make_id(alias) == _make_id(stem):
                return alias
        return stem or None

    def _dangling_stub_id(label: str, current_target: object) -> str:
        current = node_by_id.get(current_target)
        if _is_placeholder(current) and current.get("label") == label:
            return str(current_target)

        for node in all_nodes:
            nid = node.get("id")
            if (
                isinstance(nid, str)
                and node.get("label") == label
                and _is_placeholder(node)
            ):
                return nid

        stem = _make_id(label)
        stub_id = stem
        if stub_id in node_by_id:
            stub_id = _make_id("csharp_type_ref", label)
            suffix = 2
            while stub_id in node_by_id:
                stub_id = _make_id("csharp_type_ref", label, str(suffix))
                suffix += 1
        node = {
            "id": stub_id,
            "label": label,
            "file_type": "code",
            "source_file": "",
            "source_location": "",
        }
        all_nodes.append(node)
        node_by_id[stub_id] = node
        return stub_id

    REPOINT_RELATIONS = {"implements", "inherits", "references"}
    repointed_from: set[str] = set()
    for edge in all_edges:
        if edge.get("relation") not in REPOINT_RELATIONS:
            continue
        source_file = edge.get("source_file")
        if not _is_dotnet_source_file(source_file):
            continue
        source_node = node_by_id.get(edge.get("source"))
        target_node = node_by_id.get(edge.get("target"))
        if not source_node or not target_node:
            continue
        if not _is_csharp_relevant_target(target_node):
            continue
        metadata = _metadata(edge.get("metadata"))
        label = metadata.get("ref_token") or _label_for_type_ref_target(target_node, source_file)
        if not label:
            continue
        if metadata.get("qualified"):
            resolved = _resolve_qualified(label, metadata.get("ref_qualifier"), source_node, source_file)
        else:
            resolved = _resolve_label(label, source_node, source_file)
        target = edge.get("target")
        desired = resolved or _dangling_stub_id(label, target)
        if desired != target:
            edge["target"] = desired
            if isinstance(target, str) and _is_placeholder(target_node):
                repointed_from.add(target)

    if not repointed_from:
        return

    still_referenced: set[str] = set()
    for edge in all_edges:
        still_referenced.add(edge.get("source"))
        still_referenced.add(edge.get("target"))
    all_nodes[:] = [
        node for node in all_nodes
        if node.get("id") not in repointed_from or node.get("id") in still_referenced
    ]


def _import_csharp(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    text = _read_text(node, source).strip().rstrip(";")
    if text.startswith("global "):
        text = text[len("global "):].strip()
    if not text.startswith("using"):
        return
    body = text[len("using"):].strip()
    using_kind, alias, target_fqn = "namespace", None, body
    if body.startswith("static "):
        using_kind, target_fqn = "static", body[len("static "):].strip()
    elif "=" in body:
        lhs, rhs = body.split("=", 1)
        using_kind, alias, target_fqn = "alias", lhs.strip(), rhs.strip()
    if not target_fqn:
        return
    edges.append({
        "source": file_nid,
        "target": _make_id(target_fqn),
        "relation": "imports",
        "context": "import",
        "confidence": "EXTRACTED",
        "source_file": str_path,
        "source_location": f"L{node.start_point[0] + 1}",
        "weight": 1.0,
        "metadata": sanitize_metadata({k: v for k, v in
            {"using_kind": using_kind, "alias": alias, "target_fqn": target_fqn,
             "scope_kind": "namespace" if scope_stack else "file",
             "scope_id": scope_stack[-1] if scope_stack else None}.items() if v is not None}),
    })


_CSHARP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_c_sharp",
    class_types=frozenset({
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "struct_declaration",
        "record_declaration",
    }),
    function_types=frozenset({"method_declaration"}),
    import_types=frozenset({"using_directive"}),
    call_types=frozenset({"invocation_expression", "object_creation_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_access_expression"}),
    call_accessor_field="name",
    body_fallback_child_types=("declaration_list",),
    function_boundary_types=frozenset({"method_declaration"}),
    import_handler=_import_csharp,
)


def extract_csharp(path: Path) -> dict:
    """Extract C# type declarations, methods, namespaces, and usings from a .cs file."""
    from graph_fy.extractors.engine import _extract_generic
    return _extract_generic(path, _CSHARP_CONFIG)


