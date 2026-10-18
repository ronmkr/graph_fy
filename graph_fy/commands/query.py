"""Query and explain subcommands for graph_fy CLI."""
from __future__ import annotations

import json as _json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable


def _touch_query_stamp(graph_path: Path | str) -> None:
    """Record that graph_fy oriented the agent recently, next to the queried graph."""
    try:
        from graph_fy.paths import write_text_atomic
        stamp = Path(graph_path).parent / "cache" / "last_query_stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(stamp, str(time.time()))
    except Exception:
        pass


def _enforce_graph_size_cap(gp: Path) -> None:
    from graph_fy.security import check_graph_file_size_cap
    try:
        check_graph_file_size_cap(gp)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_fast_grep_and_exit(
    question: str,
    target: str = ".",
    limit: int = 10,
    as_json: bool = False,
    use_skeleton: bool = False,
) -> int:
    from graph_fy.fast_query import fast_ast_grep, format_fast_hits
    t0 = time.perf_counter()
    hits = fast_ast_grep(question, target=target, limit=limit, include_code=not use_skeleton)
    elapsed = time.perf_counter() - t0
    if as_json:
        print(_json.dumps({"query": question, "target": str(target), "count": len(hits), "elapsed_s": round(elapsed, 4), "hits": hits}, indent=2))
    else:
        print(format_fast_hits(question, hits, elapsed))
    return 0


