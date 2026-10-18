"""git_history.py — Git Co-Change, Temporal Coupling & Hotspot Engine.

Mines git commit history using numstat, computes pairwise Jaccard / Lift co-change
confidence scores, tracks 30-day code churn & author distribution, and injects
`co_changes_with` edges into the knowledge graph.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import itertools
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable


@dataclass(frozen=True)
class GitCommit:
    """Parsed git commit metadata and file diff stats."""
    commit_hash: str
    author: str
    timestamp: int
    subject: str
    files: set[str]
    insertions: dict[str, int] = field(default_factory=dict)
    deletions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FileChurn:
    """Historical commit count, 30-day churn, and author statistics for a file."""
    file_path: str
    commit_count: int
    churn_30d: int
    additions: int
    deletions: int
    primary_authors: list[str]


@dataclass(frozen=True)
class CochangePair:
    """Temporal co-change metric between a pair of files."""
    file_a: str
    file_b: str
    co_commits: int
    confidence_a_b: float
    confidence_b_a: float
    jaccard: float
    lift: float


def is_git_repo(root: Path | str) -> bool:
    """Check if root is inside a valid git working tree."""
    p = Path(root)
    if (p / ".git").exists():
        return True
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(p),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        return res.returncode == 0 and "true" in (res.stdout or "").strip()
    except Exception:
        return False


def parse_numstat_log(raw_log: str, max_files_per_commit: int = 50) -> list[GitCommit]:
    """Parse git numstat output into structured GitCommit objects.

    Filters out bulk commits touching more than max_files_per_commit files
    (e.g. bulk formatting, dependency lock updates).
    """
    commits: list[GitCommit] = []
    current_commit: dict[str, Any] | None = None

    for line in raw_log.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("COMMIT:"):
            # Finalize previous commit
            if current_commit is not None:
                files_count = len(current_commit["files"])
                if 0 < files_count <= max_files_per_commit:
                    commits.append(
                        GitCommit(
                            commit_hash=current_commit["hash"],
                            author=current_commit["author"],
                            timestamp=current_commit["timestamp"],
                            subject=current_commit["subject"],
                            files=current_commit["files"],
                            insertions=current_commit["insertions"],
                            deletions=current_commit["deletions"],
                        )
                    )

            parts = line[7:].split("|", 3)
            commit_hash = parts[0] if len(parts) > 0 else ""
            author = parts[1] if len(parts) > 1 else ""
            try:
                timestamp = int(parts[2]) if len(parts) > 2 else 0
            except ValueError:
                timestamp = 0
            subject = parts[3] if len(parts) > 3 else ""

            current_commit = {
                "hash": commit_hash,
                "author": author,
                "timestamp": timestamp,
                "subject": subject,
                "files": set(),
                "insertions": {},
                "deletions": {},
            }
        elif current_commit is not None:
            # numstat line: <added>\t<deleted>\t<path>
            parts = line.split("\t")
            if len(parts) >= 3:
                added_str, deleted_str, raw_path = parts[0], parts[1], parts[2]
                # Clean renamed paths e.g. "old => new" or "foo/{bar => baz}/qux"
                clean_path = _normalize_git_path(raw_path)
                if clean_path:
                    current_commit["files"].add(clean_path)
                    try:
                        ins = int(added_str) if added_str != "-" else 0
                        dels = int(deleted_str) if deleted_str != "-" else 0
                    except ValueError:
                        ins, dels = 0, 0
                    current_commit["insertions"][clean_path] = ins
                    current_commit["deletions"][clean_path] = dels

    # Finalize last commit
    if current_commit is not None:
        files_count = len(current_commit["files"])
        if 0 < files_count <= max_files_per_commit:
            commits.append(
                GitCommit(
                    commit_hash=current_commit["hash"],
                    author=current_commit["author"],
                    timestamp=current_commit["timestamp"],
                    subject=current_commit["subject"],
                    files=current_commit["files"],
                    insertions=current_commit["insertions"],
                    deletions=current_commit["deletions"],
                )
            )

    return commits


def _normalize_git_path(raw_path: str) -> str:
    """Normalize git diff path including rename syntaxes."""
    p = raw_path.strip()
    if " => " in p:
        if "{" in p and "}" in p:
            # Path with braces: foo/{old => new}/bar
            p = re.sub(r"\{.*?=> (.*?)\}", r"\1", p)
        else:
            # Full rename: old => new
            p = p.split(" => ")[-1]
    return Path(p).as_posix().lstrip("./")


def extract_git_history(
    root: Path | str,
    max_commits: int = 1000,
    since: str = "6 months ago",
    max_files_per_commit: int = 50,
    timeout_sec: int = 15,
) -> list[GitCommit]:
    """Execute git log to extract commit history with numstat diffs."""
    p = Path(root)
    if not is_git_repo(p):
        return []

    cmd = [
        "git",
        "log",
        "--no-merges",
        "--numstat",
        "--format=COMMIT:%H|%an|%at|%s",
        f"--since={since}",
        f"-n{max_commits}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(p),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
        if proc.returncode != 0 or not proc.stdout:
            return []
        return parse_numstat_log(proc.stdout, max_files_per_commit=max_files_per_commit)
    except Exception:
        return []


def compute_file_churn(
    commits: list[GitCommit],
    days: int = 30,
    reference_timestamp: int | None = None,
) -> dict[str, FileChurn]:
    """Calculate 30-day code churn and author metrics per file."""
    now_ts = reference_timestamp if reference_timestamp is not None else int(time.time())
    cutoff_ts = now_ts - (days * 86400)

    file_commit_counts: Counter[str] = Counter()
    file_churn_30d: Counter[str] = Counter()
    file_additions: Counter[str] = Counter()
    file_deletions: Counter[str] = Counter()
    file_authors: dict[str, Counter[str]] = defaultdict(Counter)

    for c in commits:
        is_recent = c.timestamp >= cutoff_ts
        for f in c.files:
            file_commit_counts[f] += 1
            if c.author:
                file_authors[f][c.author] += 1
            ins = c.insertions.get(f, 0)
            dels = c.deletions.get(f, 0)
            file_additions[f] += ins
            file_deletions[f] += dels
            if is_recent:
                file_churn_30d[f] += ins + dels

    result: dict[str, FileChurn] = {}
    for f, count in file_commit_counts.items():
        top_authors = [author for author, _ in file_authors[f].most_common(3)]
        result[f] = FileChurn(
            file_path=f,
            commit_count=count,
            churn_30d=file_churn_30d[f],
            additions=file_additions[f],
            deletions=file_deletions[f],
            primary_authors=top_authors,
        )

    return result


def compute_cochanges(
    commits: list[GitCommit],
    min_co_commits: int = 3,
    min_confidence: float = 0.4,
    min_lift: float = 1.0,
) -> list[CochangePair]:
    """Compute pairwise temporal co-change metrics (Jaccard and Lift)."""
    if not commits:
        return []

    total_commits = len(commits)
    file_commits: dict[str, set[str]] = defaultdict(set)
    pair_counts: Counter[tuple[str, str]] = Counter()

    for c in commits:
        sorted_files = sorted(c.files)
        for f in sorted_files:
            file_commits[f].add(c.commit_hash)

        # Count file co-occurrence pairs in this commit
        for f1, f2 in itertools.combinations(sorted_files, 2):
            pair_counts[(f1, f2)] += 1

    cochange_pairs: list[CochangePair] = []

    for (f1, f2), co_count in pair_counts.items():
        if co_count < min_co_commits:
            continue

        n_f1 = len(file_commits[f1])
        n_f2 = len(file_commits[f2])
        if n_f1 == 0 or n_f2 == 0:
            continue

        conf_1_2 = round(co_count / n_f1, 3)
        conf_2_1 = round(co_count / n_f2, 3)
        jaccard = round(co_count / (n_f1 + n_f2 - co_count), 3)
        lift = round((co_count * total_commits) / (n_f1 * n_f2), 3)

        if max(conf_1_2, conf_2_1) >= min_confidence and lift >= min_lift:
            cochange_pairs.append(
                CochangePair(
                    file_a=f1,
                    file_b=f2,
                    co_commits=co_count,
                    confidence_a_b=conf_1_2,
                    confidence_b_a=conf_2_1,
                    jaccard=jaccard,
                    lift=lift,
                )
            )

    # Sort descending by Jaccard similarity score
    cochange_pairs.sort(key=lambda p: (p.jaccard, p.co_commits), reverse=True)
    return cochange_pairs


def inject_git_temporal_edges(
    all_nodes: list[dict],
    all_edges: list[dict],
    root: Path,
    file_node_id_fn: Callable[[str], str] | None = None,
    min_co_commits: int = 3,
    min_confidence: float = 0.4,
) -> int:
    """Mine git commit history and inject temporal co-change edges into the graph.

    Also annotates file nodes with churn_30d, commit_count, and primary_authors.
    Returns the count of injected edges.
    """
    if not is_git_repo(root):
        return 0

    commits = extract_git_history(root)
    if not commits:
        return 0

    churn_map = compute_file_churn(commits)
    cochanges = compute_cochanges(
        commits, min_co_commits=min_co_commits, min_confidence=min_confidence
    )

    # Build file node lookup: source_file -> node
    file_nodes: dict[str, dict] = {}
    for node in all_nodes:
        sf = node.get("source_file")
        if sf and (node.get("file_type") == "file" or node.get("type") == "file"):
            clean_sf = Path(sf).as_posix()
            file_nodes[clean_sf] = node

    # Annotate file nodes with churn and author statistics
    for sf, churn in churn_map.items():
        if sf in file_nodes:
            n = file_nodes[sf]
            n["commit_count"] = churn.commit_count
            n["churn_30d"] = churn.churn_30d
            n["primary_authors"] = churn.primary_authors

    injected_count = 0
    existing_edges = {
        (e.get("source"), e.get("target"), e.get("relation"))
        for e in all_edges
    }

    for pair in cochanges:
        node_a = file_nodes.get(pair.file_a)
        node_b = file_nodes.get(pair.file_b)

        if node_a and node_b:
            id_a = str(node_a.get("id"))
            id_b = str(node_b.get("id"))

            if id_a and id_b and id_a != id_b:
                edge_key = (id_a, id_b, "co_changes_with")
                edge_rev = (id_b, id_a, "co_changes_with")
                if edge_key not in existing_edges and edge_rev not in existing_edges:
                    all_edges.append({
                        "source": id_a,
                        "target": id_b,
                        "relation": "co_changes_with",
                        "confidence": "INFERRED",
                        "weight": pair.jaccard,
                        "context": "git_cochange",
                        "metadata": {
                            "co_commits": pair.co_commits,
                            "confidence": max(pair.confidence_a_b, pair.confidence_b_a),
                            "lift": pair.lift,
                            "jaccard": pair.jaccard,
                        },
                        "_origin": "git",
                    })
                    existing_edges.add(edge_key)
                    injected_count += 1

    return injected_count
