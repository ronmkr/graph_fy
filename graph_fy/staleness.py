"""staleness.py — Automatic Pre-Flight Mtime Staleness Detection & Query-Time Hot-Patching.

Detects uncommitted code changes and working tree drift between on-disk files and
the indexed graph (graph.db / graph.json). Enables query-time hot-patching of
modified files in <200ms without triggering expensive whole-graph rebuilds.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Any

import networkx as nx

# File extensions recognized as extractable code and config assets
_CODE_EXTENSIONS = frozenset({
    ".py", ".rs", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".c", ".cpp",
    ".cc", ".cxx", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".kts",
    ".scala", ".sh", ".bash", ".sql", ".tf", ".proto", ".json", ".yaml", ".yml",
    ".md", ".markdown", ".vue", ".svelte", ".dart", ".zig", ".lua",
})

_EXCLUDED_DIRS = frozenset({
    ".git", "graph_fy_out", "node_modules", "target", "build", "dist",
    "__pycache__", ".venv", "venv", ".idea", ".vscode",
})


def _is_relevant_code_file(path_str: str) -> bool:
    """Return True if path represents an extractable source file and is not in excluded dirs."""
    p = Path(path_str)
    parts = set(p.parts)
    if parts.intersection(_EXCLUDED_DIRS):
        return False
    return p.suffix.lower() in _CODE_EXTENSIONS


def check_graph_staleness(
    graph_path: str | Path | None = None,
    db_path: str | Path | None = None,
    project_root: str | Path = ".",
    manifest_path: str | Path | None = None,
    hotpatch_threshold: int = 10,
) -> dict[str, Any]:
    """Check if the knowledge graph is stale compared to the current working tree.

    Returns:
        dict with:
            - is_stale: bool
            - modified_files: list of modified/added source file paths
            - deleted_files: list of deleted source file paths
            - staleness_count: total count of drifted files
            - action: 'clean' | 'hotpatch' | 'warn'
            - warning_banner: Optional markdown alert string if action == 'warn'
    """
    root_path = Path(project_root).resolve()

    # Resolve graph artifact locations
    if graph_path is None:
        if (root_path / "graph_fy_out" / "graph.json").exists():
            graph_path = root_path / "graph_fy_out" / "graph.json"
        elif (root_path / "graph.json").exists():
            graph_path = root_path / "graph.json"

    if db_path is None:
        if graph_path is not None and (Path(graph_path).parent / "graph.db").exists():
            db_path = Path(graph_path).parent / "graph.db"
        elif (root_path / "graph_fy_out" / "graph.db").exists():
            db_path = root_path / "graph_fy_out" / "graph.db"
        elif (root_path / "graph.db").exists():
            db_path = root_path / "graph.db"

    if manifest_path is None:
        if graph_path is not None and (Path(graph_path).parent / "manifest.json").exists():
            manifest_path = Path(graph_path).parent / "manifest.json"
        elif (root_path / "graph_fy_out" / "manifest.json").exists():
            manifest_path = root_path / "graph_fy_out" / "manifest.json"

    graph_file = Path(graph_path) if graph_path and Path(graph_path).exists() else (
        Path(db_path) if db_path and Path(db_path).exists() else None
    )

    if not graph_file:
        return {
            "is_stale": False,
            "modified_files": [],
            "deleted_files": [],
            "staleness_count": 0,
            "action": "clean",
            "warning_banner": None,
            "reason": "no_graph_artifact",
        }

    graph_mtime = graph_file.stat().st_mtime
    modified_files: set[str] = set()
    deleted_files: set[str] = set()

    # 1. Fast Git status check (<15ms) if working in a git repository
    is_git = (root_path / ".git").exists()
    git_success = False

    if is_git:
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(root_path),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
            )
            if res.returncode == 0:
                git_success = True
                for raw_line in res.stdout.splitlines():
                    if len(raw_line) < 4:
                        continue
                    xy = raw_line[:2]
                    path_str = raw_line[3:].strip()
                    if " -> " in path_str:
                        old_p, path_str = path_str.split(" -> ", 1)
                        if _is_relevant_code_file(old_p):
                            deleted_files.add(old_p)

                    if not _is_relevant_code_file(path_str):
                        continue

                    if "D" in xy:
                        deleted_files.add(path_str)
                    else:
                        full_p = root_path / path_str
                        if full_p.exists() and full_p.is_file():
                            # Stale if uncommitted and modified since graph creation (or racy within 1s)
                            if full_p.stat().st_mtime >= graph_mtime - 1.0 or xy.strip() != "":
                                modified_files.add(path_str)
        except Exception:
            git_success = False

    # 2. Fallback to manifest.json or SQLite file_stats if git is unavailable or failed
    if not git_success:
        if manifest_path and Path(manifest_path).exists():
            from graph_fy.manifest import load_manifest
            manifest_data = load_manifest(str(manifest_path), root=root_path)
            for f_key, meta in manifest_data.items():
                p = Path(f_key)
                if not p.is_absolute():
                    p = root_path / p
                rel = str(p.relative_to(root_path)) if p.is_relative_to(root_path) else str(p)
                if not _is_relevant_code_file(rel):
                    continue
                if not p.exists():
                    deleted_files.add(rel)
                else:
                    try:
                        cur_mtime = p.stat().st_mtime
                        stored_mtime = meta.get("mtime", 0) if isinstance(meta, dict) else float(meta)
                        if cur_mtime > stored_mtime + 0.05:
                            modified_files.add(rel)
                    except OSError:
                        pass
        elif db_path and Path(db_path).exists():
            from graph_fy.storage_sqlite import SqliteGraphStore
            try:
                with SqliteGraphStore(db_path) as store:
                    stats = store.get_file_stats()
                    for f_key, meta in stats.items():
                        p = Path(f_key)
                        if not p.is_absolute():
                            p = root_path / p
                        rel = str(p.relative_to(root_path)) if p.is_relative_to(root_path) else str(p)
                        if not _is_relevant_code_file(rel):
                            continue
                        if not p.exists():
                            deleted_files.add(rel)
                        else:
                            try:
                                cur_mtime_ns = p.stat().st_mtime_ns
                                stored_ns = meta.get("mtime_ns", 0)
                                if cur_mtime_ns > stored_ns:
                                    modified_files.add(rel)
                            except OSError:
                                pass
            except Exception:
                pass

    # Normalize list and count
    mod_list = sorted(modified_files)
    del_list = sorted(deleted_files)
    staleness_count = len(mod_list) + len(del_list)
    is_stale = staleness_count > 0

    if staleness_count == 0:
        action = "clean"
        warning = None
    elif staleness_count <= hotpatch_threshold:
        action = "hotpatch"
        warning = None
    else:
        action = "warn"
        warning = f"[GRAPH STALE: {staleness_count} files modified since last index. Run 'graph_fy update .']"

    return {
        "is_stale": is_stale,
        "modified_files": mod_list,
        "deleted_files": del_list,
        "staleness_count": staleness_count,
        "action": action,
        "warning_banner": warning,
    }


def hot_patch_graph(
    graph: nx.Graph | None = None,
    db_path: str | Path | None = None,
    modified_files: list[str | Path] | None = None,
    deleted_files: list[str | Path] | None = None,
    project_root: str | Path = ".",
) -> dict[str, Any]:
    """Incrementally hot-patch graph in-memory and/or in SQLite for modified files in <200ms."""
    root_path = Path(project_root).resolve()
    mod_paths: list[Path] = []
    for f in (modified_files or []):
        p = Path(f) if Path(f).is_absolute() else (root_path / f)
        if p.exists() and p.is_file():
            mod_paths.append(p)

    del_paths = [str(f) for f in (deleted_files or [])]

    if not mod_paths and not del_paths:
        return {"patched_files": 0, "deleted_files": 0, "nodes_added": 0, "edges_added": 0}

    t0 = time.perf_counter()
    new_nodes: list[dict] = []
    new_edges: list[dict] = []

    if mod_paths:
        from graph_fy.extract import extract
        try:
            extracted = extract(mod_paths, root=root_path, parallel=False)
            new_nodes = extracted.get("nodes", [])
            new_edges = extracted.get("edges", [])
        except Exception:
            new_nodes = []
            new_edges = []

    # 1. Patch NetworkX in-memory graph
    nodes_removed = 0
    if graph is not None:
        target_keys = set()
        for p in mod_paths:
            target_keys.add(str(p))
            try:
                target_keys.add(str(p.relative_to(root_path)))
            except ValueError:
                pass
            target_keys.add(p.name)

        for d in del_paths:
            target_keys.add(str(d))
            target_keys.add(Path(d).name)

        to_remove = []
        for n, data in graph.nodes(data=True):
            sf = data.get("source_file") or data.get("source_path") or data.get("file")
            if sf:
                sf_str = str(sf)
                if sf_str in target_keys or Path(sf_str).name in target_keys:
                    to_remove.append(n)

        if to_remove:
            graph.remove_nodes_from(to_remove)
            nodes_removed = len(to_remove)

        for n in new_nodes:
            nid = n.get("id")
            if nid:
                graph.add_node(nid, **n)

        for e in new_edges:
            u = e.get("source")
            v = e.get("target")
            if u and v:
                graph.add_edge(u, v, **e)

    # 2. Patch SQLite database store
    if db_path is not None and Path(db_path).exists():
        from graph_fy.storage_sqlite import SqliteGraphStore
        try:
            with SqliteGraphStore(db_path) as store:
                for del_f in del_paths:
                    store.remove_file(del_f)

                for p in mod_paths:
                    rel_p = str(p.relative_to(root_path)) if p.is_relative_to(root_path) else str(p)
                    # Filter nodes belonging to this file
                    file_nodes = [
                        n for n in new_nodes
                        if n.get("source_file") in (str(p), rel_p, p.name)
                    ]
                    file_node_ids = {n["id"] for n in file_nodes if "id" in n}
                    file_edges = [
                        e for e in new_edges
                        if e.get("source") in file_node_ids or e.get("target") in file_node_ids
                    ]
                    st = p.stat()
                    store.hot_patch_file(
                        rel_p,
                        file_nodes,
                        file_edges,
                        stat=(st.st_size, st.st_mtime_ns, None),
                    )
        except Exception:
            pass

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "patched_files": len(mod_paths),
        "deleted_files": len(del_paths),
        "nodes_added": len(new_nodes),
        "edges_added": len(new_edges),
        "nodes_removed": nodes_removed,
        "duration_ms": duration_ms,
    }
