"""Build and extract command implementation for graph_fy CLI."""
from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Type

def _stamped_manifest_files(
    files_by_type: dict[str, list[str]],
    sem_result: dict,
    root: Path,
    partial_source_files: "set[str] | None" = None,
    failed_ast_sources: "set[str] | list[str] | None" = None,
    unverified_semantic_sources: "set[str] | list[str] | None" = None,
) -> dict[str, list[str]]:
    """Manifest-safe files dict: only stamp semantic files that actually
    produced output (cache hit or fresh extraction). Files whose chunk failed
    have no source_file entry in sem_result — leaving their semantic_hash
    empty so detect_incremental re-queues them (#933).

    A file in ``partial_source_files`` DID produce output this run, but only a
    truncated fragment of it, so it is excluded from stamping too — otherwise
    detect_incremental would see it "done" and never re-dispatch it, leaving the
    incomplete node set live forever on the warm-incremental path. Same #933
    mechanism: leave it unstamped and it is re-queued next run.

    ``unverified_semantic_sources`` (#3203): files whose semantic extraction
    under-produced compared to their prior representation (e.g. 3 -> 1 nodes).
    They are excluded from stamping unless --allow-partial is set, so the next
    incremental run retries them.

    Both sides of the membership test are resolved against the scan ``root``
    before comparing (#1897): node/edge/hyperedge ``source_file`` values are
    root-relative on a fresh extraction while ``files_by_type`` entries are
    absolute (from detect()), so a raw string comparison never matched and
    every freshly-extracted semantic doc was dropped from the manifest.
    Mirrors the #1890 path normalization in graph_fy.llm.

    Hyperedges are counted as output (#1920): a chunk whose only result for a
    document is a hyperedge (3+ nodes sharing a concept) is valid output that
    the semantic cache persists per-``source_file`` — omitting it here left the
    doc unstamped, so detect_incremental re-queued it on every run. The stamping
    condition mirrors the cache-write keying (a hyperedge carries its own
    ``source_file``); do not derive it from member nodes.

    ``failed_ast_sources`` (#2543): code files whose AST extractor errored
    (missing optional extra, etc.) or returned zero nodes. They must not be
    stamped as up-to-date or a later install of the extra will never re-run.
    """
    root = Path(root)

    def _resolve(value: str) -> Path:
        p = Path(value)
        if not p.is_absolute():
            p = root / p
        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return p

    sem_extracted: set[Path] = set()
    # #2927: only nodes and hyperedges count as valid semantic output that stamps
    # the manifest. An edge-only result has no entity representation in the graph
    # and must be left unstamped so detect_incremental re-queues it (#933/#1666).
    for coll in ("nodes", "hyperedges"):
        for item in sem_result.get(coll, []):
            sf = item.get("source_file", "")
            if sf:
                sem_extracted.add(_resolve(sf))
    partial_resolved = {_resolve(p) for p in (partial_source_files or set())}
    unverified_resolved = {_resolve(p) for p in (unverified_semantic_sources or set())}
    failed_ast_resolved = {_resolve(p) for p in (failed_ast_sources or [])}
    sem_types = {"document", "paper", "image"}
    return {
        ftype: [
            f for f in flist
            if _resolve(f) not in failed_ast_resolved
            and (
                ftype not in sem_types
                or (
                    _resolve(f) in sem_extracted
                    and _resolve(f) not in partial_resolved
                    and _resolve(f) not in unverified_resolved
                )
            )
        ]
        for ftype, flist in files_by_type.items()
    }


def _handle_unverified_semantic_shrink(
    unverified_shrink,
    *,
    cli_allow_partial: bool,
    files_by_type,
    sem_result,
    target,
    partial_semantic_files,
    failed_ast_sources,
    semantic_files,
):
    """Shared handling for the #3203 unverified-semantic-shrink guard on both the
    raw and clustered write paths (they differ only in where the flag is read
    from — ``merged`` vs ``G.graph``). Always prints the actionable notice.

    Returns None when there is no shrink, else ``(incomplete, manifest_files,
    cleared_semantic)`` — the latter two are None unless the guard armed
    (``not cli_allow_partial``), so the caller mirrors the original inline logic.
    """
    if not unverified_shrink:
        return None
    incomplete = False
    manifest_files = None
    cleared_semantic = None
    if not cli_allow_partial:
        incomplete = True
        unverified_sources = set(unverified_shrink.keys())
        manifest_files = _stamped_manifest_files(
            files_by_type,
            sem_result,
            target,
            partial_source_files=partial_semantic_files,
            failed_ast_sources=failed_ast_sources,
            unverified_semantic_sources=unverified_sources,
        )
        stamped = {f for _flist in manifest_files.values() for f in _flist}
        cleared_semantic = {str(p) for p in semantic_files} - stamped
    details = ", ".join(
        f"'{sf}' ({prior} -> {fresh} nodes)"
        for sf, (prior, fresh) in sorted(unverified_shrink.items())
    )
    print(
        f"[graph_fy extract] semantic extraction is incomplete: unverified semantic "
        f"shrink detected for {details}. The shrink guard stays armed for this write; "
        "pass --allow-partial to overwrite a larger existing graph anyway.",
        file=sys.stderr,
    )
    return incomplete, manifest_files, cleared_semantic


def _stale_graph_sources(
    graph_path: Path,
    scan_root: Path,
    seen_files: set[str],
    detection: dict | None = None,
) -> list[str]:
    """Source files graph.json still references but the current scan no longer
    contains (#1909).

    Incremental extract's prune set was historically derived from the manifest
    alone (``manifest - corpus``), so a file that became EXCLUDED
    (.graph_fyignore/.gitignore/--exclude changed) without being listed in the
    manifest kept its stale nodes in graph.json forever. Derive prune
    candidates from the graph's own node ``source_file``s instead: anything
    the graph references that the post-exclude detect corpus no longer
    contains is stale, whether the file was deleted or newly excluded.

    Only IN-ROOT paths are candidates: out-of-root/absolute entries
    (--include sources, symlinked external corpora) are never walked by
    detect, so their absence from the corpus is not staleness evidence.
    Relative entries are re-anchored against both the scan root and the
    graph's own output root; only anchors that land inside the scan root
    count. Since #1941 extracts always store source_file relative to the SCAN
    root, so the scan-root anchor is the live one; the out-root anchor stays
    for graphs written by <=0.9.16, which stored them relative to the OUT root
    (e.g. ``../project/x.py``, #555/#1899).
    ``seen_files`` must be the FULL detect output including unclassified
    files, so nodes from walked-but-unsupported sources (e.g. introspected
    Cargo.toml manifests) are not misread as stale.

    Paths are compared NFC-normalized on both sides: macOS reports NFD
    filenames while graph ``source_file`` entries are typically NFC, and a
    raw-string membership test misread every accented live file as stale
    (#2210; same class as the manifest-layer #2221/#2224).

    Fail-closed liveness guard (#2210, mirrors watch.py's excluded-vs-deleted
    distinction): a source missing from the scan corpus is only pruned when
    the file is gone from disk, or when its exclusion is PROVABLE from the
    same scan that produced ``seen_files`` — ``detection``'s ``ignored`` /
    ``pruned_noise_dirs`` / ``skipped_sensitive`` output, or detect's
    sensitivity predicate. An alive file that merely failed the membership
    test (path-spelling drift the normalization didn't cover, walk errors,
    …) is KEPT and reported, never mass-evicted.
    """
    from graph_fy.paths import nfc
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    try:
        root_res = scan_root.resolve()
    except (OSError, RuntimeError):
        root_res = scan_root
    # <out>/graph_fy_out/graph.json — relative source_files may be anchored here.
    out_base = graph_path.parent.parent
    try:
        out_base = out_base.resolve()
    except (OSError, RuntimeError):
        pass

    def _within_root(p: Path) -> bool:
        try:
            p.relative_to(root_res)
            return True
        except ValueError:
            pass
        try:
            p.resolve().relative_to(root_res)
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    seen_nfc = {nfc(s) for s in seen_files}
    seen_basenames = {nfc(os.path.basename(s)) for s in seen_files}

    def _in_seen(p: Path) -> bool:
        if nfc(str(p)) in seen_nfc:
            return True
        try:
            return nfc(str(p.resolve())) in seen_nfc
        except (OSError, RuntimeError):
            return False

    # Provable-exclusion evidence from the scan that produced seen_files:
    # individually ignored files are exact entries; ignored/noise-pruned
    # directories are recorded once with a trailing separator and cover
    # their whole subtree. skipped_sensitive entries may carry a
    # " [reason]" suffix.
    excluded_exact: set[str] = set()
    excluded_prefixes: list[str] = []
    if detection:
        for entry in list(detection.get("ignored", [])) + list(
            detection.get("pruned_noise_dirs", [])
        ):
            e = nfc(str(entry))
            if e.endswith(os.sep) or e.endswith("/"):
                excluded_prefixes.append(e)
            else:
                excluded_exact.add(e)
        for entry in detection.get("skipped_sensitive", []):
            excluded_exact.add(nfc(str(entry).split(" [", 1)[0]))

    def _provably_excluded(c: Path) -> bool:
        spellings = [nfc(str(c))]
        try:
            spellings.append(nfc(str(c.resolve())))
        except (OSError, RuntimeError):
            pass
        for s in spellings:
            if s in excluded_exact:
                return True
            if any(s.startswith(pref) for pref in excluded_prefixes):
                return True
        try:
            from graph_fy.detect import _is_sensitive as _det_sensitive
            if _det_sensitive(c):
                return True
        except Exception:
            pass
        return False

    stale: list[str] = []
    kept_alive: list[str] = []
    checked: set[str] = set()
    for n in data.get("nodes", []):
        if not isinstance(n, dict):
            continue
        sf = n.get("source_file")
        if not sf or not isinstance(sf, str) or sf in checked:
            continue
        checked.add(sf)
        if "://" in sf:
            continue  # remote/virtual source (e.g. Google Workspace), not a scanned path
        p = Path(sf)
        if p.is_absolute():
            candidates = [p]
        else:
            rel = sf.replace("\\", "/")
            bases = [root_res]
            if out_base != root_res:
                bases.append(out_base)
            candidates = [
                Path(os.path.normpath(str(base / rel))) for base in bases
            ]
        in_root = [c for c in candidates if _within_root(c)]
        if not in_root:
            continue  # out-of-root under every anchor: never prune
        if any(_in_seen(c) for c in in_root):
            continue  # still part of the scan corpus
        # Fail-closed liveness guard (#2210): absence from the corpus is
        # only deletion evidence when the file is actually gone from disk.
        alive = []
        for c in in_root:
            try:
                if c.exists():
                    alive.append(c)
            except OSError:
                pass
        if alive:
            if all(_provably_excluded(c) for c in alive):
                stale.append(sf)  # alive but excluded under current rules (#1909)
            else:
                kept_alive.append(sf)
            continue
        # No anchored candidate exists, but a legacy bare-basename spelling
        # can't be anchored reliably — a live corpus file with the same name
        # means deletion is unproven; keep.
        rel_sf = sf.replace("\\", "/")
        if "/" not in rel_sf and nfc(rel_sf) in seen_basenames:
            kept_alive.append(sf)
            continue
        stale.append(sf)
    if kept_alive:
        print(
            f"[graph_fy] fail-closed: kept node(s) from {len(kept_alive)} "
            "source file(s) that left the scan corpus but still exist on disk "
            "(ignore rules or filters changed?). Run a full re-extraction to "
            "purge them if the exclusion is intentional.",
            file=sys.stderr,
        )
    return stale


