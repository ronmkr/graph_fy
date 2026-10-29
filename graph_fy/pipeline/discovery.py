"""File discovery, path anchoring, and cache initialization."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def infer_root_and_cache(
    paths: list[Path],
    cache_root: Path | None = None,
    anchor_root: Path | None = None,
) -> tuple[Path, Path]:
    """Determine the canonical scan root and the cache location for this extraction batch."""
    try:
        if not paths:
            root = Path(".")
        elif len(paths) == 1:
            root = paths[0].parent
        else:
            min_parts = min(len(p.parts) for p in paths)
            common_len = 0
            for i in range(min_parts):
                if len({p.parts[i] for p in paths}) == 1:
                    common_len += 1
                else:
                    break
            root = Path(*paths[0].parts[:common_len]) if common_len else Path(".")
    except Exception:
        root = Path(".")

    if anchor_root is not None:
        root = anchor_root
    elif cache_root is not None:
        root = cache_root
    root = root.resolve()

    cache_location = (cache_root if cache_root is not None else Path(".")).resolve()
    return root, cache_location


_SPECIAL_BUILD_FILENAMES = frozenset({
    "cmakelists.txt", "build.gradle", "settings.gradle",
    "build.gradle.kts", "settings.gradle.kts",
    "dockerfile", "build", "build.bazel", "workspace", "workspace.bazel", "module.bazel",
})


def collect_files(
    target: Path,
    *,
    follow_symlinks: bool = False,
    root: Path | None = None,
    all_formats: bool = False,
    dispatch_keys: set[str] | None = None,
    get_extractor_fn=None,
) -> list[Path]:
    """Collect extractable files under target, observing .gitignore and symlink rules."""
    containment_root = root if root is not None else target
    from graph_fy.detect import _resolves_under_root
    if target.is_file():
        return [target] if _resolves_under_root(target, containment_root) else []

    if dispatch_keys is None:
        from graph_fy.extract import _DISPATCH
        _EXTENSIONS = set(_DISPATCH.keys())
    else:
        _EXTENSIONS = dispatch_keys

    if get_extractor_fn is None:
        from graph_fy.extract import _get_extractor
        get_extractor_fn = _get_extractor

    from graph_fy.detect import _is_ignored, _is_noise_dir, _load_graph_fyignore
    ignore_root = root if root is not None else target
    patterns = _load_graph_fyignore(ignore_root)
    ignore_cache: dict[Path, bool] = {}

    def _ignored(p: Path) -> bool:
        return bool(patterns and _is_ignored(p, ignore_root, patterns, _cache=ignore_cache))

    if not follow_symlinks:
        if any(_is_noise_dir(part) for part in target.parts):
            return []
        has_negation = any(pat.startswith("!") for _, pat in patterns)
        results: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(target):
            dp = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if not _is_noise_dir(d, dp)
                and (has_negation or not _ignored(dp / d))
            ]
            for fname in filenames:
                p = dp / fname
                suffix = p.suffix
                low_fname = fname.lower()
                is_extractable = (
                    suffix in _EXTENSIONS
                    or suffix.lower() in _EXTENSIONS
                    or low_fname in _SPECIAL_BUILD_FILENAMES
                    or low_fname.startswith("dockerfile.")
                    or (all_formats and get_extractor_fn(p) is not None)
                )
                if is_extractable and not _ignored(p) and _resolves_under_root(p, containment_root):
                    results.append(p)
        return sorted(results)

    results = []
    for dirpath, dirnames, filenames in os.walk(target, followlinks=True):
        if os.path.islink(dirpath):
            real = os.path.realpath(dirpath)
            parent_real = os.path.realpath(os.path.dirname(dirpath))
            if parent_real == real or parent_real.startswith(real + os.sep):
                dirnames.clear()
                continue
        dp = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not _is_noise_dir(d, dp)
            and (not (dp / d).is_symlink() or _resolves_under_root(dp / d, containment_root))
        ]
        for fname in filenames:
            p = dp / fname
            suffix = p.suffix
            low_fname = fname.lower()
            is_extractable = (
                suffix in _EXTENSIONS
                or suffix.lower() in _EXTENSIONS
                or low_fname in _SPECIAL_BUILD_FILENAMES
                or low_fname.startswith("dockerfile.")
                or (all_formats and get_extractor_fn(p) is not None)
            )
            if is_extractable and not _ignored(p) and _resolves_under_root(p, containment_root):
                results.append(p)
    return sorted(results)
