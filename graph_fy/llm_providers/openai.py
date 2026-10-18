from __future__ import annotations

import os
import sys

from graph_fy.llm_common import _extraction_system, _mark_hollow
from graph_fy.llm_config import (
    _backend_pkg_hint,
    _resolve_api_timeout,
    _resolve_max_retries,
    _thinking_disabled_via_env,
)
from graph_fy.llm_json import _parse_llm_json
from graph_fy.llm_packing import _CHARS_PER_TOKEN
from graph_fy.llm_vision import _ImageRef, _openai_content


def _call_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    user_message: str,
    temperature: float | None = 0,
    reasoning_effort: str | None = None,
    max_completion_tokens: int = 8192,
    *,
    backend: str = "",
    deep_mode: bool = False,
    images: list[_ImageRef] | None = None,
    extra_body: dict | None = None,
) -> dict:
    """Call any OpenAI-compatible API (Kimi, OpenAI, etc.) and return parsed JSON."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        extra = backend if backend in ("kimi", "gemini", "openai", "ollama") else "openai"
        raise ImportError(_backend_pkg_hint("openai", extra)) from exc

    _retries = _resolve_max_retries()
    if backend == "ollama" and not os.environ.get("graph_fy_MAX_RETRIES", "").strip():
        _retries = 0
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=_resolve_api_timeout(),
        max_retries=_retries,
    )
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _extraction_system(deep=deep_mode)},
            {"role": "user", "content": _openai_content(user_message, images or [])},
        ],
        "max_completion_tokens": max_completion_tokens,
        "stream": False,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    elif "moonshot" in base_url:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    elif _thinking_disabled_via_env():
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    if backend == "ollama" and extra_body is None:
        num_ctx_raw = os.environ.get("graph_fy_OLLAMA_NUM_CTX", "").strip()
        estimated_input = len(user_message) // _CHARS_PER_TOKEN + 400
        auto_num_ctx = min(estimated_input + max_completion_tokens + 2000, 131072)
        auto_num_ctx = max(auto_num_ctx, 8192)
        if num_ctx_raw:
            try:
                num_ctx = int(num_ctx_raw)
            except ValueError:
                print(
                    f"[graph_fy] graph_fy_OLLAMA_NUM_CTX={num_ctx_raw!r} is not a valid integer; "
                    f"using auto-derived value ({auto_num_ctx}).",
                    file=sys.stderr,
                )
                num_ctx = auto_num_ctx
            else:
                if num_ctx < estimated_input:
                    print(
                        f"[graph_fy] warning: graph_fy_OLLAMA_NUM_CTX={num_ctx} is smaller than "
                        f"the estimated chunk input (~{estimated_input} tokens). Ollama will "
                        f"silently truncate the prompt and return empty responses. "
                        f"Try --token-budget {max(1024, num_ctx // 3)} or increase NUM_CTX.",
                        file=sys.stderr,
                    )
        else:
            num_ctx = auto_num_ctx
        keep_alive = os.environ.get("graph_fy_OLLAMA_KEEP_ALIVE", "30m")
        kwargs["extra_body"] = {"options": {"num_ctx": num_ctx}, "keep_alive": keep_alive}

    resp = client.chat.completions.create(**kwargs)
    if not resp.choices or resp.choices[0].message is None:
        raise ValueError("LLM returned empty or filtered response")
    raw_content = resp.choices[0].message.content
    result = _parse_llm_json(raw_content or "{}")
    result["input_tokens"] = resp.usage.prompt_tokens if resp.usage else 0
    result["output_tokens"] = resp.usage.completion_tokens if resp.usage else 0
    result["model"] = model
    result["finish_reason"] = resp.choices[0].finish_reason
    _mark_hollow(result, raw_content, backend)
    output_tokens = result["output_tokens"]
    if output_tokens < 50 and backend == "ollama":
        print(
            "[graph_fy] warning: ollama returned very few tokens — likely causes: "
            "(1) VRAM pressure: check `nvidia-smi` and reduce chunk size with "
            "--token-budget (e.g. --token-budget 4096) or set "
            "graph_fy_OLLAMA_NUM_CTX to a smaller value; "
            "(2) model too small for JSON instruction following — "
            "try a larger model with --model (e.g. --model qwen2.5-coder:14b).",
            file=sys.stderr,
        )
    return result


def _azure_client(api_key: str, endpoint: str):
    """Construct an AzureOpenAI client with env-driven api_version and timeout."""
    try:
        from openai import AzureOpenAI
    except ImportError as exc:
        raise ImportError(
            "Azure OpenAI requires the openai package. Run: pip install openai"
        ) from exc
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview").strip()
    timeout_raw = os.environ.get("graph_fy_API_TIMEOUT", "").strip()
    timeout_s: float = 600.0
    if timeout_raw:
        try:
            v = float(timeout_raw)
            if v > 0:
                timeout_s = v
        except ValueError:
            pass
    return AzureOpenAI(
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version=api_version,
        timeout=timeout_s,
        max_retries=_resolve_max_retries(),
    )


def _call_azure(
    api_key: str,
    endpoint: str,
    model: str,
    user_message: str,
    temperature: float | None = 0,
    max_tokens: int = 8192,
    *,
    deep_mode: bool = False,
) -> dict:
    """Call Azure OpenAI Service via the AzureOpenAI SDK client."""
    client = _azure_client(api_key, endpoint)
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _extraction_system(deep=deep_mode)},
            {"role": "user", "content": user_message},
        ],
        "max_completion_tokens": max_tokens,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)
    if not resp.choices or resp.choices[0].message is None:
        raise ValueError("Azure OpenAI returned empty or filtered response")
    raw_content = resp.choices[0].message.content
    result = _parse_llm_json(raw_content or "{}")
    result["input_tokens"] = resp.usage.prompt_tokens if resp.usage else 0
    result["output_tokens"] = resp.usage.completion_tokens if resp.usage else 0
    result["model"] = model
    result["finish_reason"] = resp.choices[0].finish_reason
    _mark_hollow(result, raw_content, "azure")
    return result