def _zero_node_stamped_code_sources(
    graph_path: Path,
    scan_root: Path,
    unchanged_code: list[str],
) -> list[str]:
    """Manifest-stamped code files with a registered extractor but ZERO nodes
    in the existing graph.json (#2543 heal).

    The failed-source unstamping only covers failures that happen AFTER it
    shipped; a manifest poisoned by an earlier run (extraction failed, hashes
    stamped anyway) keeps reporting the file unchanged forever, and the only
    documented recovery was deleting graph_fy_out/. A stamped file that the
    graph has no nodes for, despite an extractor being wired up for it, is
    exactly that state — re-queue it as changed. Bounded by the same no-wedge
    property: if it fails again this run it is now left unstamped, and if it
    succeeds its nodes enter graph.json so the next scan stops re-queuing it.

    Membership mirrors the ``source_file`` spellings extracts store (#1897/
    #1941: scan-root-relative, forward slash; absolute for out-of-root) and
    compares NFC-normalized (#2210/#2221).
    """
    if not unchanged_code:
        return []
    from graph_fy.paths import nfc
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    try:
        root_res = scan_root.resolve()
    except (OSError, RuntimeError):
        root_res = scan_root
    # <out>/graph_fy_out/graph.json — legacy relative source_files may be
    # anchored here instead of the scan root (<=0.9.16, #555/#1899).
    out_base = graph_path.parent.parent
    try:
        out_base = out_base.resolve()
    except (OSError, RuntimeError):
        pass

    present: set[str] = set()
    for n in data.get("nodes", []):
        if not isinstance(n, dict):
            continue
        sf = n.get("source_file")
        if not sf or not isinstance(sf, str):
            continue
        present.add(nfc(sf))
        p = Path(sf)
        if p.is_absolute():
            try:
                present.add(nfc(str(p.resolve())))
            except (OSError, RuntimeError):
                pass
        else:
            rel = sf.replace("\\", "/")
            for base in (root_res, out_base):
                present.add(nfc(os.path.normpath(str(base / rel))))

    from graph_fy.extract import _get_extractor
    healed: list[str] = []
    for f in unchanged_code:
        p = Path(f)
        if _get_extractor(p) is None:
            continue  # no extractor: absence from the graph is expected
        spellings = {nfc(str(p))}
        try:
            spellings.add(nfc(str(p.resolve())))
        except (OSError, RuntimeError):
            pass
        try:
            spellings.add(nfc(p.resolve().relative_to(root_res).as_posix()))
        except (ValueError, OSError, RuntimeError):
            pass
        if spellings & present:
            continue  # the graph has this file: stamp is honest
        healed.append(f)
    return healed


def _zero_node_stamped_semantic_sources(
    graph_path: Path,
    scan_root: Path,
    unchanged_semantic: list[str],
) -> list[str]:
    """Manifest-stamped semantic files (doc/paper/image) with ZERO nodes
    and ZERO hyperedges in the existing graph.json (#2927 heal).

    A manifest poisoned before #2927 (edge-only result cached and stamped)
    keeps reporting the file unchanged forever, freezing it out of the graph.
    Re-queue any unchanged semantic file that has neither nodes nor hyperedges
    in graph.json. If it succeeds, its nodes enter graph.json; if it produces
    no nodes or fails, it is now left unstamped, so this cannot wedge.

    Membership mirrors the ``source_file`` spellings extracts store (#1897/
    #1941: scan-root-relative, forward slash; absolute for out-of-root) and
    compares NFC-normalized (#2210/#2221).
    """
    if not unchanged_semantic:
        return []
    from graph_fy.paths import nfc
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    try:
        root_res = scan_root.resolve()
    except (OSError, RuntimeError):
        root_res = scan_root
    out_base = graph_path.parent.parent
    try:
        out_base = out_base.resolve()
    except (OSError, RuntimeError):
        pass

    present: set[str] = set()
    for n in data.get("nodes", []):
        if not isinstance(n, dict):
            continue
        sf = n.get("source_file")
        if not sf or not isinstance(sf, str):
            continue
        present.add(nfc(sf))
        p = Path(sf)
        if p.is_absolute():
            try:
                present.add(nfc(str(p.resolve())))
            except (OSError, RuntimeError):
                pass
        else:
            rel = sf.replace("\\", "/")
            for base in (root_res, out_base):
                present.add(nfc(os.path.normpath(str(base / rel))))

    hyper_items = list(data.get("hyperedges", []) or [])
    if isinstance((data.get("graph") or {}).get("hyperedges"), list):
        hyper_items.extend(data["graph"]["hyperedges"])
    for h in hyper_items:
        if not isinstance(h, dict):
            continue
        sf = h.get("source_file")
        if not sf or not isinstance(sf, str):
            continue
        present.add(nfc(sf))
        p = Path(sf)
        if p.is_absolute():
            try:
                present.add(nfc(str(p.resolve())))
            except (OSError, RuntimeError):
                pass
        else:
            rel = sf.replace("\\", "/")
            for base in (root_res, out_base):
                present.add(nfc(os.path.normpath(str(base / rel))))

    healed: list[str] = []
    for f in unchanged_semantic:
        p = Path(f)
        spellings = {nfc(str(p))}
        try:
            spellings.add(nfc(str(p.resolve())))
        except (OSError, RuntimeError):
            pass
        try:
            spellings.add(nfc(p.resolve().relative_to(root_res).as_posix()))
        except (ValueError, OSError, RuntimeError):
            pass
        if spellings & present:
            continue  # the graph has nodes or hyperedges for this file: stamp is honest
        healed.append(f)
    return healed


def _prune_graph_json_sources(graph_path: Path, stale_sources: list[str]) -> int:
    """Drop nodes/edges/hyperedges owned by ``stale_sources`` from graph.json
    in place. Returns the number of nodes removed.

    Used by the ``--no-cluster`` incremental early-exit: that path never runs
    ``build_merge`` (it would raw-dump only the new chunks), so an
    exclusion-only change must prune the existing raw graph directly or the
    newly-excluded file's nodes survive forever (#1909).
    ``stale_sources`` comes from :func:`_stale_graph_sources`, i.e. the
    graph's own ``source_file`` spellings, so exact string matching is enough.
    """
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    stale = set(stale_sources)
    links_key = "links" if "links" in data else "edges"
    nodes = [n for n in data.get("nodes", []) if isinstance(n, dict)]
    kept_nodes = [n for n in nodes if n.get("source_file") not in stale]
    removed_ids = {
        n.get("id") for n in nodes if n.get("source_file") in stale
    }
    n_removed = len(nodes) - len(kept_nodes)
    kept_edges = [
        e for e in data.get(links_key, [])
        if isinstance(e, dict)
        and e.get("source_file") not in stale
        and e.get("source") not in removed_ids
        and e.get("target") not in removed_ids
    ]
    kept_hyper = [
        h for h in data.get("hyperedges", [])
        if isinstance(h, dict) and h.get("source_file") not in stale
    ]
    if n_removed == 0 and len(kept_edges) == len(data.get(links_key, [])) and (
        len(kept_hyper) == len(data.get("hyperedges", []))
    ):
        return 0
    data["nodes"] = kept_nodes
    data[links_key] = kept_edges
    if "hyperedges" in data:
        data["hyperedges"] = kept_hyper
    from graph_fy.export import backup_if_protected as _backup
    _backup(graph_path.parent)
    from graph_fy.paths import write_json_atomic
    write_json_atomic(graph_path, data, indent=2)
    return n_removed



