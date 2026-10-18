"""Direct LLM extraction coordinator and provider dispatch."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from graph_fy.file_slice import (
    FileSlice,
    bisect_slice,
    expand_oversized_files,
    read_slice_text,
    unit_path,
)

# ── Re-exports: Configuration & Environment ──────────────────────────────────
from graph_fy.llm_config import (
    BACKENDS,
    _BACKEND_DETECTION_EXTRA_ENV,
    _FIXED_TEMPERATURE_MODEL_MARKERS,
    _backend_env_keys,
    _backend_pkg_hint,
    _bedrock_inference_config,
    _custom_providers_path,
    _default_model_for_backend,
    _format_backend_env_keys,
    _get_backend_api_key,
    _load_custom_providers,
    _model_requires_default_temperature,
    _no_window_kwargs,
    _resolve_api_timeout,
    _resolve_max_retries,
    _resolve_max_retry_depth,
    _resolve_max_tokens,
    _resolve_temperature,
    _thinking_disabled_via_env,
    backend_detection_env_vars,
    detect_backend,
    estimate_cost,
    provider_base_url_ok,
)

# ── Re-exports: Shared Prompts & Response Validation ─────────────────────────
from graph_fy.llm_common import (
    _DEEP_EXTRACTION_SUFFIX,
    _EXTRACTION_SYSTEM,
    _HOLLOW_BACKOFF_S,
    _extraction_system,
    _mark_hollow,
    _response_is_hollow,
)

# ── Re-exports: Token Estimation, Document Slicing & Packing ─────────────────
from graph_fy.llm_packing import (
    _CHARS_PER_TOKEN,
    _CONTEXT_EXCEEDED_MARKERS,
    _FILE_CHAR_CAP,
    _INJECTION_SENTINELS,
    _LABEL_IDENT_RE,
    _PDF_ESTIMATE_CACHE,
    _PDF_ESTIMATE_CACHE_MAX,
    _PER_FILE_OVERHEAD_CHARS,
    _TOKENIZER,
    _UNVERIFIED_VALUE,
    _VERIFICATION_FIELD,
    _bind_node_evidence,
    _chunk_partial_files,
    _dispatched_source_text,
    _estimate_file_tokens,
    _file_to_text,
    _get_tokenizer,
    _label_identifiers,
    _looks_like_context_exceeded,
    _looks_like_timeout,
    _mark_partial,
    _merged_partial_files,
    _neutralise_injection_sentinels,
    _pack_chunks_by_tokens,
    _partial_source_files,
    _pdf_text_for_estimate,
    _read_files,
    _resolve_under_root,
    _strip_partial_markers,
    _wrap_untrusted,
)

# ── Re-exports: Provider Implementations ──────────────────────────────────────
from graph_fy.llm_providers import (
    _EXTRACTION_JSON_SCHEMA,
    _JSON_SCHEMA_SUPPORT,
    _anthropic_response_text,
    _azure_client,
    _bedrock_response_text,
    _call_azure,
    _call_bedrock,
    _call_claude,
    _call_claude_cli,
    _call_openai_compat,
    _claude_cli_available,
    _claude_cli_envelope,
    _claude_cli_error,
    _claude_cli_supports_json_schema,
    _envelope_after_preamble,
    _ollama_host_is_link_local_or_metadata,
    _resolve_ollama_base_url,
    _validate_ollama_base_url,
)

# ── Re-exports: Vision Handling ───────────────────────────────────────────────
from graph_fy.llm_vision import (
    _IMAGE_TOKEN_ESTIMATE,
    _MAX_IMAGES_PER_CHUNK,
    _PATH_IMAGE_BACKENDS,
    _VISION_IMAGE_EXTENSIONS,
    _ImageRef,
    _anthropic_content,
    _backend_supports_vision,
    _bedrock_content,
    _build_image_refs,
    _image_notes,
    _is_vision_image,
    _openai_content,
    _partition_semantic_files,
    _strip_pixels,
    _with_image_notes,
)

# ── Re-exports: JSON Repair & Parsing ─────────────────────────────────────────
from graph_fy.llm_json import (
    _FENCE_RE,
    _FRAGMENT_KEY_TOKENS,
    _FRAGMENT_KEYS,
    _LLM_JSON_MAX_BYTES,
    _MAX_OBJECT_CANDIDATES,
    _THINK_BLOCK_RE,
    _balanced_object,
    _json_fragment_candidates,
    _json_object_candidates,
    _parse_llm_json,
    _sanitize_fragment,
)

# ── Re-exports: Community Labeling ────────────────────────────────────────────
from graph_fy.llm_labels import (
    _LABEL_BATCH_SIZE,
    _LABEL_FENCE_RE,
    _LABEL_MAX_COMMUNITIES,
    _LABEL_MAXLEN,
    _LABEL_TOP_K,
    _community_label_lines,
    _label_batch_with_retry,
    _parse_label_response,
    _placeholder_community_labels,
    generate_community_labels,
    label_communities,
)


def extract_files_direct(
    files: list[Path],
    backend: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    root: Path = Path("."),
    *,
    deep_mode: bool = False,
) -> dict:
    """Extract semantic nodes/edges from files. Accepts Path, str, or FileSlice units."""
    files = [f if isinstance(f, (Path, FileSlice)) else Path(f) for f in files]
    if backend is None:
        backend = detect_backend()
        if backend is None:
            raise ValueError(
                "No LLM backend configured. Set one of: GEMINI_API_KEY, ANTHROPIC_API_KEY, "
                "OPENAI_API_KEY, DEEPSEEK_API_KEY, MOONSHOT_API_KEY, "
                "AZURE_OPENAI_API_KEY+AZURE_OPENAI_ENDPOINT, OLLAMA_BASE_URL, "
                "or AWS credentials. Pass backend= explicitly to select a provider."
            )
    if backend not in BACKENDS:
        raise ValueError(f"Unknown backend {backend!r}. Available: {sorted(BACKENDS)}")

    cfg = BACKENDS[backend]
    key = api_key or _get_backend_api_key(backend)
    if not key and backend == "ollama":
        ollama_url = _resolve_ollama_base_url(cfg.get("base_url", ""))
        _validate_ollama_base_url(ollama_url)
        print(
            "[graph_fy] WARNING: ollama backend selected with no OLLAMA_API_KEY set; "
            f"sending corpus to {ollama_url}. Set OLLAMA_API_KEY (any non-empty value) "
            "to suppress this warning.",
            file=sys.stderr,
        )
        key = "ollama"
    if not key and backend not in ("bedrock", "claude-cli"):
        raise ValueError(
            f"No API key for backend '{backend}'. "
            f"Set {_format_backend_env_keys(backend)} or pass api_key=."
        )
    mdl = model or _default_model_for_backend(backend)
    text_files, image_files = _partition_semantic_files(files)
    user_msg = _read_files(text_files, root)
    vision = _backend_supports_vision(backend)
    read_bytes = vision and backend not in _PATH_IMAGE_BACKENDS
    image_refs = _build_image_refs(image_files, root, read_bytes=read_bytes) if image_files else []
    if image_refs and not vision:
        image_refs = _strip_pixels(image_refs)
    max_out = _resolve_max_tokens(cfg.get("max_tokens", 8192))

    if backend == "claude":
        result = _call_claude(key, mdl, user_msg, max_tokens=max_out, deep_mode=deep_mode, images=image_refs)
    elif backend == "claude-cli":
        result = _call_claude_cli(user_msg, max_tokens=max_out, deep_mode=deep_mode, images=image_refs)
    elif backend == "bedrock":
        result = _call_bedrock(mdl, user_msg, max_tokens=max_out, deep_mode=deep_mode, images=image_refs)
    elif backend == "azure":
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError(
                "Azure OpenAI backend requires AZURE_OPENAI_ENDPOINT to be set "
                "(e.g. https://my-resource.openai.azure.com/)."
            )
        result = _call_azure(
            key,
            endpoint,
            mdl,
            user_msg,
            temperature=_resolve_temperature(cfg.get("temperature", 0), mdl),
            max_tokens=max_out,
            deep_mode=deep_mode,
        )
    else:
        result = _call_openai_compat(
            cfg["base_url"],
            key,
            mdl,
            user_msg,
            temperature=_resolve_temperature(cfg.get("temperature", 0), mdl),
            reasoning_effort=cfg.get("reasoning_effort"),
            max_completion_tokens=_resolve_max_tokens(
                cfg.get("max_completion_tokens") or cfg.get("max_tokens", 8192)
            ),
            backend=backend,
            deep_mode=deep_mode,
            images=image_refs,
            extra_body=cfg.get("extra_body"),
        )

    if isinstance(result, dict):
        try:
            _n_unverified = _bind_node_evidence(result, text_files, root)
            if _n_unverified:
                print(
                    f"[graph_fy] {_n_unverified} semantic node(s) had no evidence in "
                    "the source and were flagged verification=unverified",
                    file=sys.stderr,
                )
        except Exception as _exc:  # noqa: BLE001
            print(f"[graph_fy] evidence-binding skipped: {_exc}", file=sys.stderr)
    return result


def _extract_with_adaptive_retry(
    chunk: list[Path],
    backend: str,
    api_key: str | None,
    model: str | None,
    root: Path,
    max_depth: int,
    _depth: int = 0,
    *,
    deep_mode: bool = False,
) -> dict:
    """Extract chunk; bisect and recurse on truncation, context overflow, or timeout."""
    def _merge_two(left_units, right_units) -> dict:
        left = _extract_with_adaptive_retry(
            left_units, backend, api_key, model, root, max_depth, _depth + 1, deep_mode=deep_mode
        )
        right = _extract_with_adaptive_retry(
            right_units, backend, api_key, model, root, max_depth, _depth + 1, deep_mode=deep_mode
        )
        return {
            "nodes": left.get("nodes", []) + right.get("nodes", []),
            "edges": left.get("edges", []) + right.get("edges", []),
            "hyperedges": left.get("hyperedges", []) + right.get("hyperedges", []),
            "input_tokens": left.get("input_tokens", 0) + right.get("input_tokens", 0),
            "output_tokens": left.get("output_tokens", 0) + right.get("output_tokens", 0),
            "model": model,
            "finish_reason": "stop",
            "_partial_files": _merged_partial_files(left, right),
        }

    def _split_lone_slice() -> "tuple[FileSlice, FileSlice] | None":
        if len(chunk) == 1 and isinstance(chunk[0], FileSlice) and _depth < max_depth:
            return bisect_slice(chunk[0])
        return None

    try:
        result = extract_files_direct(
            chunk, backend=backend, api_key=api_key, model=model, root=root, deep_mode=deep_mode
        )
        for _delay in (_HOLLOW_BACKOFF_S if max_depth > 0 else ()):
            if result.get("finish_reason") != "hollow":
                break
            print(
                f"[graph_fy] retrying the same chunk of {len(chunk)} in {_delay:g}s "
                f"after a hollow response",
                file=sys.stderr,
            )
            time.sleep(_delay)
            result = extract_files_direct(
                chunk, backend=backend, api_key=api_key, model=model, root=root, deep_mode=deep_mode
            )
    except Exception as exc:  # noqa: BLE001
        is_timeout = _looks_like_timeout(exc)
        if not (_looks_like_context_exceeded(exc) or is_timeout):
            raise
        reason = "timed out" if is_timeout else "exceeded context"
        if len(chunk) <= 1:
            halves = _split_lone_slice()
            if halves is not None:
                print(
                    f"[graph_fy] slice of {unit_path(chunk[0])} {reason} at "
                    f"depth {_depth}; splitting the slice and retrying",
                    file=sys.stderr,
                )
                return _merge_two([halves[0]], [halves[1]])
            fail_desc = "timed out" if is_timeout else "exceeds model context"
            print(
                f"[graph_fy] single-file chunk {unit_path(chunk[0])} {fail_desc} "
                f"and cannot be split further: {exc}",
                file=sys.stderr,
            )
            return {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0, "model": model, "finish_reason": "stop"}
        if _depth >= max_depth:
            persist_desc = "still times out" if is_timeout else "still overflows context"
            print(
                f"[graph_fy] chunk of {len(chunk)} {persist_desc} at "
                f"recursion depth {_depth} (max {max_depth}) — dropping",
                file=sys.stderr,
            )
            return {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0, "model": model, "finish_reason": "stop"}
        print(
            f"[graph_fy] chunk of {len(chunk)} {reason} at depth "
            f"{_depth} ({type(exc).__name__}); splitting in half and retrying",
            file=sys.stderr,
        )
        mid = len(chunk) // 2
        left = _extract_with_adaptive_retry(
            chunk[:mid], backend, api_key, model, root, max_depth, _depth + 1, deep_mode=deep_mode
        )
        right = _extract_with_adaptive_retry(
            chunk[mid:], backend, api_key, model, root, max_depth, _depth + 1, deep_mode=deep_mode
        )
        return {
            "nodes": left.get("nodes", []) + right.get("nodes", []),
            "edges": left.get("edges", []) + right.get("edges", []),
            "hyperedges": left.get("hyperedges", []) + right.get("hyperedges", []),
            "input_tokens": left.get("input_tokens", 0) + right.get("input_tokens", 0),
            "output_tokens": left.get("output_tokens", 0) + right.get("output_tokens", 0),
            "model": model,
            "finish_reason": "stop",
            "_partial_files": _merged_partial_files(left, right),
        }

    if result.get("finish_reason") == "hollow":
        _attempts = (len(_HOLLOW_BACKOFF_S) + 1) if max_depth > 0 else 1
        print(
            f"[graph_fy] chunk of {len(chunk)} still hollow after "
            f"{_attempts} attempt(s) — giving up on this chunk. "
            f"Its files are marked for re-extraction on the next run. A hollow "
            f"response usually means a rate limit, a transport hiccup, a refusal, "
            f"or a model that answered in prose rather than JSON.",
            file=sys.stderr,
        )
        _mark_partial(result)
        result["_partial_files"] = sorted(
            set(_chunk_partial_files(chunk)) | set(result.get("_partial_files", []) or [])
        )
        result["finish_reason"] = "stop"
        return result

    if result.get("finish_reason") != "length":
        return result

    if len(chunk) <= 1:
        halves = _split_lone_slice()
        if halves is not None:
            print(
                f"[graph_fy] slice of {unit_path(chunk[0])} truncated at depth {_depth}; "
                f"splitting the slice and retrying",
                file=sys.stderr,
            )
            return _merge_two([halves[0]], [halves[1]])
        print(
            f"[graph_fy] single-file chunk {unit_path(chunk[0])} truncated at "
            f"max_completion_tokens — partial result kept (not cached as complete)",
            file=sys.stderr,
        )
        _mark_partial(result)
        result["_partial_files"] = sorted(
            set(_chunk_partial_files(chunk)) | set(result.get("_partial_files", []) or [])
        )
        return result

    if _depth >= max_depth:
        print(
            f"[graph_fy] chunk of {len(chunk)} still truncated at recursion "
            f"depth {_depth} (max {max_depth}) — partial result kept (not cached as complete)",
            file=sys.stderr,
        )
        _mark_partial(result)
        result["_partial_files"] = sorted(
            set(_chunk_partial_files(chunk)) | set(result.get("_partial_files", []) or [])
        )
        return result

    print(
        f"[graph_fy] chunk of {len(chunk)} truncated at depth {_depth}, "
        f"splitting into halves of {len(chunk) // 2} and "
        f"{len(chunk) - len(chunk) // 2}",
        file=sys.stderr,
    )
    mid = len(chunk) // 2
    left = _extract_with_adaptive_retry(
        chunk[:mid], backend, api_key, model, root, max_depth, _depth + 1, deep_mode=deep_mode
    )
    right = _extract_with_adaptive_retry(
        chunk[mid:], backend, api_key, model, root, max_depth, _depth + 1, deep_mode=deep_mode
    )

    return {
        "nodes": left.get("nodes", []) + right.get("nodes", []),
        "edges": left.get("edges", []) + right.get("edges", []),
        "hyperedges": left.get("hyperedges", []) + right.get("hyperedges", []),
        "input_tokens": left.get("input_tokens", 0) + right.get("input_tokens", 0),
        "output_tokens": left.get("output_tokens", 0) + right.get("output_tokens", 0),
        "model": result.get("model"),
        "finish_reason": "stop",
        "_partial_files": _merged_partial_files(left, right),
    }


def extract_corpus_parallel(
    files: list[Path],
    backend: str = "kimi",
    api_key: str | None = None,
    model: str | None = None,
    root: Path = Path("."),
    chunk_size: int = 20,
    on_chunk_done: Callable | None = None,
    token_budget: int | None = 60_000,
    max_concurrency: int = 4,
    max_retry_depth: int | None = None,
    deep_mode: bool = False,
    cache_root: "Path | None" = None,
) -> dict:
    """Extract a corpus in chunks, merging results."""
    if max_retry_depth is None:
        max_retry_depth = _resolve_max_retry_depth()
    files = [f if isinstance(f, (Path, FileSlice)) else Path(f) for f in files]
    files = expand_oversized_files(files, _FILE_CHAR_CAP)
    if token_budget is not None:
        chunks = _pack_chunks_by_tokens(files, token_budget=token_budget)
    else:
        chunks = [files[i:i + chunk_size] for i in range(0, len(files), chunk_size)]

    merged: dict = {
        "nodes": [], "edges": [], "hyperedges": [],
        "input_tokens": 0, "output_tokens": 0,
        "failed_chunks": 0,
    }
    total = len(chunks)

    def _run_one(idx: int, chunk: list[Path]) -> tuple[int, dict | None, Exception | None]:
        t0 = time.time()
        try:
            result = _extract_with_adaptive_retry(
                chunk,
                backend=backend,
                api_key=api_key,
                model=model,
                root=root,
                max_depth=max_retry_depth,
                deep_mode=deep_mode,
            )
            result["elapsed_seconds"] = round(time.time() - t0, 2)
            return idx, result, None
        except Exception as exc:  # noqa: BLE001
            return idx, None, exc

    if backend == "ollama" and os.environ.get("graph_fy_OLLAMA_PARALLEL", "").strip() != "1":
        max_concurrency = 1
    if backend == "claude-cli" and os.environ.get("graph_fy_CLAUDE_CLI_PARALLEL", "").strip() != "1":
        max_concurrency = 1

    def _checkpoint_chunk(result: dict, chunk: "list[Path | FileSlice]") -> None:
        if os.environ.get("graph_fy_NO_INCREMENTAL_CACHE"):
            return
        try:
            from .cache import save_semantic_cache as _scs
            allowed = [unit_path(item) for item in chunk]
            _scs(
                result.get("nodes", []),
                result.get("edges", []),
                result.get("hyperedges", []),
                root=root,
                cache_root=cache_root,
                merge_existing=True,
                allowed_source_files=allowed,
                mode="deep" if deep_mode else None,
                prompt=_extraction_system(deep=deep_mode),
                partial_source_files=_partial_source_files(result) or None,
            )
        except Exception as _exc:  # noqa: BLE001
            print(f"[graph_fy] incremental cache checkpoint failed: {_exc}", file=sys.stderr)

    workers = max(1, min(max_concurrency, total))
    if workers == 1:
        for idx, chunk in enumerate(chunks):
            _, result, exc = _run_one(idx, chunk)
            if exc is not None:
                print(f"[graph_fy] chunk {idx + 1}/{total} failed: {exc}", file=sys.stderr)
                merged["failed_chunks"] += 1
                continue
            assert result is not None
            _merge_into(merged, result)
            _checkpoint_chunk(result, chunk)
            if callable(on_chunk_done):
                on_chunk_done(idx, total, result)
    else:
        results_by_idx: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_one, idx, chunk) for idx, chunk in enumerate(chunks)]
            for future in as_completed(futures):
                idx, result, exc = future.result()
                if exc is not None:
                    print(
                        f"[graph_fy] chunk {idx + 1}/{total} failed: {exc}",
                        file=sys.stderr,
                    )
                    merged["failed_chunks"] += 1
                    continue
                assert result is not None
                results_by_idx[idx] = result
                _checkpoint_chunk(result, chunks[idx])
                if callable(on_chunk_done):
                    on_chunk_done(idx, total, result)
        for idx in sorted(results_by_idx):
            _merge_into(merged, results_by_idx[idx])

    if merged["failed_chunks"] > 0:
        print(
            f"[graph_fy] WARNING: {merged['failed_chunks']}/{total} semantic chunk(s) failed"
            " — see errors above. Partial results returned.",
            file=sys.stderr,
        )

    dispatched = {unit_path(f) for chunk in chunks for f in chunk}

    def _resolve_against_root(value: "str | Path") -> Path:
        p = Path(value)
        if not p.is_absolute():
            p = root / p
        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return p

    _dispatched_resolved = {_resolve_against_root(p) for p in dispatched}

    def _out_of_scope(item: dict) -> bool:
        sf = item.get("source_file")
        if not sf:
            return False
        p = _resolve_against_root(sf)
        return p.is_file() and p not in _dispatched_resolved

    dropped_ids: set = set()
    dropped_files: set[str] = set()
    kept_nodes: list[dict] = []
    for n in merged.get("nodes", []):
        if _out_of_scope(n):
            if n.get("id") is not None:
                dropped_ids.add(n.get("id"))
            dropped_files.add(str(n.get("source_file")))
            continue
        kept_nodes.append(n)
    dropped_node_count = len(merged.get("nodes", [])) - len(kept_nodes)
    merged["out_of_scope_dropped"] = dropped_node_count
    if dropped_node_count:
        merged["nodes"] = kept_nodes
        merged["edges"] = [
            e for e in merged.get("edges", [])
            if not _out_of_scope(e)
            and e.get("source") not in dropped_ids
            and e.get("target") not in dropped_ids
        ]
        merged["hyperedges"] = [
            h for h in merged.get("hyperedges", [])
            if not _out_of_scope(h)
            and not (dropped_ids & set(h.get("nodes", []) or []))
        ]
        shown = ", ".join(sorted(Path(f).name for f in dropped_files)[:5])
        more = f" (+{len(dropped_files) - 5} more)" if len(dropped_files) > 5 else ""
        print(
            f"[graph_fy] WARNING: dropped {dropped_node_count} out-of-scope node(s) "
            f"attributed to file(s) not dispatched for extraction: {shown}{more}. "
            "The model mis-attributed them to another corpus file; they were "
            "excluded from the graph (#1895).",
            file=sys.stderr,
        )

    covered: set[Path] = set()
    for n in merged.get("nodes", []):
        sf = n.get("source_file")
        if sf:
            p = Path(sf)
            covered.add(p if p.is_absolute() else (root / p))
    uncovered = sorted(
        p for p in dispatched
        if p.resolve() not in {c.resolve() for c in covered}
    )
    merged["uncovered_files"] = [str(p) for p in uncovered]
    if uncovered:
        shown = ", ".join(p.name for p in uncovered[:5])
        more = f" (+{len(uncovered) - 5} more)" if len(uncovered) > 5 else ""
        print(
            f"[graph_fy] WARNING: {len(uncovered)}/{len(dispatched)} dispatched file(s) "
            f"produced no nodes and are absent from the graph: {shown}{more}. The model "
            "returned a response but omitted them; a re-run will retry them.",
            file=sys.stderr,
        )
    return merged


def _merge_into(merged: dict, result: dict) -> None:
    """Append a chunk result into the running merged accumulator."""
    merged["nodes"].extend(result.get("nodes", []))
    merged["edges"].extend(result.get("edges", []))
    merged["hyperedges"].extend(result.get("hyperedges", []))
    merged["input_tokens"] += result.get("input_tokens", 0)
    merged["output_tokens"] += result.get("output_tokens", 0)
    incoming = result.get("_partial_files")
    if incoming:
        merged["_partial_files"] = sorted(
            set(merged.get("_partial_files", []) or []) | set(incoming)
        )


def _call_llm(
    prompt: str,
    *,
    backend: str,
    max_tokens: int = 200,
    model: str | None = None,
    usage_out: dict | None = None,
) -> str:
    """Send a plain-text prompt to `backend` and return the model's text reply."""
    if backend not in BACKENDS:
        raise ValueError(f"Unknown backend {backend!r}")
    cfg = BACKENDS[backend]
    key = _get_backend_api_key(backend)
    if not key and backend == "ollama":
        ollama_url = _resolve_ollama_base_url(cfg.get("base_url", ""))
        _validate_ollama_base_url(ollama_url)
        key = "ollama"
    if not key and backend not in ("bedrock", "claude-cli"):
        raise ValueError(
            f"No API key for backend '{backend}'. Set {_format_backend_env_keys(backend)}."
        )
    mdl = model or _default_model_for_backend(backend)

    def _rec(inp, out) -> None:
        if usage_out is not None:
            usage_out["input"] = usage_out.get("input", 0) + int(inp or 0)
            usage_out["output"] = usage_out.get("output", 0) + int(out or 0)

    if backend == "claude":
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(_backend_pkg_hint("anthropic", "anthropic")) from exc
        client = anthropic.Anthropic(api_key=key, base_url=cfg["base_url"], timeout=_resolve_api_timeout(), max_retries=_resolve_max_retries())
        resp = client.messages.create(
            model=mdl,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        u = getattr(resp, "usage", None)
        if u is not None:
            _rec(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))
        return _anthropic_response_text(resp.content, default="")

    if backend == "claude-cli":
        import platform, shutil
        claude_cmd = "claude"
        if platform.system() == "Windows":
            cmd_path = shutil.which("claude.cmd")
            if cmd_path:
                claude_cmd = cmd_path
            elif shutil.which("claude") is None:
                raise RuntimeError("Claude Code CLI not found on $PATH")
        elif shutil.which("claude") is None:
            raise RuntimeError("Claude Code CLI not found on $PATH")
        cli_args = [claude_cmd, "-p", "--output-format", "json", "--no-session-persistence"]
        if model is not None:
            cli_args.extend(["--model", mdl])
        proc = subprocess.run(
            cli_args,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_resolve_api_timeout(),
            check=False,
            **_no_window_kwargs(),
        )
        cli_error = _claude_cli_error(proc.stdout)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or cli_error or "(no stderr, no error envelope)"
            raise RuntimeError(f"claude -p exited {proc.returncode}: {detail[:500]}")
        if cli_error:
            raise RuntimeError(f"claude -p reported an error: {cli_error[:500]}")
        envelope = _claude_cli_envelope(proc.stdout)
        cli_usage = envelope.get("usage") or {}
        if cli_usage:
            _rec(
                (cli_usage.get("input_tokens", 0) or 0)
                + (cli_usage.get("cache_read_input_tokens", 0) or 0)
                + (cli_usage.get("cache_creation_input_tokens", 0) or 0),
                cli_usage.get("output_tokens", 0),
            )
        return envelope.get("result", "")

    if backend == "bedrock":
        try:
            import boto3
            import botocore.config
        except ImportError as exc:
            raise ImportError(_backend_pkg_hint("boto3", "bedrock")) from exc
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        profile = os.environ.get("AWS_PROFILE")
        session = boto3.Session(profile_name=profile, region_name=region)
        client = session.client(
            "bedrock-runtime",
            config=botocore.config.Config(
                read_timeout=_resolve_api_timeout(),
                connect_timeout=10,
                retries={"max_attempts": _resolve_max_retries() + 1, "mode": "adaptive"},
            ),
        )
        resp = client.converse(
            modelId=mdl,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig=_bedrock_inference_config(max_tokens, mdl),
        )
        bu = resp.get("usage") or {}
        if bu:
            _rec(bu.get("inputTokens", 0), bu.get("outputTokens", 0))
        return _bedrock_response_text(resp, default="")

    if backend == "azure":
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError(
                "Azure OpenAI backend requires AZURE_OPENAI_ENDPOINT to be set."
            )
        azure_client = _azure_client(key, endpoint)
        azure_kwargs: dict = {
            "model": mdl,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": max_tokens,
        }
        azure_temp = _resolve_temperature(cfg.get("temperature", 0), mdl)
        if azure_temp is not None:
            azure_kwargs["temperature"] = azure_temp
        resp = azure_client.chat.completions.create(**azure_kwargs)
        if not resp.choices or resp.choices[0].message is None:
            raise ValueError("Azure OpenAI returned empty or filtered response")
        au = getattr(resp, "usage", None)
        if au is not None:
            _rec(getattr(au, "prompt_tokens", 0), getattr(au, "completion_tokens", 0))
        return resp.choices[0].message.content or ""

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError(_backend_pkg_hint("openai", "openai")) from exc
    client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=_resolve_api_timeout(), max_retries=_resolve_max_retries())
    kwargs: dict = {
        "model": mdl,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_tokens,
        "stream": False,
    }
    temperature = _resolve_temperature(cfg.get("temperature", 0), mdl)
    if temperature is not None:
        kwargs["temperature"] = temperature
    if cfg.get("reasoning_effort"):
        kwargs["reasoning_effort"] = cfg["reasoning_effort"]
    if cfg.get("extra_body") is not None:
        kwargs["extra_body"] = cfg["extra_body"]
    elif "moonshot" in cfg["base_url"]:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    elif _thinking_disabled_via_env():
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    resp = client.chat.completions.create(**kwargs)
    if not resp.choices or resp.choices[0].message is None:
        raise ValueError("LLM returned empty or filtered response")
    ou = getattr(resp, "usage", None)
    if ou is not None:
        _rec(getattr(ou, "prompt_tokens", 0), getattr(ou, "completion_tokens", 0))
    return resp.choices[0].message.content or ""
