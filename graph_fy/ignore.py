"""Ignore pattern matching, VCS discovery, and noise directory pruning.

Implements gitignore semantics (including globstar, negation, parent exclusion,
last-match-wins) and heuristics for pruning compiler artifacts, coverage reports,
virtual environments, and noisy directories.
"""

from __future__ import annotations

import fnmatch
import locale
import os
import re
import stat
import subprocess
import sys as _sys
import unicodedata
from pathlib import Path
from typing import Callable


def _is_regular_file(path: Path) -> bool:
    """True only for regular files (symlinks followed).

    Named pipes, sockets and device nodes must never reach a reader:
    ``open()`` on a FIFO with no writer blocks forever and never raises, so a
    single ``pipe.py`` in a scanned repository hangs the run with no output.
    A symlink is resolved deliberately — a link pointing at a FIFO blocks
    exactly like the FIFO itself. A path that cannot be stat'ed is treated as
    not readable rather than raising.
    """
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


# Directory names to always skip - venvs, caches, build artifacts, deps
_SKIP_DIRS = {
    "venv", ".venv",  # "env"/".env"/"*_env" are gated on venv markers below (#2058)
    "node_modules", "__pycache__", ".git",
    "dist", "build", "target",  # bare "out" is gated on build-output evidence below (#3347)
    "site-packages", "lib64",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".eggs", "*.egg-info",  # nox is tox's successor, same .nox/ venv shape (#1804)
    "graph_fy_out", "graph_fy_out",  # never treat the default output as source input (#524)
    # Coverage/test-artefact dirs — generated, never architecturally meaningful
    "lcov-report",                          # Vitest/Istanbul/nyc HTML reports (#870);
                                            # bare "coverage" is gated on report
                                            # artefacts below (#2339)
    "visual-tests", "visual-test",          # Playwright/visual-regression bundles (#869)
    "__snapshots__",                        # Jest/Vitest snapshot dir (unambiguous)
    "storybook-static",                     # Storybook production build output
    "dist-protected",                       # Protected dist variants (same noise as dist)
    # Framework cache/build dirs — generated, never architecturally meaningful (#873)
    ".next", ".nuxt", ".turbo", ".angular",
    ".idea", ".cache", ".parcel-cache", ".svelte-kit", ".terraform", ".serverless",
    ".graph_fy",  # graph_fy's own extraction cache — never index self-generated data
    ".obsidian", ".smart-env",  # Obsidian vault metadata and plugin caches (#2493)
    ".worktrees",  # git worktree convention (#947) — sibling checkouts, always redundant
}

# Large generated files that are never useful to extract
_SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "poetry.lock", "Gemfile.lock",
    "composer.lock", "go.sum", "go.work.sum",
    # Removed allowlist config (#2112) — no longer consumed, so keep a leftover
    # file out of the unclassified list instead of surfacing it as scan input.
    ".graph_fyinclude",
}

# A bare "snapshots" dir is a Jest/Vitest artifact only when it actually holds
# snapshot files or lives directly under a JS test root. Elsewhere it is often a
# real code namespace (e.g. Rails app/services/snapshots/), so pruning it by name
# silently dropped legitimate source from the graph (#1666). "__snapshots__" stays
# unconditionally pruned above; only the ambiguous bare name is gated here.
_JS_SNAPSHOT_TEST_ROOTS = frozenset({"__tests__", "__test__"})

# Files a coverage tool writes into its own output dir. Any one of them is proof
# the directory is generated: lcov (lcov.info), nyc/Istanbul (coverage-final.json,
# clover.xml, the lcov-report/ subtree), coverage.py (coverage.xml, .coverage),
# JaCoCo/Cobertura (jacoco.xml, cobertura-coverage.xml).
_COVERAGE_ARTIFACT_FILES = frozenset({
    "lcov.info", "coverage-final.json", "coverage-summary.json",
    "clover.xml", "coverage.xml", "cobertura-coverage.xml", "jacoco.xml",
    ".coverage", "index.html",
})
_COVERAGE_ARTIFACT_DIRS = frozenset({"lcov-report", "html-report"})


def _has_coverage_artifacts(d: "Path") -> bool:
    """True only when *d* holds files a coverage tool actually generated."""
    try:
        for name in _COVERAGE_ARTIFACT_FILES:
            if (d / name).is_file():
                return True
        for name in _COVERAGE_ARTIFACT_DIRS:
            if (d / name).is_dir():
                return True
    except OSError:
        pass
    return False


