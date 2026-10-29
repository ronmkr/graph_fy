"""Tests for git worktree pruning in detect (#4683, #4833 / #69)."""
from pathlib import Path
import subprocess
import pytest

from graph_fy.detect import detect, FileType


def test_detect_prunes_linked_git_worktree_inside_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)

    main_py = repo / "main.py"
    main_py.write_text("def hello(): pass\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True)

    wt = repo / "wt"
    subprocess.run(["git", "worktree", "add", str(wt)], cwd=repo, capture_output=True, check=True)

    result = detect(repo)
    code_files = [Path(p).resolve() for p in result["files"][FileType.CODE]]

    assert main_py.resolve() in code_files
    # wt/main.py must NOT be indexed as a second duplicate file
    assert (wt / "main.py").resolve() not in code_files


def test_detect_prunes_sibling_worktree_when_scanning_parent(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)

    main_py = repo / "main.py"
    main_py.write_text("def hello(): pass\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True)

    sibling_wt = tmp_path / "sibling_wt"
    subprocess.run(["git", "worktree", "add", str(sibling_wt)], cwd=repo, capture_output=True, check=True)

    result = detect(tmp_path)
    code_files = [Path(p).resolve() for p in result["files"][FileType.CODE]]

    assert main_py.resolve() in code_files
    assert (sibling_wt / "main.py").resolve() not in code_files


def test_detect_preserves_worktree_when_scanning_worktree_directly(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)

    main_py = repo / "main.py"
    main_py.write_text("def hello(): pass\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True)

    wt = tmp_path / "wt_direct"
    subprocess.run(["git", "worktree", "add", str(wt)], cwd=repo, capture_output=True, check=True)

    # When scanning wt directly, its files should be found
    result = detect(wt)
    code_files = [Path(p).resolve() for p in result["files"][FileType.CODE]]
    assert (wt / "main.py").resolve() in code_files
