"""Lua module require() path resolver."""
from __future__ import annotations

from pathlib import Path
from graph_fy.extractors.base import _make_id


def _resolve_lua_import_target(raw_module: str, str_path: str) -> str:
    """Resolve a Lua require() module name to a node id."""
    if not raw_module:
        return ""
    rel = raw_module.replace(".", "/")
    try:
        start_dir = Path(str_path).parent
    except Exception:
        start_dir = None
    if start_dir is not None:
        probe = start_dir
        for _ in range(6):
            for suffix in (".lua", ".luau"):
                cand = probe / f"{rel}{suffix}"
                if cand.is_file():
                    return _make_id(str(cand))
            for suffix in (".lua", ".luau"):
                cand = probe / rel / f"init{suffix}"
                if cand.is_file():
                    return _make_id(str(cand))
            if probe.parent == probe:
                break
            probe = probe.parent
    return _make_id(raw_module)
