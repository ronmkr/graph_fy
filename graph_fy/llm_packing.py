from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

from graph_fy.file_slice import (
    FileSlice,
    bisect_slice,
    expand_oversized_files,
    read_slice_text,
    unit_path,
)
from graph_fy.llm_vision import (
    _IMAGE_TOKEN_ESTIMATE,
    _MAX_IMAGES_PER_CHUNK,
    _is_vision_image,
)

# `_read_files` truncates each file at this many characters before joining into
# the user message. Token estimates use the same cap so packing matches reality.
_FILE_CHAR_CAP = 20_000
# `_read_files` wraps each file in an `<untrusted_source path=... sha256=...>`
# delimiter block (see issue #1210); this is roughly the per-file overhead in
# characters that wrapper adds (open tag + 64-char sha + close tag + newlines).
_PER_FILE_OVERHEAD_CHARS = 160
# Coarse fallback used only when `tiktoken` is not installed. 1 token ≈ 4 chars
# is the standard heuristic for English/code on BPE tokenizers.
_CHARS_PER_TOKEN = 4


def _get_tokenizer():
    """Return a tiktoken encoder for accurate token counts, or None if tiktoken
    is not installed. We use `cl100k_base` (GPT-4 / GPT-3.5-turbo) as a proxy:
    Kimi-K2 ships a tiktoken-based tokenizer with very similar BPE behaviour,
    and Claude's tokenizer has a comparable token-to-char ratio for prose/code.
    Estimates only need to be within ~5%, not exact.
    """
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # network failure on first-use download, etc.
        return None


# Cached at import time. None if tiktoken is unavailable; consumers must handle.
_TOKENIZER = _get_tokenizer()


def _file_to_text(path: Path) -> str:
    """Return a text-like file's content for the extraction prompt.

    Most files are read directly. PDFs are binary, so reading them with
    `read_text` yields garbage (the same failure images had); route them through
    pypdf instead. A scanned PDF with no text layer extracts to an empty string,
    which still produces a reference node rather than noise.
    """
    if path.suffix.lower() == ".pdf":
        from graph_fy.detect import extract_pdf_text
        return extract_pdf_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def _resolve_under_root(path: Path, root: Path) -> Path | None:
    """Return the resolved path only when it stays inside ``root``."""
    try:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_path


