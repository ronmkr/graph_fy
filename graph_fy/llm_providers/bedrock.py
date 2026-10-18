from __future__ import annotations

import os

from graph_fy.llm_common import _extraction_system, _mark_hollow
from graph_fy.llm_config import (
    _bedrock_inference_config,
    _resolve_api_timeout,
    _resolve_max_retries,
)
from graph_fy.llm_json import _parse_llm_json
from graph_fy.llm_vision import _ImageRef, _bedrock_content


def _bedrock_response_text(resp: dict, default: str = "") -> str:
    """Return the first Converse content block that carries text."""
    content = resp.get("output", {}).get("message", {}).get("content", [])
    if not isinstance(content, list):
        return default
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return default


def _call_bedrock(
    model: str,
    user_message: str,
    max_tokens: int = 8192,
    *,
    deep_mode: bool = False,
    images: list[_ImageRef] | None = None,
) -> dict:
    """Call AWS Bedrock via boto3 Converse API using the standard AWS credential chain."""
    try:
        import boto3
        import botocore.config
        import botocore.exceptions
    except ImportError as exc:
        raise ImportError(
            "AWS Bedrock extraction requires boto3. Run: pip install graph_fyy[bedrock]"
        ) from exc

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

    try:
        resp = client.converse(
            modelId=model,
            system=[{"text": _extraction_system(deep=deep_mode)}],
            messages=[{"role": "user", "content": _bedrock_content(user_message, images or [])}],
            inferenceConfig=_bedrock_inference_config(max_tokens, model),
        )
    except botocore.exceptions.ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"]["Message"]
        raise RuntimeError(f"Bedrock API error ({code}): {msg}") from exc

    text = _bedrock_response_text(resp, default="{}")
    result = _parse_llm_json(text)
    usage = resp.get("usage", {})
    result["input_tokens"] = usage.get("inputTokens", 0)
    result["output_tokens"] = usage.get("outputTokens", 0)
    result["model"] = model
    result["finish_reason"] = "length" if resp.get("stopReason") == "max_tokens" else "stop"
    _mark_hollow(result, text, "bedrock")
    return result
