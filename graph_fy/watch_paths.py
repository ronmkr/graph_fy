from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import Any

from graph_fy.paths import is_absolute_any_platform


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _changed_path_candidates(raw: Path, *, change_root: Path, watch_root: Path) -> list[Path]:
    """Return plausible absolute locations for a hook-provided changed path.

    Git hooks pass paths relative to the repository root. Watch callers may
    also pass paths relative to the watched root. Keep both interpretations so
    a graph rooted at ``src`` accepts ``src/app.py`` and ``app.py``.
    """
    if raw.is_absolute():
        lexical = Path(os.path.abspath(raw))
        resolved = raw.resolve()
        return [lexical] if lexical == resolved else [lexical, resolved]

    candidates: list[Path] = []
    seen: set[str] = set()
    for base in (change_root, watch_root):
        lexical = Path(os.path.abspath(base / raw))
        for cand in (lexical, lexical.resolve()):
            key = os.fspath(cand)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(cand)
    return candidates


# Every stored path that names a file in the scanned tree. ``definition_file``
# is the implementation site recorded when a C/C++/ObjC declaration and its
# definition merge into one node; it must stay repo-relative like its sibling
# ``source_file`` so a graph built on one machine reads on another.
_PORTABLE_PATH_KEYS = ("source_file", "definition_file")


def _relativize_source_files(payload: dict, root: Path, *, scope: Path | None = None) -> None:
    for bucket in ("nodes", "edges", "hyperedges"):
        for item in payload.get(bucket, []):
            for key in _PORTABLE_PATH_KEYS:
                source = item.get(key)
                if not source:
                    continue
                source_path = Path(source)
                if not source_path.is_absolute():
                    continue
                try:
                    resolved = source_path.resolve()
                    if scope is not None and not _is_relative_to(resolved, scope):
                        continue
                    item[key] = resolved.relative_to(root).as_posix()
                except ValueError:
                    continue


def _rebase_relative_source_files(payload: dict, source_root: Path, target_root: Path) -> None:
    """Rebase cache-root-relative source paths onto the project root."""
    if source_root == target_root:
        return
    for bucket in ("nodes", "edges", "hyperedges"):
        for item in payload.get(bucket, []):
            for key in _PORTABLE_PATH_KEYS:
                source = item.get(key)
                if not source or Path(source).is_absolute():
                    continue
                try:
                    item[key] = (source_root / source).relative_to(target_root).as_posix()
                except ValueError:
                    continue


