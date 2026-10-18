"""C, C++, and Objective-C include path and declaration/definition class merger."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graph_fy.resolvers.common import _resolve_cached

_DECLDEF_HEADER_SUFFIXES = frozenset({".h", ".hpp", ".hh", ".hxx"})
_DECLDEF_IMPL_SUFFIXES = frozenset({".m", ".mm", ".cpp", ".cc", ".cxx", ".c"})


def _resolve_c_include_path(raw: str, str_path: str, extra_include_dirs: list[Path] | None = None) -> "Path | None":
    """Resolve a quoted #include path to a real file on disk.

    Checks:
    1. Same-directory relative to the including file.
    2. Caller-provided extra include directories.
    3. Target-scoped CMake include directories (with transitive link library DAG inheritance).
    """
    if not raw:
        return None
    candidate = _resolve_cached((Path(str_path).parent / raw))
    if candidate.is_file():
        return candidate

    if extra_include_dirs:
        for inc_dir in extra_include_dirs:
            c = _resolve_cached(inc_dir / raw)
            if c.is_file():
                return c

    try:
        from graph_fy.extractors.cmake import get_cmake_include_dirs
        for inc_dir in get_cmake_include_dirs(str_path):
            c = _resolve_cached(inc_dir / raw)
            if c.is_file():
                return c
    except Exception:
        pass

    return None



def _decldef_class_stem(source_file: str) -> tuple[str, str] | None:
    """Return (dir, base_stem) for a header/impl source file, else None."""
    if not source_file:
        return None
    p = Path(source_file)
    suffix = p.suffix.lower()
    if suffix not in _DECLDEF_HEADER_SUFFIXES and suffix not in _DECLDEF_IMPL_SUFFIXES:
        return None
    stem = p.stem.split("+", 1)[0]
    if not stem:
        return None
    return (str(p.parent), stem)


def _source_stem(node: dict) -> str:
    """Filename stem of a node's source_file."""
    return Path(str(node.get("source_file", ""))).stem


def _merge_decl_def_classes(
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Merge a class declared in a header with its definition in an impl file into ONE node."""
    by_id: dict[str, list[dict]] = {}
    for n in all_nodes:
        if n.get("file_type") != "code":
            continue
        nid = n.get("id")
        sf = str(n.get("source_file", ""))
        if not isinstance(nid, str) or not nid or not sf:
            continue
        by_id.setdefault(nid, []).append(n)

    drop_objs: set[int] = set()
    for nid, group in by_id.items():
        if len(group) < 2:
            continue
        sibling_keys: set[tuple[str, str]] = set()
        headers: list[dict] = []
        ok = True
        for node in group:
            sf = str(node.get("source_file", ""))
            ds = _decldef_class_stem(sf)
            if ds is None:
                ok = False
                break
            sibling_keys.add(ds)
            if Path(sf).suffix.lower() in _DECLDEF_HEADER_SUFFIXES:
                headers.append(node)
        if not ok:
            continue
        if len(sibling_keys) != 1 or not headers:
            continue
        if len(headers) == 1:
            keeper = headers[0]
        else:
            base_headers = [h for h in headers if "+" not in _source_stem(h)]
            if len(base_headers) > 1:
                continue
            keeper = base_headers[0] if base_headers else min(headers, key=_source_stem)

        impls = sorted(
            (n for n in group
             if n is not keeper
             and Path(str(n.get("source_file", ""))).suffix.lower()
             in _DECLDEF_IMPL_SUFFIXES),
            key=lambda n: (str(n.get("source_file", "")),
                           str(n.get("source_location", ""))),
        )
        if impls:
            definition = impls[0]
            if definition.get("source_file"):
                keeper["definition_file"] = definition["source_file"]
            if definition.get("source_location"):
                keeper["definition_location"] = definition["source_location"]

        for node in group:
            if node is not keeper:
                drop_objs.add(id(node))

    if not drop_objs:
        return

    all_nodes[:] = [n for n in all_nodes if id(n) not in drop_objs]

    seen_keys: set[tuple] = set()
    rewritten: list[dict] = []
    for e in all_edges:
        src = e.get("source")
        tgt = e.get("target")
        if src == tgt:
            continue
        k = (src, tgt, e.get("relation"), e.get("context"))
        if k in seen_keys:
            continue
        seen_keys.add(k)
        rewritten.append(e)
    all_edges[:] = rewritten
