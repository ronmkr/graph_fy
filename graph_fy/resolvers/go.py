"""Go package and qualified type resolver."""
from __future__ import annotations

import re
from pathlib import Path

from graph_fy.ids import make_id as _make_id
from graph_fy.resolvers.common import _is_type_like_definition, _resolve_cached


def _go_import_path_for_file(
    source_file: str | Path,
    root: Path,
    module_cache: dict[Path, str | None] | None = None,
) -> str | None:
    """Return the canonical Go import path for a source file inside a module."""
    cache = module_cache if module_cache is not None else {}
    path = Path(source_file)
    if not path.is_absolute():
        path = root / path
    try:
        directory = _resolve_cached(path).parent
    except OSError:
        directory = path.absolute().parent

    module_dir: Path | None = None
    module_path: str | None = None
    for candidate in (directory, *directory.parents):
        if candidate in cache:
            cached = cache[candidate]
            if cached:
                module_dir, module_path = candidate, cached
            break
        go_mod = candidate / "go.mod"
        if not go_mod.is_file():
            continue
        try:
            match = re.search(
                r"(?m)^\s*module\s+([^\s]+)",
                go_mod.read_text(encoding="utf-8"),
            )
        except (OSError, UnicodeError):
            match = None
        module_dir = candidate
        module_path = match.group(1) if match else None
        cache[candidate] = module_path
        break

    if not module_dir or not module_path:
        return None
    try:
        relative_dir = directory.relative_to(module_dir)
    except ValueError:
        return None
    suffix = relative_dir.as_posix()
    return module_path if suffix == "." else f"{module_path}/{suffix}"


def _resolve_go_type_references(
    per_file: list[dict],
    paths: list[Path],
    all_nodes: list[dict],
    all_edges: list[dict],
    root: Path,
    resolution_context_nodes: list[dict] | None = None,
    resolution_context_edges: list[dict] | None = None,
) -> None:
    """Resolve qualified Go types through aliases and exact module paths."""
    imports_by_file: dict[str, dict[str, str]] = {}
    actual_path_by_file: dict[str, Path] = {}
    for path, result in zip(paths, per_file):
        imports = result.get("go_imports") or {}
        for node in result.get("nodes", []):
            source_file = node.get("source_file")
            if source_file:
                imports_by_file[str(source_file)] = imports
                actual_path_by_file[str(source_file)] = path

    if not imports_by_file:
        return

    definition_nodes = all_nodes + (resolution_context_nodes or [])
    definition_edges = all_edges + (resolution_context_edges or [])
    contained = {edge.get("target") for edge in definition_edges
                 if edge.get("relation") == "contains"}
    module_cache: dict[Path, str | None] = {}
    fqn_to_ids: dict[str, list[str]] = {}
    for node in definition_nodes:
        source_file = str(node.get("source_file") or "")
        label = str(node.get("label") or "")
        nid = node.get("id")
        if (not source_file or not label or not nid or nid not in contained
                or not _is_type_like_definition(node)):
            continue
        actual_path = actual_path_by_file.get(source_file, Path(source_file))
        package_path = _go_import_path_for_file(actual_path, root, module_cache)
        if package_path:
            fqn_to_ids.setdefault(f"{package_path}.{label}", []).append(nid)

    qualified_stubs = {
        node["id"]: str(node.get("label") or "")
        for node in all_nodes
        if node.get("id") and not node.get("source_file")
        and "." in str(node.get("label") or "")
    }
    if not qualified_stubs:
        return

    node_ids = {node.get("id") for node in all_nodes if node.get("id")}
    external_stub_ids: dict[str, str] = {}
    new_nodes: list[dict] = []

    def external_stub(fqn: str) -> str:
        existing = external_stub_ids.get(fqn)
        if existing:
            return existing
        nid = _make_id("go", "type", fqn)
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
        if edge.get("relation") not in {"references", "embeds"}:
            continue
        target = edge.get("target")
        qualified = qualified_stubs.get(target)
        if not qualified:
            continue
        alias, _, type_name = qualified.rpartition(".")
        import_path = imports_by_file.get(
            str(edge.get("source_file") or ""), {}
        ).get(alias)
        if not import_path or not type_name:
            continue
        fqn = f"{import_path}.{type_name}"
        candidates = fqn_to_ids.get(fqn, [])
        edge["target"] = candidates[0] if len(candidates) == 1 else external_stub(fqn)
        repointed_from.add(str(target))

    if new_nodes:
        all_nodes.extend(new_nodes)

    if not repointed_from:
        return
    referenced = {endpoint for edge in all_edges
                  for endpoint in (edge.get("source"), edge.get("target"))}
    all_nodes[:] = [
        node for node in all_nodes
        if node.get("id") not in repointed_from or node.get("id") in referenced
    ]


def _merge_go_package_types(
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Merge a Go package's type declared in one file with the copies minted by the
    other files that declare methods on it into ONE node (#3399)."""
    by_id: dict[str, list[dict]] = {}
    for n in all_nodes:
        nid = n.get("id")
        label = str(n.get("label", ""))
        if not isinstance(nid, str) or not nid or n.get("file_type") != "code":
            continue
        # A Go type name holds neither character, while every other label the extractor
        # mints does: `Run()`, `.Close()`, and the file node's own `a.go`.
        if not label or "." in label or "(" in label:
            continue
        if Path(str(n.get("source_file", ""))).suffix.lower() != ".go":
            continue
        by_id.setdefault(nid, []).append(n)

    declaring_files: dict[str, set[str]] = {}
    for e in all_edges:
        if e.get("relation") == "contains":
            declaring_files.setdefault(str(e.get("target", "")), set()).add(
                str(e.get("source_file", "")))

    drop_objs: set[int] = set()
    for nid, group in by_id.items():
        if len(group) < 2:
            continue
        files = [Path(str(n.get("source_file", ""))) for n in group]
        if len({f.parent for f in files}) != 1:
            continue
        if any(f.name.endswith("_test.go") for f in files):
            continue
        # A type whose declaring file is outside the corpus leaves the whole group
        # method-only; one node is still the answer, so fold on the lowest path.
        declared = declaring_files.get(nid, set())
        candidates = [n for n in group if str(n.get("source_file", "")) in declared] or group
        keeper = min(candidates, key=lambda n: (str(n.get("source_file", "")),
                                                str(n.get("source_location", ""))))
        for node in group:
            if node is not keeper:
                drop_objs.add(id(node))

    if drop_objs:
        all_nodes[:] = [n for n in all_nodes if id(n) not in drop_objs]