class _StoredSourcePaths:
    """Resolve source_file values across current and legacy graph roots."""

    def __init__(
        self,
        existing: dict,
        *,
        out: Path,
        project_root: Path,
        watch_root: Path,
        normalize_source: Any,
    ) -> None:
        self.project_root = project_root
        self.watch_root = watch_root
        self._normalize_source = normalize_source
        self.existing_source_root = project_root
        relative_marker_prefix: str | None = None

        root_marker = out / ".graph_fy_root"
        if root_marker.exists():
            try:
                saved_root = Path(root_marker.read_text(encoding="utf-8-sig").strip())
                if saved_root.is_absolute():
                    # #2603: the marker holds the SCAN root, but stored
                    # source_file values are relative to the BUILD's cwd
                    # (the skill builds from the repo root scoped to a
                    # subfolder). Trusting the marker blindly re-anchors
                    # "src/mod.py" under src/, doubling the path; every
                    # unchanged source is then judged deleted and evicted,
                    # collapsing the graph. Only adopt an anchor the stored
                    # paths actually resolve under; when none does, keep the
                    # marker (previous behavior) so a fully-deleted corpus
                    # still evicts.
                    resolved = saved_root.resolve()
                    if self._anchors_stored_sources(existing, resolved):
                        self.existing_source_root = resolved
                    else:
                        # In the #2603 hook path watch_path is the absolute
                        # marker, so project_root == watch_root == the bad
                        # marker and the first candidate also fails to anchor;
                        # Path.cwd() (the repo root a post-commit hook runs from)
                        # is the load-bearing rescue candidate here. Keep both so
                        # a relative-watch_path invocation is also covered.
                        for candidate in (self.project_root, Path.cwd().resolve()):
                            if self._anchors_stored_sources(existing, candidate):
                                self.existing_source_root = candidate
                                break
                        else:
                            self.existing_source_root = resolved
                else:
                    invocation_root = Path.cwd().resolve()
                    if (invocation_root / saved_root).resolve() == watch_root:
                        self.existing_source_root = invocation_root
                        relative_marker_prefix = posixpath.normpath(saved_root.as_posix())
            except (OSError, ValueError):
                pass

        self.legacy_watch_relative = False
        if relative_marker_prefix not in (None, "."):
            has_project_relative_source = False
            for bucket in ("nodes", "links", "edges", "hyperedges"):
                for item in existing.get(bucket, []):
                    stored = normalize_source(item.get("source_file"))
                    if not stored or Path(stored).is_absolute():
                        continue
                    normalized = posixpath.normpath(stored)
                    if (
                        normalized == relative_marker_prefix
                        or normalized.startswith(relative_marker_prefix + "/")
                    ):
                        has_project_relative_source = True
                        break
                if has_project_relative_source:
                    break
            self.legacy_watch_relative = not has_project_relative_source

    def _anchors_stored_sources(self, existing: dict, root: Path, sample: int = 25) -> bool:
        """Whether stored relative source_file paths resolve under ``root``.

        Samples the first ``sample`` relative entries: the first hit accepts
        the anchor; ``sample`` consecutive misses reject it. A graph with no
        relative sources returns True (any anchor is harmless there). The
        bound keeps the check O(sample) on large graphs; its known limit is a
        commit that deletes ``sample``-plus files whose nodes happen to sort
        first — the anchor then falls back to the marker, matching the
        pre-fix behavior (no worse).
        """
        checked = 0
        for bucket in ("nodes", "links", "edges", "hyperedges"):
            for item in existing.get(bucket, []):
                raw = item.get("source_file") if isinstance(item, dict) else None
                stored = self._normalize_source(raw) if raw else None
                if not stored or is_absolute_any_platform(stored):
                    continue
                checked += 1
                if (root / Path(posixpath.normpath(stored))).exists():
                    return True
                if checked >= sample:
                    return False
        return checked == 0

    def normalize(self, source_file: str | None) -> str | None:
        normalized = self._normalize_source(source_file, str(self.project_root))
        return posixpath.normpath(normalized) if normalized else normalized

    def absolute_identity(self, source_file: str | None, root: Path) -> str | None:
        normalized = self._normalize_source(source_file)
        if not normalized:
            return normalized
        source_path = Path(posixpath.normpath(normalized))
        if not source_path.is_absolute():
            source_path = root / source_path
        return Path(os.path.abspath(source_path)).as_posix()

    def identity(self, source_file: str | None) -> str | None:
        normalized = self._normalize_source(source_file)
        if normalized and not Path(normalized).is_absolute() and self.legacy_watch_relative:
            return self.absolute_identity(normalized, self.watch_root)
        return self.absolute_identity(normalized, self.existing_source_root)

    def in_watch_root(self, source_file: str | None) -> bool:
        identity = self.identity(source_file)
        return bool(identity) and _is_relative_to(Path(identity), self.watch_root)

    def is_evicted(self, item: dict, identities: set[str]) -> bool:
        return self.identity(item.get("source_file")) in identities

    def rebase_preserved(self, item: dict) -> None:
        identity = self.identity(item.get("source_file"))
        if not identity:
            return
        identity_path = Path(identity)
        if not _is_relative_to(identity_path, self.watch_root):
            normalized = self.normalize(item.get("source_file"))
            if normalized:
                item["source_file"] = normalized
            return
        try:
            item["source_file"] = identity_path.relative_to(self.project_root).as_posix()
        except ValueError:
            item["source_file"] = identity


# A source_file that is a URL/virtual scheme (gdoc://, s3://, http://, ...) rather
# than a filesystem path: its on-disk existence is meaningless, so it must never be
# evicted by the disk-absence sweep. Matched with a regex, NOT a literal "://",
# because path normalization on the write side (Path.as_posix) collapses the double
# slash to one — a stored "gdoc://x" reads back as "gdoc:/x" on the next update, and
# a literal "://" check would then miss it and wrongly evict the node (#2051 follow-up).
# The scheme is required to be 2+ chars so a Windows drive letter (C:/...) is not
# misread as a remote source.
_REMOTE_SOURCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+://?")


def _is_remote_source(source_file: str) -> bool:
    return bool(_REMOTE_SOURCE_RE.match(source_file))