# Files only a compiler/bundler writes. Any one of them inside an ``out/``
# tree is proof the directory is generated output rather than source.
_BUILD_OUTPUT_SUFFIXES = (
    ".class", ".jar", ".o", ".obj", ".exe", ".dll", ".pyc",
    ".js.map", ".css.map", ".d.ts",
)


def _has_build_output_markers(d: "Path") -> bool:
    """True only when *d* holds evidence a build tool generated it."""
    try:
        if (d / "production").is_dir():  # IntelliJ IDEA compile output layout
            return True
        if (d / "_next").is_dir():      # `next export` static-site output
            return True
        subdirs: list = []
        with os.scandir(d) as it:
            for entry in it:
                if entry.is_file() and entry.name.lower().endswith(_BUILD_OUTPUT_SUFFIXES):
                    return True
                if entry.is_dir():
                    subdirs.append(entry.path)
        budget = 4000
        for sub in subdirs:
            if budget <= 0:
                break
            with os.scandir(sub) as it:
                for entry in it:
                    budget -= 1
                    if entry.is_file() and entry.name.lower().endswith(_BUILD_OUTPUT_SUFFIXES):
                        return True
                    if budget <= 0:
                        break
    except OSError:
        pass
    return False


def _has_venv_markers(d: "Path") -> bool:
    """True only when *d* has actual virtualenv/conda structure on disk."""
    try:
        if (d / "pyvenv.cfg").is_file():
            return True
        if (d / "bin" / "activate").is_file() or (d / "Scripts" / "activate").is_file():
            return True
        if next(d.glob("lib/python*"), None) is not None:
            return True
        if (d / "conda-meta").is_dir():
            return True
    except OSError:
        pass
    return False


def _is_noise_dir(part: str, parent: "Path | None" = None) -> bool:
    """Return True if this directory name looks like a venv, cache, or dep dir."""
    if part in _SKIP_DIRS:
        return True
    if part in ("env", ".env") or part.endswith("_env"):
        if parent is None:
            return False
        return _has_venv_markers(parent / part)
    if part == "coverage":
        if parent is None:
            return False
        return _has_coverage_artifacts(parent / part)
    if part == "out":
        if parent is None:
            return False
        return _has_build_output_markers(parent / part)
    if part == "snapshots":
        if parent is None:
            return False
        snap_dir = parent / part
        if parent.name in _JS_SNAPSHOT_TEST_ROOTS:
            return True
        try:
            if next(snap_dir.glob("*.snap"), None) is not None:
                return True
        except OSError:
            pass
        return False
    if part.endswith("_venv"):
        return True
    if part.endswith(".egg-info"):
        return True
    if part == "worktrees" and parent is not None and parent.name.startswith("."):
        return True
    return False


_VCS_MARKERS = (".git", ".hg", ".svn", "_darcs", ".fossil")


def _nfc(text: str) -> str:
    """Normalize text to NFC so ignore matching survives Unicode form drift."""
    return unicodedata.normalize("NFC", text)


def _parse_gitignore_line(raw: str) -> str:
    """Parse one raw line from a .graph_fyignore file per gitignore spec."""
    line = raw.rstrip("\n\r")
    line = line.lstrip()
    if not line or line.startswith("#"):
        return ""
    line = re.sub(r"\s+#+[^\\].*$", "", line)
    line = line.replace("\\#", "#")
    line = re.sub(r"(?<!\\) +$", "", line)
    return _nfc(line)


def _is_valid_git_marker(dot_git: Path, is_home: bool = False) -> bool:
    """Whether ``dot_git`` is a valid git marker (#4444)."""
    try:
        if dot_git.is_dir():
            if not is_home:
                return True
            return (dot_git / "HEAD").is_file() and (dot_git / "objects").is_dir() and (dot_git / "refs").is_dir()
        if dot_git.is_file():
            with dot_git.open("rb") as fh:
                return fh.read(7) == b"gitdir:"
    except OSError:
        pass
    return False


def _has_vcs_marker(directory: Path, is_home: bool = False) -> bool:
    for marker in _VCS_MARKERS:
        path = directory / marker
        if marker == ".git":
            if _is_valid_git_marker(path, is_home=is_home):
                return True
        elif path.exists():
            return True
    return False


def _find_vcs_root(start: Path) -> Path | None:
    """Walk upward from start; return the first directory containing a VCS marker."""
    current = start.resolve()
    home = Path.home()
    while True:
        if _has_vcs_marker(current, is_home=(current == home)):
            return current
        parent = current.parent
        if parent == current or current == home:
            return None
        current = parent


