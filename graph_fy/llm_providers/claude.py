from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys

from graph_fy.llm_common import _extraction_system, _mark_hollow
from graph_fy.llm_config import (
    BACKENDS,
    _backend_pkg_hint,
    _no_window_kwargs,
    _resolve_api_timeout,
    _resolve_max_retries,
)
from graph_fy.llm_json import _parse_llm_json
from graph_fy.llm_vision import _ImageRef, _anthropic_content, _with_image_notes


def _anthropic_response_text(content, default: str | None = None) -> str | None:
    """Return the first Anthropic content block that carries text.

    Current Claude models emit a ``ThinkingBlock`` ahead of the ``TextBlock``
    when extended thinking is enabled (including the default-on path where the
    thinking text is omitted). Indexing ``content[0]`` therefore raises or
    yields no text (#2697). Select on the block's type instead of its position.
    """
    if not content:
        return default
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type is not None and block_type != "text":
            continue
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            return text
    return default


def _call_claude(
    api_key: str,
    model: str,
    user_message: str,
    max_tokens: int = 8192,
    *,
    deep_mode: bool = False,
    images: list[_ImageRef] | None = None,
) -> dict:
    """Call Anthropic Claude directly (not via OpenAI compat layer)."""
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError(_backend_pkg_hint("anthropic", "anthropic")) from exc

    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=BACKENDS["claude"]["base_url"],
        timeout=_resolve_api_timeout(),
        max_retries=_resolve_max_retries(),
    )
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_extraction_system(deep=deep_mode),
        messages=[{"role": "user", "content": _anthropic_content(user_message, images or [])}],
    )
    raw_content = _anthropic_response_text(resp.content)
    result = _parse_llm_json(raw_content or "{}")
    result["input_tokens"] = resp.usage.input_tokens if resp.usage else 0
    result["output_tokens"] = resp.usage.output_tokens if resp.usage else 0
    result["model"] = model
    result["finish_reason"] = "length" if resp.stop_reason == "max_tokens" else "stop"
    _mark_hollow(result, raw_content, "claude")
    return result


def _envelope_after_preamble(stdout: str):
    """Recover the envelope when `claude -p` prefixes it with a diagnostic line."""
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(stdout):
        if ch not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    return None


def _claude_cli_envelope(stdout: str) -> dict:
    """Parse the JSON returned by `claude -p --output-format json`."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        envelope = _envelope_after_preamble(stdout)
        if envelope is None:
            raise RuntimeError(
                f"claude -p produced unparseable JSON envelope: {exc}; "
                f"first 500 chars of stdout: {stdout[:500]!r}"
            ) from exc
    if isinstance(envelope, list):
        result_events = [
            e for e in envelope
            if isinstance(e, dict) and e.get("type") == "result"
        ]
        if result_events:
            return result_events[-1]
        if envelope and isinstance(envelope[-1], dict):
            return envelope[-1]
        raise RuntimeError(
            "claude -p returned a JSON array with no result object; "
            f"first 500 chars of stdout: {stdout[:500]!r}"
        )
    return envelope


def _claude_cli_error(stdout: str) -> str:
    """Return the CLI's own error text when the envelope flags `is_error`."""
    try:
        envelope = _claude_cli_envelope(stdout)
    except RuntimeError:
        return ""
    if not envelope.get("is_error"):
        return ""
    detail = envelope.get("result")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return "unspecified error"


_EXTRACTION_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": {"type": "object"}},
            "edges": {"type": "array", "items": {"type": "object"}},
            "hyperedges": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["nodes", "edges"],
    }
)

_JSON_SCHEMA_SUPPORT: dict[str, bool] = {}


def _claude_cli_supports_json_schema(claude_cmd: str) -> bool:
    """Return True if this Claude Code CLI accepts ``--json-schema``."""
    cached = _JSON_SCHEMA_SUPPORT.get(claude_cmd)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [claude_cmd, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            **_no_window_kwargs(),
        )
        supported = "--json-schema" in (proc.stdout or "")
    except (OSError, subprocess.SubprocessError):
        supported = False
    _JSON_SCHEMA_SUPPORT[claude_cmd] = supported
    return supported


def _call_claude_cli(
    user_message: str,
    max_tokens: int = 8192,
    *,
    deep_mode: bool = False,
    images: list[_ImageRef] | None = None,
) -> dict:
    """Call Claude via the locally-installed Claude Code CLI (`claude -p`)."""
    claude_cmd = "claude"
    if platform.system() == "Windows":
        cmd_path = shutil.which("claude.cmd")
        if cmd_path:
            claude_cmd = cmd_path
        elif shutil.which("claude") is None:
            raise RuntimeError(
                "Claude Code CLI not found on $PATH. Install from "
                "https://claude.ai/code and run `claude` once to authenticate."
            )
    elif shutil.which("claude") is None:
        raise RuntimeError(
            "Claude Code CLI not found on $PATH. Install from "
            "https://claude.ai/code and run `claude` once to authenticate."
        )

    add_dir_args: list[str] = []
    if images:
        user_message = _with_image_notes(user_message, images, with_paths=True)
        seen_dirs: set[str] = set()
        for r in images:
            d = str(r.path.parent)
            if d not in seen_dirs:
                seen_dirs.add(d)
                add_dir_args.extend(["--add-dir", d])

    combined_message = (
        _extraction_system(deep=deep_mode)
        + "\n\n---\n"
        + "Now extract the knowledge graph from the following source file(s) "
        + "and output ONLY the JSON object described above. No prose, no "
        + "preamble, no markdown fences.\n\n"
        + user_message
    )
    cli_args = [
        claude_cmd, "-p",
        "--output-format", "json",
        "--no-session-persistence",
        *add_dir_args,
    ]
    cli_model = os.environ.get("graph_fy_CLAUDE_CLI_MODEL", "").strip()
    if cli_model:
        cli_args.extend(["--model", cli_model])
    supports_fn = getattr(sys.modules.get("graph_fy.llm"), "_claude_cli_supports_json_schema", _claude_cli_supports_json_schema)
    if supports_fn(claude_cmd):
        cli_args.extend(["--json-schema", _EXTRACTION_JSON_SCHEMA])
    proc = subprocess.run(
        cli_args,
        input=combined_message,
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
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        raw_content = json.dumps(structured)
    else:
        raw_content = envelope.get("result", "")
    result = _parse_llm_json(raw_content or "{}")
    usage = envelope.get("usage") or {}
    result["input_tokens"] = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    result["output_tokens"] = int(usage.get("output_tokens", 0) or 0)
    model_usage = envelope.get("modelUsage") or {}
    result["model"] = next(iter(model_usage), "claude-code-plan")
    stop_reason = envelope.get("stop_reason", "")
    result["finish_reason"] = "length" if stop_reason == "max_tokens" else "stop"
    _mark_hollow(result, raw_content, "claude-cli")
    return result


def _claude_cli_available() -> bool:
    """True if the Claude Code CLI can actually be launched."""
    if os.environ.get("graph_fy_NO_CLAUDE_CLI") or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if platform.system() == "Windows":
        return bool(shutil.which("claude.cmd") or shutil.which("claude"))
    return shutil.which("claude") is not None
