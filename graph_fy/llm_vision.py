"""Vision and multimodal request handling for LLM backends.

Handles raster image filtering, sizing caps, base64 encoding, Bedrock format conversion,
Claude-cli Read tool directives, and multimodal content block assembly.
"""

from __future__ import annotations

import base64
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from graph_fy.file_slice import FileSlice

# Raster image types a vision model can actually look at.
_VISION_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_TOKEN_ESTIMATE = 1_600
_MAX_IMAGES_PER_CHUNK = 20
_PATH_IMAGE_BACKENDS = {"claude-cli"}


@dataclass
class _ImageRef:
    """A single image destined for a vision request."""

    path: Path        # absolute path
    rel: str          # path relative to the corpus root
    media_type: str   # e.g. "image/png"
    raw: bytes | None

    @property
    def b64(self) -> str:
        return base64.standard_b64encode(self.raw).decode("ascii") if self.raw else ""

    @property
    def bedrock_format(self) -> str:
        return self.media_type.split("/", 1)[-1]


def _is_vision_image(path: Path) -> bool:
    return path.suffix.lower() in _VISION_IMAGE_EXTENSIONS


def _partition_semantic_files(
    units: "list[Path | FileSlice]",
) -> tuple["list[Path | FileSlice]", list[Path]]:
    """Split a chunk into (text-like units, raster-image files)."""
    text_units = [u for u in units if isinstance(u, FileSlice) or not _is_vision_image(u)]
    image_files = [u for u in units if not isinstance(u, FileSlice) and _is_vision_image(u)]
    return text_units, image_files


def _build_image_refs(image_files: list[Path], root: Path, *, read_bytes: bool = True) -> list[_ImageRef]:
    """Build `_ImageRef`s for raster images."""
    import graph_fy.llm as _llm

    refs: list[_ImageRef] = []
    for p in image_files:
        abs_path = _llm._resolve_under_root(p, root)
        if abs_path is None:
            print(f"[graph_fy] skipping image {p}: symlink target outside corpus root", file=sys.stderr)
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = Path(p).as_posix()
        media = _IMAGE_MEDIA_TYPES.get(p.suffix.lower(), "image/png")
        raw: bytes | None = None
        if read_bytes:
            try:
                raw = abs_path.read_bytes()
            except OSError as exc:
                print(f"[graph_fy] could not read image {rel}: {exc}", file=sys.stderr)
                raw = None
            if raw is not None and len(raw) > _MAX_IMAGE_BYTES:
                print(
                    f"[graph_fy] image {rel} is {len(raw) // 1024} KB, over the "
                    f"{_MAX_IMAGE_BYTES // (1024 * 1024)} MB inline-image limit for this "
                    "backend; sending it as a reference node without inline pixels.",
                    file=sys.stderr,
                )
                raw = None
        refs.append(_ImageRef(abs_path, rel, media, raw))
    return refs


def _strip_pixels(refs: list[_ImageRef]) -> list[_ImageRef]:
    """Return refs with pixel data dropped (for non-vision backends)."""
    return [replace(r, raw=None) for r in refs]


def _backend_supports_vision(backend: str) -> bool:
    """Whether `backend`'s configured model can see images."""
    import graph_fy.llm as _llm

    if backend == "ollama":
        return os.environ.get("graph_fy_OLLAMA_VISION", "").strip() == "1"
    return bool(_llm.BACKENDS.get(backend, {}).get("vision", False))


def _image_notes(refs: list[_ImageRef], *, with_paths: bool = False) -> str:
    """Text block listing the images so the model emits one node per image."""
    if not refs:
        return ""
    if with_paths:
        header = (
            "Use the Read tool to open and view each image file at the path below, "
            "then emit one node per image"
        )
    else:
        header = (
            "The following image file(s) are attached as visual input. Emit one "
            "node per image"
        )
    lines = [
        "=== IMAGES ===",
        f"{header} with \"file_type\":\"image\" and the listed source_file, a label "
        "describing what it depicts (diagram, screenshot, chart, photo, UI, logo), "
        "and edges to any code/doc nodes the image clearly references.",
    ]
    for i, r in enumerate(refs, 1):
        note = f"[image {i}] source_file: {r.rel}"
        if with_paths:
            note += f"  path: {r.path}"
        if r.raw is None and not with_paths:
            note += " (not shown: unreadable or exceeds size limit)"
        lines.append(note)
    return "\n".join(lines)


def _with_image_notes(user_message: str, refs: list[_ImageRef], *, with_paths: bool = False) -> str:
    notes = _image_notes(refs, with_paths=with_paths)
    if not notes:
        return user_message
    if not user_message.strip():
        return notes
    return f"{user_message}\n\n{notes}"


def _anthropic_content(user_message: str, refs: list[_ImageRef]):
    """Build the Anthropic `messages[].content` value (str, or block list with images)."""
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": r.media_type, "data": r.b64}}
        for r in refs
        if r.raw
    ]
    text = _with_image_notes(user_message, refs)
    if not blocks:
        return text
    return [*blocks, {"type": "text", "text": text}]


def _openai_content(user_message: str, refs: list[_ImageRef]):
    """Build the OpenAI-compatible user `content` value (str, or part list with images)."""
    parts: list[dict] = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{r.media_type};base64,{r.b64}", "detail": "auto"},
        }
        for r in refs
        if r.raw
    ]
    text = _with_image_notes(user_message, refs)
    if not parts:
        return text
    return [{"type": "text", "text": text}, *parts]


def _bedrock_content(user_message: str, refs: list[_ImageRef]) -> list[dict]:
    """Build the Bedrock Converse user content list (raw bytes, not base64)."""
    content: list[dict] = [
        {"image": {"format": r.bedrock_format, "source": {"bytes": r.raw}}}
        for r in refs
        if r.raw
    ]
    content.append({"text": _with_image_notes(user_message, refs)})
    return content
