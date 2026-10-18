"""Cluster and label commands implementation for graph_fy CLI."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Type


def cmd_cluster(
    argv: list[str],
    cmd: str,
    default_out_dir: str | Path,
    stage_timer_cls: Type[Any],
) -> int:
    """Handle `graph_fy cluster-only` and `graph_fy label` subcommands."""
    # `label` is `cluster-only` that always (re)generates community names with
    # the configured backend, even when a .graph_fy_labels.json already exists.
    force_relabel = cmd == "label"
    no_viz = "--no-viz" in argv
    no_label = "--no-label" in argv
    missing_only = "--missing-only" in argv
    co_timing = "--timing" in argv
    _backend_arg = next((a for a in argv if a.startswith("--backend=")), None)
    label_backend = _backend_arg.split("=", 1)[1] if _backend_arg else None
    _model_arg = next((a for a in argv if a.startswith("--model=")), None)
    label_model = _model_arg.split("=", 1)[1] if _model_arg else None
    _min_cs_arg = next((a for a in argv if a.startswith("--min-community-size=")), None)
    min_community_size = int(_min_cs_arg.split("=")[1]) if _min_cs_arg else 3
    args = argv[2:]
    watch_path: Path | None = None
    graph_override: Path | None = None
    co_resolution: float = 1.0
    co_exclude_hubs: float | None = None
    label_max_concurrency: int = 4
    label_batch_size: int = 100
    label_max_concurrency_explicit = False
    label_batch_size_explicit = False
    i_arg = 0
    while i_arg < len(args):
        a = args[i_arg]
        if a == "--graph" and i_arg + 1 < len(args):
            graph_override = Path(args[i_arg + 1])
            i_arg += 2
        elif a == "--backend" and i_arg + 1 < len(args):
            label_backend = args[i_arg + 1]
            i_arg += 2
        elif a.startswith("--backend="):
            label_backend = a.split("=", 1)[1]
            i_arg += 1
        elif a == "--model" and i_arg + 1 < len(args):
            label_model = args[i_arg + 1]
            i_arg += 2
        elif a.startswith("--model="):
            label_model = a.split("=", 1)[1]
            i_arg += 1
        elif a == "--resolution" and i_arg + 1 < len(args):
            co_resolution = float(args[i_arg + 1])
            i_arg += 2
        elif a.startswith("--resolution="):
            co_resolution = float(a.split("=", 1)[1])
            i_arg += 1
        elif a == "--exclude-hubs" and i_arg + 1 < len(args):
            co_exclude_hubs = float(args[i_arg + 1])
            i_arg += 2
        elif a.startswith("--exclude-hubs="):
            co_exclude_hubs = float(a.split("=", 1)[1])
            i_arg += 1
        elif a == "--max-concurrency" and i_arg + 1 < len(args):
            label_max_concurrency = int(args[i_arg + 1])
            label_max_concurrency_explicit = True
            i_arg += 2
        elif a.startswith("--max-concurrency="):
            label_max_concurrency = int(a.split("=", 1)[1])
            label_max_concurrency_explicit = True
            i_arg += 1
        elif a == "--batch-size" and i_arg + 1 < len(args):
            label_batch_size = int(args[i_arg + 1])
            label_batch_size_explicit = True
            i_arg += 2
        elif a.startswith("--batch-size="):
            label_batch_size = int(a.split("=", 1)[1])
            label_batch_size_explicit = True
            i_arg += 1
        elif a in ("--no-viz", "--missing-only") or a.startswith("--min-community-size="):
            i_arg += 1
        elif not a.startswith("-") and watch_path is None:
            watch_path = Path(a).resolve()
            i_arg += 1
        else:
            i_arg += 1

    if watch_path is None:
        watch_path = Path(".").resolve()
    if graph_override is not None:
        graph_json = graph_override
    else:
        out_dir_path = Path(default_out_dir)
        graph_json = watch_path / out_dir_path / "graph.json"
        if not graph_json.exists() and (watch_path / "graph_fy_out" / "graph.json").exists():
            graph_json = watch_path / "graph_fy_out" / "graph.json"
    if not graph_json.exists():
        print(
            f"error: no graph found at {graph_json} — run /graph_fy first",
            file=sys.stderr,
        )
        return 1

    from graph_fy.build import build_from_json
    from graph_fy.cluster import cluster, score_all, remap_communities_to_previous
    from graph_fy.analyze import god_nodes, surprising_connections, suggest_questions
    from graph_fy.report import generate
    from graph_fy.export import to_json, to_html

    stages = stage_timer_cls(co_timing)
    print("Loading existing graph...")
    from graph_fy.security import check_graph_file_size_cap as _check_cap

    try:
        _check_cap(graph_json)
    except ValueError:
        try:
            _over_cap_bytes = graph_json.stat().st_size
        except OSError:
            _over_cap_bytes = -1
        print(
            f"warning: graph.json exceeds cap ({_over_cap_bytes} bytes); "
            "continuing with best-effort visualization",
            file=sys.stderr,
        )
    _raw = json.loads(graph_json.read_text(encoding="utf-8"))
    _directed = bool(_raw.get("directed", False))
    G = build_from_json(_raw, directed=_directed)
    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    stages.mark("load")
    print("Re-clustering...")
    communities = cluster(G, resolution=co_resolution, exclude_hubs_percentile=co_exclude_hubs)
    previous_node_community = {
        n["id"]: n["community"]
        for n in _raw.get("nodes", [])
        if n.get("community") is not None and n.get("id") is not None
    }
    if previous_node_community:
        communities = remap_communities_to_previous(communities, previous_node_community)
    stages.mark("cluster")
    cohesion = score_all(G, communities)
    gods = god_nodes(G, exclude_hubs_percentile=co_exclude_hubs)
    surprises = surprising_connections(G, communities)
    stages.mark("analyze")

    _out_name = Path(default_out_dir).name
    if graph_json.parent.name in (_out_name, "graph_fy_out", "graph_fy_out"):
        out = graph_json.parent
    else:
        out = watch_path / default_out_dir
    out.mkdir(parents=True, exist_ok=True)
    if str(default_out_dir) == "graph_fy_out":
        compat_link = out.parent / "graph_fy_out"
        if not compat_link.exists() and not compat_link.is_symlink():
            try:
                compat_link.symlink_to("graph_fy_out", target_is_directory=True)
            except Exception:
                pass
    labels_path = out / ".graph_fy_labels.json"
    existing_labels: dict[int, str] = {}
    if labels_path.exists():
        try:
            existing_labels = {
                int(k): v
                for k, v in json.loads(labels_path.read_text(encoding="utf-8")).items()
                if isinstance(v, str)
            }
        except Exception:
            existing_labels = {}

    label_token_usage = {"input": 0, "output": 0}
    placeholder_only = False
    if labels_path.exists() and not force_relabel:
        _ignored_label_flags = [
            flag
            for flag, given in (
                ("--backend", label_backend is not None),
                ("--model", label_model is not None),
                ("--batch-size", label_batch_size_explicit),
                ("--max-concurrency", label_max_concurrency_explicit),
            )
            if given
        ]
        if _ignored_label_flags:
            print(
                f"[graph_fy] warning: {'/'.join(_ignored_label_flags)} ignored: "
                f"reusing saved labels at {labels_path}. "
                f"Run `graph_fy label` to relabel, or delete the labels file.",
                file=sys.stderr,
            )
        from graph_fy.cluster import community_member_sigs, label_communities_by_hub

        sig_path = labels_path.parent / (labels_path.name + ".sig")
        saved_sigs: dict[int, str] = {}
        if sig_path.exists():
            try:
                saved_sigs = {
                    int(k): v
                    for k, v in json.loads(sig_path.read_text(encoding="utf-8")).items()
                    if isinstance(v, str)
                }
            except Exception:
                saved_sigs = {}
        cur_sigs = community_member_sigs(communities)
        count_mismatch = len(existing_labels) != len(communities)
        labels: dict[int, str] = {}
        hub_labels: dict[int, str] | None = None
        changed = 0
        for cid in communities:
            have_label = cid in existing_labels and existing_labels[cid] != f"Community {cid}"
            if saved_sigs:
                fresh = have_label and saved_sigs.get(cid) == cur_sigs.get(cid)
            else:
                fresh = have_label and not count_mismatch
            if fresh:
                labels[cid] = existing_labels[cid]
            else:
                if hub_labels is None:
                    hub_labels = label_communities_by_hub(G, communities)
                labels[cid] = hub_labels[cid]
                if have_label:
                    changed += 1
        if changed:
            print(
                f"[graph_fy] community set changed since labeling "
                f"({len(existing_labels)} saved labels, {len(communities)} communities now; "
                f"renamed {changed} community(ies) by their hub). "
                f"Run `graph_fy label` to refresh names with the LLM.",
                file=sys.stderr,
            )
    elif no_label and not force_relabel:
        labels = {cid: f"Community {cid}" for cid in communities}
        placeholder_only = True
    else:
        from graph_fy.cluster import label_communities_by_hub
        from graph_fy.llm import generate_community_labels

        print("Labeling communities...")
        hub_labels = label_communities_by_hub(G, communities)
        label_communities_input = communities
        labels = dict(hub_labels)
        if missing_only:
            labels = {
                cid: existing_labels.get(cid, hub_labels[cid])
                for cid in communities
            }
            label_communities_input = {
                cid: members
                for cid, members in communities.items()
                if cid not in existing_labels or existing_labels.get(cid) == f"Community {cid}"
            }
        generated_labels, _ = generate_community_labels(
            G,
            label_communities_input,
            backend=label_backend,
            model=label_model,
            gods=gods,
            max_concurrency=label_max_concurrency,
            batch_size=label_batch_size,
            usage_out=label_token_usage,
        )
        labels.update({
            cid: v
            for cid, v in generated_labels.items()
            if v and v != f"Community {cid}" and v != str(cid)
        })
    stages.mark("label")
    questions = suggest_questions(G, communities, labels)
    _commit = _raw.get("built_at_commit")
    if not _commit:
        from graph_fy.watch import _git_head as _gh

        _commit = _gh(cwd=watch_path)

    from graph_fy.export import backup_if_protected as _backup
    from graph_fy.exporters.html import _HTML_STALE_MARKER

    _backup(out)
    html_stale_marker = out / _HTML_STALE_MARKER

    def _clear_html_stale_marker() -> None:
        try:
            html_stale_marker.unlink(missing_ok=True)
        except OSError as exc:
            print(
                "warning: graph.html stale marker could not be cleared; "
                f"regeneration may be retried: {exc}",
                file=sys.stderr,
            )

    stale_marker_preexisted = html_stale_marker.exists()
    html_stale_marker.touch()

    if not to_json(
        G,
        communities,
        str(out / "graph.json"),
        community_labels=labels,
        built_at_commit=_commit,
    ):
        if not stale_marker_preexisted:
            _clear_html_stale_marker()
        print(
            "graph.json NOT written: refusing to overwrite (see warning above). "
            "GRAPH_REPORT.md, .graph_fy_labels.json and .graph_fy_analysis.json "
            "left untouched.",
            file=sys.stderr,
        )
        return 1

    tokens = label_token_usage
    from graph_fy.report import load_learning_for_report as _llfr

    report = generate(
        G,
        communities,
        cohesion,
        labels,
        gods,
        surprises,
        {"warning": "cluster-only mode — file stats not available"},
        tokens,
        str(watch_path),
        suggested_questions=questions,
        min_community_size=min_community_size,
        built_at_commit=_commit,
        learning=_llfr(out / "graph.json"),
    )
    (out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    stages.mark("report")
    analysis = {
        "communities": {str(k): v for k, v in communities.items()},
        "cohesion": {str(k): v for k, v in cohesion.items()},
        "gods": gods,
        "surprises": surprises,
        "questions": questions,
    }
    (out / ".graph_fy_analysis.json").write_text(
        json.dumps(analysis, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if not placeholder_only:
        from graph_fy.paths import write_json_atomic as _wja

        _wja(labels_path, {str(k): v for k, v in labels.items()}, ensure_ascii=False)
        from graph_fy.cluster import community_member_sigs as _cms

        (labels_path.parent / (labels_path.name + ".sig")).write_text(
            json.dumps({str(k): v for k, v in _cms(communities).items()}),
            encoding="utf-8",
        )

    html_target = out / "graph.html"
    if no_viz:
        if html_target.exists():
            html_target.unlink()
        _clear_html_stale_marker()
        stages.mark("export")
        stages.total()
        print(f"Done - {len(communities)} communities. GRAPH_REPORT.md and graph.json updated (--no-viz; graph.html removed).")
    else:
        html_written = False
        skip_reason: str | None = None
        try:
            from graph_fy.exporters.html import _viz_node_limit

            viz_limit = _viz_node_limit()
            if viz_limit <= 0:
                html_target.unlink(missing_ok=True)
                _clear_html_stale_marker()
                skip_reason = "graph_fy_VIZ_NODE_LIMIT=0 disables HTML visualization"
            else:
                html_written = to_html(
                    G,
                    communities,
                    str(html_target),
                    community_labels=labels or None,
                    node_limit=viz_limit,
                )
                if html_written:
                    _clear_html_stale_marker()
                else:
                    skip_reason = "no useful community aggregation could be generated"
                    if html_target.exists():
                        skip_reason += "; existing graph.html left unchanged"
        except ValueError as viz_err:
            skip_reason = str(viz_err)
            if html_target.exists():
                skip_reason += "; existing graph.html left unchanged"

        if skip_reason:
            print(f"Skipped graph.html: {skip_reason}")
        stages.mark("export")
        stages.total()
        if html_written:
            print(f"Done - {len(communities)} communities. GRAPH_REPORT.md, graph.json and graph.html updated.")
        else:
            print(f"Done - {len(communities)} communities. GRAPH_REPORT.md and graph.json updated.")

    return 0