def _path_identity(path: Path) -> str:
    """Portable comparison key for an existing filesystem path."""
    return _nfc(os.path.normcase(os.path.abspath(os.fspath(path))))


def _git_tracked_path_keys(root: Path) -> tuple[set[str], set[str]]:
    """Return tracked-file keys and their ancestor-directory keys under *root*."""
    root = root.resolve()
    vcs_root = _find_vcs_root(root)
    if vcs_root is None or not (vcs_root / ".git").exists():
        return set(), set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(vcs_root), "ls-files", "-z", "--cached"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return set(), set()
    if proc.returncode != 0:
        return set(), set()

    tracked_files: set[str] = set()
    tracked_dirs: set[str] = set()
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        path = Path(os.path.abspath(vcs_root / os.fsdecode(raw)))
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not _is_regular_file(path):
            continue
        tracked_files.add(_path_identity(path))
        parent = path.parent
        while parent != root:
            parent_key = _path_identity(parent)
            if parent_key in tracked_dirs:
                break
            tracked_dirs.add(parent_key)
            parent = parent.parent
    return tracked_files, tracked_dirs


def _git_info_exclude(vcs_root: Path) -> Path | None:
    """Resolve ``$GIT_DIR/info/exclude`` for the repo rooted at ``vcs_root``."""
    dot_git = vcs_root / ".git"
    git_dir: Path | None = None
    if dot_git.is_dir():
        git_dir = dot_git
    elif dot_git.is_file():
        try:
            content = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            content = ""
        if content.startswith("gitdir:"):
            gd = Path(content[len("gitdir:"):].strip())
            if not gd.is_absolute():
                gd = (vcs_root / gd).resolve()
            git_dir = gd
            commondir = gd / "commondir"
            if commondir.exists():
                try:
                    cd_raw = commondir.read_text(encoding="utf-8", errors="ignore").strip()
                except OSError:
                    cd_raw = ""
                if cd_raw:
                    cd = Path(cd_raw)
                    git_dir = cd if cd.is_absolute() else (gd / cd).resolve()
    if git_dir is None:
        return None
    exclude = git_dir / "info" / "exclude"
    return exclude if exclude.is_file() else None


_warned_ignore_encodings: set[str] = set()


