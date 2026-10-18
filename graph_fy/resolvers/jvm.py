"""Java and JVM cross-file import and type reference resolver."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _make_id, _read_text


def _resolve_cross_file_java_imports(
    per_file: list[dict],
    paths: list[Path],
) -> list[dict]:
    """Two-pass Java import resolution."""
    try:
        import tree_sitter_java as tsjava
        from tree_sitter import Language, Parser
    except ImportError:
        return []

    language = Language(tsjava.language())
    parser = Parser(language)

    parsed: dict[str, tuple[bytes, object]] = {}
    pkg_by_src: dict[str, str] = {}
    for path, file_result in zip(paths, per_file):
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue
        parsed[str(path)] = (source, tree)
        pkg = ""
        for child in tree.root_node.children:
            if child.type == "package_declaration":
                pkg = _read_text(child, source).strip()[len("package"):].strip().rstrip(";").strip()
                break
        pkg_by_src[str(path)] = pkg
        for node in file_result.get("nodes", []):
            src = node.get("source_file")
            if src:
                pkg_by_src.setdefault(src, pkg)

    def _pkg_matches(imp_pkg: str, tgt_pkg: str) -> bool:
        if imp_pkg == tgt_pkg:
            return True
        if tgt_pkg:
            if not imp_pkg.startswith(tgt_pkg + "."):
                return False
            rest = imp_pkg[len(tgt_pkg) + 1:]
        else:
            rest = imp_pkg
        return bool(rest) and all(seg[:1].isupper() for seg in rest.split("."))

    name_to_ids: dict[str, list[tuple[str, str]]] = {}
    for file_result in per_file:
        for node in file_result.get("nodes", []):
            label = node.get("label", "")
            nid = node.get("id", "")
            src = node.get("source_file", "")
            if not label or not nid or not src:
                continue
            if label.endswith(")") or label.endswith(".java"):
                continue
            if not label[0].isalpha() or not label[0].isupper():
                continue
            name_to_ids.setdefault(label, []).append((nid, pkg_by_src.get(src, "")))

    new_edges: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for path in paths:
        file_nid = _make_id(str(path))
        entry = parsed.get(str(path))
        if entry is None:
            continue
        source, tree = entry

        def walk(n) -> None:
            if n.type == "import_declaration":
                raw = _read_text(n, source).strip()
                body = raw[len("import"):].strip().rstrip(";").strip()
                if body.startswith("static "):
                    body = body[len("static "):].strip()
                if body.endswith(".*"):
                    return
                parts = body.split(".")
                if not parts:
                    return
                last = parts[-1]
                imp_pkg = ".".join(parts[:-1])
                if last and last[0].islower() and len(parts) >= 2:
                    last = parts[-2]
                    imp_pkg = ".".join(parts[:-2])
                at_line = n.start_point[0] + 1
                for tgt_nid, tgt_pkg in name_to_ids.get(last, []):
                    if tgt_nid == file_nid:
                        continue
                    if not _pkg_matches(imp_pkg, tgt_pkg):
                        continue
                    key = (file_nid, tgt_nid)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    new_edges.append({
                        "source": file_nid,
                        "target": tgt_nid,
                        "relation": "imports",
                        "confidence": "EXTRACTED",
                        "confidence_score": 1.0,
                        "source_file": str(path),
                        "source_location": f"L{at_line}",
                        "weight": 1.0,
                    })
            for child in n.children:
                walk(child)

        walk(tree.root_node)

    return new_edges


def _resolve_java_type_references(
    per_file: list[dict],
    paths: list[Path],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Re-point dangling Java implements/inherits edges using file import statements."""
    try:
        import tree_sitter_java as tsjava
        from tree_sitter import Language, Parser
    except ImportError:
        return

    language = Language(tsjava.language())
    parser = Parser(language)

    pkg_by_file: dict[str, str] = {}
    imports_by_file: dict[str, dict[str, str]] = {}
    for path, result in zip(paths, per_file):
        srcs = {n.get("source_file") for n in result.get("nodes", []) if n.get("source_file")}
        if not srcs:
            continue
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue
        pkg = ""
        imps: dict[str, str] = {}

        def walk(n) -> None:
            nonlocal pkg
            if n.type == "package_declaration":
                pkg = _read_text(n, source).strip()[len("package"):].strip().rstrip(";").strip()
            elif n.type == "import_declaration":
                body = _read_text(n, source).strip()[len("import"):].strip().rstrip(";").strip()
                if body.startswith("static "):
                    body = body[len("static "):].strip()
                if body.endswith(".*") or "." not in body:
                    return
                simple = body.split(".")[-1]
                if simple and simple[0].isupper():
                    imps[simple] = body
            for child in n.children:
                walk(child)

        walk(tree.root_node)
        for s in srcs:
            pkg_by_file[s] = pkg
            imports_by_file[s] = imps

    fqn_to_id: dict[str, str] = {}
    node_by_id = {
        node.get("id"): node for node in all_nodes if node.get("id")
    }
    type_parent_by_id: dict[str, str] = {}
    for edge in all_edges:
        if edge.get("relation") != "contains":
            continue
        child = node_by_id.get(edge.get("target"))
        parent = node_by_id.get(edge.get("source"))
        if not child or not parent:
            continue
        child_src = child.get("source_file", "")
        parent_label = parent.get("label", "")
        if (
            child_src
            and parent.get("source_file") == child_src
            and parent_label[:1].isupper()
            and not parent_label.endswith(".java")
        ):
            type_parent_by_id[child["id"]] = parent["id"]

    for node in all_nodes:
        label = node.get("label", "")
        src = node.get("source_file", "")
        nid = node.get("id", "")
        if not (label and src and nid) or src not in pkg_by_file:
            continue
        if not label[:1].isupper() or label.endswith(")") or label.endswith(".java"):
            continue
        pkg = pkg_by_file[src]
        fqn_to_id.setdefault(f"{pkg}.{label}" if pkg else label, nid)
        type_path = [label]
        seen = {nid}
        parent_id = type_parent_by_id.get(nid)
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = node_by_id[parent_id]
            type_path.append(parent["label"])
            parent_id = type_parent_by_id.get(parent_id)
        if len(type_path) > 1:
            nested_name = ".".join(reversed(type_path))
            fqn_to_id.setdefault(
                f"{pkg}.{nested_name}" if pkg else nested_name,
                nid,
            )

    stub_label: dict[str, str] = {
        node["id"]: node.get("label", "")
        for node in all_nodes
        if node.get("id") and not node.get("source_file")
        and (node.get("label", "")[:1].isupper() or "." in node.get("label", ""))
    }
    if not stub_label:
        return

    REPOINT_RELATIONS = {"implements", "inherits", "extends", "imports", "references"}

    node_ids = {n.get("id") for n in all_nodes if n.get("id")}
    external_stub_ids: dict[str, str] = {}
    new_nodes: list[dict] = []

    def _external_stub(fqn: str) -> str:
        nid = external_stub_ids.get(fqn)
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
        external_stub_ids[fqn] = nid
        return nid

    repointed_from: set[str] = set()
    for edge in all_edges:
        if edge.get("relation") not in REPOINT_RELATIONS:
            continue
        tgt = edge.get("target")
        label = stub_label.get(tgt)
        if not label:
            continue
        ref_file = edge.get("source_file", "")
        if "." in label:
            resolved = fqn_to_id.get(label)
            if resolved and resolved != tgt:
                edge["target"] = resolved
                repointed_from.add(tgt)
            continue
        fqn = imports_by_file.get(ref_file, {}).get(label)
        if fqn:
            resolved = fqn_to_id.get(fqn)
            if resolved is None:
                head = fqn.split(".")[:-1]
                while resolved is None and head and head[-1][:1].isupper():
                    head.pop()
                    resolved = fqn_to_id.get(".".join(head + [label]))
            if resolved is None:
                edge["target"] = _external_stub(fqn)
                repointed_from.add(tgt)
                continue
        else:
            pkg = pkg_by_file.get(ref_file, "")
            resolved = fqn_to_id.get(f"{pkg}.{label}" if pkg else label)
        if resolved and resolved != tgt:
            edge["target"] = resolved
            repointed_from.add(tgt)

    if new_nodes:
        all_nodes.extend(new_nodes)

    seen_attribute_refs: set[tuple] = set()
    deduped_edges: list[dict] = []
    for edge in all_edges:
        if (
            edge.get("relation") == "references"
            and edge.get("context") == "attribute"
            and edge.get("source_file", "") in pkg_by_file
        ):
            key = (
                edge.get("source"),
                edge.get("target"),
                edge.get("relation"),
                edge.get("context"),
                edge.get("source_file"),
                edge.get("source_location"),
            )
            if key in seen_attribute_refs:
                continue
            seen_attribute_refs.add(key)
        deduped_edges.append(edge)
    all_edges[:] = deduped_edges

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
