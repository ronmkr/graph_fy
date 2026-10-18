"""Manifest management and incremental change detection for graph_fy.

Handles loading, saving, and checking file mtimes and MD5 content hashes
with atomic writes, NFC Unicode normalization, and relative path storage.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Any

from graph_fy.paths import os_path as _os_path, out_path, write_json_atomic

_MANIFEST_PATH = str(out_path("manifest.json"))
_MTIME_COARSE_S = 2.0
_MTIME_SUBSECOND_S = 0.05


def _nfc(s: str) -> str:
    """NFC-normalize a path string used as a manifest key."""
    return unicodedata.normalize("NFC", s)


def _md5_file(path: Path) -> str:
    """MD5 of file contents streamed in 64KB chunks — for change detection only."""
    h = hashlib.md5(usedforsecurity=False)
    try:
        with open(_os_path(path), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _stat_and_hash(path_str: str) -> tuple[str, float, str] | None:
    """Stat + MD5 a single file; returns None on OSError (e.g. deleted mid-run)."""
    try:
        p = Path(path_str)
        return path_str, os.stat(_os_path(p)).st_mtime, _md5_file(p)
    except OSError:
        return None


def _to_relative_for_storage(key: str, root: Path) -> str:
    """Return ``key`` as a forward-slash relative path from ``root``."""
    p = Path(key)
    if not p.is_absolute():
        return key
    try:
        base = _nfc(str(Path(root).resolve()))
        rel = os.path.relpath(_nfc(str(p)), base)
    except (ValueError, OSError):
        return key
    if rel == ".." or rel.startswith(".." + os.sep) or rel.startswith("../"):
        return key
    return rel.replace(os.sep, "/")


def _to_absolute_from_storage(key: str, root: Path) -> str:
    """Inverse of :func:`_to_relative_for_storage`."""
    p = Path(key)
    if p.is_absolute():
        return str(p)
    return _nfc(str(Path(root).resolve() / p))


def load_manifest(
    manifest_path: str = _MANIFEST_PATH,
    *,
    root: Path | None = None,
) -> dict:
    """Load the manifest from a previous run. Returns {} on any error."""
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return raw
    if root is None:
        return {_nfc(k): v for k, v in raw.items()}
    return {_nfc(_to_absolute_from_storage(k, root)): v for k, v in raw.items()}


def save_manifest(
    files: dict[str, list[str]],
    manifest_path: str = _MANIFEST_PATH,
    *,
    kind: str = "both",
    root: Path | None = None,
    scan_corpus: set[str] | list[str] | None = None,
    clear_semantic: set[str] | list[str] | None = None,
    clear_ast: set[str] | list[str] | None = None,
) -> None:
    """Save current file mtimes + content hashes for change detection."""
    existing = load_manifest(manifest_path, root=root)

    def _path_index(paths: set[str] | list[str] | None) -> set[str] | None:
        if paths is None:
            return None
        indexed: set[str] = set()
        for p in paths:
            indexed.add(p)
            indexed.add(_nfc(p))
        return indexed

    scan_set = _path_index(scan_corpus)
    clear_set = _path_index(clear_semantic)
    clear_ast_set = _path_index(clear_ast)
    try:
        root_res: Path | None = Path(root).resolve() if root is not None else None
    except (OSError, RuntimeError):
        root_res = Path(root) if root is not None else None

    def _in_scan(path_str: str) -> bool:
        if path_str in scan_set or _nfc(path_str) in scan_set:
            return True
        try:
            resolved = str(Path(path_str).resolve())
            return resolved in scan_set or _nfc(resolved) in scan_set
        except (OSError, RuntimeError):
            return False

    def _in_clear(path_str: str) -> bool:
        if clear_set is None:
            return False
        if path_str in clear_set or _nfc(path_str) in clear_set:
            return True
        try:
            resolved = str(Path(path_str).resolve())
            return resolved in clear_set or _nfc(resolved) in clear_set
        except (OSError, RuntimeError):
            return False

    def _in_clear_ast(path_str: str) -> bool:
        if clear_ast_set is None:
            return False
        if path_str in clear_ast_set or _nfc(path_str) in clear_ast_set:
            return True
        try:
            resolved = str(Path(path_str).resolve())
            return resolved in clear_ast_set or _nfc(resolved) in clear_ast_set
        except (OSError, RuntimeError):
            return False

    def _in_root(path_str: str) -> bool:
        if root_res is None:
            return False
        p = Path(path_str)
        try:
            p.relative_to(root_res)
            return True
        except ValueError:
            pass
        try:
            p.resolve().relative_to(root_res)
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    def _normalise_entry(entry):
        if isinstance(entry, (int, float)):
            return {"mtime": entry, "ast_hash": "", "semantic_hash": ""}
        if isinstance(entry, dict) and "hash" in entry and "ast_hash" not in entry:
            return {"mtime": entry.get("mtime", 0), "ast_hash": entry["hash"], "semantic_hash": ""}
        if isinstance(entry, dict):
            return entry
        return None

    manifest: dict[str, dict] = {}
    for f, entry in existing.items():
        normalised = _normalise_entry(entry)
        if normalised is None:
            continue
        try:
            if not Path(f).exists():
                continue
        except OSError:
            continue
        if scan_set is not None and not _in_scan(f) and _in_root(f):
            continue
        if clear_ast_set is not None and _in_clear_ast(f):
            normalised = {**normalised, "ast_hash": "", "semantic_hash": ""}
        elif clear_set is not None and _in_clear(f):
            normalised = {**normalised, "semantic_hash": ""}
        manifest[f] = normalised

    all_files = [f for file_list in files.values() for f in file_list]
    with ThreadPoolExecutor() as pool:
        raw = pool.map(_stat_and_hash, all_files)
    hashed: dict[str, tuple[float, str]] = {
        r[0]: (r[1], r[2]) for r in raw if r is not None
    }

    for f in all_files:
        if f not in hashed:
            continue
        mtime, h = hashed[f]
        key = _nfc(f)
        prev = _normalise_entry(existing.get(key, {})) or {}
        if kind in ("ast", "both"):
            ast_h = h
        else:
            ast_h = prev.get("ast_hash", "")
        if kind in ("semantic", "both"):
            sem_h = h
        else:
            sem_h = prev.get("semantic_hash", "") if h == prev.get("ast_hash", "") else ""

        prev_seen = prev.get("seen")
        is_unchanged = (
            isinstance(prev_seen, (int, float))
            and mtime == prev.get("mtime")
            and (ast_h == prev.get("ast_hash", "") if kind in ("ast", "both") else True)
            and (sem_h == prev.get("semantic_hash", "") if kind in ("semantic", "both") else True)
            and not _in_clear_ast(f)
            and not _in_clear(f)
        )
        entry: dict = {
            "mtime": mtime,
            "seen": prev_seen if is_unchanged else time.time(),
            "ast_hash": ast_h,
            "semantic_hash": sem_h,
        }
        manifest[key] = entry
    if root is not None:
        manifest = {_nfc(_to_relative_for_storage(k, root)): v for k, v in manifest.items()}
    else:
        manifest = {_nfc(k): v for k, v in manifest.items()}

    manifest_p = Path(manifest_path)
    if manifest_p.is_file():
        try:
            disk_raw = json.loads(manifest_p.read_text(encoding="utf-8"))
            if isinstance(disk_raw, dict) and disk_raw == manifest:
                return
        except Exception:
            pass

    write_json_atomic(manifest_path, manifest, indent=2)


def _mtime_may_hide_a_rewrite(current_mtime: float, stored: dict) -> bool:
    """Was this manifest row written in the same tick as the file it describes?"""
    seen = stored.get("seen")
    if not isinstance(seen, (int, float)):
        return False
    delta = float(seen) - float(current_mtime)
    if delta < 0:
        return False
    coarse = float(current_mtime).is_integer()
    return delta < (_MTIME_COARSE_S if coarse else _MTIME_SUBSECOND_S)


def detect_incremental(
    root: Path,
    manifest_path: str = _MANIFEST_PATH,
    *,
    detect_fn: Callable[..., dict] | None = None,
    follow_symlinks: bool | None = None,
    google_workspace: bool | None = None,
    kind: str = "semantic",
    extra_excludes: list[str] | None = None,
    gitignore: bool = True,
) -> dict:
    """Like detect(), but returns only new or modified files since the last run."""
    if detect_fn is None:
        from graph_fy.detect import detect as _detect
        detect_fn = _detect

    full = detect_fn(
        root,
        follow_symlinks=follow_symlinks,
        google_workspace=google_workspace,
        extra_excludes=extra_excludes,
        gitignore=gitignore,
    )
    manifest = load_manifest(manifest_path, root=root)

    if not manifest:
        full["incremental"] = True
        full["new_files"] = full["files"]
        full["unchanged_files"] = {k: [] for k in full["files"]}
        full["new_total"] = full["total_files"]
        full["deleted_files"] = []
        full["excluded_files"] = []
        return full

    new_files: dict[str, list[str]] = {k: [] for k in full["files"]}
    unchanged_files: dict[str, list[str]] = {k: [] for k in full["files"]}

    for ftype, file_list in full["files"].items():
        for f in file_list:
            stored = manifest.get(_nfc(f))
            try:
                current_mtime = os.stat(_os_path(Path(f))).st_mtime
            except Exception:
                current_mtime = 0

            if isinstance(stored, (int, float)):
                changed = current_mtime != stored
            elif isinstance(stored, dict):
                if "hash" in stored and "ast_hash" not in stored:
                    stored = {"mtime": stored.get("mtime", 0), "ast_hash": stored["hash"], "semantic_hash": ""}
                hash_key = "semantic_hash" if kind == "semantic" else "ast_hash"
                stored_hash = stored.get(hash_key, "")
                if not stored_hash:
                    changed = True
                else:
                    stored_mtime = stored.get("mtime")
                    if isinstance(stored_mtime, dict):
                        stored_mtime = stored_mtime.get("mtime")
                    if not isinstance(stored_mtime, (int, float)):
                        stored_mtime = None
                    if stored_mtime is None or current_mtime != stored_mtime:
                        changed = _md5_file(Path(f)) != stored_hash
                    elif _mtime_may_hide_a_rewrite(current_mtime, stored):
                        changed = _md5_file(Path(f)) != stored_hash
                    else:
                        changed = False
            else:
                changed = True

            if changed:
                new_files[ftype].append(f)
            else:
                unchanged_files[ftype].append(f)

    current_files = {_nfc(f) for flist in full["files"].values() for f in flist}
    deleted_files: list[str] = []
    excluded_files: list[str] = []
    for f in manifest:
        if _nfc(f) in current_files:
            continue
        try:
            alive = Path(f).exists()
        except OSError:
            alive = False
        (excluded_files if alive else deleted_files).append(f)

    new_total = sum(len(v) for v in new_files.values())
    full["incremental"] = True
    full["new_files"] = new_files
    full["unchanged_files"] = unchanged_files
    full["new_total"] = new_total
    full["deleted_files"] = deleted_files
    full["excluded_files"] = excluded_files
    return full
