"""Web and framework extractor routines (Svelte, Astro, Vue, and JS dynamic import rescue)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _file_stem, _make_id
from graph_fy.extractors.resolution import (
    _load_tsconfig_aliases,
    _load_tsconfig_base_url,
    _resolve_js_module_path,
    _resolve_tsconfig_alias,
    _vue_mask_non_script,
)
from graph_fy.extractors.ts_normalize import _normalize_ts_import_types


def _get_engine_and_configs():
    import graph_fy.extract as ext
    from graph_fy.extractors.engine import _extract_generic
    return _extract_generic, ext._JS_CONFIG, ext._TS_CONFIG, ext._TSX_CONFIG


def _rescue_js_dynamic_imports(path: Path, result: dict) -> None:
    """Recover ``import('…')`` edges the AST pass does not emit for plain JS/TS.

    tree-sitter models ``await import('x')`` as a ``call_expression``, not an
    ``import_statement``, so the specifier only reaches the graph when
    ``walk_calls`` visits that call — which it never does at module scope
    (only function bodies are walked for calls). The Svelte/Astro/Vue
    extractors already patch the same gap by regex because their AST pass
    fails wholesale; plain ``.ts``/``.js`` was left out on the reasoning that
    its AST pass "works". It works for STATIC imports; dynamic ones outside a
    walked body fell through silently (#2575), and because they cluster under
    hub modules the loss compounds with ``affected`` traversal depth.

    Dedupe: a dynamic import the AST pass DID capture is already in the graph
    as an ``imports_from`` edge marked ``deferred`` (``_dynamic_import_js``).
    Re-emitting it here as a second ``dynamic_import`` edge would state the
    same fact twice, so a match whose resolved target already has a deferred
    edge FROM THIS FILE'S NODE is skipped. The source check matters: the AST
    pass anchors the edge on the enclosing function when the ``import()`` is
    written inside one, and that is a different fact from "this file depends on
    that module" — the only one file-level traversal can use (#2584).

    Regex false positives in comments/strings are the precedented trade of
    the Svelte/Vue rescues; a ``//``-prefix guard covers the common case.
    """
    try:
        import re as _re
        src = path.read_text(encoding="utf-8", errors="replace")
        if not _re.search(r"(?<!\w)import\s*\(", src):  # cheap bail — most files have none
            return
        existing_ids = {n["id"] for n in result.get("nodes", [])}
        file_node_id = _make_id(str(path))
        aliases = _load_tsconfig_aliases(path.parent)
        base_url = _load_tsconfig_base_url(path.parent)
        deferred_ids: set[str] = set()
        deferred_files: set[str] = set()
        rescued_targets: set[str] = set()
        for e in result.get("edges", []):
            if (e.get("deferred") and e.get("relation") == "imports_from"
                    and e.get("source") == file_node_id):
                deferred_ids.add(e.get("target"))
                tf = e.get("target_file")
                if tf:
                    try:
                        deferred_files.add(str(Path(tf).resolve()))
                    except OSError:
                        deferred_files.add(str(tf))
        for m in _re.finditer(
            r"""(?<!\w)import\s*\(\s*(?:'([^'\n]+)'|"([^"\n]+)"|`([^`$\n]+)`)\s*\)""",
            src,
        ):
            raw = m.group(1) or m.group(2) or m.group(3)
            if not raw:
                continue
            line_start = src.rfind("\n", 0, m.start()) + 1
            if "//" in src[line_start:m.start()]:
                continue  # line-commented-out import
            resolution = _resolve_rescued_specifier(path, raw, aliases, base_url)
            if resolution is None:
                continue
            node_id, _stub_sf, resolved_file = resolution
            if node_id in deferred_ids or _make_id("ref", raw) in deferred_ids:
                continue
            if resolved_file is not None:
                try:
                    if str(resolved_file.resolve()) in deferred_files:
                        continue
                except OSError:
                    pass
            emit_key = str(resolved_file.resolve()) if resolved_file is not None else raw
            if emit_key in rescued_targets:
                continue
            rescued_targets.add(emit_key)
            _emit_rescued_import(
                result, existing_ids, file_node_id, path, raw,
                "dynamic_import", aliases, base_url,
            )
    except Exception:
        pass


_JS_RATIONALE_PREFIXES = (
    "// NOTE:", "// IMPORTANT:", "// HACK:", "// WHY:", "// RATIONALE:",
    "// TODO:", "// FIXME:",
    "* NOTE:", "* IMPORTANT:", "* HACK:", "* WHY:", "* RATIONALE:",
    "* TODO:", "* FIXME:",
)

