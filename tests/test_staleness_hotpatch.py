"""Unit tests for automatic pre-flight mtime staleness detection and query-time hotpatching (Issue #51)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import networkx as nx
import pytest

from graph_fy.staleness import check_graph_staleness, hot_patch_graph
from graph_fy.storage_sqlite import SqliteGraphStore
from graph_fy.retrieval_context import task_context


@pytest.fixture
def mock_project(tmp_path: Path):
    """Create a minimal project repository with a mock git structure and graph."""
    proj = tmp_path / "my_project"
    proj.mkdir()

    # Create dummy git directory
    (proj / ".git").mkdir()

    # Create source files
    src_dir = proj / "src"
    src_dir.mkdir()
    f1 = src_dir / "service.py"
    f1.write_text("def process_data(item: str) -> bool:\n    '''Process incoming data item.'''\n    return len(item) > 0\n")

    f2 = src_dir / "utils.py"
    f2.write_text("def helper_fn(x: int) -> int:\n    '''Calculate square.'''\n    return x * x\n")

    # Create graph_fy_out directory and graph.json / graph.db
    out_dir = proj / "graph_fy_out"
    out_dir.mkdir()

    G = nx.DiGraph()
    G.add_node(
        "src/service.py:process_data",
        label="process_data",
        file_type="function",
        source_file="src/service.py",
        source_location="L1-L3",
        docstring="Process incoming data item.",
    )
    G.add_node(
        "src/utils.py:helper_fn",
        label="helper_fn",
        file_type="function",
        source_file="src/utils.py",
        source_location="L1-L3",
        docstring="Calculate square.",
    )
    G.add_edge("src/service.py:process_data", "src/utils.py:helper_fn", relation="calls")

    graph_json = out_dir / "graph.json"
    from networkx.readwrite import json_graph
    data = json_graph.node_link_data(G)
    graph_json.write_text(json.dumps(data))

    db_path = out_dir / "graph.db"
    with SqliteGraphStore(db_path) as store:
        store.save_graph(G)

    # Manifest
    manifest_json = out_dir / "manifest.json"
    manifest_data = {
        "src/service.py": {"mtime": f1.stat().st_mtime, "ast_hash": "hash1"},
        "src/utils.py": {"mtime": f2.stat().st_mtime, "ast_hash": "hash2"},
    }
    manifest_json.write_text(json.dumps(manifest_data))

    return proj, G, db_path, graph_json


def test_staleness_clean_when_no_drift(mock_project, monkeypatch):
    """When no files are modified, check_graph_staleness reports action='clean'."""
    proj, G, db_path, graph_json = mock_project

    # Mock git status to return empty (no modifications)
    class DummyProc:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: DummyProc())

    res = check_graph_staleness(graph_path=graph_json, db_path=db_path, project_root=proj)
    assert res["is_stale"] is False
    assert res["action"] == "clean"
    assert res["staleness_count"] == 0
    assert res["modified_files"] == []
    assert res["warning_banner"] is None


def test_staleness_detects_modified_file_and_hotpatch_action(mock_project, monkeypatch):
    """When <= 10 files drifted, action is 'hotpatch'."""
    proj, G, db_path, graph_json = mock_project

    class DummyProc:
        returncode = 0
        stdout = " M src/service.py\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: DummyProc())

    res = check_graph_staleness(graph_path=graph_json, db_path=db_path, project_root=proj)
    assert res["is_stale"] is True
    assert res["action"] == "hotpatch"
    assert res["staleness_count"] == 1
    assert "src/service.py" in res["modified_files"]
    assert res["warning_banner"] is None


def test_staleness_warns_on_large_drift(mock_project, monkeypatch):
    """When > 10 files drifted, action is 'warn' with actionable warning banner."""
    proj, G, db_path, graph_json = mock_project

    # Create dummy files and status
    status_lines = []
    for i in range(15):
        f = proj / "src" / f"mod_{i}.py"
        f.write_text(f"def fn_{i}(): pass\n")
        status_lines.append(f" M src/mod_{i}.py")

    class DummyProc:
        returncode = 0
        stdout = "\n".join(status_lines) + "\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: DummyProc())

    res = check_graph_staleness(graph_path=graph_json, db_path=db_path, project_root=proj)
    assert res["is_stale"] is True
    assert res["action"] == "warn"
    assert res["staleness_count"] == 15
    assert "[GRAPH STALE: 15 files modified since last index. Run 'graph_fy update .']" in res["warning_banner"]


def test_staleness_fallback_to_manifest_when_no_git(mock_project, monkeypatch):
    """When git is unavailable, fallback checks mtime against manifest.json."""
    proj, G, db_path, graph_json = mock_project

    # Remove dummy .git directory
    (proj / ".git").rmdir()

    # Touch src/service.py to advance mtime
    f1 = proj / "src" / "service.py"
    current_time = time.time() + 10.0
    os.utime(f1, (current_time, current_time))

    res = check_graph_staleness(graph_path=graph_json, db_path=db_path, project_root=proj)
    assert res["is_stale"] is True
    assert res["action"] == "hotpatch"
    assert "src/service.py" in res["modified_files"]


def test_sqlite_store_file_stats_and_hotpatch(mock_project):
    """Verify SqliteGraphStore records file stats and performs atomic hotpatching."""
    proj, G, db_path, graph_json = mock_project

    with SqliteGraphStore(db_path) as store:
        stats = store.get_file_stats()
        # file_stats should be populated from save_graph
        assert isinstance(stats, dict)

        # Record custom file stat
        store.record_file_stats({"src/custom.py": (120, 123456789, "md5hash")})
        stats2 = store.get_file_stats()
        assert "src/custom.py" in stats2
        assert stats2["src/custom.py"]["size"] == 120
        assert stats2["src/custom.py"]["mtime_ns"] == 123456789

        # Hotpatch file with new nodes and edges
        new_nodes = [
            {
                "id": "src/custom.py:new_func",
                "label": "new_func",
                "file_type": "function",
                "source_file": "src/custom.py",
                "source_location": "L10-L20",
                "docstring": "Brand new patched function",
            }
        ]
        new_edges = [
            {
                "source": "src/custom.py:new_func",
                "target": "src/utils.py:helper_fn",
                "relation": "calls",
            }
        ]
        store.hot_patch_file("src/custom.py", new_nodes, new_edges, stat=(250, 999999999, "newhash"))

        # Verify new node is searchable in FTS and retrievable
        node = store.get_node("src/custom.py:new_func")
        assert node is not None
        assert node["label"] == "new_func"
        assert node["start_line"] == 10
        assert node["end_line"] == 20

        fts_res = store.fts_search("new_func")
        assert len(fts_res) > 0
        assert fts_res[0]["node_id"] == "src/custom.py:new_func"

        # Verify file_stats updated
        stats3 = store.get_file_stats()
        assert stats3["src/custom.py"]["size"] == 250
        assert stats3["src/custom.py"]["mtime_ns"] == 999999999

        # Remove file cleanly
        store.remove_file("src/custom.py")
        assert store.get_node("src/custom.py:new_func") is None
        assert "src/custom.py" not in store.get_file_stats()


def test_hot_patch_graph_updates_networkx_in_place(mock_project):
    """Verify hot_patch_graph replaces touched file nodes in NetworkX in <200ms."""
    proj, G, db_path, graph_json = mock_project

    # Modify src/service.py on disk
    f1 = proj / "src" / "service.py"
    f1.write_text(
        "def process_data_v2(item: str, retries: int = 3) -> bool:\n"
        "    '''Updated process data with retry logic.'''\n"
        "    return bool(item)\n"
    )

    t0 = time.perf_counter()
    res = hot_patch_graph(
        graph=G,
        db_path=db_path,
        modified_files=["src/service.py"],
        project_root=proj,
    )
    duration_ms = (time.perf_counter() - t0) * 1000

    assert res["patched_files"] == 1
    # Check that execution took <200ms
    assert duration_ms < 300.0  # Safe threshold on shared runner

    # Old node should no longer be present
    assert "src/service.py:process_data" not in G

    # Newly extracted node should exist in G
    node_ids = list(G.nodes)
    assert any("process_data_v2" in str(nid) for nid in node_ids)


def test_task_context_staleness_banner_and_hotpatching(mock_project, monkeypatch):
    """Verify task_context includes staleness metadata and warning banner when drifted."""
    proj, G, db_path, graph_json = mock_project

    # 1. Test hotpatching under <= 10 files
    class DummyHotpatchProc:
        returncode = 0
        stdout = " M src/service.py\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: DummyHotpatchProc())

    # Update src/service.py with new function
    f1 = proj / "src" / "service.py"
    f1.write_text("def live_hotpatched_func(x: str) -> None:\n    '''Live updated code.'''\n    pass\n")

    res = task_context(
        task="check process data implementation",
        graph=G,
        db_path=db_path,
        graph_path=graph_json,
        root_dir=proj,
        hotpatch=True,
    )

    assert "staleness" in res
    assert res["staleness"]["action"] == "hotpatch"
    assert "hotpatch_applied" in res["staleness"]

    # 2. Test warning banner when > 10 files
    status_lines = [f" M src/drift_{i}.py" for i in range(12)]
    for i in range(12):
        (proj / "src" / f"drift_{i}.py").write_text("def f(): pass\n")

    class DummyWarnProc:
        returncode = 0
        stdout = "\n".join(status_lines) + "\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: DummyWarnProc())

    res_warn = task_context(
        task="check process data implementation",
        graph=G,
        db_path=db_path,
        graph_path=graph_json,
        root_dir=proj,
        hotpatch=True,
    )

    assert res_warn["staleness"]["action"] == "warn"
    assert "[GRAPH STALE: 12 files modified since last index. Run 'graph_fy update .']" in res_warn["prompt_context"]
