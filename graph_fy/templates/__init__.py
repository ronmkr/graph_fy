"""Template assets and loaders for graph_fy visualizers."""
from __future__ import annotations

import functools
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent


@functools.lru_cache(maxsize=32)
def get_template(name: str) -> str:
    """Load a static CSS or JS template asset with in-memory caching.

    Tries `importlib.resources` first (for packaged wheel installations),
    falling back to local filesystem path.
    """
    try:
        from importlib import resources

        return (
            resources.files("graph_fy.templates")
            .joinpath(name)
            .read_text(encoding="utf-8")
        )
    except Exception:
        path = _TEMPLATE_DIR / name
        return path.read_text(encoding="utf-8")
