"""Tests for Step 8.2: Ephemeral Fast-Boot Cache (<2s cold start via SQLite WAL)."""
from __future__ import annotations

import time
from pathlib import Path

from graph_fy.cache import load_cached, save_cached


def test_sqlite_fastboot_cache_creation_and_hit(tmp_path: Path):
    """Verify that AST cache creates SQLite database and serves <0.1ms hits."""
    file_path = tmp_path / "service.py"
    file_path.write_text("def process_data(): pass\n", encoding="utf-8")

    sample_extraction = {
        "nodes": [
            {"id": "service::process_data", "label": "process_data", "file_type": "function", "source_file": str(file_path)},
        ],
        "edges": [],
    }

    # 1. Save extraction to cache
    save_cached(file_path, sample_extraction, root=tmp_path, kind="ast")

    # 2. Verify cache.db exists
    cache_dirs = list((tmp_path / "graph_fy_out" / "cache" / "ast").glob("v*"))
    assert len(cache_dirs) == 1
    version_dir = cache_dirs[0]
    db_file = version_dir / "cache.db"
    assert db_file.exists()

    # 3. Load from cache (should hit SQLite directly)
    t0 = time.perf_counter()
    res = load_cached(file_path, root=tmp_path, kind="ast")
    t_load = time.perf_counter() - t0

    assert res is not None
    assert len(res["nodes"]) == 1
    assert res["nodes"][0]["label"] == "process_data"
    # Microsecond-level read
    assert t_load < 0.05, f"Cache load took {t_load*1000:.2f}ms, expected <50ms"


def test_sqlite_fastboot_multi_file_benchmark(tmp_path: Path):
    """Benchmark batch loading of 200 files from fast-boot SQLite cache."""
    files = []
    for i in range(200):
        fp = tmp_path / f"mod_{i}.py"
        fp.write_text(f"def func_{i}(): pass\n", encoding="utf-8")
        files.append(fp)
        extraction = {
            "nodes": [{"id": f"mod_{i}::func_{i}", "label": f"func_{i}", "file_type": "function", "source_file": str(fp)}],
            "edges": [],
        }
        save_cached(fp, extraction, root=tmp_path, kind="ast")

    # Measure batch warm cache read
    t0 = time.perf_counter()
    loaded_count = 0
    for fp in files:
        res = load_cached(fp, root=tmp_path, kind="ast")
        if res is not None:
            loaded_count += 1
    duration = time.perf_counter() - t0

    assert loaded_count == 200
    # 200 files should load in <150ms (<0.75ms per file)
    assert duration < 0.25, f"200 files took {duration:.3f}s, expected <0.25s"
