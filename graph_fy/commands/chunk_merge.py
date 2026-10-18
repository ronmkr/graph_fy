"""Chunk and semantic merge command implementations for graph_fy CLI."""
from __future__ import annotations

import glob as _glob
import json
import sys
from pathlib import Path

from graph_fy.cache import check_semantic_cache
from graph_fy.paths import write_json_atomic as _wja
from graph_fy.semantic_cleanup import load_validated_semantic_fragment


def cmd_cache_check(argv: list[str], graph_fy_out: str | Path) -> int:
    """Handle `graph_fy cache-check` command."""
    if len(argv) < 3:
        print(
            "Usage: graph_fy cache-check <files_from> [--root <dir>] "
            "[--mode <m> | --deep] [--prompt-file <path>]",
            file=sys.stderr,
        )
        return 1
    files_from = Path(argv[2])
    root = Path(".")
    cache_mode: str | None = None
    prompt_file: str | None = None
    i = 3
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            root = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--mode" and i + 1 < len(argv):
            cache_mode = argv[i + 1]
            i += 2
        elif argv[i].startswith("--mode="):
            cache_mode = argv[i].split("=", 1)[1]
            i += 1
        elif argv[i] == "--deep":
            cache_mode = "deep"
            i += 1
        elif argv[i] == "--prompt-file" and i + 1 < len(argv):
            prompt_file = argv[i + 1]
            i += 2
        elif argv[i].startswith("--prompt-file="):
            prompt_file = argv[i].split("=", 1)[1]
            i += 1
        else:
            i += 1
    files = [f for f in files_from.read_text(encoding="utf-8").splitlines() if f.strip()]
    cached_nodes, cached_edges, cached_hyperedges, uncached = check_semantic_cache(
        files, root, mode=cache_mode, prompt_file=prompt_file
    )
    out = root / graph_fy_out
    out.mkdir(parents=True, exist_ok=True)
    if cached_nodes or cached_edges or cached_hyperedges:
        (out / ".graph_fy_cached.json").write_text(
            json.dumps(
                {"nodes": cached_nodes, "edges": cached_edges, "hyperedges": cached_hyperedges},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    (out / ".graph_fy_uncached.txt").write_text("\n".join(uncached), encoding="utf-8")
    print(f"Cache: {len(files) - len(uncached)} hit, {len(uncached)} miss")
    return 0


def cmd_merge_chunks(argv: list[str]) -> int:
    """Handle `graph_fy merge-chunks` command."""
    if len(argv) < 3:
        print("Usage: graph_fy merge-chunks <chunk_files...> --out <path>", file=sys.stderr)
        return 1
    out_path: Path | None = None
    chunk_args: list[str] = []
    i = 2
    while i < len(argv):
        if argv[i] == "--out" and i + 1 < len(argv):
            out_path = Path(argv[i + 1])
            i += 2
        else:
            chunk_args.append(argv[i])
            i += 1
    if not out_path:
        print("error: --out <path> required", file=sys.stderr)
        return 1
    chunk_files: list[str] = []
    for arg in chunk_args:
        expanded = _glob.glob(arg)
        chunk_files.extend(sorted(expanded) if expanded else [arg])
    merged: dict = {
        "nodes": [],
        "edges": [],
        "hyperedges": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }
    seen_ids: set[str] = set()
    valid_chunks = 0
    for cf in chunk_files:
        chunk, _chunk_errs = load_validated_semantic_fragment(Path(cf))
        if _chunk_errs:
            print(
                f"[graph_fy merge-chunks] warning: skipping invalid chunk {cf}: "
                f"{'; '.join(_chunk_errs[:3])}",
                file=sys.stderr,
            )
            continue
        valid_chunks += 1
        for n in chunk.get("nodes", []):
            if n.get("id") not in seen_ids:
                seen_ids.add(n["id"])
                merged["nodes"].append(n)
        merged["edges"].extend(chunk.get("edges", []))
        merged["hyperedges"].extend(chunk.get("hyperedges", []))
        for _tok in ("input_tokens", "output_tokens"):
            _v = chunk.get(_tok, 0)
            merged[_tok] += _v if isinstance(_v, (int, float)) else 0
    if not valid_chunks:
        print(
            f"[graph_fy merge-chunks] error: no valid chunks to merge; "
            f"refusing to write {out_path}",
            file=sys.stderr,
        )
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _wja(out_path, merged, ensure_ascii=False)
    chunk_summary = (
        f"{valid_chunks} chunks"
        if valid_chunks == len(chunk_files)
        else f"{valid_chunks} of {len(chunk_files)} chunks"
    )
    print(
        f"Merged {chunk_summary}: {len(merged['nodes'])} nodes, {len(merged['edges'])} edges, "
        f"{merged['input_tokens']:,} in / {merged['output_tokens']:,} out tokens"
    )
    return 0


def cmd_merge_semantic(argv: list[str]) -> int:
    """Handle `graph_fy merge-semantic` command."""
    if len(argv) < 3:
        print("Usage: graph_fy merge-semantic --cached <path> --new <path> --out <path>", file=sys.stderr)
        return 1
    cached_path: Path | None = None
    new_path: Path | None = None
    out_path2: Path | None = None
    i = 2
    while i < len(argv):
        if argv[i] == "--cached" and i + 1 < len(argv):
            cached_path = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--new" and i + 1 < len(argv):
            new_path = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--out" and i + 1 < len(argv):
            out_path2 = Path(argv[i + 1])
            i += 2
        else:
            i += 1
    if not out_path2:
        print("error: --out <path> required", file=sys.stderr)
        return 1
    empty: dict = {"nodes": [], "edges": [], "hyperedges": []}
    cached_data = json.loads(cached_path.read_text(encoding="utf-8")) if cached_path and cached_path.exists() else empty
    new_data = json.loads(new_path.read_text(encoding="utf-8")) if new_path and new_path.exists() else empty
    seen_ids2: set[str] = set()
    all_nodes: list[dict] = []
    for n in cached_data.get("nodes", []) + new_data.get("nodes", []):
        if n.get("id") not in seen_ids2:
            seen_ids2.add(n["id"])
            all_nodes.append(n)
    merged2 = {
        "nodes": all_nodes,
        "edges": cached_data.get("edges", []) + new_data.get("edges", []),
        "hyperedges": cached_data.get("hyperedges", []) + new_data.get("hyperedges", []),
    }
    out_path2.parent.mkdir(parents=True, exist_ok=True)
    _wja(out_path2, merged2, ensure_ascii=False)
    print(f"Merged: {len(merged2['nodes'])} nodes, {len(merged2['edges'])} edges")
    return 0