def cmd_build(
    argv: list[str],
    cmd: str = "extract",
    default_out_dir: str | Path = "graph_fy_out",
    stage_timer_cls: Type[Any] | None = None,
) -> int:
    """Handle `graph_fy extract` and `graph_fy build` subcommands."""
    _graph_fy_OUT = str(default_out_dir)
    if stage_timer_cls is not None:
        _StageTimer = stage_timer_cls
    else:
        from graph_fy.paths import _StageTimer

    sys_argv_backup = sys.argv
    sys.argv = list(argv)
    try:
        # Full-pipeline local graph build:
        # Runs detect -> AST extraction on code and docs -> build -> cluster -> FTS5 index -> write outputs.
        # Fully local, zero LLM calls, zero network access.
        has_path = True
        if len(sys.argv) < 3 or sys.argv[2].startswith("-"):
            has_path = False
            target = Path(".").resolve()
        else:
            target = Path(sys.argv[2]).resolve()
            if not target.exists():
                print(f"error: path not found: {target}", file=sys.stderr)
                sys.exit(1)

        backend: str | None = None
        model: str | None = None
        extract_mode: str | None = None
        out_dir: Path | None = None
        cli_postgres_dsn: str | None = None
        cli_cargo: bool = False
        cli_allow_partial: bool = False
        no_cluster = False
        dedup_llm = False
        # --no-dedup: skip entity deduplication entirely. On an incremental
        # merge the fuzzy pass runs over the COMBINED node set (existing graph +
        # new chunk), so a small diff merged into a large graph can collapse
        # pre-existing nodes from files the diff never touched. Turning dedup
        # off also arms build_merge's #479 shrink guard, which is disabled while
        # dedup is on because fuzzy merging shrinks the graph legitimately (#2881).
        no_dedup = False
        google_workspace = False
        global_merge = False
        code_only = False
        no_gitignore = False
        global_repo_tag: str | None = None
        # Performance/tuning knobs (issue #792). None means "use library default".
        cli_max_workers: int | None = None
        cli_token_budget: int | None = None
        cli_max_concurrency: int | None = None
        cli_api_timeout: float | None = None
        # Clustering tuning knobs
        cli_resolution: float = 1.0
        cli_exclude_hubs: float | None = None
        cli_excludes: list[str] = []
        cli_timing: bool = False
        # --force parity with `graph_fy update`: the flag or graph_fy_FORCE=1
        # disables the incremental gate and skips semantic-cache reads (#1894).
        force = os.environ.get("graph_fy_FORCE", "").lower() in ("1", "true", "yes")

        def _parse_int(name: str, raw: str) -> int:
            try:
                v = int(raw)
            except ValueError:
                print(f"error: {name} must be a positive integer (got {raw!r})", file=sys.stderr)
                sys.exit(2)
            if v <= 0:
                print(f"error: {name} must be > 0 (got {v})", file=sys.stderr)
                sys.exit(2)
            return v

        def _parse_float(name: str, raw: str) -> float:
            try:
                v = float(raw)
            except ValueError:
                print(f"error: {name} must be a positive number (got {raw!r})", file=sys.stderr)
                sys.exit(2)
            if v <= 0:
                print(f"error: {name} must be > 0 (got {v})", file=sys.stderr)
                sys.exit(2)
            return v

        args = sys.argv[3:] if has_path else sys.argv[2:]
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--backend" and i + 1 < len(args):
                backend = args[i + 1]; i += 2
            elif a.startswith("--backend="):
                backend = a.split("=", 1)[1]; i += 1
            elif a == "--model" and i + 1 < len(args):
                model = args[i + 1]; i += 2
            elif a.startswith("--model="):
                model = a.split("=", 1)[1]; i += 1
            elif a == "--mode" and i + 1 < len(args):
                extract_mode = args[i + 1]; i += 2
            elif a.startswith("--mode="):
                extract_mode = a.split("=", 1)[1]; i += 1
            elif a in ("--out", "--output") and i + 1 < len(args):
                # --output is an alias of --out (#2004): it was silently dropped
                # before, and `graph_fy tree` already documents --output, so the
                # mistake is natural. (--output= does not startswith --out=.)
                out_dir = Path(args[i + 1]); i += 2
            elif a.startswith(("--out=", "--output=")):
                out_dir = Path(a.split("=", 1)[1]); i += 1
            elif a == "--no-cluster":
                no_cluster = True; i += 1
            elif a == "--dedup-llm":
                dedup_llm = True; i += 1
            elif a == "--no-dedup":
                no_dedup = True; i += 1
            elif a == "--code-only":
                code_only = True; i += 1
            elif a == "--google-workspace":
                google_workspace = True; i += 1
            elif a == "--no-gitignore":
                no_gitignore = True; i += 1
            elif a == "--global":
                global_merge = True; i += 1
            elif a == "--as" and i + 1 < len(args):
                global_repo_tag = args[i + 1]; i += 2
            elif a == "--max-workers" and i + 1 < len(args):
                cli_max_workers = _parse_int("--max-workers", args[i + 1]); i += 2
            elif a.startswith("--max-workers="):
                cli_max_workers = _parse_int("--max-workers", a.split("=", 1)[1]); i += 1
            elif a == "--token-budget" and i + 1 < len(args):
                cli_token_budget = _parse_int("--token-budget", args[i + 1]); i += 2
            elif a.startswith("--token-budget="):
                cli_token_budget = _parse_int("--token-budget", a.split("=", 1)[1]); i += 1
            elif a == "--max-concurrency" and i + 1 < len(args):
                cli_max_concurrency = _parse_int("--max-concurrency", args[i + 1]); i += 2
            elif a.startswith("--max-concurrency="):
                cli_max_concurrency = _parse_int("--max-concurrency", a.split("=", 1)[1]); i += 1
            elif a == "--api-timeout" and i + 1 < len(args):
                cli_api_timeout = _parse_float("--api-timeout", args[i + 1]); i += 2
            elif a.startswith("--api-timeout="):
                cli_api_timeout = _parse_float("--api-timeout", a.split("=", 1)[1]); i += 1
            elif a == "--resolution" and i + 1 < len(args):
                cli_resolution = _parse_float("--resolution", args[i + 1]); i += 2
            elif a.startswith("--resolution="):
                cli_resolution = _parse_float("--resolution", a.split("=", 1)[1]); i += 1
            elif a == "--exclude-hubs" and i + 1 < len(args):
                cli_exclude_hubs = float(args[i + 1]); i += 2
            elif a.startswith("--exclude-hubs="):
                cli_exclude_hubs = float(a.split("=", 1)[1]); i += 1
            elif a == "--exclude" and i + 1 < len(args):
                cli_excludes.append(args[i + 1]); i += 2
            elif a.startswith("--exclude="):
                cli_excludes.append(a.split("=", 1)[1]); i += 1
            elif a == "--postgres" and i + 1 < len(args):
                cli_postgres_dsn = args[i + 1]; i += 2
            elif a.startswith("--postgres="):
                cli_postgres_dsn = a.split("=", 1)[1]; i += 1
            elif a == "--cargo":
                cli_cargo = True
                i += 1
            elif a == "--force":
                force = True; i += 1
            elif a == "--allow-partial":
                cli_allow_partial = True; i += 1
            elif a == "--timing":
                cli_timing = True; i += 1
            else:
                i += 1

        if not has_path and cli_postgres_dsn is None:
            print("Usage: graph_fy extract <path> [--code-only] [options]", file=sys.stderr)
            print("error: must specify a path to scan or a --postgres DSN", file=sys.stderr)
            sys.exit(1)

        if no_dedup and dedup_llm:
            # --dedup-llm is pass 3 of the dedup pipeline, so with dedup off it
            # would be a silent no-op that still demands an API key.
            print(
                "error: --no-dedup and --dedup-llm are mutually exclusive "
                "(--dedup-llm is a tiebreaker inside the dedup pass)",
                file=sys.stderr,
            )
            sys.exit(2)

        _VALID_MODES = {"deep"}
        if extract_mode is not None and extract_mode not in _VALID_MODES:
            print(
                f"error: unknown --mode '{extract_mode}'. "
                f"Available: {', '.join(sorted(_VALID_MODES))}",
                file=sys.stderr,
            )
            sys.exit(2)
        deep_mode = extract_mode == "deep"
        if deep_mode:
            print("[graph_fy extract] deep mode enabled: richer semantic extraction")

        # CLI flag wins over env var. Setting graph_fy_API_TIMEOUT here so
        # _call_openai_compat picks it up without needing a new kwarg path.
        if cli_api_timeout is not None:
            os.environ["graph_fy_API_TIMEOUT"] = str(cli_api_timeout)
        if cli_max_workers is not None:
            os.environ["graph_fy_MAX_WORKERS"] = str(cli_max_workers)

        # Resolve output dir. The user-facing contract is "<out>/graph_fy_out/"
        # so a fresh checkout writes graph_fy_out/ at the project root, matching
        # the skill.md pipeline.
        out_root = (out_dir.resolve() if out_dir else target)
        graph_fy_out = out_root / _graph_fy_OUT
        graph_fy_out.mkdir(parents=True, exist_ok=True)
        if _graph_fy_OUT == "graph_fy_out":
            compat_link = out_root / "graph_fy_out"
            if not compat_link.exists() and not compat_link.is_symlink():
                try:
                    compat_link.symlink_to("graph_fy_out", target_is_directory=True)
                except Exception:
                    pass
        # Persist corpus-shaping options so later update/watch/hook rebuilds
        # use the same file set as the initial extraction (#1886).
        from graph_fy.watch import (
            _write_build_config as _write_build_cfg,
            _read_build_excludes as _read_build_ex,
            _read_build_gitignore as _read_build_gi,
        )
        # #1971 persistence: an explicit --no-gitignore persists False; a later
        # flag-less `graph_fy extract` must NOT clobber it back to True, which
        # would make the git-ignored code silently disappear again (the exact
        # complaint #1971 is about). Honor the persisted value for THIS run when
        # the flag is absent (read before the write below), and write False only
        # when the flag is set — None leaves the setting as-is, mirroring how
        # #1886 persists --exclude.
        _effective_gitignore = False if no_gitignore else _read_build_gi(graph_fy_out)
        # An explicit list replaces the persisted one; omission reuses it.
        _effective_excludes = cli_excludes or _read_build_ex(graph_fy_out)
        _write_build_cfg(
            graph_fy_out,
            excludes=cli_excludes or None,
            gitignore=False if no_gitignore else None,
        )

        stages = _StageTimer(cli_timing)

        from graph_fy.detect import (
            detect as _detect,
            detect_incremental as _detect_incremental,
            save_manifest as _save_manifest,
        )
        manifest_path = graph_fy_out / "manifest.json"
        existing_graph_path = graph_fy_out / "graph.json"
        # #1925: a missing manifest.json must not degrade to a full scan that
        # discards the existing graph's semantic layer. An existing graph.json
        # is a sufficient incremental baseline: detect_incremental treats an
        # absent manifest as "everything is new" (re-extract all, nothing
        # deleted), and build_merge + _stale_graph_sources reconcile replaced
        # and genuinely-deleted sources against the current corpus, so doc/
        # paper/image nodes survive a --code-only rebuild instead of being
        # dropped with the rest of the committed graph.
        incremental_mode = existing_graph_path.exists() if has_path else False
        # --force: full scan, not the manifest-gated incremental diff — a warm
        # unchanged tree would otherwise dispatch zero files (#1894).
        incremental_mode = incremental_mode and not force
        # #2923/#3125: --force --code-only must NOT drop the existing semantic layer.
        # The AST pass is fully replaced (full code re-scan, AST extraction on all
        # code files), but the semantic pass is skipped, so doc/paper/image nodes
        # from the existing graph carry forward via the merge (build_merge /
        # merge_raw_extraction keep them because no new semantic-tier sources
        # are dispatched). We do NOT set incremental_mode = True here because that
        # would run _detect_incremental and drop unchanged code files from the AST
        # pass; instead we keep incremental_mode = False so all code files are
        # scanned, while merge_existing_graph below ensures build_merge still runs.
        merge_existing_graph = incremental_mode or (code_only and existing_graph_path.exists())
        if force and code_only and existing_graph_path.exists():
            print(
                "[graph_fy extract] --force --code-only: full AST re-scan, "
                "existing semantic layer preserved (no semantic pass this run)"
            )
        if force:
            print("[graph_fy extract] --force: full re-scan, semantic cache reads skipped")
        elif incremental_mode and not manifest_path.exists():
            print(
                "[graph_fy extract] manifest.json missing; using existing "
                "graph.json as the incremental baseline (all files re-checked; "
                "nodes for files outside this run's scope are preserved)"
            )

        if not has_path:
            detection = {}
            code_files = []
            doc_files = []
            paper_files = []
            image_files = []
            deleted_files = []
            excluded_files = []
            graph_stale_sources = []
            unchanged_total = 0
            files_by_type = {}
        elif incremental_mode:
            print(f"[graph_fy extract] incremental scan of {target}")
            detection = _detect_incremental(
                target,
                manifest_path=str(manifest_path),
                google_workspace=google_workspace or None,
                extra_excludes=_effective_excludes or None,
                gitignore=_effective_gitignore,
            )
            files_by_type = detection.get("files", {})
            new_by_type = detection.get("new_files", {})
            code_files = [Path(p) for p in new_by_type.get("code", [])]
            doc_files = [Path(p) for p in new_by_type.get("document", [])]
            paper_files = [Path(p) for p in new_by_type.get("paper", [])]
            image_files = [Path(p) for p in new_by_type.get("image", [])]
            deleted_files = list(detection.get("deleted_files", []))
            excluded_files = list(detection.get("excluded_files", []))
            unchanged_total = sum(len(v) for v in detection.get("unchanged_files", {}).values())
            # #1909: derive the prune set from the existing graph itself, not
            # just the manifest. A file that became excluded without ever
            # being manifest-listed (every pre-#1897 graph is in this state)
            # still has stale nodes carried forward by build_merge unless the
            # graph's own sources are reconciled against the current corpus.
            _seen_files = {f for _fl in files_by_type.values() for f in _fl}
            _seen_files.update(detection.get("unclassified", []))
            graph_stale_sources = _stale_graph_sources(
                existing_graph_path, target, _seen_files, detection=detection
            )
            # #2543 heal: manifests poisoned BEFORE failed-source unstamping
            # existed carry live hashes for code files whose extraction failed
            # (missing extra, crash) — stamped up-to-date yet absent from
            # graph.json, so the incremental gate skips them forever. Treat
            # such a file as changed and re-queue it; if it fails again this
            # run it is now left unstamped, so this cannot wedge.
            _healed_sources = _zero_node_stamped_code_sources(
                existing_graph_path,
                target,
                detection.get("unchanged_files", {}).get("code", []),
            )
            if _healed_sources:
                print(
                    f"[graph_fy extract] re-queuing {len(_healed_sources)} "
                    f"manifest-stamped code file(s) with no nodes in graph.json "
                    f"(prior failed extraction, #2543)"
                )
                code_files.extend(Path(p) for p in _healed_sources)
            # #2927 heal: manifests poisoned BEFORE zero-node semantic cache rejection
            # existed carry live hashes for semantic files (doc/paper/image) whose
            # extraction produced zero nodes and zero hyperedges (e.g. edge-only).
            # Re-queue any such file so it is re-dispatched and self-heals.
            _unchanged_sem: list[str] = []
            for _k in ("document", "paper", "image"):
                _unchanged_sem.extend(detection.get("unchanged_files", {}).get(_k, []))
            _healed_sem_sources = _zero_node_stamped_semantic_sources(
                existing_graph_path,
                target,
                _unchanged_sem,
            )
            if _healed_sem_sources:
                print(
                    f"[graph_fy extract] re-queuing {len(_healed_sem_sources)} "
                    f"manifest-stamped semantic file(s) with no nodes or hyperedges in graph.json "
                    f"(prior empty/edge-only extraction, #2927)"
                )
                _healed_sem_set = set(_healed_sem_sources)
                for _p in detection.get("unchanged_files", {}).get("document", []):
                    if _p in _healed_sem_set:
                        doc_files.append(Path(_p))
                for _p in detection.get("unchanged_files", {}).get("paper", []):
                    if _p in _healed_sem_set:
                        paper_files.append(Path(_p))
                for _p in detection.get("unchanged_files", {}).get("image", []):
                    if _p in _healed_sem_set:
                        image_files.append(Path(_p))
        else:
            print(f"[graph_fy extract] scanning {target}")
            detection = _detect(
                target,
                google_workspace=google_workspace or None,
                extra_excludes=_effective_excludes or None,
                cache_root=out_root,
                gitignore=_effective_gitignore,
            )
            files_by_type = detection.get("files", {})
            code_files = [Path(p) for p in files_by_type.get("code", [])]
            doc_files = [Path(p) for p in files_by_type.get("document", [])]
            paper_files = [Path(p) for p in files_by_type.get("paper", [])]
            image_files = [Path(p) for p in files_by_type.get("image", [])]
            deleted_files = []
            excluded_files = []
            graph_stale_sources = []
            unchanged_total = 0
            if existing_graph_path.exists():
                _seen_files = {f for _fl in files_by_type.values() for f in _fl}
                _seen_files.update(detection.get("unclassified", []))
                graph_stale_sources = _stale_graph_sources(
                    existing_graph_path, target, _seen_files, detection=detection
                )

        if code_only:
            _skipped_semantic = [*doc_files, *paper_files, *image_files]
            if _skipped_semantic:
                _s_names = ", ".join(sorted({p.name for p in _skipped_semantic})[:6])
                _s_more = f" (+{len(_skipped_semantic) - 6} more)" if len(_skipped_semantic) > 6 else ""
                print(f"[graph_fy extract] --code-only: skipping {len(_skipped_semantic)} non-code file(s): {_s_names}{_s_more}")
            semantic_files = []
        elif deep_mode:
            semantic_files = [
                Path(p) for _k in ("document", "paper", "image")
                for p in files_by_type.get(_k, [])
            ]
        else:
            semantic_files = [*doc_files, *paper_files, *image_files]
        if incremental_mode:
            _excl_note = f"; {len(excluded_files)} excluded" if excluded_files else ""
            print(
                f"[graph_fy {cmd}] {len(code_files) + len(semantic_files)} code and doc file(s) changed; "
                f"{unchanged_total} unchanged; {len(deleted_files)} deleted"
                f"{_excl_note}"
            )
        else:
            print(
                f"[graph_fy {cmd}] found {len(code_files) + len(semantic_files)} code and doc file(s)"
            )
        # Surface files that were seen but not classified (extensionless non-shebang
        # project files like Dockerfile/Makefile, or unsupported extensions), so they
        # are no longer invisible in graph_fy's own output (#1692).
        _unclassified = detection.get("unclassified", []) if isinstance(detection, dict) else []
        if _unclassified:
            _names = ", ".join(sorted({Path(p).name for p in _unclassified})[:6])
            _more = f" (+{len(_unclassified) - 6} more)" if len(_unclassified) > 6 else ""
            print(
                f"[graph_fy extract] {len(_unclassified)} file(s) not classified "
                f"(no supported extension or shebang), skipped: {_names}{_more}"
            )
        # Name the files dropped by the sensitive-file filter so a wrongly-flagged
        # source/doc is visible, not just a count (#2106). Operational skips
        # (symlink/office/Workspace) carry a " [reason]" suffix; exclude those here
        # so this line reports only the security-heuristic drops.
        _sensitive = detection.get("skipped_sensitive", []) if isinstance(detection, dict) else []
        _sec = [s for s in _sensitive if " [" not in s]
        if _sec:
            _snames = ", ".join(sorted({Path(p).name for p in _sec})[:6])
            _smore = f" (+{len(_sec) - 6} more)" if len(_sec) > 6 else ""
            print(
                f"[graph_fy extract] {len(_sec)} file(s) skipped as potentially sensitive "
                f"(rename or move if wrongly flagged): {_snames}{_smore}"
            )
        stages.mark("detect")

        # Resolve the LLM backend only now that we know whether the corpus
        # needs one. A code-only corpus is pure local AST and must not require
        # an API key; the key is enforced below only when there's LLM work.
        from graph_fy.llm import (
            BACKENDS as _BACKENDS,
            detect_backend as _detect_backend,
            estimate_cost as _estimate_cost,
            extract_corpus_parallel as _extract_corpus_parallel,
            _format_backend_env_keys,
            _get_backend_api_key,
        )
        needs_llm = bool(semantic_files) or dedup_llm
        if backend is None and needs_llm:
            backend = _detect_backend()
        if backend is not None and backend not in _BACKENDS:
            print(
                f"error: unknown backend '{backend}'. "
                f"Available: {', '.join(sorted(_BACKENDS))}",
                file=sys.stderr,
            )
            sys.exit(1)
        if needs_llm:
            if backend is None:
                reasons = []
                if semantic_files:
                    reasons.append(
                        f"{len(semantic_files)} doc/paper/image file(s) need semantic extraction"
                    )
                if dedup_llm:
                    reasons.append("--dedup-llm was passed")
                hint = ""
                if semantic_files:
                    hint = (" Or pass --code-only to index just the code "
                            "(local AST, no key) and skip the non-code files.")
                print(
                    "error: no LLM API key found (" + "; ".join(reasons) + "). "
                    "Set GEMINI_API_KEY or GOOGLE_API_KEY (gemini), MOONSHOT_API_KEY "
                    "(kimi), ANTHROPIC_API_KEY (claude), OPENAI_API_KEY (openai), "
                    "DEEPSEEK_API_KEY (deepseek), or pass --backend. A code-only "
                    "corpus needs no key." + hint,
                    file=sys.stderr,
                )
                sys.exit(1)
            if backend == "ollama":
                from graph_fy.llm import _validate_ollama_base_url
                _oll_url = os.environ.get("OLLAMA_BASE_URL", _BACKENDS["ollama"].get("base_url", ""))
                try:
                    _validate_ollama_base_url(_oll_url, warn=False)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    sys.exit(2)
            if not _get_backend_api_key(backend):
                allow_no_key = False
                if backend == "ollama":
                    from urllib.parse import urlparse
                    ollama_url = os.environ.get(
                        "OLLAMA_BASE_URL",
                        _BACKENDS["ollama"].get("base_url", ""),
                    )
                    try:
                        host = (urlparse(ollama_url).hostname or "").lower()
                    except Exception:
                        host = ""
                    allow_no_key = (
                        host in ("localhost", "127.0.0.1", "::1")
                        or host.startswith("127.")
                    )
                elif backend == "bedrock":
                    allow_no_key = bool(
                        os.environ.get("AWS_PROFILE")
                        or os.environ.get("AWS_REGION")
                        or os.environ.get("AWS_DEFAULT_REGION")
                        or os.environ.get("AWS_ACCESS_KEY_ID")
                    )
                elif backend == "claude-cli":
                    import shutil as _shutil
                    allow_no_key = _shutil.which("claude") is not None
                    if not allow_no_key:
                        print(
                            "error: backend 'claude-cli' requires the `claude` CLI on $PATH "
                            "(install Claude Code and run `claude` once to authenticate).",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                if not allow_no_key:
                    print(
                        f"error: backend '{backend}' requires {_format_backend_env_keys(backend)} to be set.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        # Track whether this run's extraction was incomplete (a whole extractor
        # pass crashed, or some semantic chunks failed). A partial result must not
        # be force-written over a good complete graph — the final write falls back
        # to the #479 shrink guard unless --allow-partial is set.
        _extraction_incomplete = False
        # A walk that couldn't fully enumerate the corpus (permission-denied
        # subtree, I/O error) yields a legitimately smaller graph that must not
        # be force-written over a complete one — same failure class as a crashed
        # pass. detect()/detect_incremental() already record these; consume them.
        if detection.get("walk_errors"):
            _extraction_incomplete = True

        if incremental_mode:
            from graph_fy.extractors.terraform import refresh_terraform_paths
            code_files = refresh_terraform_paths(
                code_files, [Path(p) for p in files_by_type.get("code", [])],
                [Path(p) for p in [*deleted_files, *excluded_files, *graph_stale_sources]],
            )

        # AST extraction on code files. Empty code list (docs-only corpus) is
        # the issue #698 case — skip cleanly instead of crashing inside extract().
        ast_result: dict = {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
        if code_files:
            from graph_fy.extract import extract as _ast_extract
            # Anchor the cache at the output root, not the scanned project:
            # with --out, a <target>/graph_fy_out/cache/ would leak a
            # graph_fy_out/ dir into a project that asked for external output.
            # `root` stays the scanned project so source_file/ids relativize
            # against it; conflating the two basenamed every node (#1941).
            ast_kwargs: dict = {"cache_root": out_root, "root": target}
            if cli_max_workers is not None:
                ast_kwargs["max_workers"] = cli_max_workers
            # #2437/#2438 (the `graph_fy update` twin of watch's #2406 fix): an
            # incremental re-scan extracts only the changed code files, so the
            # cross-file resolvers cannot see a callee living in an unchanged
            # file and every changed->unchanged call edge silently vanished on
            # merge. Hand extract() read-only resolution context from the
            # persisted graph: its AST-tier nodes (including bounded resolver
            # metadata) plus the structural edges the resolvers walk, scoped to
            # the UNCHANGED
            # live corpus — never a re-extracted, deleted, or excluded file, so
            # stale symbols cannot resurrect. Fails open (changed-batch-only
            # resolution, the pre-fix behavior) on an unreadable graph.
            if incremental_mode and existing_graph_path.exists():
                _ctx_nodes: list[dict] = []
                _ctx_edges: list[dict] = []
                try:
                    from graph_fy.build import _is_ast_tier as _ctx_is_ast_tier
                    from graph_fy.security import (
                        check_graph_file_size_cap as _ctx_size_cap,
                    )
                    _ctx_size_cap(existing_graph_path)
                    _ctx_graph = json.loads(
                        existing_graph_path.read_text(encoding="utf-8")
                    )
                    _ctx_root = Path(os.path.abspath(target))

                    def _ctx_identity(source_file) -> str | None:
                        # graph.json source_file values are relative to the
                        # scanned root (`root=target` above); detect's
                        # unchanged_files keep their scan-time form. Compare
                        # both as absolute posix paths.
                        if not source_file:
                            return None
                        _p = Path(str(source_file))
                        if not _p.is_absolute():
                            _p = _ctx_root / _p
                        return Path(os.path.abspath(_p)).as_posix()

                    _ctx_live = {
                        _ctx_identity(f)
                        for _flist in detection.get("unchanged_files", {}).values()
                        for f in _flist
                    }
                    _ctx_live.discard(None)
                    _ctx_live.difference_update(_ctx_identity(p) for p in code_files)
                    for _node in _ctx_graph.get("nodes", []):
                        if not _node.get("id") or not _ctx_is_ast_tier(_node):
                            continue
                        _sf = _node.get("source_file")
                        if not _sf or _ctx_identity(_sf) not in _ctx_live:
                            continue
                        _ctx_node = {
                            "id": _node["id"],
                            "label": _node.get("label"),
                            "source_file": _sf,
                            "file_type": _node.get("file_type"),
                            "type": _node.get("type"),
                        }
                        for _marker in ("_callable", "_callable_class"):
                            if _node.get(_marker):
                                _ctx_node[_marker] = _node[_marker]
                        _metadata = _node.get("metadata")
                        if isinstance(_metadata, dict):
                            _ruby_metadata = {
                                key: _metadata[key]
                                for key in (
                                    "ruby_resolution_schema",
                                    "ruby_method_kind",
                                    "ruby_lookup_unsafe",
                                    "ruby_reopened",
                                    "ruby_external_method_owners",
                                )
                                if key in _metadata
                            }
                            if _ruby_metadata:
                                _ctx_node["metadata"] = _ruby_metadata
                        _ctx_nodes.append(_ctx_node)
                    for _edge in _ctx_graph.get(
                        "links", _ctx_graph.get("edges", [])
                    ):
                        if _edge.get("relation") not in (
                            "contains", "method", "inherits"
                        ):
                            continue
                        if not _ctx_is_ast_tier(_edge):
                            continue
                        _sf = _edge.get("source_file")
                        if not _sf or _ctx_identity(_sf) not in _ctx_live:
                            continue
                        _ctx_edge = {
                            "source": _edge.get("source"),
                            "target": _edge.get("target"),
                            "relation": _edge.get("relation"),
                            "source_file": _sf,
                        }
                        _edge_metadata = _edge.get("metadata")
                        if (
                            isinstance(_edge_metadata, dict)
                            and isinstance(
                                _edge_metadata.get("ruby_superclass_ref"), str
                            )
                        ):
                            _ctx_edge["metadata"] = {
                                "ruby_superclass_ref": _edge_metadata[
                                    "ruby_superclass_ref"
                                ]
                            }
                            _lexical_scopes = _edge_metadata.get(
                                "ruby_lexical_scopes"
                            )
                            if isinstance(_lexical_scopes, list) and all(
                                isinstance(_scope, str)
                                for _scope in _lexical_scopes
                            ):
                                _ctx_edge["metadata"]["ruby_lexical_scopes"] = list(
                                    _lexical_scopes
                                )
                        _ctx_edges.append(_ctx_edge)
                except Exception:
                    _ctx_nodes, _ctx_edges = [], []
                if _ctx_nodes:
                    ast_kwargs["resolution_context_nodes"] = _ctx_nodes
                if _ctx_edges:
                    ast_kwargs["resolution_context_edges"] = _ctx_edges
            print(f"[graph_fy extract] AST extraction on {len(code_files)} code files...")
            try:
                ast_result = _ast_extract(code_files, **ast_kwargs)
            except Exception as exc:
                print(f"[graph_fy extract] AST extraction failed: {exc}", file=sys.stderr)
                # #2445: losing the whole AST pass is fatal by default. The
                # empty stand-in only reaches the shrink guard when an existing
                # graph is larger — on a fresh build it used to be written as a
                # 0-node graph with exit 0, indistinguishable from success.
                # --allow-partial opts back into the best-effort continuation.
                if not cli_allow_partial:
                    sys.exit(1)
                ast_result = {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
                _extraction_incomplete = True  # the whole AST pass was lost
        stages.mark("AST extract")

        # Semantic extraction on docs/papers/images. Check cache first.
        from graph_fy.cache import (
            check_semantic_cache as _check_semantic_cache,
            prune_semantic_cache as _prune_semantic_cache,
            save_semantic_cache as _save_semantic_cache,
            scope_semantic_result as _scope_semantic_result,
        )
        sem_result: dict = {
            "nodes": [], "edges": [], "hyperedges": [],
            "input_tokens": 0, "output_tokens": 0,
        }
        # Semantic files whose extraction truncated this run. They are left
        # unstamped in the manifest so detect_incremental re-queues them next run
        # (mirrors the #933 failed-chunk handling); captured below before the
        # _partial markers are stripped from the corpus.
        _partial_semantic_files: set[str] = set()
        sem_cache_hits = 0
        sem_cache_misses = 0
        # Deep mode uses its own namespace (cache/semantic-deep/) so deep and
        # standard results for the same content never shadow each other (#1894).
        sem_cache_mode = "deep" if deep_mode else None
        # Entries are attributed to the extraction prompt that produced them, so
        # a release that changes the prompt re-extracts rather than replaying the
        # older vintage alongside the new one (#1939). Read and write must pass
        # the same prompt, or the write lands where the next read won't look.
        from graph_fy.llm import _extraction_system as _sem_prompt_for
        sem_prompt = _sem_prompt_for(deep=deep_mode)
        if semantic_files:
            sem_paths_str = [str(p) for p in semantic_files]
            if force:
                # --force: skip the cache READ so every semantic file is
                # re-dispatched; the save below still runs so the fresh
                # results replace the stale entries.
                cached_nodes, cached_edges, cached_hyperedges = [], [], []
                uncached_paths = list(sem_paths_str)
            else:
                cached_nodes, cached_edges, cached_hyperedges, uncached_paths = (
                    _check_semantic_cache(sem_paths_str, root=target, cache_root=out_root,
                                          mode=sem_cache_mode, prompt=sem_prompt)
                )
            sem_cache_hits = len(semantic_files) - len(uncached_paths)
            sem_cache_misses = len(uncached_paths)
            sem_result["nodes"].extend(cached_nodes)
            sem_result["edges"].extend(cached_edges)
            sem_result["hyperedges"].extend(cached_hyperedges)
            if sem_cache_hits:
                print(f"[graph_fy extract] semantic cache: {sem_cache_hits} hit / {sem_cache_misses} miss")

            if uncached_paths:
                print(f"[graph_fy extract] semantic extraction on {len(uncached_paths)} files via {backend}...")
                corpus_kwargs: dict = {
                    "backend": backend,
                    "model": model,
                    "root": target,
                    "cache_root": out_root,
                }
                if deep_mode:
                    corpus_kwargs["deep_mode"] = True
                if cli_token_budget is not None:
                    corpus_kwargs["token_budget"] = cli_token_budget
                if cli_max_concurrency is not None:
                    corpus_kwargs["max_concurrency"] = cli_max_concurrency

                # Minimal progress callback so the CLI is no longer silent
                # during long local-inference runs (issue #792 addendum).
                # Also track per-chunk success so we can fail loudly when
                # every chunk errors (e.g. missing backend SDK package).
                _chunk_stats = {"total": 0, "succeeded": 0}
                def _progress(idx: int, total: int, _result: dict) -> None:
                    _chunk_stats["total"] = total
                    _chunk_stats["succeeded"] += 1
                    print(
                        f"[graph_fy extract] chunk {idx + 1}/{total} done",
                        flush=True,
                    )
                corpus_kwargs["on_chunk_done"] = _progress

                try:
                    fresh = _extract_corpus_parallel(
                        [Path(p) for p in uncached_paths],
                        **corpus_kwargs,
                    )
                except ImportError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    sys.exit(1)
                except Exception as exc:
                    print(
                        f"[graph_fy extract] semantic extraction failed: {exc}",
                        file=sys.stderr,
                    )
                    fresh = {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}
                    _extraction_incomplete = True  # the semantic pass crashed

                # on_chunk_done only fires after a chunk succeeds. If fresh
                # semantic extraction was requested and no chunks completed,
                # fail instead of writing an AST-only graph with exit 0.
                if uncached_paths and _chunk_stats["succeeded"] == 0:
                    print(
                        f"[graph_fy extract] error: all semantic chunks failed "
                        f"for backend '{backend}' ({len(uncached_paths)} uncached files) - "
                        f"see per-chunk errors above. If you see 'requires the X package', "
                        f"run `pip install X` and retry.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                # Some (but not all) chunks failed — the graph is missing nodes
                # from the failed chunks, so it must not clobber a larger complete
                # graph without an explicit --allow-partial override.
                if _chunk_stats["total"] and _chunk_stats["succeeded"] < _chunk_stats["total"]:
                    _extraction_incomplete = True
                # #2926: scope the fresh result to the files actually dispatched,
                # mirroring the allowed_source_files guard the cache write below
                # applies. A model can attribute stray nodes/edges to a corpus
                # file that was not dispatched this run; build_merge() derives
                # its replace-set from the source_files present in new chunks,
                # so such a fragment would REPLACE that file's entire prior
                # contribution in graph.json while its manifest entry still says
                # unchanged — no later incremental run re-dispatches it and the
                # loss is permanent until a full rebuild.
                _dropped_files, _dropped_items = _scope_semantic_result(
                    fresh, target, uncached_paths,
                )
                if _dropped_files:
                    print(
                        f"[graph_fy extract] dropped {_dropped_items} out-of-scope "
                        f"item(s) attributed to {len(_dropped_files)} file(s) not "
                        f"dispatched this run: {', '.join(sorted(_dropped_files))}"
                    )
                # Which files truncated this run (item markers + the empty-parse
                # _partial_files set). Computed BEFORE the save so it can be passed
                # as partial_source_files: without it, a file whose only truncated
                # chunk parsed empty (so it has no item markers here) would be
                # written as a complete cache entry, re-promoting it (#1950).
                from graph_fy.llm import (
                    _partial_source_files as _partial_sf,
                    _strip_partial_markers as _strip_partial,
                )
                _partial_semantic_files = set(_partial_sf(fresh))
                # A chunk that came back hollow after every retry, or as
                # unparseable JSON, or that simply omitted some of its files,
                # does not raise - it returns fewer nodes - so it counted as a
                # SUCCEEDED chunk above and the run read as complete, force=True
                # bypassed the shrink guard, and a 570-node graph was overwritten
                # with 111 nodes without a word (#3105). With an LLM backend
                # that is the normal way an extraction silently produces a
                # fraction of the graph, so it must arm the guard exactly like a
                # crashed chunk does. --allow-partial still overrides.
                _omitted_files = list(fresh.get("uncovered_files") or [])
                if _omitted_files or _partial_semantic_files:
                    _extraction_incomplete = True
                    print(
                        f"[graph_fy extract] semantic extraction is incomplete: "
                        f"{len(_omitted_files)} dispatched file(s) produced no nodes and "
                        f"{len(_partial_semantic_files)} came back truncated or hollow. "
                        f"The shrink guard stays armed for this write; pass "
                        f"--allow-partial to overwrite a larger existing graph anyway.",
                        file=sys.stderr,
                    )
                try:
                    _save_semantic_cache(
                        fresh.get("nodes", []),
                        fresh.get("edges", []),
                        fresh.get("hyperedges", []),
                        root=target,
                        cache_root=out_root,
                        allowed_source_files=uncached_paths,
                        mode=sem_cache_mode,
                        prompt=sem_prompt,
                        partial_source_files=_partial_semantic_files or None,
                    )
                except Exception as exc:
                    print(f"[graph_fy extract] warning: could not write semantic cache: {exc}", file=sys.stderr)
                # Strip the markers before the corpus feeds the graph so the
                # internal flag never leaks into graph.json.
                _strip_partial(fresh)
                sem_result["nodes"].extend(fresh.get("nodes", []))
                sem_result["edges"].extend(fresh.get("edges", []))
                sem_result["hyperedges"].extend(fresh.get("hyperedges", []))
                sem_result["input_tokens"] += fresh.get("input_tokens", 0)
                sem_result["output_tokens"] += fresh.get("output_tokens", 0)

        # Prune orphaned semantic cache entries. The semantic cache is
        # content-hash-keyed and unversioned, so it is never swept by the AST
        # version-cleanup: every content change or file deletion leaves a
        # permanent orphan that accumulates unbounded (#1527). Sweep it against
        # the FULL live document set (``files_by_type`` — present in both the
        # incremental and full branches), NOT the incremental ``semantic_files``
        # changed-subset, which would delete every unchanged doc's valid entry.
        # Best-effort: a prune failure must never break extraction.
        # Hash keys are anchored to the corpus (``target``) — the same anchor
        # the cache read/write above use — while the stat-index artifact
        # follows the cache location (``out_root``). Anchoring these hashes to
        # ``out_root`` instead would mismatch every key under ``--out`` and
        # sweep the entire fresh cache as orphaned (#1990/#1991).
        try:
            from graph_fy.cache import file_hash as _file_hash
            _live_hashes: set[str] = set()
            for _kind in ("document", "paper", "image"):
                for _fp in files_by_type.get(_kind, []):
                    _abs = Path(_fp)
                    if not _abs.is_absolute():
                        _abs = Path(target) / _abs
                    if not _abs.is_file():
                        continue  # deleted/missing — leave out so its entry is pruned
                    try:
                        _live_hashes.add(_file_hash(_abs, target, cache_root=out_root))
                    except OSError:
                        pass
            # A pathless database extraction has no filesystem corpus to sweep.
            if has_path:
                _prune_semantic_cache(out_root, _live_hashes)
        except Exception as exc:
            print(f"[graph_fy extract] warning: could not prune semantic cache: {exc}", file=sys.stderr)
        stages.mark("semantic extract")

        pg_result: dict = {"nodes": [], "edges": []}
        if cli_postgres_dsn is not None:
            from graph_fy.pg_introspect import introspect_postgres
            print(f"[graph_fy extract] introspecting PostgreSQL schema...")
            try:
                pg_result = introspect_postgres(cli_postgres_dsn)
            except (ConnectionError, ImportError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"[graph_fy extract] PostgreSQL: {len(pg_result['nodes'])} nodes, "
                  f"{len(pg_result['edges'])} edges")

        cargo_result: dict = {"nodes": [], "edges": []}
        if cli_cargo:
            from graph_fy.cargo_introspect import introspect_cargo
            print("[graph_fy extract] introspecting Cargo workspace...")
            try:
                cargo_result = introspect_cargo(target)
            except (ConnectionError, ImportError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"[graph_fy extract] Cargo: {len(cargo_result['nodes'])} nodes, "
                  f"{len(cargo_result['edges'])} edges")

        scip_result: dict = {"nodes": [], "edges": []}
        scip_candidates = [
            target / "index.scip.json",
            target / "scip.json",
            target / ".scip" / "index.scip.json",
            target / ".scip" / "scip.json",
        ]
        for scip_path in scip_candidates:
            if scip_path.is_file():
                try:
                    from graph_fy.scip_ingest import ingest_scip_json
                    scip_data = json.loads(scip_path.read_text(encoding="utf-8", errors="replace"))
                    scip_res = ingest_scip_json(scip_data, source_file=str(scip_path))
                    if scip_res.get("nodes") or scip_res.get("edges"):
                        scip_result["nodes"].extend(scip_res["nodes"])
                        scip_result["edges"].extend(scip_res["edges"])
                        print(f"[graph_fy extract] SCIP compiler index ingested ({len(scip_res['nodes'])} nodes, {len(scip_res['edges'])} edges)")
                        break
                except Exception as exc:
                    print(f"[graph_fy extract] warning: failed to ingest SCIP index at {scip_path}: {exc}", file=sys.stderr)

        # Merge AST + semantic + pg_result + cargo_result + scip_result. Order matters for deduplication: passing AST
        # first means semantic node attributes win on collision (richer labels
        # for symbols also referenced in docs). Hyperedges only come from the
        # semantic side.
        merged: dict = {
            "nodes": list(ast_result.get("nodes", [])) + list(sem_result.get("nodes", [])) + list(pg_result.get("nodes", [])) + list(cargo_result.get("nodes", [])) + list(scip_result.get("nodes", [])),
            "edges": list(ast_result.get("edges", [])) + list(sem_result.get("edges", [])) + list(pg_result.get("edges", [])) + list(cargo_result.get("edges", [])) + list(scip_result.get("edges", [])),
            "hyperedges": list(sem_result.get("hyperedges", [])),
            "input_tokens": ast_result.get("input_tokens", 0) + sem_result.get("input_tokens", 0),
            "output_tokens": ast_result.get("output_tokens", 0) + sem_result.get("output_tokens", 0),
            "extracted_sources": list(ast_result.get("extracted_sources", [])),
        }

        graph_json_path = graph_fy_out / "graph.json"
        analysis_path = graph_fy_out / ".graph_fy_analysis.json"

        # Build a manifest-safe files dict: only stamp semantic_hash for files
        # that actually produced output (cache hit or fresh extraction). Files
        # whose chunk failed have no source_file entry in sem_result — leaving
        # their semantic_hash empty so detect_incremental re-queues them (#933).
        # Path normalization against the scan root happens inside the helper
        # (#1897) so fresh root-relative source_files match detect()'s
        # absolute file lists.
        # #2543: also drop AST sources that failed (missing optional extra /
        # zero-node anomaly) so they are not frozen as up-to-date.
        _failed_ast_sources = list(ast_result.get("failed_sources") or [])
        _manifest_files = _stamped_manifest_files(
            files_by_type,
            sem_result,
            target,
            partial_source_files=_partial_semantic_files,
            failed_ast_sources=_failed_ast_sources,
        )

        # Files dispatched this run but dropped by _stamped_manifest_files
        # above (failed chunk, LLM omission, or any future exclusion) still
        # carry a stale semantic_hash from a prior successful run in the
        # on-disk manifest; save_manifest's seed loop would otherwise copy it
        # verbatim and mask the omission (#1948). Derived from semantic_files
        # — what was actually SENT to the backend this run (narrowed by the
        # incremental gate and --code-only, widened by deep mode) — NOT from
        # files_by_type: the full live corpus includes untouched files that
        # were never dispatched, and clearing those would blank the whole
        # manifest on every partial incremental run, forcing a full-corpus
        # re-extraction on the next one.
        _stamped_semantic = {
            f for _flist in _manifest_files.values() for f in _flist
        }
        _cleared_semantic = {str(p) for p in semantic_files} - _stamped_semantic
        # #2543: AST failures need both hashes blanked (clear_ast), not just
        # semantic_hash — otherwise a prior bad stamp keeps the file "unchanged".
        _cleared_ast = set(_failed_ast_sources)

        # Full-scan manifest saves prune rows for in-root files that left the
        # scan corpus but still exist on disk (#1908). The corpus must be the
        # RAW detect output (files_by_type), NOT the #933-stamp-filtered
        # _manifest_files above — pruning to the filtered set would erase
        # failed-chunk/omitted-doc rows and every doc row on --code-only runs.
        _scan_corpus = (
            {f for _fl in files_by_type.values() for f in _fl}
            if has_path else None
        )

        def _invalidate_file_manifest_for_db_graph() -> None:
            if has_path:
                return
            try:
                manifest_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"error: could not invalidate file manifest: {exc}", file=sys.stderr)
                sys.exit(1)

        if no_cluster:
            # --no-cluster: dump the raw merged extraction as graph.json.
            # No NetworkX, no community detection, no analysis sidecar.
            # Dedupe nodes (by id) and parallel edges so the raw output matches the
            # clustered path (whose DiGraph collapses both) and stays deterministic
            # across modes (#1317; node dedup also collapses shared Swift module
            # anchors emitted per importing file, #1327).
            from graph_fy.build import dedupe_edges as _dedupe_edges, dedupe_nodes as _dedupe_nodes
            from graph_fy.export import (
                backup_if_protected as _backup,
                existing_graph_node_count as _existing_graph_node_count,
            )
            if (
                incremental_mode
                and not code_files
                and not semantic_files
                and not deleted_files
                and not pg_result.get("nodes")
                and not pg_result.get("edges")
                and not cargo_result.get("nodes")
                and not cargo_result.get("edges")
            ):
                # An exclusion-only change reaches this gate (excluded files
                # are deliberately NOT in deleted_files, #1908) but must still
                # scrub the newly-excluded sources from the raw graph (#1909).
                # This path never runs build_merge, so prune in place.
                if graph_stale_sources:
                    _n_pruned = _prune_graph_json_sources(
                        existing_graph_path, graph_stale_sources
                    )
                    if _n_pruned:
                        print(
                            f"[graph_fy extract] pruned {_n_pruned} node(s) from "
                            f"{len(graph_stale_sources)} source file(s) no longer "
                            "in the scan (deleted or excluded)."
                        )
                print(
                    "[graph_fy extract] no incremental changes detected "
                    "(--no-cluster); outputs left untouched."
                )
                try:
                    _save_manifest(_manifest_files, manifest_path=str(manifest_path), kind="both", root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic, clear_ast=_cleared_ast or None)
                except Exception as exc:
                    print(f"[graph_fy extract] warning: could not write manifest: {exc}", file=sys.stderr)
                stages.total()
                sys.exit(0)

            if merge_existing_graph:
                # #2169: this raw path used to write ONLY this run's extraction
                # over graph.json — on an incremental run that is just the
                # changed files, silently dropping every node/edge owned by an
                # unchanged file. Merge the existing graph forward first, with
                # the same replace/prune semantics as the clustered path's
                # build_merge: re-extracted sources replaced, deleted +
                # excluded + graph-stale sources pruned, everything else
                # carried. Survivors are prepended, so the dedupe below keeps
                # this run's fresh attributes for re-extracted nodes.
                from graph_fy.build import merge_raw_extraction as _merge_raw_extraction
                _raw_prune_sources: list[str] = list(deleted_files)
                for _src in list(excluded_files) + graph_stale_sources:
                    if _src not in _raw_prune_sources:
                        _raw_prune_sources.append(_src)
                try:
                    merged = _merge_raw_extraction(
                        merged,
                        graph_path=existing_graph_path,
                        prune_sources=_raw_prune_sources or None,
                        root=target,
                    )
                except RuntimeError as exc:
                    # Existing graph present but unparseable: refuse to
                    # raw-dump this run's partial extraction over it.
                    print(f"error: {exc}", file=sys.stderr)
                    sys.exit(1)
                _shrink = _handle_unverified_semantic_shrink(
                    merged.get("_unverified_semantic_shrink"),
                    cli_allow_partial=cli_allow_partial,
                    files_by_type=files_by_type,
                    sem_result=sem_result,
                    target=target,
                    partial_semantic_files=_partial_semantic_files,
                    failed_ast_sources=_failed_ast_sources,
                    semantic_files=semantic_files,
                )
                if _shrink is not None and _shrink[0]:
                    _extraction_incomplete = True
                    _manifest_files = _shrink[1]
                    _stamped_semantic = {
                        f for _flist in _manifest_files.values() for f in _flist
                    }
                    _cleared_semantic = _shrink[2]
            merged["nodes"] = _dedupe_nodes(merged["nodes"])
            merged["edges"] = _dedupe_edges(merged["edges"])
            # Disambiguate colliding-basename file-node labels (#2032). This raw
            # --no-cluster path bypasses build_from_json (where the clustered path
            # gets this), so apply it directly on the merged node list.
            from graph_fy.build import disambiguate_file_labels_in_nodes as _disamb_labels
            _disamb_labels(merged["nodes"])
            # Backfill source_file from endpoint nodes — this raw path bypasses
            # build_from_json's backfill, and semantic edges sometimes omit it (#1279).
            _node_sf = {n.get("id"): n.get("source_file") for n in merged["nodes"]}
            for _e in merged["edges"]:
                if not _e.get("source_file"):
                    _e["source_file"] = (
                        _node_sf.get(_e.get("source")) or _node_sf.get(_e.get("target")) or ""
                    )
            # RT-parity for the raw path: an incomplete build must not force a
            # partial graph over a larger complete one here either. The clustered
            # path gets this from to_json's #479 guard; this path never calls
            # to_json, so replicate the shrink check against the existing file and
            # exit before the write/manifest unless --allow-partial is set.
            if _extraction_incomplete and not cli_allow_partial:
                from graph_fy.export import MALFORMED_GRAPH as _MALFORMED_GRAPH
                _existing_n = _existing_graph_node_count(graph_json_path)
                _malformed = _existing_n is _MALFORMED_GRAPH
                _shrinks = isinstance(_existing_n, int) and len(merged["nodes"]) < _existing_n
                if _malformed or _shrinks:
                    _detail = (
                        f"the existing {graph_json_path} is present but unparseable "
                        "(corrupt or a mid-write), so a shrink cannot be ruled out"
                        if _malformed
                        else f"smaller than the existing {graph_json_path} "
                        f"({len(merged['nodes'])} < {_existing_n} nodes)"
                    )
                    print(
                        "[graph_fy extract] error: extraction was incomplete (an AST/"
                        f"semantic pass failed) and the resulting --no-cluster graph is {_detail}. "
                        "Refusing to overwrite a complete graph with a partial one. Re-run after "
                        "fixing the failures, or pass --allow-partial to overwrite anyway.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            _backup(graph_fy_out)
            _invalidate_file_manifest_for_db_graph()
            from graph_fy.paths import write_json_atomic as _write_json_atomic
            _write_json_atomic(graph_json_path, merged, indent=2)
            try:
                # Record the scan root so a later build_merge / update runbook can
                # relativize deleted-file paths correctly even for a custom --out
                # (its grandparent-of-graph.json fallback points at the wrong dir
                # otherwise, and deleted files never prune — #2012/#1571).
                (graph_fy_out / ".graph_fy_root").write_text(
                    str(Path(target).resolve()), encoding="utf-8"
                )
            except OSError:
                pass
            stages.mark("write")
            try:
                from graph_fy.build import build as _build_bare
                _bare_G = _build_bare([merged], root=target)
                from graph_fy.retrieval import build_index as _build_fts_index
                _build_fts_index(_bare_G, graph_fy_out / "graph.db")
                print(f"[graph_fy extract] wrote {graph_fy_out / 'graph.db'} (unified graph + FTS5 search index)")
            except Exception as _idx_err:
                print(f"[graph_fy extract] warning: could not build FTS5 index: {_idx_err}", file=sys.stderr)
            cost = _estimate_cost(
                backend, merged["input_tokens"], merged["output_tokens"]
            )
            print(
                f"[graph_fy extract] wrote {graph_json_path} — "
                f"{len(merged['nodes'])} nodes, {len(merged['edges'])} edges "
                f"(no clustering)"
            )
            if merged["input_tokens"] or merged["output_tokens"]:
                print(
                    f"[graph_fy extract] tokens: "
                    f"{merged['input_tokens']:,} in / "
                    f"{merged['output_tokens']:,} out, "
                    f"est. cost: ${cost:.4f}"
                )
            try:
                if has_path:
                    _save_manifest(_manifest_files, manifest_path=str(manifest_path), kind="both", root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic, clear_ast=_cleared_ast or None)
            except Exception as exc:
                print(f"[graph_fy extract] warning: could not write manifest: {exc}", file=sys.stderr)
            if global_merge:
                from graph_fy.global_graph import global_add as _global_add
                _tag = global_repo_tag or target.name
                try:
                    result = _global_add(graph_fy_out / "graph.json", _tag)
                    if result["skipped"]:
                        print(f"[graph_fy global] '{_tag}' unchanged since last add - skipped.")
                    else:
                        print(f"[graph_fy global] '{_tag}' merged into global graph "
                              f"(+{result['nodes_added']} nodes, -{result['nodes_removed']} pruned).")
                except Exception as exc:
                    print(f"[graph_fy global] warning: failed to merge into global graph: {exc}", file=sys.stderr)
            stages.total()
            sys.exit(0)

        # Build graph + cluster + score + write.
        from graph_fy.build import (
            build as _build,
            build_from_json as _build_from_json,
            build_merge as _build_merge,
        )
        from graph_fy.cluster import cluster as _cluster, score_all as _score_all
        from graph_fy.export import to_json as _to_json
        from graph_fy.analyze import god_nodes as _god_nodes, surprising_connections as _surprising
        dedup_backend = backend if dedup_llm else None
        if merge_existing_graph:
            # Prune everything the current scan no longer covers: genuinely
            # deleted manifest rows, excluded-but-alive manifest rows (#1908),
            # and the graph's own stale sources — which catches files that
            # became excluded without ever being manifest-listed (#1909).
            _prune_sources: list[str] = list(deleted_files)
            for _src in list(excluded_files) + graph_stale_sources:
                if _src not in _prune_sources:
                    _prune_sources.append(_src)
            try:
                G = _build_merge(
                    [merged],
                    graph_path=existing_graph_path,
                    prune_sources=_prune_sources or None,
                    dedup=not no_dedup,
                    dedup_llm_backend=dedup_backend,
                    root=target,
                )
                _shrink = _handle_unverified_semantic_shrink(
                    G.graph.get("_unverified_semantic_shrink") if hasattr(G, "graph") else None,
                    cli_allow_partial=cli_allow_partial,
                    files_by_type=files_by_type,
                    sem_result=sem_result,
                    target=target,
                    partial_semantic_files=_partial_semantic_files,
                    failed_ast_sources=_failed_ast_sources,
                    semantic_files=semantic_files,
                )
                if _shrink is not None and _shrink[0]:
                    _extraction_incomplete = True
                    _manifest_files = _shrink[1]
                    _stamped_semantic = {
                        f for _flist in _manifest_files.values() for f in _flist
                    }
                    _cleared_semantic = _shrink[2]
            except ValueError as exc:
                # --no-dedup arms build_merge's #479 shrink guard, which refuses
                # to drop nodes belonging to files this run neither re-extracted
                # nor pruned. Report the refusal instead of a traceback (#2881):
                # graph.json on disk is untouched, so the old graph is intact.
                print(f"[graph_fy extract] {exc}", file=sys.stderr)
                sys.exit(1)
        else:
            G = _build([merged], dedup=not no_dedup, dedup_llm_backend=dedup_backend, root=target)
        stages.mark("build")
        if G.number_of_nodes() == 0:
            print(
                "[graph_fy extract] graph is empty — extraction produced no nodes. "
                "Possible causes: all files skipped, binary-only corpus, or LLM "
                "returned no edges.",
                file=sys.stderr,
            )
            sys.exit(1)

        communities = _cluster(G, resolution=cli_resolution, exclude_hubs_percentile=cli_exclude_hubs)
        stages.mark("cluster")
        cohesion = _score_all(G, communities)
        try:
            # The percentile that suppressed hubs in cluster() above suppresses
            # them in the ranking too (#3205).
            gods = _god_nodes(G, exclude_hubs_percentile=cli_exclude_hubs)
        except Exception:
            gods = []
        try:
            surprises = _surprising(G, communities)
        except Exception:
            surprises = []
        stages.mark("analyze")

        from graph_fy.export import backup_if_protected as _backup
        _backup(graph_fy_out)
        _invalidate_file_manifest_for_db_graph()
        # force=True bypasses the #479 shrink guard entirely. A full build
        # legitimately shrinks (fuzzy dedup collapse, deleted code) so it keeps
        # force=True — EXCEPT when this run's extraction was incomplete (an
        # extractor pass crashed or some semantic chunks failed). Then a partial
        # graph could silently overwrite a good complete one, so fall back to the
        # shrink guard (force=False) unless the user opts in with --allow-partial.
        #
        # Both write paths are guarded: the clustered path here via to_json's
        # #479 check, and the `--no-cluster` raw-dump path above via the same
        # shrink check against the existing file (existing_graph_node_count).
        #
        # Trade-off: this reuses to_json's coarse node-count guard, not the
        # source-aware _check_shrink that watch/update use. On an incremental run
        # a legitimate deletion that coincides with an unrelated transient chunk
        # failure can therefore be refused here — recoverable by re-running or
        # passing --allow-partial (the good graph is preserved and the manifest
        # is not stamped, so the retry re-extracts).
        _force_write = cli_allow_partial or not _extraction_incomplete
        # Stamp provenance from the ANALYSED repo, not the shell's cwd: without
        # this, to_json's fallback asks `git rev-parse HEAD` in whatever repo the
        # command was invoked from, so `graph_fy extract <target>` run from
        # another repo's root stamped the invoker's commit into the target's
        # graph.json — and cluster then propagates that stamp into
        # GRAPH_REPORT.md (#2534 keeps the extract-time stamp by design). Same
        from graph_fy.cluster import label_communities_by_hub as _label_by_hub
        hub_labels = _label_by_hub(G, communities)
        from graph_fy.watch import _git_head as _gh_target
        _wrote = _to_json(G, communities, str(graph_json_path), force=_force_write,
                          built_at_commit=_gh_target(cwd=Path(target).resolve()),
                          community_labels=hub_labels)
        if not _wrote:
            # The shrink guard refused: this partial build is smaller than the
            # existing graph. Exit before writing the manifest/marker below, which
            # would otherwise stamp these files as done and make the next
            # incremental run skip re-extracting them (poisoning the manifest
            # against the graph we declined to write). Exit non-zero so a retry
            # re-attempts.
            print(
                "[graph_fy extract] error: extraction was incomplete (an AST/semantic "
                f"pass failed) and the resulting graph is smaller than the existing "
                f"{graph_json_path}. Refusing to overwrite a complete graph with a "
                "partial one. Re-run after fixing the failures, or pass --allow-partial "
                "to overwrite anyway.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            # See the --no-cluster path above: persist the scan root so build_merge
            # can relativize deleted-file paths under a custom --out (#2012/#1571).
            (graph_fy_out / ".graph_fy_root").write_text(
                str(Path(target).resolve()), encoding="utf-8"
            )
        except OSError:
            pass
        stages.mark("export")
        if merged.get("output_tokens", 0) > 0:
            (graph_fy_out / ".graph_fy_semantic_marker").write_text(
                json.dumps({"output_tokens": merged["output_tokens"]}), encoding="utf-8"
            )
        if global_merge:
            from graph_fy.global_graph import global_add as _global_add
            _tag = global_repo_tag or target.name
            try:
                result = _global_add(graph_fy_out / "graph.json", _tag)
                if result["skipped"]:
                    print(f"[graph_fy global] '{_tag}' unchanged since last add - skipped.")
                else:
                    print(f"[graph_fy global] '{_tag}' merged into global graph "
                          f"(+{result['nodes_added']} nodes, -{result['nodes_removed']} pruned).")
            except Exception as exc:
                print(f"[graph_fy global] warning: failed to merge into global graph: {exc}", file=sys.stderr)
        analysis = {
            "communities": {str(k): v for k, v in communities.items()},
            "cohesion": {str(k): v for k, v in cohesion.items()},
            "gods": gods,
            "surprises": surprises,
            "tokens": {
                "input": merged["input_tokens"],
                "output": merged["output_tokens"],
            },
        }
        from graph_fy.paths import write_json_atomic as _wja
        _wja(analysis_path, analysis, indent=2)
        try:
            if has_path:
                _save_manifest(_manifest_files, manifest_path=str(manifest_path), kind="both", root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic, clear_ast=_cleared_ast or None)
        except Exception as exc:
            print(f"[graph_fy extract] warning: could not write manifest: {exc}", file=sys.stderr)

        # Build SQLite FTS5 search index
        try:
            from graph_fy.retrieval import build_index as _build_fts_index
            _build_fts_index(G, graph_fy_out / "graph.db")
            print(f"[graph_fy extract] wrote {graph_fy_out / 'graph.db'} (unified graph + FTS5 search index)")
        except Exception as _idx_err:
            print(f"[graph_fy extract] warning: could not build FTS5 index: {_idx_err}", file=sys.stderr)

        # Generate GRAPH_REPORT.md
        try:
            from graph_fy.report import generate as _generate_report, load_learning_for_report as _llfr
            from graph_fy.analyze import suggest_questions
            questions = suggest_questions(G, communities, hub_labels)
            _commit = _gh_target(cwd=Path(target).resolve())
            _det = detection if 'detection' in locals() and isinstance(detection, dict) else {}
            report = _generate_report(
                G, communities, cohesion, hub_labels, gods, surprises,
                _det, {"input": 0, "output": 0}, str(Path(target).resolve()),
                suggested_questions=questions,
                built_at_commit=_commit,
                learning=_llfr(graph_json_path),
            )
            (graph_fy_out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
            print(f"[graph_fy extract] wrote {graph_fy_out / 'GRAPH_REPORT.md'}")
        except Exception as _rep_err:
            print(f"[graph_fy extract] warning: could not generate GRAPH_REPORT.md: {_rep_err}", file=sys.stderr)

        # Save labels sidecar
        try:
            (graph_fy_out / ".graph_fy_labels.json").write_text(
                json.dumps({str(k): v for k, v in hub_labels.items()}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

        # Generate graph.html
        if not ("--no-viz" in sys.argv):
            html_target = graph_fy_out / "graph.html"
            try:
                from graph_fy.export import to_html as _to_html
                from graph_fy.exporters.html import _viz_node_limit
                viz_limit = _viz_node_limit()
                if viz_limit > 0:
                    _to_html(G, communities, str(html_target), community_labels=hub_labels, node_limit=viz_limit)
                    print(f"[graph_fy extract] wrote {html_target}")
            except Exception as _viz_err:
                print(f"[graph_fy extract] warning: could not write {html_target}: {_viz_err}", file=sys.stderr)

        cost = _estimate_cost(backend, merged["input_tokens"], merged["output_tokens"])
        print(
            f"[graph_fy extract] wrote {graph_json_path}: "
            f"{G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
            f"{len(communities)} communities"
        )
        print(f"[graph_fy extract] wrote {analysis_path}")
        if incremental_mode:
            _excl_note = f", {len(excluded_files)} excluded" if excluded_files else ""
            print(
                f"[graph_fy extract] incremental summary: "
                f"{sem_cache_hits + unchanged_total} files cached/unchanged, "
                f"{len(code_files) + sem_cache_misses} re-extracted, "
                f"{len(deleted_files)} deleted{_excl_note}"
            )
        elif sem_cache_hits:
            print(f"[graph_fy extract] semantic cache: {sem_cache_hits} cached, {sem_cache_misses} re-extracted")
        if merged["input_tokens"] or merged["output_tokens"]:
            print(
                f"[graph_fy extract] tokens: "
                f"{merged['input_tokens']:,} in / "
                f"{merged['output_tokens']:,} out, "
                f"est. cost (~{backend}): ${cost:.4f}"
            )
        stages.total()

        return 0
    finally:
        sys.argv = sys_argv_backup
