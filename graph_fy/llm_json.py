"""Robust JSON parsing, fragment candidate extraction, and sanitization for LLM responses.

Handles think tags, multiple markdown fences, candidate ranking, and node/edge/hyperedge
structure normalization.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Iterator

_LLM_JSON_MAX_BYTES = 10 * 1024 * 1024  # 10 MB hard cap before json.loads (F-016)

# Keys that identify an extraction fragment. Used to tell the graph object
# apart from a brace that merely appeared in the model's narration (#2882).
_FRAGMENT_KEYS = ("nodes", "edges", "hyperedges")
_FRAGMENT_KEY_TOKENS = tuple(f'"{k}"' for k in _FRAGMENT_KEYS)
_MAX_OBJECT_CANDIDATES = 64
_THINK_BLOCK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.S)


def _sanitize_fragment(parsed: dict) -> dict:
    """Force ``nodes``/``edges``/``hyperedges`` to lists of dicts, in place."""
    for key in ("nodes", "edges", "hyperedges"):
        value = parsed.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            parsed[key] = []
            continue
        parsed[key] = [entry for entry in value if isinstance(entry, dict)]

    hyperedges = parsed.get("hyperedges")
    if hyperedges:
        from graph_fy.build import _coerce_hyperedge_member_refs
        for he in hyperedges:
            if isinstance(he.get("nodes"), list):
                he["nodes"] = _coerce_hyperedge_member_refs(he, he["nodes"])
    return parsed


def _balanced_object(text: str, start: int) -> str | None:
    """Return the balanced ``{...}`` substring starting at ``start``, else None."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _json_object_candidates(text: str) -> list[int]:
    """Indices of ``{`` that plausibly start an extraction fragment."""
    preferred: list[int] = []
    rest: list[int] = []
    idx = text.find("{")
    while idx != -1:
        bucket = (
            preferred
            if any(k in text[idx:idx + 200] for k in _FRAGMENT_KEY_TOKENS)
            else rest
        )
        if len(bucket) < _MAX_OBJECT_CANDIDATES:
            bucket.append(idx)
        elif len(preferred) >= _MAX_OBJECT_CANDIDATES and len(rest) >= _MAX_OBJECT_CANDIDATES:
            break
        idx = text.find("{", idx + 1)
    return preferred + rest


def _json_fragment_candidates(text: str) -> Iterator[str]:
    """Yield candidate JSON texts from a model reply, most-likely first."""
    for _lang, body in sorted(
        _FENCE_RE.findall(text), key=lambda b: b[0].strip().lower() not in ("json", "")
    ):
        yield body.strip()
    for start in _json_object_candidates(text):
        blob = _balanced_object(text, start)
        if blob is not None:
            yield blob


def _parse_llm_json(raw: str) -> dict:
    """Strip optional markdown fences and parse JSON. Returns empty fragment on failure."""
    if len(raw) > _LLM_JSON_MAX_BYTES:
        print(
            f"[graph_fy] LLM response exceeds {_LLM_JSON_MAX_BYTES} bytes "
            f"({len(raw)} bytes); refusing to parse and dropping chunk.",
            file=sys.stderr,
        )
        return {"nodes": [], "edges": [], "hyperedges": []}

    stripped = _THINK_BLOCK_RE.sub(" ", raw).strip()

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return _sanitize_fragment(parsed)
    except json.JSONDecodeError:
        pass

    empty_fragment: dict | None = None
    fallback: dict | None = None
    for candidate in _json_fragment_candidates(stripped):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if any(k in parsed for k in _FRAGMENT_KEYS):
            cand = _sanitize_fragment(parsed)
            if any(cand.get(k) for k in _FRAGMENT_KEYS):
                return cand
            if empty_fragment is None:
                empty_fragment = cand
        elif fallback is None:
            fallback = parsed

    for weaker in (empty_fragment, fallback):
        if weaker is not None:
            return _sanitize_fragment(weaker)

    print(
        f"[graph_fy] LLM returned invalid JSON, skipping chunk "
        f"(first 200 chars: {raw[:200]!r})",
        file=sys.stderr,
    )
    return {"nodes": [], "edges": [], "hyperedges": []}
