"""Save-result and reflect command implementations for graph_fy CLI."""
from __future__ import annotations

import argparse as _ap
import sys
from pathlib import Path

from graph_fy.ingest import save_query_result as _sqr
from graph_fy.reflect import lessons_fresh as _lessons_fresh, reflect as _reflect


def cmd_save_result(argv: list[str], graph_fy_out: str | Path) -> int:
    """Handle `graph_fy save-result` command."""
    p = _ap.ArgumentParser(prog="graph_fy save-result")
    p.add_argument("--question", required=True)
    p.add_argument("--answer", default=None)
    p.add_argument("--answer-file", dest="answer_file", default=None)
    p.add_argument("--type", dest="query_type", default="query")
    p.add_argument("--nodes", nargs="*", default=[])
    p.add_argument("--outcome", choices=("useful", "dead_end", "corrected"), default=None)
    p.add_argument("--correction", default=None)
    p.add_argument("--memory-dir", default=str(Path(graph_fy_out) / "memory"))
    opts = p.parse_args(argv[2:])
    if opts.answer_file:
        opts.answer = Path(opts.answer_file).read_text(encoding="utf-8").strip()
    elif not opts.answer:
        p.error("--answer or --answer-file is required")

    out = _sqr(
        question=opts.question,
        answer=opts.answer,
        memory_dir=Path(opts.memory_dir),
        query_type=opts.query_type,
        source_nodes=opts.nodes or None,
        outcome=opts.outcome,
        correction=opts.correction,
    )
    print(f"Saved to {out}")
    return 0


def cmd_reflect(argv: list[str], graph_fy_out: str | Path) -> int:
    """Handle `graph_fy reflect` command."""
    p = _ap.ArgumentParser(prog="graph_fy reflect")
    p.add_argument("--memory-dir", default=str(Path(graph_fy_out) / "memory"))
    p.add_argument(
        "--out",
        default=str(Path(graph_fy_out) / "reflections" / "LESSONS.md"),
    )
    p.add_argument("--graph", default=None)
    p.add_argument("--analysis", default=None)
    p.add_argument("--labels", default=None)
    p.add_argument(
        "--half-life-days",
        type=float,
        default=30.0,
        help="signal weight halves every N days (default 30)",
    )
    p.add_argument(
        "--min-corroboration",
        type=int,
        default=2,
        help="distinct useful results to promote a node to preferred (default 2)",
    )
    p.add_argument(
        "--if-stale",
        action="store_true",
        help="skip when LESSONS.md is already newer than every input (e.g. the git hook just refreshed it)",
    )
    opts = p.parse_args(argv[2:])

    graph_arg = opts.graph
    if graph_arg is None:
        default_graph = Path(graph_fy_out) / "graph.json"
        if default_graph.exists():
            graph_arg = str(default_graph)

    _gp = Path(graph_arg) if graph_arg else None
    _analysis_path = None
    _labels_path = None
    if _gp is not None:
        _analysis_path = (
            Path(opts.analysis)
            if opts.analysis
            else (_gp.parent / ".graph_fy_analysis.json")
        )
        _labels_path = (
            Path(opts.labels)
            if opts.labels
            else (_gp.parent / ".graph_fy_labels.json")
        )

    if opts.if_stale and _lessons_fresh(
        Path(opts.out), Path(opts.memory_dir), _gp, _analysis_path, _labels_path
    ):
        print(f"Lessons already up to date -> {opts.out} (skipped; omit --if-stale to force)")
    else:
        out_path, agg = _reflect(
            memory_dir=Path(opts.memory_dir),
            out_path=Path(opts.out),
            graph_path=_gp,
            analysis_path=_analysis_path,
            labels_path=_labels_path,
            half_life_days=opts.half_life_days,
            min_corroboration=opts.min_corroboration,
        )
        c = agg["counts"]
        print(
            f"Reflected {agg['total']} memories "
            f"({c['useful']} useful, {c['dead_end']} dead ends, "
            f"{c['corrected']} corrected) -> {out_path}"
        )
    return 0
