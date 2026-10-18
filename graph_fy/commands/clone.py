"""Clone repository command implementation for graph_fy CLI."""
from __future__ import annotations

import re as _re
import subprocess as _sp
import sys
from pathlib import Path


def _clone_repo(
    url: str, branch: str | None = None, out_dir: Path | None = None
) -> Path:
    """Clone a GitHub repo to a local cache dir and return the path.

    Clones into ~/.graph_fy/repos/<owner>/<repo> by default so repeated
    runs on the same URL reuse the existing clone (git pull instead of clone).
    """
    # Normalise URL — strip trailing .git if present
    url = url.rstrip("/")
    if not url.endswith(".git"):
        git_url = url + ".git"
    else:
        git_url = url
        url = url[:-4]

    # Extract owner/repo from URL
    m = _re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        print(f"error: not a recognised GitHub URL: {url}", file=sys.stderr)
        sys.exit(1)
    owner, repo = m.group(1), m.group(2)

    if out_dir:
        dest = out_dir
    else:
        dest = Path.home() / ".graph_fy" / "repos" / owner / repo

    if branch and branch.startswith("-"):
        print(f"error: invalid branch name: {branch!r}", file=sys.stderr)
        sys.exit(1)

    if dest.exists():
        print(f"Repo already cloned at {dest} - pulling latest...", flush=True)
        cmd = ["git", "-C", str(dest), "pull"]
        if branch:
            cmd += ["origin", "--", branch]
        result = _sp.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"warning: git pull failed:\n{result.stderr}", file=sys.stderr)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {url} -> {dest} ...", flush=True)
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd += ["--branch", branch]
        cmd += ["--", git_url, str(dest)]
        result = _sp.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"error: git clone failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)

    print(f"Ready at: {dest}", flush=True)
    return dest


def cmd_clone(argv: list[str]) -> int:
    """Handle `graph_fy clone` command."""
    if len(argv) < 3:
        print(
            "Usage: graph_fy clone <github-url> [--branch <branch>] [--out <dir>]",
            file=sys.stderr,
        )
        return 1
    url = argv[2]
    branch: str | None = None
    out_dir: Path | None = None
    args = argv[3:]
    i = 0
    while i < len(args):
        if args[i] == "--branch" and i + 1 < len(args):
            branch = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out_dir = Path(args[i + 1])
            i += 2
        else:
            i += 1
    local_path = _clone_repo(url, branch=branch, out_dir=out_dir)
    print(local_path)
    return 0
