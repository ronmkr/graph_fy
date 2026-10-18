from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_PENDING_FILENAME = ".pending_changes"
_PENDING_DRAIN_MAX_PASSES = 20

# Build options that must survive into later rebuilds. The initial `extract`
# scan honours `--exclude`, but `update`/`watch`/hook rebuilds re-run detect()
# and would silently re-include excluded paths unless the patterns are persisted
# (#1886). We store them beside the graph so any rebuild driver can re-apply them.
_BUILD_CONFIG_FILENAME = ".graph_fy_build.json"


def _queue_pending(out_dir: Path, changed_paths: list[Path]) -> None:
    """Append ``changed_paths`` to ``out_dir/.pending_changes`` (one per line).

    Used by a post-commit hook process that cannot acquire ``_rebuild_lock``
    so its change set is not silently dropped (#1059). The lock-holding
    process drains this file before and after its rebuild and merges the
    contents with its own change set.

    Opened in append mode so concurrent writers do not clobber each other on
    POSIX; each ``write()`` of a small payload is effectively atomic. A
    trailing newline is always written so partial-line corruption stays
    confined to the offending entry and is skipped on drain.
    """
    if not changed_paths:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    pending = out_dir / _PENDING_FILENAME
    payload = "".join(f"{os.fspath(p)}\n" for p in changed_paths)
    with open(pending, "a", encoding="utf-8") as fh:
        fh.write(payload)


def _drain_pending(out_dir: Path) -> list[Path]:
    """Read + unlink ``out_dir/.pending_changes`` and return deduplicated paths.

    Returns an empty list if the file does not exist. Empty/whitespace lines
    are silently skipped so a partial concurrent write that left only a
    fragment cannot poison the merge.
    """
    pending = out_dir / _PENDING_FILENAME
    if not pending.exists():
        return []
    try:
        raw = pending.read_text(encoding="utf-8")
    except OSError:
        return []
    # Unlink BEFORE returning so a crash between read and process retains the
    # data in the next caller's view via the lines we are about to return —
    # i.e. losing the file after reading is fine, losing it before would be a
    # bug. Use missing_ok to tolerate a racing drain on platforms where
    # rename/unlink may interleave.
    with contextlib.suppress(FileNotFoundError):
        pending.unlink()
    seen: set[str] = set()
    out: list[Path] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(Path(s))
    return out


def _write_build_config(
    out_dir: Path,
    *,
    excludes: list[str] | None,
    gitignore: bool | None = None,
) -> None:
    """Persist corpus-shaping options under ``out_dir``.

    Best effort and non clobbering: omitted options retain their existing values.
    """
    if not excludes and gitignore is None:
        return
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / _BUILD_CONFIG_FILENAME
        try:
            config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        if excludes:
            config["excludes"] = list(excludes)
        if gitignore is not None:
            config["gitignore"] = gitignore
        path.write_text(json.dumps(config), encoding="utf-8")
    except OSError:
        pass


def _read_build_excludes(out_dir: Path) -> list[str]:
    """Return the persisted ``--exclude`` patterns for this graph, or []."""
    try:
        path = out_dir / _BUILD_CONFIG_FILENAME
        if path.is_file():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            ex = cfg.get("excludes") if isinstance(cfg, dict) else None
            if isinstance(ex, list):
                return [str(x) for x in ex if isinstance(x, str) and x]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _read_build_gitignore(out_dir: Path) -> bool:
    """Return whether rebuilds should honor VCS ignore files (default True)."""
    try:
        path = out_dir / _BUILD_CONFIG_FILENAME
        if path.is_file():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and isinstance(cfg.get("gitignore"), bool):
                return cfg["gitignore"]
    except (OSError, json.JSONDecodeError):
        pass
    return True


def _merge_changed_paths(*sources: list[Path] | None) -> list[Path]:
    """Concatenate path lists, preserving order and dropping duplicates.

    Used to combine a hook process's own ``changed_paths`` with the drained
    contents of ``.pending_changes`` so the lock-holding rebuild covers
    every queued commit's worth of files (#1059).
    """
    seen: set[str] = set()
    out: list[Path] = []
    for src in sources:
        if not src:
            continue
        for p in src:
            key = os.fspath(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


@contextlib.contextmanager
def _rebuild_lock(out_dir: Path, *, blocking: bool = False):
    """Per-repo advisory lock around a rebuild.

    Yields True if acquired, False if another rebuild is already running and
    ``blocking`` is False. Uses fcntl.flock so the lock is released
    automatically if the process is killed (no stale-lock cleanup needed).

    While the lock is held, ``.rebuild.lock`` contains the owning PID followed
    by a newline so external pollers (publish scripts, etc.) can read it.
    On successful release the file is unlinked so downstream tooling that
    waits for the lock to clear by polling for its absence unblocks promptly.

    Falls back to a no-op yield(True) on platforms without fcntl (Windows).
    """
    try:
        import fcntl
    except ImportError:
        yield True
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / ".rebuild.lock"
    # "a+" creates the file if missing without truncating an existing holder's
    # PID payload — important because another process may have already written
    # its PID before we attempt the flock.
    fh = open(lock_path, "a+", encoding="utf-8")
    acquired = False
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        acquired = True
        # Replace any prior owner's PID with ours so external readers see a
        # single parseable line, not a digit-concatenation across rebuilds.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except OSError:
            pass
        yield True
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()
        # Signal "rebuild done" by removing the lock file. Only the holder
        # unlinks; a non-acquiring caller leaves the existing lock in place.
        if acquired:
            with contextlib.suppress(OSError):
                lock_path.unlink()


def _apply_resource_limits() -> None:
    """Best-effort nice + memory cap. Called from inline hook scripts.

    graph_fy_REBUILD_MEMORY_LIMIT_MB caps RSS-ish memory. Uses RLIMIT_DATA on
    macOS (RLIMIT_AS is unreliable under Apple's libmalloc) and RLIMIT_AS on
    Linux. Silently skips if the platform doesn't support it.
    """
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass
    mb = os.environ.get("graph_fy_REBUILD_MEMORY_LIMIT_MB", "").strip()
    if not mb:
        return
    try:
        limit = int(mb) * 1024 * 1024
    except ValueError:
        return
    try:
        import resource
        which = resource.RLIMIT_DATA if sys.platform == "darwin" else resource.RLIMIT_AS
        soft, hard = resource.getrlimit(which)
        new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit else limit
        resource.setrlimit(which, (limit, new_hard))
    except (ImportError, ValueError, OSError):
        pass


def _git_head(cwd: Path | str | None = None) -> str | None:
    """Return current git HEAD commit hash, or None outside a repo.

    ``cwd`` selects the repository to ask (#2316). Without it the command
    inherits the caller's working directory, so `graph_fy update <target>`
    stamped the *invoking* repo's commit into the target's graph.json — the
    same CWD-anchoring mistake as the manifest path, but writing wrong
    provenance rather than a misplaced file.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3,
            stdin=subprocess.DEVNULL, cwd=str(cwd) if cwd is not None else None,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _report_root_label(watch_path: Path) -> str:
    if watch_path.is_absolute():
        return watch_path.name or str(watch_path)
    return Path.cwd().name if watch_path == Path(".") else str(watch_path)