_JS_DOC_REF_RE = re.compile(r"\b(ADR[- ]?\d{1,5}|RFC[- ]?\d{1,5})\b", re.IGNORECASE)
_JS_COMMENT_LINE_RE = re.compile(r"^\s*(//|/\*|\*)")


def _extract_js_rationale(path: Path, result: dict) -> None:
    """Post-pass: extract rationale comments and doc references from JS/TS source.
    Mutates result in-place by appending to result['nodes'] and result['edges'].
    """
    try:
        source_text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    import graph_fy.extract as ext
    _shorten_label = getattr(ext, "_shorten_rationale_label", lambda t, **kw: t[:80])

    stem = _file_stem(path)
    str_path = str(path)
    nodes = result["nodes"]
    edges = result["edges"]
    seen_ids = {n["id"] for n in nodes}
    file_nid = _make_id(str(path))
    seen_doc_refs: set[str] = set()

    def _add_rationale(text: str, line: int) -> None:
        label = _shorten_label(text)
        rid = _make_id(stem, "rationale", str(line))
        if rid not in seen_ids:
            seen_ids.add(rid)
            nodes.append({
                "id": rid,
                "label": label,
                "file_type": "rationale",
                "source_file": str_path,
                "source_location": f"L{line}",
            })
        edges.append({
            "source": rid,
            "target": file_nid,
            "relation": "rationale_for",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    def _add_doc_ref(token: str, line: int) -> None:
        kind, num = re.match(r"([A-Za-z]+)[- ]?(\d+)", token).groups()
        kind = kind.upper()
        label = f"{kind}-{num.zfill(4)}" if kind == "ADR" else f"{kind}-{num}"
        if label in seen_doc_refs:
            return
        seen_doc_refs.add(label)
        rid = _make_id("docref", label)
        if rid not in seen_ids:
            seen_ids.add(rid)
            nodes.append({
                "id": rid,
                "label": label,
                "file_type": "doc_ref",
                "source_file": str_path,
                "source_location": f"L{line}",
            })
        edges.append({
            "source": file_nid,
            "target": rid,
            "relation": "cites",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    for lineno, line_text in enumerate(source_text.splitlines(), start=1):
        stripped = line_text.strip()
        if any(stripped.startswith(p) for p in _JS_RATIONALE_PREFIXES):
            _add_rationale(stripped.lstrip("/* "), lineno)
        if _JS_COMMENT_LINE_RE.match(line_text):
            for m in _JS_DOC_REF_RE.finditer(stripped):
                _add_doc_ref(m.group(1), lineno)


def _resolve_rescued_specifier(
    path: Path,
    raw: str,
    aliases,
    base_url,
) -> tuple[str, str, Path | None] | None:
    """Resolve a regex-rescued import specifier the way ``_import_js`` does."""
    if raw.startswith("."):
        resolved = _resolve_js_module_path(
            Path(os.path.normpath(path.parent / raw))
        )
        resolved_file = resolved if resolved is not None and resolved.is_file() else None
        return _make_id(str(resolved)), str(resolved), resolved_file
    resolved_alias = _resolve_tsconfig_alias(raw, aliases, base_url=base_url)
    if resolved_alias is not None:
        resolved_alias = _resolve_js_module_path(resolved_alias)
        resolved_file = (resolved_alias if resolved_alias is not None
                         and resolved_alias.is_file() else None)
        return _make_id(str(resolved_alias)), str(resolved_alias), resolved_file
    if raw.startswith("@/"):
        resolved_unmapped = _resolve_js_module_path(raw, path.parent)
        if resolved_unmapped is not None and resolved_unmapped.is_file():
            return _make_id(str(resolved_unmapped)), str(resolved_unmapped), resolved_unmapped
    module_name = raw.split("/")[-1]
    if not module_name:
        return None
    return _make_id(module_name), raw, None


def _emit_rescued_import(
    result: dict,
    existing_ids: set,
    file_node_id: str,
    path: Path,
    raw: str,
    relation: str,
    aliases,
    base_url,
) -> None:
    """Shared edge/stub emit for the Svelte/Astro/Vue regex-rescue import passes."""
    resolution = _resolve_rescued_specifier(path, raw, aliases, base_url)
    if resolution is None:
        return
    node_id, stub_source_file, resolved_file = resolution
    edge = {
        "source": file_node_id, "target": node_id,
        "relation": relation, "confidence": "EXTRACTED",
        "source_file": str(path),
    }
    if resolved_file is not None:
        edge["target_file"] = str(resolved_file)
        result.setdefault("edges", []).append(edge)
        return
    if node_id in existing_ids:
        result.setdefault("edges", []).append(edge)
        return
    result.setdefault("nodes", []).append({
        "id": node_id, "label": raw,
        "file_type": "code", "source_file": stub_source_file,
        "confidence": "EXTRACTED",
    })
    result.setdefault("edges", []).append(edge)
    existing_ids.add(node_id)


def extract_svelte(path: Path) -> dict:
    """Extract imports from .svelte files: script-block via JS AST + template regex fallback."""
    _extract_generic, js_config, _, _ = _get_engine_and_configs()
    result = _extract_generic(path, js_config)
    try:
        import re as _re
        src = path.read_text(encoding="utf-8", errors="replace")
        existing_ids = {n["id"] for n in result.get("nodes", [])}
        file_node_id = _make_id(str(path))
        aliases = _load_tsconfig_aliases(path.parent)
        base_url = _load_tsconfig_base_url(path.parent)
        for m in _re.finditer(r"""import\(\s*['"]([^'"]+)['"]\s*\)""", src):
            raw = m.group(1)
            if not raw:
                continue
            _emit_rescued_import(
                result, existing_ids, file_node_id, path, raw,
                "dynamic_import", aliases, base_url,
            )
        script_re = _re.compile(
            r"<script\b[^>]*>([\s\S]*?)</script\s*>", _re.IGNORECASE
        )
        static_import_re = _re.compile(
            r"""import\s+(?:[^'"`;]+?\s+from\s+)?['"]([^'"]+)['"]"""
        )
        for script_match in script_re.finditer(src):
            script_body = script_match.group(1)
            for m in static_import_re.finditer(script_body):
                raw = m.group(1)
                if not raw:
                    continue
                _emit_rescued_import(
                    result, existing_ids, file_node_id, path, raw,
                    "imports_from", aliases, base_url,
                )
    except Exception:
        pass
    return result


def extract_astro(path: Path) -> dict:
    """Extract imports from .astro files: frontmatter (TS) + template regex fallback."""
    _extract_generic, js_config, _, _ = _get_engine_and_configs()
    result = _extract_generic(path, js_config)
    try:
        import re as _re
        src = path.read_text(encoding="utf-8", errors="replace")
        existing_ids = {n["id"] for n in result.get("nodes", [])}
        file_node_id = _make_id(str(path))
        aliases = _load_tsconfig_aliases(path.parent)
        base_url = _load_tsconfig_base_url(path.parent)
        for m in _re.finditer(r"""import\(\s*['"]([^'"]+)['"]\s*\)""", src):
            raw = m.group(1)
            if not raw:
                continue
            _emit_rescued_import(
                result, existing_ids, file_node_id, path, raw,
                "dynamic_import", aliases, base_url,
            )
        frontmatter_re = _re.compile(
            r"\A\s*---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|\Z)"
        )
        script_re = _re.compile(
            r"<script\b[^>]*>([\s\S]*?)</script\s*>", _re.IGNORECASE
        )
        static_import_re = _re.compile(
            r"""import\s+(?:[^'"`;]+?\s+from\s+)?['"]([^'"]+)['"]"""
        )
        regions: list[str] = []
        fm = frontmatter_re.search(src)
        if fm:
            regions.append(fm.group(1))
        for script_match in script_re.finditer(src):
            regions.append(script_match.group(1))
        for region in regions:
            for m in static_import_re.finditer(region):
                raw = m.group(1)
                if not raw:
                    continue
                _emit_rescued_import(
                    result, existing_ids, file_node_id, path, raw,
                    "imports_from", aliases, base_url,
                )
    except Exception:
        pass
    return result


def extract_vue(path: Path) -> dict:
    """Extract imports, symbols, and type refs from a ``.vue`` SFC."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": []}

    _extract_generic, js_config, ts_config, tsx_config = _get_engine_and_configs()

    masked, lang = _vue_mask_non_script(src)
    if lang == "tsx":
        config = tsx_config
    elif lang in ("js", "jsx"):
        config = js_config
    else:
        config = ts_config
    masked_bytes = masked.encode("utf-8")
    if config in (ts_config, tsx_config):
        masked_bytes = _normalize_ts_import_types(
            masked_bytes, tsx=config is tsx_config
        ) or masked_bytes

    result = _extract_generic(path, config, source_override=masked_bytes)

    try:
        existing_ids = {n["id"] for n in result.get("nodes", [])}
        file_node_id = _make_id(str(path))
        aliases = _load_tsconfig_aliases(path.parent)
        base_url = _load_tsconfig_base_url(path.parent)
        for m in re.finditer(r"""import\(\s*['"]([^'"]+)['"]\s*\)""", src):
            raw = m.group(1)
            if not raw:
                continue
            _emit_rescued_import(
                result, existing_ids, file_node_id, path, raw,
                "dynamic_import", aliases, base_url,
            )
    except Exception:
        pass
    return result
