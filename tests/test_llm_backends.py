"""Tests for direct semantic-extraction backend selection and caching (#4835)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph_fy import llm


def _clear_backend_env(monkeypatch):
    for env_key in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MOONSHOT_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
    ):
        monkeypatch.delenv(env_key, raising=False)


def test_gemini_accepts_gemini_api_key(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")

    assert llm.detect_backend() == "gemini"
    assert llm._get_backend_api_key("gemini") == "gemini-key"


def test_gemini_accepts_google_api_key(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")

    assert llm.detect_backend() == "gemini"
    assert llm._get_backend_api_key("gemini") == "google-key"


def test_backend_detection_prefers_gemini(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")

    assert llm.detect_backend() == "gemini"


def test_openai_backend_detected(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    assert llm.detect_backend() == "openai"
    assert llm._get_backend_api_key("openai") == "openai-key"


def test_detect_backend_returns_azure_when_both_vars_set(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my-resource.openai.azure.com/")

    assert llm.detect_backend() == "azure"
    assert llm._get_backend_api_key("azure") == "azure-key"


def test_detect_backend_azure_requires_endpoint_not_just_key(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")

    assert llm.detect_backend() != "azure"


def _install_capturing_anthropic(monkeypatch, usage=None):
    """Inject a stub `anthropic` module that records each messages.create() call."""
    import sys
    import types

    calls = []

    class _Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"nodes":[{"id":"x"}],"edges":[],"hyperedges":[]}',
                    )
                ],
                usage=usage or SimpleNamespace(input_tokens=100, output_tokens=100),
                stop_reason="end_turn",
            )

    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = lambda **_: SimpleNamespace(messages=_Messages())
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    return calls


@pytest.mark.parametrize("deep_mode", [False, True], ids=["normal", "deep"])
def test_claude_system_prompt_marked_cacheable(monkeypatch, deep_mode):
    calls = _install_capturing_anthropic(monkeypatch)

    llm._call_claude("sk-ant-test", "claude-sonnet-4-6", "u", deep_mode=deep_mode)

    assert calls[0]["system"] == [
        {
            "type": "text",
            "text": llm._extraction_system(deep=deep_mode),
            "cache_control": {"type": "ephemeral"},
        }
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.anthropic.com",
        "https://opencode.ai/zen",
    ],
)
@pytest.mark.parametrize("deep_mode", [False, True], ids=["normal", "deep"])
@pytest.mark.parametrize(
    "setting,enabled",
    [
        (None, True),
        ("", True),
        ("  ", True),
        ("1", True),
        ("true", True),
        ("yes", True),
        (" ON ", True),
        ("0", False),
        ("false", False),
        ("no", False),
        (" OFF ", False),
    ],
)
def test_claude_prompt_cache_override(monkeypatch, base_url, deep_mode, setting, enabled):
    calls = _install_capturing_anthropic(monkeypatch)
    monkeypatch.setitem(llm.BACKENDS["claude"], "base_url", base_url)
    if setting is not None:
        monkeypatch.setenv("GRAPHIFY_CLAUDE_PROMPT_CACHE", setting)

    result = llm._call_claude("test-key", "custom-model", "u", deep_mode=deep_mode)

    prompt = llm._extraction_system(deep=deep_mode)
    assert calls[0]["system"] == (
        [
            {
                "type": "text",
                "text": prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if enabled
        else prompt
    )
    assert result["input_tokens"] == 100  # Compatible APIs may omit cache usage.
    assert result["nodes"] == [{"id": "x"}]


@pytest.mark.parametrize("env_var", ["GRAPH_FY_CLAUDE_PROMPT_CACHE", "GRAPHIFY_CLAUDE_PROMPT_CACHE"])
def test_claude_prompt_cache_invalid_override(monkeypatch, env_var):
    calls = _install_capturing_anthropic(monkeypatch)
    monkeypatch.setenv(env_var, "flase")

    with pytest.raises(ValueError, match=env_var):
        llm._call_claude("test-key", "custom-model", "u")

    assert calls == []


def test_claude_prompt_cache_unsupported_endpoint(monkeypatch):
    """An endpoint rejecting cache metadata works when caching is disabled."""
    _install_capturing_anthropic(monkeypatch)
    import anthropic

    requests = []
    clients = []

    def create(**kwargs):
        requests.append(kwargs)
        if isinstance(kwargs["system"], list):
            raise ValueError("cache_control is unsupported")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"nodes":[],"edges":[]}')],
            usage=SimpleNamespace(input_tokens=12, output_tokens=3),
            stop_reason="end_turn",
        )

    def client(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace(messages=SimpleNamespace(create=create))

    monkeypatch.setattr(anthropic, "Anthropic", client)
    endpoint = "https://gateway.example/anthropic"
    monkeypatch.setitem(llm.BACKENDS["claude"], "base_url", endpoint)
    monkeypatch.setenv("GRAPHIFY_CLAUDE_PROMPT_CACHE", "1")
    with pytest.raises(ValueError, match="cache_control is unsupported"):
        llm._call_claude("test-key", "custom-model", "u")
    assert len(requests) == 1  # No silent retry or endpoint/model guessing.

    monkeypatch.setenv("GRAPHIFY_CLAUDE_PROMPT_CACHE", "0")
    result = llm._call_claude("test-key", "custom-model", "u")

    assert len(requests) == 2
    assert requests[1]["system"] == llm._extraction_system()
    assert requests[1]["model"] == "custom-model"
    assert result["input_tokens"] == 12
    assert result["output_tokens"] == 3
    assert all(c["base_url"] == endpoint and c["api_key"] == "test-key" for c in clients)


def test_claude_system_block_same_across_files(monkeypatch):
    """The cache only hits when every call sends a byte-identical prefix."""
    calls = _install_capturing_anthropic(monkeypatch)

    llm._call_claude("sk-ant-test", "claude-sonnet-4-6", "first file")
    llm._call_claude("sk-ant-test", "claude-sonnet-4-6", "second file")

    assert calls[0]["system"] == calls[1]["system"]


def test_claude_input_tokens_include_cached_prompt(monkeypatch):
    """Anthropic reports cached prompt tokens outside input_tokens; cost.json needs the total."""
    _install_capturing_anthropic(
        monkeypatch,
        usage=SimpleNamespace(
            input_tokens=24,
            output_tokens=7,
            cache_creation_input_tokens=1505,
            cache_read_input_tokens=3010,
        ),
    )

    result = llm._call_claude("sk-ant-test", "claude-sonnet-4-6", "u")

    assert result["input_tokens"] == 24 + 1505 + 3010
