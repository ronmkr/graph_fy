"""Watch and update command implementations for graph_fy CLI."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def cmd_watch(argv: list[str]) -> int:
    """Handle `graph_fy watch` command."""
    hot = "--hot" in argv
    debounce = 3.0
    watch_path: Path | None = None
    i_arg = 2
    while i_arg < len(argv):
        a = argv[i_arg]
        if a == "--hot":
            hot = True
            i_arg += 1
        elif a.startswith("--debounce="):
            try:
                debounce = float(a.split("=", 1)[1])
            except ValueError:
                pass
            i_arg += 1
        elif a == "--debounce" and i_arg + 1 < len(argv):
            try:
                debounce = float(argv[i_arg + 1])
            except ValueError:
                pass
            i_arg += 2
        elif not a.startswith("--") and watch_path is None:
            watch_path = Path(a)
            i_arg += 1
        else:
            i_arg += 1

    if watch_path is None:
        watch_path = Path(".")
    if not watch_path.exists():
        print(f"error: path not found: {watch_path}", file=sys.stderr)
        return 1
    from graph_fy.watch import watch as _watch

    try:
        _watch(watch_path, debounce=debounce, hot=hot)
        return 0
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_update(argv: list[str], graph_fy_out: str | Path) -> int:
    """Handle `graph_fy update` command."""
    force = os.environ.get("graph_fy_FORCE", "").lower() in ("1", "true", "yes")
    no_cluster = False
    args = argv[2:]
    watch_arg: str | None = None
    for a in args:
        if a == "--force":
            force = True
            continue
        if a == "--no-cluster":
            no_cluster = True
            continue
        if a.startswith("-"):
            print(f"error: unknown update option: {a}", file=sys.stderr)
            return 2
        if watch_arg is not None:
            print("error: update accepts at most one path argument", file=sys.stderr)
            return 2
        watch_arg = a

    if watch_arg is not None:
        watch_path = Path(watch_arg)
    else:
        saved = Path(graph_fy_out) / ".graph_fy_root"
        if saved.exists():
            watch_path = Path(saved.read_text(encoding="utf-8-sig").strip())
        else:
            watch_path = Path(".")
    if not watch_path.exists():
        print(f"error: path not found: {watch_path}", file=sys.stderr)
        return 1
    from graph_fy.watch import _rebuild_code

    print(f"Re-extracting code files in {watch_path} (no LLM needed)...")
    ok = _rebuild_code(watch_path, force=force, no_cluster=no_cluster, block_on_lock=True)
    if ok:
        print("Graph updated successfully.")
        return 0
    else:
        print(
            "Nothing to update or rebuild failed — check output above.",
            file=sys.stderr,
        )
        return 1


def cmd_check_update(argv: list[str]) -> int:
    """Handle `graph_fy check-update` command."""
    if len(argv) < 3:
        print("Usage: graph_fy check-update <path>", file=sys.stderr)
        return 1
    from graph_fy.watch import check_update

    check_update(Path(argv[2]).resolve())
    return 0
