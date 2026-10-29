"""Worker dispatch, caching, and per-file AST extraction."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from graph_fy.cache import load_cached
from graph_fy.extractors.models import _JS_CACHE_BYPASS_SUFFIXES


def run_worker_phase(
    paths: list[Path],
    root: Path,
    cache_location: Path,
    *,
    parallel: bool = True,
    max_workers: int | None = None,
    get_extractor_fn=None,
    extract_parallel_fn=None,
    extract_sequential_fn=None,
    prepare_terraform_fn=None,
) -> tuple[list[dict], list[str]]:
    """Execute Phase 2: parallel or sequential AST extraction over all files."""
    if get_extractor_fn is None:
        from graph_fy.extract import _get_extractor
        get_extractor_fn = _get_extractor

    if extract_parallel_fn is None:
        from graph_fy.extract import _extract_parallel
        extract_parallel_fn = _extract_parallel

    if extract_sequential_fn is None:
        from graph_fy.extract import _extract_sequential
        extract_sequential_fn = _extract_sequential

    if prepare_terraform_fn is None:
        from graph_fy.extractors.terraform import prepare_terraform
        prepare_terraform_fn = prepare_terraform

    from graph_fy.extract import (
        _PARALLEL_THRESHOLD,
        _DEP_MISSING_MARKER,
        _DEP_LOAD_FAILED_MARKER,
        _EXTRA_FOR_EXTENSION,
    )

    total = len(paths)
    per_file: list[dict | None] = [None] * total
    uncached_work: list[tuple[int, Path]] = []

    for i, path in enumerate(paths):
        if get_extractor_fn(path) is None:
            per_file[i] = {"nodes": [], "edges": []}
            continue
        bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES
        if not bypass_cache:
            cached = load_cached(path, root, cache_root=cache_location)
            if cached is not None:
                per_file[i] = cached
                continue
        uncached_work.append((i, path))

    if uncached_work:
        ran_parallel = False
        if parallel and len(uncached_work) >= _PARALLEL_THRESHOLD:
            ran_parallel = extract_parallel_fn(
                uncached_work, per_file, root, max_workers, total, cache_location
            )
        if not ran_parallel:
            extract_sequential_fn(
                [(i, p) for (i, p) in uncached_work if per_file[i] is None],
                per_file, root, total, cache_location,
            )

    for i in range(total):
        if per_file[i] is None:
            per_file[i] = {
                "nodes": [], "edges": [],
                "error": "internal: no extraction result produced",
            }

    _empty_sources: list[str] = []
    for i, _p in enumerate(paths):
        _res = per_file[i] or {}
        if _res.get("nodes") or _res.get("error") or _res.get("skipped"):
            continue
        if get_extractor_fn(_p) is not None:
            _empty_sources.append(str(_p))
    if _empty_sources:
        _shown = ", ".join(Path(x).name for x in _empty_sources[:5])
        _more = f" (+{len(_empty_sources) - 5} more)" if len(_empty_sources) > 5 else ""
        print(
            f"  warning: {len(_empty_sources)} source file(s) produced zero nodes and "
            f"are absent from the graph: {_shown}{_more}. A re-run will retry them "
            f"(empties are no longer cached); if it persists, please report the "
            f"file(s) (#1666).",
            file=sys.stderr, flush=True,
        )

    _failed_sources: list[str] = []
    _failed_seen: set[str] = set()
    for i, _p in enumerate(paths):
        _res = per_file[i] or {}
        _key = str(_p)
        if _res.get("error"):
            if _key not in _failed_seen:
                _failed_sources.append(_key)
                _failed_seen.add(_key)
            continue
        if _res.get("skipped"):
            continue
        if (not _res.get("nodes")) and get_extractor_fn(_p) is not None:
            if _key not in _failed_seen:
                _failed_sources.append(_key)
                _failed_seen.add(_key)

    from graph_fy.detect import CODE_EXTENSIONS as _CODE_EXTS
    _no_extractor: dict[str, int] = {}
    for _p in paths:
        _ext = _p.suffix.lower()
        if _ext in _CODE_EXTS and get_extractor_fn(_p) is None:
            _no_extractor[_ext] = _no_extractor.get(_ext, 0) + 1
    if _no_extractor:
        _by_count = ", ".join(
            f"{ext} ({n})" for ext, n in sorted(_no_extractor.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        _tot = sum(_no_extractor.values())
        print(
            f"  warning: {_tot} file(s) are classified as code but graph_fy has no AST "
            f"extractor for their language, so they contributed nothing to the graph: "
            f"{_by_count}. Please open an issue to request support for these (#1689).",
            file=sys.stderr, flush=True,
        )

    _missing_dep_count: dict[str, int] = {}
    _missing_dep_error: dict[str, str] = {}
    for i, _p in enumerate(paths):
        _err = (per_file[i] or {}).get("error") or ""
        if _DEP_MISSING_MARKER in _err or _DEP_LOAD_FAILED_MARKER in _err:
            _ext = _p.suffix.lower()
            _missing_dep_count[_ext] = _missing_dep_count.get(_ext, 0) + 1
            _missing_dep_error.setdefault(_ext, _err)
    for _ext, _n in sorted(_missing_dep_count.items(), key=lambda kv: (-kv[1], kv[0])):
        _extra = _EXTRA_FOR_EXTENSION.get(_ext)
        _err_text = _missing_dep_error[_ext]
        if _extra and _DEP_MISSING_MARKER in _err_text:
            _reason = _err_text.split(". ")[0]
            _hint = f' Install it with: pip install "graph_fyy[{_extra}]"'
            _cause = "a dependency is missing"
        else:
            _reason = _err_text
            _hint = ""
            _cause = ("a dependency is missing" if _DEP_MISSING_MARKER in _err_text
                      else "a dependency failed to load")
        print(
            f"  warning: {_n} {_ext} file(s) contributed nothing to the graph "
            f"because {_cause}: {_reason}.{_hint} (#1745)",
            file=sys.stderr, flush=True,
        )

    _syntax_error_files: list[tuple[str, int | None, int]] = []
    for i, _p in enumerate(paths):
        _res = per_file[i] or {}
        _pe = _res.get("parse_errors")
        if not _pe:
            continue
        if len(_res.get("nodes", [])) <= 1 or _pe.get("multiline_error"):
            _rel = os.path.relpath(str(_p), str(root)).replace("\\", "/")
            _kept = max(len(_res.get("nodes", [])) - 1, 0)
            _syntax_error_files.append((_rel, _pe.get("first_error_line"), _kept))
    if _syntax_error_files:
        def _describe_syntax_error(rel: str, line: "int | None", kept: int) -> str:
            _where = f"first error at line {line}" if line else "syntax error"
            _got = "no symbols extracted" if kept == 0 else f"{kept} symbol(s) extracted"
            return f"{rel} ({_where}, {_got})"

        _shown = ", ".join(
            _describe_syntax_error(*f) for f in _syntax_error_files[:5]
        )
        _more = (
            f" (+{len(_syntax_error_files) - 5} more)"
            if len(_syntax_error_files) > 5 else ""
        )
        print(
            f"  warning: {len(_syntax_error_files)} file(s) had syntax errors and "
            f"may be partially extracted: {_shown}{_more}",
            file=sys.stderr, flush=True,
        )

    for path, result in zip(paths, per_file):
        if path.suffix in (".tf", ".tfvars", ".hcl"):
            prepare_terraform_fn(result, path, root)

    return per_file, _failed_sources
