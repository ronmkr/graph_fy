"""PHP namespace and type reference resolver."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graph_fy.resolvers.common import _make_id, _read_text


_PHP_SUPERTYPE_RELATIONS = ("inherits", "implements", "mixes_in")
_PHP_REPOINT_RELATIONS = frozenset({"inherits", "implements", "mixes_in", "imports", "references"})


def _php_fqn_from_raw(raw: str, ns: str, uses: dict[str, str]) -> str:
    """Resolve a raw (possibly qualified) PHP class reference to an FQN."""
    raw = raw.strip()
    if raw.startswith("\\"):
        return raw[1:]
    if "\\" in raw:
        first, rest = raw.split("\\", 1)
        mapped = uses.get(first.lower())
        if mapped:
            return f"{mapped}\\{rest}"
        return f"{ns}\\{raw}" if ns else raw
    mapped = uses.get(raw.lower())
    if mapped:
        return mapped
    return f"{ns}\\{raw}" if ns else raw


def _resolve_php_type_references(
    per_file: list[dict],
    paths: list[Path],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Disambiguate PHP inherits/implements/mixes_in/imports/references targets."""
    try:
        import tree_sitter_php as tsphp
        from tree_sitter import Language, Parser
    except ImportError:
        return

    lang_fn = getattr(tsphp, "language_php", None) or getattr(tsphp, "language", None)
    if lang_fn is None:
        return
    language = Language(lang_fn())
    parser = Parser(language)

    ns_by_file: dict[str, str] = {}
    uses_by_file: dict[str, dict[str, str]] = {}
    raw_by_file: dict[str, dict[tuple[str, str], str | None]] = {}

    for path, result in zip(paths, per_file):
        srcs = {n.get("source_file") for n in result.get("nodes", []) if n.get("source_file")}
        if not srcs:
            continue
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue

        namespaces: list[str] = []
        uses: dict[str, str] = {}
        raws: dict[tuple[str, str], str | None] = {}

        def _record_raw(relation: str, raw: str) -> None:
            bare = raw.rsplit("\\", 1)[-1].strip().lower()
            if not bare:
                return
            key = (relation, bare)
            if key in raws and raws[key] != raw:
                raws[key] = None
            else:
                raws.setdefault(key, raw)

        def _record_use_clause(clause, prefix: str) -> None:
            target = None
            alias = None
            saw_as = False
            for c in clause.children:
                if c.type in ("function", "const"):
                    return
                if c.type == "as":
                    saw_as = True
                elif c.type in ("qualified_name", "name"):
                    if saw_as:
                        alias = _read_text(c, source)
                    elif target is None:
                        target = _read_text(c, source)
            if not target:
                return
            fqn = (f"{prefix}\\{target}" if prefix else target).lstrip("\\")
            key = (alias or fqn.rsplit("\\", 1)[-1]).strip().lower()
            if key:
                uses.setdefault(key, fqn)

        def walk(n) -> None:
            t = n.type
            if t == "namespace_definition":
                for c in n.children:
                    if c.type == "namespace_name":
                        namespaces.append(_read_text(c, source))
                        break
            elif t == "namespace_use_declaration":
                prefix = ""
                group = None
                for c in n.children:
                    if c.type == "namespace_name":
                        prefix = _read_text(c, source)
                    elif c.type == "namespace_use_group":
                        group = c
                    elif c.type == "namespace_use_clause":
                        _record_use_clause(c, "")
                if group is not None:
                    for c in group.children:
                        if c.type == "namespace_use_clause":
                            _record_use_clause(c, prefix)
                return
            elif t == "class_declaration":
                for child in n.children:
                    if child.type == "base_clause":
                        for sub in child.children:
                            if sub.type in ("name", "qualified_name"):
                                _record_raw("inherits", _read_text(sub, source))
                    elif child.type == "class_interface_clause":
                        for sub in child.children:
                            if sub.type in ("name", "qualified_name"):
                                _record_raw("implements", _read_text(sub, source))
                    elif child.type == "declaration_list":
                        for member in child.children:
                            if member.type != "use_declaration":
                                continue
                            for sub in member.children:
                                if sub.type in ("name", "qualified_name"):
                                    _record_raw("mixes_in", _read_text(sub, source))
            for child in n.children:
                walk(child)

        walk(tree.root_node)
        if len(set(namespaces)) > 1:
            continue
        ns = namespaces[0] if namespaces else ""
        for s in srcs:
            ns_by_file[s] = ns
            uses_by_file[s] = uses
            raw_by_file[s] = raws

    if not ns_by_file:
        return

    fqn_to_id: dict[str, str] = {}
    for node in all_nodes:
        label = node.get("label", "")
        src = node.get("source_file", "")
        nid = node.get("id", "")
        if not (label and src and nid) or src not in ns_by_file:
            continue
        if label.endswith(")") or "." in label:
            continue
        ns = ns_by_file[src]
        fqn = f"{ns}\\{label}" if ns else label
        fqn_to_id.setdefault(fqn.lower(), nid)

    node_ids = {n.get("id") for n in all_nodes if n.get("id")}
    stub_label: dict[str, str] = {
        n["id"]: n.get("label", "")
        for n in all_nodes
        if n.get("id") and not n.get("source_file") and n.get("label")
    }

    external_stub_ids: dict[str, str] = {}
    new_nodes: list[dict] = []

    def _external_stub(fqn: str) -> str:
        key = fqn.lower()
        nid = external_stub_ids.get(key)
        if nid:
            return nid
        nid = _make_id(fqn)
        if nid not in node_ids:
            new_nodes.append({
                "id": nid,
                "label": fqn,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
            })
            node_ids.add(nid)
        external_stub_ids[key] = nid
        return nid

    repointed_from: set[str] = set()
    for edge in all_edges:
        relation = edge.get("relation")
        if relation not in _PHP_REPOINT_RELATIONS:
            continue
        ref_file = edge.get("source_file", "")
        if ref_file not in ns_by_file:
            continue
        tgt = edge.get("target")
        label = stub_label.get(tgt)
        uses = uses_by_file.get(ref_file, {})
        if not label and relation == "imports":
            label = next((alias for alias in uses if _make_id(alias) == tgt), "")
        if not label:
            continue
        bare = label.strip().lower()
        ns = ns_by_file[ref_file]

        raw = None
        if relation in _PHP_SUPERTYPE_RELATIONS:
            raw = raw_by_file.get(ref_file, {}).get((relation, bare))

        explicit = False
        if raw and "\\" in raw:
            fqn = _php_fqn_from_raw(raw, ns, uses)
            explicit = True
        elif bare in uses:
            fqn = uses[bare]
            explicit = True
        elif ns:
            fqn = f"{ns}\\{label}"
        else:
            continue

        resolved = fqn_to_id.get(fqn.lower())
        if resolved and resolved != tgt:
            edge["target"] = resolved
            repointed_from.add(tgt)
        elif explicit and resolved is None:
            edge["target"] = _external_stub(fqn)
            repointed_from.add(tgt)

    if new_nodes:
        all_nodes.extend(new_nodes)
    if not repointed_from:
        return

    still_referenced: set[str] = set()
    for edge in all_edges:
        still_referenced.add(edge.get("source"))
        still_referenced.add(edge.get("target"))
    all_nodes[:] = [
        n for n in all_nodes
        if n.get("id") not in repointed_from or n.get("id") in still_referenced
    ]
