from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit


def _resolve_ollama_base_url(default: str) -> str:
    """Resolve Ollama base URL (OLLAMA_BASE_URL -> OLLAMA_HOST -> default). Normalize host/port/v1."""
    ollama_base_url = os.environ.get("OLLAMA_BASE_URL")
    if ollama_base_url is not None:
        return ollama_base_url
    ollama_host = os.environ.get("OLLAMA_HOST")
    if ollama_host is None:
        return default
    host = ollama_host.strip()
    if not host:
        return default
    # Bare port or :port -> localhost on that port
    if host.isdigit():
        host = f"localhost:{host}"
    elif host.startswith(":") and host[1:].isdigit():
        host = f"localhost{host}"
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    # Default port 11434 if absent
    try:
        parts = urlsplit(host)
        if parts.hostname and parts.port is None:
            hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            userinfo = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
            host = urlunsplit(parts._replace(netloc=f"{userinfo}{hostname}:11434"))
    except (ValueError, TypeError):
        pass
    host = host.rstrip("/")
    if not host.endswith("/v1"):
        host = f"{host}/v1"
    return host


def _ollama_host_is_link_local_or_metadata(host: str) -> bool:
    """True if host resolves to link-local or cloud-metadata address (anti-SSRF)."""
    import ipaddress
    import socket
    if host in ("metadata.google.internal", "metadata.google.com", "0.0.0.0", "::", "[::]"):  # nosec: B104
        return True
    if host.startswith("169.254."):  # link-local literal
        return True
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_link_local:  # 169.254.0.0/16 and fe80::/10
            return True
    return False


def _validate_ollama_base_url(url: str, *, warn: bool = True) -> None:
    """Validate Ollama URL; reject link-local/metadata, warn if non-loopback."""
    try:
        parsed = urlparse(url)
    except Exception:
        if warn:
            print(
                f"[graph_fy] WARNING: OLLAMA_BASE_URL={url!r} is not a parseable URL.",
                file=sys.stderr,
            )
        return
    if parsed.scheme not in ("http", "https"):
        if warn:
            print(
                f"[graph_fy] WARNING: OLLAMA_BASE_URL has unexpected scheme {parsed.scheme!r}; "
                "expected http or https.",
                file=sys.stderr,
            )
        return
    host = (parsed.hostname or "").lower()
    if _ollama_host_is_link_local_or_metadata(host):
        raise ValueError(
            f"OLLAMA_BASE_URL points at a link-local/metadata address ({host!r}); refusing to "
            "send the corpus there. Set it to a real Ollama host."
        )
    is_loopback = host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")
    if warn and not is_loopback:
        scheme_note = " (UNENCRYPTED)" if parsed.scheme == "http" else ""
        print(
            f"[graph_fy] WARNING: OLLAMA_BASE_URL points to non-loopback host {host!r}{scheme_note}. "
            "Your full corpus will be sent to that endpoint. "
            "Set OLLAMA_BASE_URL=http://localhost:11434/v1 to keep extraction local.",
            file=sys.stderr,
        )


BACKENDS: dict[str, dict] = {
    "claude": {
        "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        "default_model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        "env_key": "ANTHROPIC_API_KEY",
        "pricing": {"input": 3.0, "output": 15.0},  # USD per 1M tokens
        "temperature": 0,
        "max_tokens": 16384,
        "vision": True,
    },
    "kimi": {
        "base_url": os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1"),
        "default_model": "kimi-k2.6",
        "env_key": "MOONSHOT_API_KEY",
        "vision": True,  # native multimodal (MoonViT)
        "pricing": {"input": 0.74, "output": 4.66},  # USD per 1M tokens
        "temperature": None,  # fixed temperature on kimi-k2.6
        "max_tokens": 16384,
    },
    "ollama": {
        "base_url": _resolve_ollama_base_url("http://localhost:11434/v1"),
        "default_model": os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        "env_key": "OLLAMA_API_KEY",
        "pricing": {"input": 0.0, "output": 0.0},
        "temperature": 0,
        "max_tokens": 16384,
    },
    "gemini": {
        "base_url": os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        "default_model": "gemini-3-flash-preview",
        "env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "model_env_key": "graph_fy_GEMINI_MODEL",
        "pricing": {"input": 0.50, "output": 3.00},  # USD per 1M tokens
        "temperature": 0,
        "reasoning_effort": "low",
        "max_completion_tokens": 16384,
        "vision": True,
    },
    "openai": {
        "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "default_model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        "env_key": "OPENAI_API_KEY",
        "model_env_key": "graph_fy_OPENAI_MODEL",
        "max_tokens": 16384,
        "pricing": {"input": 0.40, "output": 1.60},  # USD per 1M tokens
        "temperature": 0,  # reasoning models auto-omit temperature
        "vision": True,
    },
    "deepseek": {
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "default_model": "deepseek-v4-flash",
        "env_key": "DEEPSEEK_API_KEY",
        "model_env_key": "graph_fy_DEEPSEEK_MODEL",
        "pricing": {"input": 0.44, "output": 1.32},  # USD per 1M tokens
        "temperature": 0,
        "max_tokens": 16384,
    },
    "azure": {
        "default_model": os.environ.get("AZURE_OPENAI_DEPLOYMENT", os.environ.get("graph_fy_AZURE_MODEL", "gpt-4o")),
        "env_key": "AZURE_OPENAI_API_KEY",
        "model_env_key": "graph_fy_AZURE_MODEL",
        "pricing": {"input": 2.50, "output": 10.00},  # USD per 1M tokens
        "temperature": 0,
        "max_tokens": 16384,
    },
    "bedrock": {
        "default_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_env_key": "graph_fy_BEDROCK_MODEL",
        "pricing": {"input": 3.0, "output": 15.0},  # USD per 1M tokens
        "temperature": 0,
        "max_tokens": 16384,
        "vision": True,
    },
    "claude-cli": {
        "default_model": "claude-code-plan",
        "pricing": {"input": 0.0, "output": 0.0},
        "temperature": 0,
        "max_tokens": 16384,
        "vision": True,
    },
}


