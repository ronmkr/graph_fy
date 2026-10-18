"""Pascal unit and class resolver."""
from __future__ import annotations

from pathlib import Path
from graph_fy.extractors.base import _file_stem, _make_id


_pascal_unit_cache: dict[str, dict[str, str]] = {}
_pascal_class_stem_cache: dict[str, dict[str, str]] = {}


def _pascal_project_root(from_path: Path) -> Path:
    """Return the highest ancestor directory that looks like a Pascal project root."""
    best = from_path.parent
    current = from_path.parent
    for _ in range(12):
        if len(current.parts) <= 1:
            break
        pas_count = sum(1 for _ in current.glob("*.pas"))
        dpr_count = sum(1 for _ in current.glob("*.dpr"))
        if pas_count >= 2 or dpr_count >= 1:
            best = current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return best


def _pascal_resolve_unit(from_path: Path, unit_name: str) -> str:
    """Resolve a Pascal unit name to the graph_fy node ID of its source file."""
    root = _pascal_project_root(from_path)
    root_key = str(root)
    if root_key not in _pascal_unit_cache:
        unit_map: dict[str, str] = {}
        for ext in (".pas", ".pp", ".dpr", ".dpk", ".inc"):
            for f in root.rglob("*" + ext):
                unit_map[f.stem.lower()] = _make_id(str(f))
        _pascal_unit_cache[root_key] = unit_map
    return _pascal_unit_cache[root_key].get(unit_name.lower(), _make_id(unit_name))


def _pascal_resolve_class(from_path: Path, class_name: str) -> str | None:
    """Resolve a Pascal class/interface name to the node ID of its defining file's class node."""
    prefix = class_name[:1]
    unit_name = class_name[1:] if prefix in ("T", "I") else class_name

    root = _pascal_project_root(from_path)
    root_key = str(root)
    if root_key not in _pascal_class_stem_cache:
        stem_map: dict[str, str] = {}
        for ext in (".pas", ".pp", ".dpr", ".dpk"):
            for f in root.rglob("*" + ext):
                stem_map[f.stem.lower()] = _file_stem(f)
        _pascal_class_stem_cache[root_key] = stem_map

    file_stem = _pascal_class_stem_cache[root_key].get(unit_name.lower())
    if file_stem:
        return _make_id(file_stem, class_name)
    return None