# Delimiters and role tokens neutralized to prevent prompt injection.
_INJECTION_SENTINELS = re.compile(
    r"</?untrusted_source\b[^>]*>"
    r"|<\|[A-Za-z0-9_.\-]{1,64}\|>"
    r"|<<SYS>>|<</SYS>>"
    r"|\[/?(?:INST|SYSTEM)\]"
    r"|^\s*###?\s*(?:system|instruction)s?\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _neutralise_injection_sentinels(text: str) -> str:
    """Defang jailbreak control tokens by inserting zero-width spaces."""
    return _INJECTION_SENTINELS.sub(lambda m: m.group(0)[0] + "​" + m.group(0)[1:], text)


def _wrap_untrusted(rel: str, content: str) -> str:
    """Wrap file content in hash-stamped untrusted_source delimiter."""
    sha = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    safe = _neutralise_injection_sentinels(content)
    return (
        f'<untrusted_source path="{rel}" sha256="{sha}">\n'
        f"{safe}\n"
        f"</untrusted_source>"
    )


def _read_files(units: "list[Path | FileSlice]", root: Path) -> str:
    """Format file/slice contents into wrapped untrusted_source prompt blocks."""
    parts: list[str] = []
    for u in units:
        p = unit_path(u)
        safe_path = _resolve_under_root(p, root)
        if safe_path is None:
            print(f"[graph_fy] skipping {p}: symlink target outside corpus root", file=sys.stderr)
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = Path(p).as_posix()
        try:
            if isinstance(u, FileSlice):
                content = read_slice_text(u)
            else:
                content = _file_to_text(safe_path)
        except OSError:
            continue
        parts.append(_wrap_untrusted(rel, content[:_FILE_CHAR_CAP]))
    return "\n\n".join(parts)


_LABEL_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VERIFICATION_FIELD = "verification"
_UNVERIFIED_VALUE = "unverified"


def _label_identifiers(label: str) -> list[str]:
    """Identifier tokens from a node label, stripped of a trailing call/args
    parenthesis (``foo()`` -> ``foo``, ``Cls.method(x)`` -> ``Cls``/``method``)."""
    if not label:
        return []
    base = label.split("(", 1)[0]
    return [t for t in _LABEL_IDENT_RE.findall(base) if len(t) >= 3]


def _dispatched_source_text(units: "list[Path | FileSlice]", root: Path) -> dict[Path, str]:
    """Map each dispatched text unit's resolved path to the (lower-cased, capped)
    source bytes the model actually saw via :func:`_read_files`."""
    by_path: dict[Path, str] = {}
    for u in units:
        p = unit_path(u)
        safe = _resolve_under_root(p, root)
        if safe is None:
            continue
        try:
            content = read_slice_text(u) if isinstance(u, FileSlice) else _file_to_text(safe)
        except Exception:  # noqa: BLE001
            continue
        by_path[safe] = by_path.get(safe, "") + content[:_FILE_CHAR_CAP].lower()
    return by_path


def _bind_node_evidence(result: dict, text_units: "list[Path | FileSlice]", root: Path) -> int:
    """Downgrade code-typed nodes whose symbol name has no evidence in the source
    the model read, returning the number downgraded."""
    nodes = result.get("nodes")
    if not nodes:
        return 0
    if not any(isinstance(n, dict) and n.get("file_type") == "code" and n.get("source_file")
               for n in nodes):
        return 0
    source_by_path = _dispatched_source_text(text_units, root)
    if not source_by_path:
        return 0
    downgraded = 0
    for n in nodes:
        if not isinstance(n, dict) or n.get("file_type") != "code":
            continue
        sf = n.get("source_file")
        if not sf:
            continue
        p = Path(sf)
        if not p.is_absolute():
            p = root / p
        try:
            key = p.resolve()
        except (OSError, RuntimeError):
            continue
        src = source_by_path.get(key)
        if src is None:
            continue
        idents = _label_identifiers(str(n.get("label", ""))) + _label_identifiers(str(n.get("id", "")))
        if not idents:
            continue
        if any(ident.lower() in src for ident in idents):
            continue
        if n.get("confidence") in (None, "", "EXTRACTED") and not n.get(_VERIFICATION_FIELD):
            n[_VERIFICATION_FIELD] = _UNVERIFIED_VALUE
            downgraded += 1
    return downgraded


_PDF_ESTIMATE_CACHE: "dict[tuple, str]" = {}
_PDF_ESTIMATE_CACHE_MAX = 512


def _pdf_text_for_estimate(path: Path) -> str:
    """Extracted text of a PDF, memoised for the packing pass."""
    try:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return ""
    hit = _PDF_ESTIMATE_CACHE.get(key)
    if hit is not None:
        return hit
    text = _file_to_text(path)
    if len(_PDF_ESTIMATE_CACHE) >= _PDF_ESTIMATE_CACHE_MAX:
        _PDF_ESTIMATE_CACHE.clear()
    _PDF_ESTIMATE_CACHE[key] = text
    return text


def _estimate_file_tokens(unit: "Path | FileSlice") -> int:
    """Estimate the prompt-token cost of a file or slice under `_read_files` rules."""
    tokenizer = getattr(sys.modules.get("graph_fy.llm"), "_TOKENIZER", _TOKENIZER)
    if isinstance(unit, FileSlice):
        if tokenizer is None:
            return (min(unit.end - unit.start, _FILE_CHAR_CAP) + _PER_FILE_OVERHEAD_CHARS) // _CHARS_PER_TOKEN
        try:
            content = read_slice_text(unit)[:_FILE_CHAR_CAP]
        except OSError:
            return 0
        return len(tokenizer.encode(content, disallowed_special=())) + (_PER_FILE_OVERHEAD_CHARS // _CHARS_PER_TOKEN)

    path = unit
    if _is_vision_image(path):
        return _IMAGE_TOKEN_ESTIMATE

    if path.suffix.lower() == ".pdf":
        try:
            content = _pdf_text_for_estimate(path)[:_FILE_CHAR_CAP]
        except Exception:
            return 0
    elif tokenizer is None:
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        chars = min(size, _FILE_CHAR_CAP) + _PER_FILE_OVERHEAD_CHARS
        return chars // _CHARS_PER_TOKEN
    else:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:_FILE_CHAR_CAP]
        except OSError:
            return 0

    if tokenizer is None:
        return (len(content) + _PER_FILE_OVERHEAD_CHARS) // _CHARS_PER_TOKEN
    return len(tokenizer.encode(content, disallowed_special=())) + (_PER_FILE_OVERHEAD_CHARS // _CHARS_PER_TOKEN)


def _pack_chunks_by_tokens(
    files: "list[Path | FileSlice]",
    token_budget: int,
) -> "list[list[Path | FileSlice]]":
    """Greedily pack files/slices into chunks that fit a token budget."""
    if token_budget <= 0:
        raise ValueError(f"token_budget must be positive, got {token_budget}")

    by_dir: dict[Path, "list[Path | FileSlice]"] = {}
    for f in files:
        by_dir.setdefault(unit_path(f).parent, []).append(f)

    chunks: "list[list[Path | FileSlice]]" = []
    current: "list[Path | FileSlice]" = []
    current_tokens = 0
    current_images = 0

    for directory in sorted(by_dir):
        for unit in by_dir[directory]:
            cost = _estimate_file_tokens(unit)
            is_image = not isinstance(unit, FileSlice) and _is_vision_image(unit)
            over_budget = current_tokens + cost > token_budget
            over_images = is_image and current_images >= _MAX_IMAGES_PER_CHUNK
            if current and (over_budget or over_images):
                chunks.append(current)
                current = []
                current_tokens = 0
                current_images = 0
            current.append(unit)
            current_tokens += cost
            current_images += is_image

    if current:
        chunks.append(current)
    return chunks


_CONTEXT_EXCEEDED_MARKERS = (
    "context size",
    "context length",
    "context_length",
    "context window",
    "n_keep",
    "exceeds the available",
    "n_ctx",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "context_length_exceeded",
)


def _looks_like_context_exceeded(exc: BaseException) -> bool:
    """Heuristically classify an exception as a context-window overflow."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _CONTEXT_EXCEEDED_MARKERS)


def _looks_like_timeout(exc: BaseException) -> bool:
    """Classify an exception as a recognized subprocess or SDK timeout."""
    types: list[type[BaseException]] = [subprocess.TimeoutExpired]
    try:
        import openai
        types.append(openai.APITimeoutError)
    except ImportError:
        pass
    try:
        import anthropic
        types.append(anthropic.APITimeoutError)
    except ImportError:
        pass
    try:
        import botocore.exceptions
        types.extend([botocore.exceptions.ReadTimeoutError, botocore.exceptions.ConnectTimeoutError])
    except ImportError:
        pass
    return isinstance(exc, tuple(types))


def _mark_partial(result: dict) -> None:
    """Tag every node/edge/hyperedge in a truncated chunk result with an internal ``_partial`` marker."""
    for bucket in ("nodes", "edges", "hyperedges"):
        for item in result.get(bucket, []):
            if isinstance(item, dict):
                item["_partial"] = True


def _chunk_partial_files(chunk) -> list[str]:
    """Source paths covered by a chunk, for marking a chunk that truncated to an empty parse."""
    return sorted({str(unit_path(u)) for u in chunk})


def _merged_partial_files(*results: dict) -> list[str]:
    """Union of the ``_partial_files`` carried by each result (survives merges)."""
    out: set[str] = set()
    for r in results:
        out.update(r.get("_partial_files", []) or [])
    return sorted(out)


def _partial_source_files(result: dict) -> list[str]:
    """Source files known partial: those carrying a ``_partial`` item marker, plus any in ``_partial_files``."""
    seen: set[str] = set(result.get("_partial_files", []) or [])
    for bucket in ("nodes", "edges", "hyperedges"):
        for item in result.get(bucket, []):
            if isinstance(item, dict) and item.get("_partial"):
                sf = item.get("source_file")
                if sf:
                    seen.add(str(sf))
    return sorted(seen)


def _strip_partial_markers(result: dict) -> None:
    """Remove the internal ``_partial`` marker from every item in ``result``."""
    for bucket in ("nodes", "edges", "hyperedges"):
        for item in result.get(bucket, []):
            if isinstance(item, dict):
                item.pop("_partial", None)