def _custom_providers_path(global_: bool = True) -> Path:
    if global_:
        return Path.home() / ".graph_fy" / "providers.json"
    return Path(".graph_fy") / "providers.json"


def provider_base_url_ok(base_url: str, name: str, *, warn: bool = True) -> bool:
    """Structural safety check for a custom-provider base_url."""
    try:
        parsed = urlparse(base_url)
    except Exception:
        if warn:
            print(f"[graph_fy] WARNING: provider {name!r} has an unparseable base_url; ignoring.", file=sys.stderr)
        return False
    if parsed.scheme not in ("http", "https"):
        if warn:
            print(
                f"[graph_fy] WARNING: provider {name!r} base_url scheme {parsed.scheme!r} is not "
                "http/https; ignoring.",
                file=sys.stderr,
            )
        return False
    host = (parsed.hostname or "").lower()
    is_loopback = host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")
    if warn and parsed.scheme == "http" and not is_loopback:
        print(
            f"[graph_fy] WARNING: provider {name!r} sends your corpus to {host!r} over plaintext "
            "http. Use https unless this is a trusted local endpoint.",
            file=sys.stderr,
        )
    return True


def _load_custom_providers() -> dict[str, dict]:
    local_path = _custom_providers_path(global_=False)
    global_path = _custom_providers_path(global_=True)
    allow_local = os.environ.get("graph_fy_ALLOW_LOCAL_PROVIDERS", "").strip().lower() in ("1", "true", "yes")
    if local_path.is_file() and not allow_local:
        print(
            f"[graph_fy] WARNING: ignoring project-local {local_path} (custom providers control "
            "where your corpus and API key are sent). Set graph_fy_ALLOW_LOCAL_PROVIDERS=1 to load it.",
            file=sys.stderr,
        )

    providers: dict[str, dict] = {}
    paths = [local_path, global_path] if allow_local else [global_path]
    for path in paths:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for name, cfg in data.items():
                        if not (isinstance(name, str) and isinstance(cfg, dict)):
                            continue
                        if name in BACKENDS or name in providers:
                            continue
                        if not provider_base_url_ok(str(cfg.get("base_url", "")), name):
                            continue
                        if "pricing" not in cfg:
                            cfg = dict(cfg, pricing={"input": 0.0, "output": 0.0})
                        providers[name] = cfg
            except Exception:
                pass
    return providers


BACKENDS.update(_load_custom_providers())


