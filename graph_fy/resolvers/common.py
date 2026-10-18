"""Common resolution path caching, source key memoization, and node id disambiguation."""
from __future__ import annotations

import functools
import hashlib
import os
from pathlib import Path
from typing import Any

from graph_fy.ids import make_id as _make_id


def _file_stem(path: Path) -> str:
    return path.with_suffix("").as_posix()


def _read_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


@functools.lru_cache(maxsize=65536)
def _cached_source_key(source_file: str, root_str: str, _cwd: str) -> str:
    """Resolve-and-relativize one source path, memoized (#perf)."""
    source_path = Path(source_file)
    try:
        return str(source_path.resolve().relative_to(root_str))
    except Exception:
        return str(source_path)


def _get_process_cwd() -> str:
    return os.getcwd()


def _source_key(source_file: str, root: Path) -> str:
    if not source_file:
        return ""
    return _cached_source_key(source_file, str(root), _get_process_cwd())


def _node_disambiguation_source_key(node: dict, root: Path) -> str:
    source_file = str(node.get("source_file", ""))
    if source_file:
        return _source_key(source_file, root)
    return _source_key(str(node.get("origin_file", "")), root)


def _disambiguate_colliding_node_ids(
    nodes: list[dict],
    edges: list[dict],
    raw_calls: list[dict],
    root: Path,
) -> None:
    """Rewrite only colliding node IDs, using source path as the disambiguator."""
    by_id: dict[str, list[dict]] = {}
    for node in nodes:
        if node.get("type") in ("module", "namespace"):
            continue
        nid = node.get("id")
        if isinstance(nid, str) and nid:
            by_id.setdefault(nid, []).append(node)

    remap: dict[tuple[str, str], str] = {}
    ambiguous_ids: set[str] = set()
    for old_id, group in by_id.items():
        source_keys = {_node_disambiguation_source_key(node, root) for node in group}
        if len(group) < 2 or len(source_keys) < 2:
            continue
        ambiguous_ids.add(old_id)
        naive: dict[str, str] = {}
        for source_key in source_keys:
            if source_key:
                naive[source_key] = _make_id(source_key, old_id)
        seen: dict[str, int] = {}
        for nid in naive.values():
            seen[nid] = seen.get(nid, 0) + 1
        needs_hash = {sk for sk, nid in naive.items() if seen.get(nid, 0) > 1}
        for node in group:
            source_key = _node_disambiguation_source_key(node, root)
            if not source_key:
                continue
            if source_key in needs_hash:
                salt = hashlib.sha1(source_key.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
                new_id = _make_id(source_key, old_id, salt)
            else:
                new_id = naive.get(source_key) or _make_id(source_key, old_id)
            remap[(old_id, source_key)] = new_id
            if new_id != old_id:
                node["id"] = new_id

    if not remap:
        for edge in edges:
            edge.pop("target_file", None)
        return

    unambiguous_remaps: dict[str, str] = {}
    for old_id, group in by_id.items():
        if old_id in ambiguous_ids:
            continue
        candidates = {
            node["id"] for node in group
            if isinstance(node.get("id"), str) and node["id"] != old_id
        }
        if len(candidates) == 1:
            unambiguous_remaps[old_id] = next(iter(candidates))

    _HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".hxx")
    header_remaps: dict[str, str] = {}
    for old_id in ambiguous_ids:
        for node in by_id.get(old_id, []):
            sk = _node_disambiguation_source_key(node, root)
            if sk and Path(sk).suffix.lower() in _HEADER_SUFFIXES:
                new_id = remap.get((old_id, sk))
                if new_id:
                    header_remaps[old_id] = new_id
                    break

    for edge in edges:
        edge_source_key = _source_key(str(edge.get("source_file", "")), root)
        source_key = (edge.get("source", ""), edge_source_key)
        target_file = edge.pop("target_file", None)
        if target_file and edge.get("relation") in ("imports", "imports_from", "re_exports"):
            target_edge_key = _source_key(str(target_file), root)
        else:
            target_edge_key = edge_source_key
        target_key = (edge.get("target", ""), target_edge_key)
        if source_key in remap:
            edge["source"] = remap[source_key]
        elif edge.get("source") in unambiguous_remaps:
            edge["source"] = unambiguous_remaps[str(edge["source"])]
        if (edge.get("relation") in ("imports", "imports_from")
                and edge.get("target") in header_remaps):
            edge["target"] = header_remaps[str(edge["target"])]
        elif target_key in remap:
            edge["target"] = remap[target_key]
        elif edge.get("target") in unambiguous_remaps:
            edge["target"] = unambiguous_remaps[str(edge["target"])]

    for raw_call in raw_calls:
        call_source_key = _source_key(str(raw_call.get("source_file", "")), root)
        caller_key = (raw_call.get("caller_nid", ""), call_source_key)
        if caller_key in remap:
            raw_call["caller_nid"] = remap[caller_key]
        elif raw_call.get("caller_nid") in unambiguous_remaps:
            raw_call["caller_nid"] = unambiguous_remaps[str(raw_call["caller_nid"])]


def _is_type_like_definition(node: dict) -> bool:
    if node.get("type") == "namespace":
        return False
    label = str(node.get("label", "")).strip()
    if not label:
        return False
    if label.endswith(")") or label.startswith("."):
        return False
    if "." in label:
        return False
    return node.get("file_type") == "code"


@functools.lru_cache(maxsize=65536)
def _cached_realpath(path_str: str, _cwd: str) -> Path:
    return Path(path_str).resolve()


def _resolve_cached(path: "Path | str") -> Path:
    """Path.resolve() with a per-(path, cwd) memo."""
    return _cached_realpath(str(path), _get_process_cwd())