def cmd_query(
    argv: list[str],
    default_graph_path_fn: Callable[[], str],
    find_graph_path_fn: Callable[[str], Path],
) -> int:
    """Handle `graph_fy query` command."""
    if len(argv) < 3:
        print(
            'Usage: graph_fy query "<text>" [--limit N] [--hops N] [--dfs] [--context C] [--budget N] [--graph path] [--fast-grep [target]] [--terse] [--caveman]',
            file=sys.stderr,
        )
        return 1

    from graph_fy.serve import _query_graph_text
    from graph_fy.security import sanitize_label
    from networkx.readwrite import json_graph
    from graph_fy import querylog

    is_fast_grep = False
    fast_target = "."
    if argv[2] in ("--fast-grep", "--fast"):
        is_fast_grep = True
        question = argv[3] if len(argv) > 3 else ""
        args = argv[4:]
    else:
        question = argv[2]
        args = argv[3:]

    use_dfs = "--dfs" in argv
    budget = 2000
    limit = 10
    hops = 2
    force_text = False
    graph_paths: list[str] = []
    graph_path = default_graph_path_fn()
    context_filters: list[str] = []
    terse = "--terse" in argv or "--caveman" in argv
    verbose = "--verbose" in argv or "-v" in argv
    use_skeleton = "--skeleton" in argv
    as_rag_prompt = "--rag-prompt" in argv
    is_parallel = "--parallel" in argv
    batch_file = None
    parallel_queries: list[str] = []
    if as_rag_prompt:
        use_skeleton = True
    i = 0
    while i < len(args):
        if args[i] in ("--terse", "--caveman"):
            terse = True
            i += 1
        elif args[i] == "--skeleton":
            use_skeleton = True
            i += 1
        elif args[i] == "--rag-prompt":
            as_rag_prompt = True
            use_skeleton = True
            i += 1
        elif args[i] in ("--verbose", "-v"):
            verbose = True
            i += 1
        elif args[i] == "--parallel":
            is_parallel = True
            i += 1
        elif args[i] == "--batch" and i + 1 < len(args):
            is_parallel = True
            batch_file = Path(args[i + 1])
            i += 2
        elif args[i].startswith("--batch="):
            is_parallel = True
            batch_file = Path(args[i].split("=", 1)[1])
            i += 1
        elif args[i] == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                print("error: --limit must be an integer", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--limit="):
            try:
                limit = int(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --limit must be an integer", file=sys.stderr)
                return 1
            i += 1
        elif args[i] == "--hops" and i + 1 < len(args):
            try:
                hops = int(args[i + 1])
            except ValueError:
                print("error: --hops must be an integer", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--hops="):
            try:
                hops = int(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --hops must be an integer", file=sys.stderr)
                return 1
            i += 1
        elif args[i] == "--text":
            force_text = True
            i += 1
        elif args[i] == "--budget" and i + 1 < len(args):
            try:
                budget = int(args[i + 1])
            except ValueError:
                print("error: --budget must be an integer", file=sys.stderr)
                return 1
            i += 2
        elif args[i].startswith("--budget="):
            try:
                budget = int(args[i].split("=", 1)[1])
            except ValueError:
                print("error: --budget must be an integer", file=sys.stderr)
                return 1
            i += 1
        elif args[i] == "--context" and i + 1 < len(args):
            context_filters.append(args[i + 1])
            i += 2
        elif args[i].startswith("--context="):
            context_filters.append(args[i].split("=", 1)[1])
            i += 1
        elif args[i] in ("--graph", "-g") and i + 1 < len(args):
            for p in args[i + 1].split(","):
                if p.strip():
                    graph_paths.append(p.strip())
            i += 2
        elif args[i].startswith("--graph="):
            for p in args[i].split("=", 1)[1].split(","):
                if p.strip():
                    graph_paths.append(p.strip())
            i += 1
        elif args[i] in ("--multi-repo", "--workspace") and i + 1 < len(args):
            from graph_fy.multi_repo import find_workspace_graphs
            graph_paths.extend([str(p) for p in find_workspace_graphs(args[i + 1])])
            i += 2
        elif args[i].startswith("--multi-repo=") or args[i].startswith("--workspace="):
            from graph_fy.multi_repo import find_workspace_graphs
            m_dir = args[i].split("=", 1)[1]
            graph_paths.extend([str(p) for p in find_workspace_graphs(m_dir)])
            i += 1
        elif args[i] in ("--root", "-r", "--dir", "-d") and i + 1 < len(args):
            rdir = Path(args[i + 1])
            graph_paths.append(str(rdir / "graph_fy_out" / "graph.json" if (rdir / "graph_fy_out" / "graph.json").exists() else rdir / "graph.json"))
            i += 2
        elif args[i].startswith("--root=") or args[i].startswith("--dir="):
            rdir = Path(args[i].split("=", 1)[1])
            graph_paths.append(str(rdir / "graph_fy_out" / "graph.json" if (rdir / "graph_fy_out" / "graph.json").exists() else rdir / "graph.json"))
            i += 1
        elif args[i] in ("--fast-grep", "--fast"):
            is_fast_grep = True
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                fast_target = args[i + 1]
                i += 2
            else:
                i += 1
        elif args[i].startswith("--fast-grep="):
            is_fast_grep = True
            fast_target = args[i].split("=", 1)[1]
            i += 1
        elif args[i].startswith("--fast="):
            is_fast_grep = True
            fast_target = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--target", "-t") and i + 1 < len(args):
            fast_target = args[i + 1]
            i += 2
        elif args[i].startswith("--target="):
            fast_target = args[i].split("=", 1)[1]
            i += 1
        elif is_parallel and not args[i].startswith("-"):
            parallel_queries.append(args[i])
            i += 1
        elif not args[i].startswith("-"):
            if is_fast_grep and fast_target == ".":
                fast_target = args[i]
            i += 1
        else:
            i += 1

    if is_fast_grep:
        return _run_fast_grep_and_exit(
            question,
            target=fast_target,
            limit=limit,
            as_json="--json" in argv,
            use_skeleton=use_skeleton,
        )

    if is_parallel:
        if batch_file and batch_file.exists():
            parallel_queries.extend([line.strip() for line in batch_file.read_text(encoding="utf-8").splitlines() if line.strip()])
        if question and not question.startswith("-") and question != "--parallel":
            parallel_queries.insert(0, question)
        from graph_fy.multi_repo import query_parallel, resolve_multi_graphs
        target_gps = graph_paths if graph_paths else [default_graph_path_fn()]
        G_par = resolve_multi_graphs(target_gps) if len(target_gps) > 1 else None
        first_gp = target_gps[0] if target_gps else None
        par_res = query_parallel(
            queries=parallel_queries,
            graph=G_par,
            graph_path=first_gp,
            limit=limit,
            hops=hops,
            budget=budget,
            use_skeleton=use_skeleton,
        )
        if "--json" in argv:
            print(_json.dumps(par_res, indent=2))
        else:
            print(f"Executed {par_res['count']} queries in parallel ({par_res['total_wall_time_s']}s wall clock, concurrency={par_res['concurrency']}):\n")
            for item in par_res["queries"]:
                print(f"=== Query: \"{item['query']}\" ({item['elapsed_s']}s, {item['hits_count']} hits) ===")
                if item["hits"]:
                    for h in item["hits"][:3]:
                        print(f"  • {h.get('label', '')} ({h.get('source_path', '')}:{h.get('source_location', '')})")
                else:
                    print("  (no hits)")
                print()
        return 0

    if len(graph_paths) > 1:
        from graph_fy.multi_repo import resolve_multi_graphs
        G = resolve_multi_graphs(graph_paths)
        gp = Path(graph_paths[0]).resolve()
    else:
        gp = Path(graph_paths[0] if graph_paths else graph_path).resolve()
        if not gp.exists():
            print(
                f"error: graph file not found: {gp}\nTip: Use 'graph_fy query \"{question}\" --fast-grep' for unindexed sub-second AST search.",
                file=sys.stderr,
            )
            return 1
        if not gp.suffix == ".json":
            print("error: graph file must be a .json file", file=sys.stderr)
            return 1
        _enforce_graph_size_cap(gp)
        try:
            _raw = _json.loads(gp.read_text(encoding="utf-8"))
            if "links" not in _raw and "edges" in _raw:
                _raw = dict(_raw, links=_raw["edges"])
            _raw = dict(
                _raw,
                links=[
                    {
                        **link,
                        "_src": link.get("_src", link.get("source")),
                        "_tgt": link.get("_tgt", link.get("target")),
                    }
                    for link in _raw.get("links", [])
                ],
            )
            try:
                G = json_graph.node_link_graph(_raw, edges="links")
            except TypeError:
                G = json_graph.node_link_graph(_raw)
            try:
                from graph_fy.build import graph_has_legacy_ids as _legacy
                if _legacy(_raw.get("nodes", [])):
                    print(
                        "[graph_fy] note: this graph uses the pre-#1504 node-ID scheme; "
                        "rebuild with `graph_fy extract --force` to get path-qualified IDs "
                        "(fixes same-name-file collisions).",
                        file=sys.stderr,
                    )
            except Exception:
                pass
        except Exception as exc:
            print(f"error: could not load graph: {exc}", file=sys.stderr)
            return 1

    _t0 = time.perf_counter()

    def _has_fts_index(p: Path) -> bool:
        if (p.parent / "index.db").exists():
            return True
        g_db = p.parent / "graph.db"
        if not g_db.exists():
            return False
        try:
            import sqlite3
            with sqlite3.connect(str(g_db), timeout=2.0) as conn:
                c = conn.cursor()
                c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts';")
                if not c.fetchone():
                    return False
                c.execute("SELECT 1 FROM nodes_fts LIMIT 1;")
                return c.fetchone() is not None
        except Exception:
            return False

    _is_legacy_text = bool(
        (not (as_rag_prompt or use_skeleton))
        and (
            context_filters
            or use_dfs
            or budget != 2000
            or force_text
            or "who calls " in question.lower()
        )
        or not _has_fts_index(gp)
    )
    if _is_legacy_text:
        _mode = "dfs" if use_dfs else "bfs"
        _result = _query_graph_text(
            G,
            question,
            mode=_mode,
            depth=2,
            token_budget=budget,
            context_filters=context_filters,
            graph_path=str(gp),
        )
        querylog.log_query(
            kind="query",
            question=question,
            corpus=str(gp),
            result=_result,
            mode=_mode,
            depth=2,
            token_budget=budget,
            duration_ms=(time.perf_counter() - _t0) * 1000,
        )
        _touch_query_stamp(gp)
        if terse:
            from graph_fy.retrieval import format_query_results
            print(format_query_results(_result, terse=True))
        else:
            print(_result)
        if verbose:
            try:
                from graph_fy.mcp import estimate_token_savings
                nodes_data = []
                for line in _result.splitlines():
                    if line.startswith("NODE "):
                        parts = line.split()
                        node_dict = {"label": parts[1] if len(parts) > 1 else ""}
                        for part in parts[2:]:
                            if part.startswith("src="):
                                node_dict["source_file"] = part[4:]
                            elif part.startswith("loc="):
                                node_dict["source_location"] = part[4:]
                        nodes_data.append(node_dict)
                root_dir = str(gp.parent.parent) if gp.parent.name == "graph_fy_out" else str(gp.parent)
                savings = estimate_token_savings(nodes_data, root_dir=root_dir)
                print(
                    f"\n[verbose] Token savings: {savings['tokens_saved']} tokens saved | "
                    f"{savings['file_reads_avoided']} file reads avoided | "
                    f"{savings['compression_ratio']}x compression"
                )
            except Exception as exc:
                print(f"\n[verbose] Could not estimate token savings: {exc}")
    else:
        from graph_fy.retrieval import query as _retrieval_query
        idx_path = gp.parent / "graph.db" if (gp.parent / "graph.db").exists() else gp.parent / "index.db"
        results = _retrieval_query(
            question,
            graph=G,
            db_path=idx_path,
            limit=limit,
            hops=hops,
            graph_path=gp,
            skeleton=use_skeleton,
        )
        querylog.log_query(
            kind="query",
            question=question,
            corpus=str(gp),
            result=results,
            mode="fts5_graph",
            depth=hops,
            token_budget=limit,
            duration_ms=(time.perf_counter() - _t0) * 1000,
        )
        _touch_query_stamp(gp)
        if verbose:
            try:
                from graph_fy.mcp import estimate_token_savings
                root_dir = str(gp.parent.parent) if gp.parent.name == "graph_fy_out" else str(gp.parent)
                nodes_list = results.get("nodes", []) if isinstance(results, dict) else results
                savings = estimate_token_savings(nodes_list, root_dir=root_dir)
                if isinstance(results, dict):
                    results["token_savings"] = savings
                if terse:
                    from graph_fy.retrieval import format_query_results
                    print(format_query_results(results, terse=True, skeleton=use_skeleton))
                else:
                    print(_json.dumps(results, indent=2))
                print(
                    f"\n[verbose] Token savings: {savings['tokens_saved']} tokens saved | "
                    f"{savings['file_reads_avoided']} file reads avoided | "
                    f"{savings['compression_ratio']}x compression"
                )
            except Exception as exc:
                if terse:
                    from graph_fy.retrieval import format_query_results
                    print(format_query_results(results, terse=True, skeleton=use_skeleton))
                else:
                    print(_json.dumps(results, indent=2))
                print(f"\n[verbose] Could not estimate token savings: {exc}")
        else:
            if as_rag_prompt and isinstance(results, list):
                from graph_fy.retrieval import format_rag_prompt
                print(format_rag_prompt(results, question, token_budget=budget))
            elif terse:
                from graph_fy.retrieval import format_query_results
                print(format_query_results(results, terse=True, skeleton=use_skeleton))
            else:
                print(_json.dumps(results, indent=2))
    return 0


def cmd_explain(
    argv: list[str],
    default_graph_path_fn: Callable[[], str],
    find_graph_path_fn: Callable[[str], Path],
) -> int:
    """Handle `graph_fy explain` command."""
    raw_args = argv[2:]
    positional = []
    graph_path = default_graph_path_fn()
    terse = False
    i = 0
    while i < len(raw_args):
        a = raw_args[i]
        if a == "--graph" and i + 1 < len(raw_args):
            graph_path = raw_args[i + 1]
            i += 2
        elif a.startswith("--graph="):
            graph_path = a.split("=", 1)[1]
            i += 1
        elif a in ("--root", "-r", "--dir", "-d") and i + 1 < len(raw_args):
            rdir = Path(raw_args[i + 1])
            graph_path = str(rdir / "graph_fy_out" / "graph.json" if (rdir / "graph_fy_out" / "graph.json").exists() else rdir / "graph.json")
            i += 2
        elif a.startswith("--root=") or a.startswith("--dir="):
            rdir = Path(a.split("=", 1)[1])
            graph_path = str(rdir / "graph_fy_out" / "graph.json" if (rdir / "graph_fy_out" / "graph.json").exists() else rdir / "graph.json")
            i += 1
        elif a in ("--terse", "--caveman"):
            terse = True
            i += 1
        elif not a.startswith("--"):
            positional.append(a)
            i += 1
        else:
            i += 1

    if not positional:
        print('Usage: graph_fy explain "<node>" [--graph path] [--terse] [--caveman]', file=sys.stderr)
        return 1

    from graph_fy.serve import _find_node, find_node_ambiguity
    from networkx.readwrite import json_graph

    label = positional[0]
    gp = Path(graph_path).resolve()
    if not gp.exists():
        print(f"error: graph file not found: {gp}", file=sys.stderr)
        return 1
    _enforce_graph_size_cap(gp)
    _raw = _json.loads(gp.read_text(encoding="utf-8"))
    if "links" not in _raw and "edges" in _raw:
        _raw = dict(_raw, links=_raw["edges"])
    # Force directed so the renderer can recover stored caller→callee direction.
    _raw = {**_raw, "directed": True}
    try:
        G = json_graph.node_link_graph(_raw, edges="links")
    except TypeError:
        G = json_graph.node_link_graph(_raw)
    matches = _find_node(G, label)
    if not matches:
        print(f"No node matching '{label}' found.")
        return 0
    rivals = find_node_ambiguity(G, label)
    if rivals:
        print(f"Ambiguous: '{label}' matches {len(rivals)} nodes in different files.")
        for rival in rivals:
            print(f"  {G.nodes[rival].get('source_file') or rival}")
            print(f"    id: {rival}")
        print(
            "Retry with path::symbol using one of the paths above (e.g. "
            f"<path>::{label}) or the full node id."
        )
        return 1
    nid = matches[0]
    d = G.nodes[nid]
    from graph_fy.build import edge_data
    connections: list[tuple[str, str, dict]] = []  # (direction, neighbor_id, edge_data)
    for nb in G.successors(nid):
        _ed = edge_data(G, nid, nb)
        connections.append(
            ("out" if _ed.get("_src", nid) == nid else "in", nb, _ed)
        )
    for nb in G.predecessors(nid):
        _ed = edge_data(G, nb, nid)
        connections.append(
            ("in" if _ed.get("_src", nb) == nb else "out", nb, _ed)
        )
    connections.sort(key=lambda c: G.degree(c[1]), reverse=True)

    if terse:
        lbl = d.get("label", nid)
        sfile = d.get("source_file", "")
        sloc = d.get("source_location", "")
        if sfile and sloc:
            loc_str = f"{sfile}:{sloc}"
        else:
            loc_str = sfile or sloc or ""
        print(f"[{nid}] {lbl}{' ' + loc_str if loc_str else ''}".strip())
        for direction, nb, edata in connections[:20]:
            rel = edata.get("relation", "connected")
            if direction == "out":
                print(f"  {rel} -> {nb}")
            else:
                print(f"  {rel} <- {nb}")
    else:
        print(f"Node: {d.get('label', nid)}")
        print(f"  ID:        {nid}")
        print(
            f"  Source:    {d.get('source_file', '')} {d.get('source_location', '')}".rstrip()
        )
        print(f"  Type:      {d.get('file_type', '')}")
        print(f"  Community: {d.get('community_name') or d.get('community', '')}")
        try:
            from graph_fy.reflect import load_learning_overlay as _llo
            from graph_fy.security import sanitize_label as _sl
            _overlay = _llo(gp)
            _entry = _overlay.get(str(nid))
            if _entry:
                _status = _sl(str(_entry.get("status", "")))
                if _status == "contested":
                    _line = (f"  Lesson: contested (useful {_entry.get('uses', 0)} / "
                             f"dead-end {_entry.get('neg', 0)})")
                elif _status == "preferred":
                    _line = (f"  Lesson: preferred source (start here) — "
                             f"{_entry.get('uses', 0)} useful, score={_entry.get('score', 0)}")
                else:
                    _line = (f"  Lesson: {_status or 'tentative'} — "
                             f"{_entry.get('uses', 0)} useful, score={_entry.get('score', 0)}")
                if _entry.get("stale"):
                    _line += " [code changed since — re-verify]"
                print(_line)
        except Exception:
            pass
        print(f"  Degree:    {G.degree(nid)}")
        if connections:
            print(f"\nConnections ({len(connections)}):")
            for direction, nb, edata in connections[:20]:
                rel = edata.get("relation", "")
                conf = edata.get("confidence", "")
                arrow = "-->" if direction == "out" else "<--"
                loc = edata.get("source_location") or ""
                sfile = edata.get("source_file") or ""
                at = f" {sfile}:{loc}" if loc else ""
                print(f"  {arrow} {G.nodes[nb].get('label', nb)} [{rel}] [{conf}]{at}")
            if len(connections) > 20:
                remainder = connections[20:]
                print(f"  ... and {len(remainder)} more")
                by_file: dict[tuple[str, str], int] = {}
                for direction, _nb, edata in remainder:
                    sfile = edata.get("source_file") or "(unknown file)"
                    key = (direction, sfile)
                    by_file[key] = by_file.get(key, 0) + 1
                grouped = sorted(by_file.items(), key=lambda kv: (-kv[1], kv[0]))
                print("  Grouped by file:")
                for (direction, sfile), count in grouped[:20]:
                    arrow = "-->" if direction == "out" else "<--"
                    noun = "connection" if count == 1 else "connections"
                    print(f"    {arrow} {sfile}: {count} {noun}")
                if len(grouped) > 20:
                    print(f"    ... and {len(grouped) - 20} more files")
    from graph_fy import querylog
    querylog.log_query(
        kind="explain",
        question=argv[2],
        corpus=str(gp),
        nodes_returned=len(connections),
    )
    _touch_query_stamp(gp)
    return 0