def _resolve_max_tokens(default: int) -> int:
    """Honour graph_fy_MAX_OUTPUT_TOKENS env var override, else use backend default."""
    raw = os.environ.get("graph_fy_MAX_OUTPUT_TOKENS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return default


_FIXED_TEMPERATURE_MODEL_MARKERS = ("o1", "o1-", "o3", "o3-", "o4", "o4-", "gpt-5")


def _model_requires_default_temperature(model: str) -> bool:
    """True if `model` is a reasoning model that rejects an explicit temperature."""
    m = (model or "").lower()
    base = m.rsplit("/", 1)[-1]
    if base.startswith("gpt-5"):
        return True
    for fam in ("o1", "o3", "o4"):
        if base == fam or base.startswith(fam + "-"):
            return True
    return False


def _resolve_temperature(default: float | None, model: str = "") -> float | None:
    """Resolve the temperature to send, honouring graph_fy_LLM_TEMPERATURE."""
    raw = os.environ.get("graph_fy_LLM_TEMPERATURE", "").strip()
    if raw:
        if raw.lower() in ("none", "omit", "default"):
            return None
        try:
            return float(raw)
        except ValueError:
            print(
                f"[graph_fy] graph_fy_LLM_TEMPERATURE={raw!r} is not a number or "
                "'none'; falling back to the backend default.",
                file=sys.stderr,
            )
    if _model_requires_default_temperature(model):
        return None
    return default


def _bedrock_inference_config(max_tokens: int, model: str = "") -> dict:
    """Build Bedrock inferenceConfig, honouring graph_fy_LLM_TEMPERATURE."""
    cfg: dict = {"maxTokens": max_tokens}
    temp = _resolve_temperature(0, model)
    if temp is not None:
        cfg["temperature"] = temp
    return cfg


def _no_window_kwargs() -> dict:
    """subprocess kwargs that suppress the console window claude.cmd would otherwise pop on Windows."""
    import subprocess
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _resolve_api_timeout(default: float = 600.0) -> float:
    """Honour graph_fy_API_TIMEOUT env var override, else use default (seconds)."""
    raw = os.environ.get("graph_fy_API_TIMEOUT", "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return default


def _resolve_max_retries(default: int = 6) -> int:
    """How many times the provider SDK retries a transient error before giving up."""
    raw = os.environ.get("graph_fy_MAX_RETRIES", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 0:
                return v
        except ValueError:
            pass
    return default


def _resolve_max_retry_depth(default: int = 3) -> int:
    """How deep adaptive retry may bisect a truncated chunk."""
    raw = os.environ.get("graph_fy_MAX_RETRY_DEPTH", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 0:
                return v
        except ValueError:
            pass
    return default


def _thinking_disabled_via_env() -> bool:
    """Opt-in (graph_fy_DISABLE_THINKING) to send ``{"thinking": {"type": "disabled"}}``."""
    return os.environ.get("graph_fy_DISABLE_THINKING", "").strip().lower() in ("1", "true", "yes", "on")


def _backend_env_keys(backend: str) -> list[str]:
    """Return accepted API-key environment variables for a backend."""
    cfg = BACKENDS[backend]
    keys = cfg.get("env_keys")
    if keys:
        return list(keys)
    env_key = cfg.get("env_key")
    if env_key:
        return [env_key]
    return []


def _get_backend_api_key(backend: str) -> str:
    """Return the first configured API key for backend, or an empty string."""
    for env_key in _backend_env_keys(backend):
        value = os.environ.get(env_key)
        if value:
            return value
    return ""


def _format_backend_env_keys(backend: str) -> str:
    """Return user-facing accepted API-key variable names."""
    keys = _backend_env_keys(backend)
    return " or ".join(keys) if keys else "AWS_PROFILE or AWS_REGION"


def _default_model_for_backend(backend: str) -> str:
    """Return configured model override or backend default model."""
    cfg = BACKENDS[backend]
    model_env_key = cfg.get("model_env_key")
    if model_env_key:
        model = os.environ.get(model_env_key)
        if model:
            return model
    return cfg["default_model"]


def _backend_pkg_hint(pkg: str, extra: str) -> str:
    """Package-missing message that works for the recommended `uv tool` install."""
    return (
        f"the '{pkg}' package is required for this backend but is not installed. "
        f"Install it with:  uv tool install \"graph_fyy[{extra}]\" --force  "
        f"(uv tool), or  pip install {pkg}  (pip/venv install)."
    )


_BACKEND_DETECTION_EXTRA_ENV = (
    "AZURE_OPENAI_ENDPOINT",
    "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
    "OLLAMA_BASE_URL", "OLLAMA_HOST",
)


def backend_detection_env_vars() -> tuple[str, ...]:
    """Every environment variable ``detect_backend()`` consults, in probe order."""
    seen: dict[str, None] = {}
    for name in BACKENDS:
        for env_key in _backend_env_keys(name):
            seen.setdefault(env_key, None)
    for env_key in _BACKEND_DETECTION_EXTRA_ENV:
        seen.setdefault(env_key, None)
    return tuple(seen)


def detect_backend() -> str | None:
    """Return the name of whichever backend has an API key set, or None."""
    for backend in ("gemini", "kimi", "claude", "openai", "deepseek"):
        if _get_backend_api_key(backend):
            return backend
    if _get_backend_api_key("azure") and os.environ.get("AZURE_OPENAI_ENDPOINT"):
        return "azure"
    if os.environ.get("AWS_PROFILE") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"):
        return "bedrock"
    ollama_url = _resolve_ollama_base_url("")
    if ollama_url:
        _validate_ollama_base_url(ollama_url)
        return "ollama"
    for name in BACKENDS:
        if name not in ("gemini", "kimi", "claude", "openai", "deepseek", "azure", "bedrock", "ollama", "claude-cli"):
            if _get_backend_api_key(name):
                return name
    return None


def estimate_cost(backend: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a given token count using published pricing."""
    if backend not in BACKENDS:
        return 0.0
    p = BACKENDS[backend]["pricing"]
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000