def _read_ignore_text(path: Path) -> str:
    """Read an ignore file, preferring UTF-8 but never silently dropping a rule."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    fallback = locale.getpreferredencoding(False) or "latin-1"
    for enc in (fallback, "latin-1"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        key = str(path)
        if key not in _warned_ignore_encodings:
            _warned_ignore_encodings.add(key)
            print(
                f"[graph_fy] WARNING: {path} is not valid UTF-8; read it as "
                f"{enc} instead. Re-save it as UTF-8 — patterns with non-ASCII "
                "characters may not match as written.",
                file=_sys.stderr,
            )
        return text
    return raw.decode("utf-8", errors="ignore")


def _load_dir_own_ignore(d: Path, *, gitignore: bool = True) -> list[tuple[Path, str]]:
    """Read .gitignore/.graph_fyignore directly inside *d* (not its ancestors)."""
    patterns: list[tuple[Path, str]] = []
    for fname in ((".gitignore", ".graph_fyignore") if gitignore else (".graph_fyignore",)):
        ignore_file = d / fname
        if ignore_file.exists():
            for raw in _read_ignore_text(ignore_file).splitlines():
                line = _parse_gitignore_line(raw)
                if line:
                    patterns.append((d, line))
    return patterns


def _load_graph_fyignore(root: Path, *, gitignore: bool = True) -> list[tuple[Path, str]]:
    """Read .graph_fyignore files and return (anchor_dir, pattern) pairs."""
    root = root.resolve()
    ceiling = _find_vcs_root(root) or root

    dirs: list[Path] = []
    current = root
    while True:
        dirs.append(current)
        if current == ceiling:
            break
        current = current.parent
    dirs.reverse()

    patterns: list[tuple[Path, str]] = []
    info_exclude = _git_info_exclude(ceiling) if gitignore else None
    if info_exclude is not None:
        for raw in _read_ignore_text(info_exclude).splitlines():
            line = _parse_gitignore_line(raw)
            if line:
                patterns.append((ceiling, line))

    for d in dirs:
        patterns.extend(_load_dir_own_ignore(d, gitignore=gitignore))
    return patterns


_PARSED_PATTERN_CACHE: dict[str, tuple[bool, bool, bool, str]] = {}
_PARSED_PATTERN_CACHE_MAX = 100_000


def _parse_ignore_pattern(pattern: str) -> tuple[bool, bool, bool, str]:
    """Split one gitignore-style pattern into its matching flags, cached."""
    got = _PARSED_PATTERN_CACHE.get(pattern)
    if got is None:
        negated = pattern.startswith("!")
        raw = pattern[1:] if negated else pattern
        directory_only = raw.endswith("/")
        path_relative = "/" in raw.rstrip("/")
        got = (negated, directory_only, path_relative, raw.strip("/"))
        if len(_PARSED_PATTERN_CACHE) >= _PARSED_PATTERN_CACHE_MAX:
            _PARSED_PATTERN_CACHE.clear()
        _PARSED_PATTERN_CACHE[pattern] = got
    return got


_CASEFOLD_PATHS = os.path.normcase("Aa") != "Aa"


def _lexical_relative(
    target: Path, target_parts: tuple[str, ...], anchor: Path
) -> str | None:
    """String-space equivalent of ``_nfc(str(target.relative_to(anchor)).replace(os.sep, "/"))``."""
    anchor_parts = anchor.parts
    if not anchor_parts:
        try:
            return _nfc(str(target.relative_to(anchor)).replace(os.sep, "/"))
        except ValueError:
            return None
    n = len(anchor_parts)
    if len(target_parts) < n:
        return None
    head = target_parts[:n]
    if head != anchor_parts and (
        not _CASEFOLD_PATHS
        or tuple(map(os.path.normcase, head))
        != tuple(map(os.path.normcase, anchor_parts))
    ):
        return None
    tail = target_parts[n:]
    if not tail:
        return "."
    return _nfc("/".join(tail))


def _match_globstar_parts(
    path_parts: tuple[str, ...],
    pattern_parts: tuple[str, ...],
    path_idx: int,
    pattern_idx: int,
    memo: dict[tuple[int, int], bool],
) -> bool:
    """Recursive ``**``-aware segment match, memoized via an explicit dict."""
    key = (path_idx, pattern_idx)
    cached = memo.get(key)
    if cached is not None:
        return cached

    if pattern_idx == len(pattern_parts):
        result = path_idx == len(path_parts)
    else:
        part = pattern_parts[pattern_idx]
        if part == "**":
            if pattern_idx == len(pattern_parts) - 1:
                result = path_idx < len(path_parts)
            else:
                result = _match_globstar_parts(
                    path_parts, pattern_parts, path_idx, pattern_idx + 1, memo
                ) or (
                    path_idx < len(path_parts)
                    and _match_globstar_parts(
                        path_parts, pattern_parts, path_idx + 1, pattern_idx, memo
                    )
                )
        else:
            result = (
                path_idx < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_idx], part)
                and _match_globstar_parts(
                    path_parts, pattern_parts, path_idx + 1, pattern_idx + 1, memo
                )
            )
    memo[key] = result
    return result


def _match_anchored_ignore_pattern(path: str, pattern: str) -> bool:
    """Match an anchored gitignore pattern without letting ``*`` cross ``/``."""
    path_parts = tuple(path.split("/"))
    pattern_parts = tuple(pattern.split("/"))
    if "**" not in pattern_parts:
        if len(path_parts) != len(pattern_parts):
            return False
        return all(
            fnmatch.fnmatchcase(pp, qp) for pp, qp in zip(path_parts, pattern_parts)
        )
    return _match_globstar_parts(path_parts, pattern_parts, 0, 0, {})


def _is_ignored(
    path: Path,
    root: Path,
    patterns: list[tuple[Path, str]],
    *,
    _cache: dict[Path, bool] | None = None,
) -> bool:
    """Return True if the path should be ignored per .graph_fyignore patterns."""
    if not patterns:
        return False

    root_nparts = len(root.parts)

    def _eval(target: Path) -> bool:
        if _cache is not None and target in _cache:
            return _cache[target]

        target_parts = target.parts
        target_name_nfc: str | None = None
        target_is_dir: bool | None = None
        rel_root_known = False
        rel_root: str | None = None
        rel_by_anchor: dict[Path, tuple[str | None, int]] = {}
        segs_by_rel: dict[str, tuple[list[str], list[str]]] = {}

        def _segments(rel: str) -> tuple[list[str], list[str]]:
            got = segs_by_rel.get(rel)
            if got is None:
                parts = rel.split("/")
                prefixes: list[str] = []
                acc = ""
                for part in parts:
                    acc = part if not acc else acc + "/" + part
                    prefixes.append(acc)
                got = (parts, prefixes)
                segs_by_rel[rel] = got
            return got

        def _matches(rel: str, p: str, path_relative: bool) -> bool:
            nonlocal target_name_nfc
            if path_relative:
                return _match_anchored_ignore_pattern(rel, p)
            if fnmatch.fnmatch(rel, p):
                return True
            if target_name_nfc is None:
                target_name_nfc = _nfc(target.name)
            if fnmatch.fnmatch(target_name_nfc, p):
                return True
            parts, prefixes = _segments(rel)
            for part, prefix in zip(parts, prefixes):
                if fnmatch.fnmatch(part, p):
                    return True
                if fnmatch.fnmatch(prefix, p):
                    return True
            return False

        result = False
        for anchor, pattern in patterns:
            negated, directory_only, path_relative, p = _parse_ignore_pattern(pattern)
            if not p:
                continue

            cached_rel = rel_by_anchor.get(anchor)
            if cached_rel is None:
                cached_rel = (
                    _lexical_relative(target, target_parts, anchor),
                    len(anchor.parts),
                )
                rel_by_anchor[anchor] = cached_rel
            rel_anchor, anchor_nparts = cached_rel
            if rel_anchor is None:
                continue
            matched = False
            if rel_anchor != ".":
                rel = rel_anchor
                if not path_relative and root_nparts > anchor_nparts:
                    if not rel_root_known:
                        rel_root_known = True
                        rel_root = _lexical_relative(target, target_parts, root)
                    if rel_root is not None:
                        rel = rel_root
                matched = _matches(rel, p, path_relative=path_relative)
                if matched and directory_only:
                    if target_is_dir is None:
                        target_is_dir = target.is_dir()
                    if not target_is_dir:
                        matched = False

            if matched:
                result = not negated
        if _cache is not None:
            _cache[target] = result
        return result

    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return _eval(path)

    ancestor = root
    for part in rel_parts[:-1]:
        ancestor = ancestor / part
        if _eval(ancestor):
            return True
    return _eval(path)


def _is_scan_ignored(
    path: Path,
    root: Path,
    patterns: list[tuple[Path, str]],
    explicit_patterns: list[tuple[Path, str]],
    tracked_files: set[str],
    tracked_dirs: set[str],
    *,
    cache: dict[Path, bool],
    explicit_cache: dict[Path, bool],
) -> bool:
    """Apply ignore rules while preserving Git-tracked paths."""
    if not _is_ignored(path, root, patterns, _cache=cache):
        return False
    if _is_ignored(path, root, explicit_patterns, _cache=explicit_cache):
        return True
    identity = _path_identity(path)
    return identity not in tracked_files and identity not in tracked_dirs


def ignored_predicate(
    root: Path,
    *,
    extra_excludes: list[str] | None = None,
    gitignore: bool = True,
) -> Callable[[Path], bool]:
    """Build a per-path predicate answering "would detect() exclude this path?"."""
    root = root.resolve()
    patterns = _load_graph_fyignore(root, gitignore=gitignore)
    explicit_patterns = _load_graph_fyignore(root, gitignore=False)
    tracked_files, tracked_dirs = (
        _git_tracked_path_keys(root)
        if gitignore and len(patterns) > len(explicit_patterns)
        else (set(), set())
    )
    if extra_excludes:
        for pat in extra_excludes:
            line = _parse_gitignore_line(pat)
            if line:
                patterns.append((root, line))
                explicit_patterns.append((root, line))
    cache: dict[Path, bool] = {}
    explicit_cache: dict[Path, bool] = {}
    loaded_dirs: set[Path] = {root}

    def _ignored(path: Path) -> bool:
        path = Path(os.path.abspath(path))
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            return False
        if path.name in _SKIP_FILES:
            return True
        parent = root
        for part in rel_parts[:-1]:
            if _is_noise_dir(part, parent):
                return True
            parent = parent / part
        ancestor = root
        for part in rel_parts[:-1]:
            ancestor = ancestor / part
            if ancestor not in loaded_dirs:
                loaded_dirs.add(ancestor)
                patterns.extend(_load_dir_own_ignore(ancestor, gitignore=gitignore))
                explicit_patterns.extend(
                    _load_dir_own_ignore(ancestor, gitignore=False)
                )
        return _is_scan_ignored(
            path,
            root,
            patterns,
            explicit_patterns,
            tracked_files,
            tracked_dirs,
            cache=cache,
            explicit_cache=explicit_cache,
        )

    return _ignored
