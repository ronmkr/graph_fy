"""Handler for 'graph_fy eval' and 'graph_fy evals'."""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path


def cmd_eval(argv: list[str], default_graph_path_fn) -> int:
    """Execute scenario evaluation benchmarks."""
    args = argv[2:]
    if "-h" in args or "--help" in args:
        print("Usage: graph_fy eval [options]")
        print("")
        print("Options:")
        print("  --comparative, --compare   Run comparative benchmark (With Skill vs Without Skill)")
        print("  --simulated                Use analytical heuristic baseline (default for comparative)")
        print("  --empirical                Use recorded empirical agent execution traces")
        print("  --traces <path>            Path to empirical traces JSON file")
        print("  --graph, -g <path>         Path to graph.json")
        print("  --db <path>                Path to SQLite FTS5 index.db")
        print("  --root, -r <dir>           Repository root directory")
        print("  --scenarios, -s <path>     Scenarios JSON path")
        print("  --category, -c <name>      Filter scenarios by category")
        print("  --limit, -n <N>            Limit number of evaluated scenarios")
        print("  --report <path>            Write markdown report to file")
        print("  --json                     Output JSON results to stdout")
        return 0

    graph_paths: list[str] = []
    db_path = None
    root_dir = "."
    as_json = "--json" in args
    comparative = "--comparative" in args or "--compare" in args
    mode = "empirical" if "--empirical" in args else "simulated"
    traces_path = None
    report_path = None
    scenarios_path = None
    limit = None
    category = None

    i = 0
    while i < len(args):
        if args[i] in ("--graph", "-g") and i + 1 < len(args):
            for p in args[i + 1].split(","):
                if p.strip():
                    graph_paths.append(p.strip())
            i += 2
        elif args[i].startswith("--graph="):
            for p in args[i].split("=", 1)[1].split(","):
                if p.strip():
                    graph_paths.append(p.strip())
            i += 1
        elif args[i] in ("--db",) and i + 1 < len(args):
            db_path = args[i + 1]
            i += 2
        elif args[i].startswith("--db="):
            db_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--root", "-r") and i + 1 < len(args):
            root_dir = args[i + 1]
            i += 2
        elif args[i].startswith("--root=") or args[i].startswith("-r="):
            root_dir = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--report",) and i + 1 < len(args):
            report_path = args[i + 1]
            i += 2
        elif args[i].startswith("--report="):
            report_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--empirical",):
            mode = "empirical"
            i += 1
        elif args[i] in ("--simulated",):
            mode = "simulated"
            i += 1
        elif args[i] in ("--traces",) and i + 1 < len(args):
            traces_path = args[i + 1]
            i += 2
        elif args[i].startswith("--traces="):
            traces_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--scenarios", "-s"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                scenarios_path = args[i + 1]
                i += 2
            else:
                scenarios_path = "evals/real_world_scenarios.json"
                i += 1
        elif args[i].startswith("--scenarios="):
            scenarios_path = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--limit", "-n") and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif args[i].startswith("--limit="):
            try:
                limit = int(args[i].split("=", 1)[1])
            except ValueError:
                pass
            i += 1
        elif args[i] in ("--category", "-c") and i + 1 < len(args):
            category = args[i + 1]
            i += 2
        elif args[i].startswith("--category="):
            category = args[i].split("=", 1)[1]
            i += 1
        else:
            i += 1

    target_graph_path = graph_paths[0] if graph_paths else default_graph_path_fn()
    if comparative:
        from graph_fy.comparative_eval import run_comparative_evals
        comp_report = run_comparative_evals(
            graph_path=target_graph_path,
            db_path=db_path,
            root_dir=root_dir,
            scenarios_path=scenarios_path or "evals/real_world_scenarios.json",
            category_filter=category,
            limit=limit,
            mode=mode,
            traces_path=traces_path,
        )
        md_content = comp_report.to_markdown()
        if report_path:
            Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(md_content, encoding="utf-8")
            print(f"Comparative report written to {report_path}", file=sys.stderr if as_json else sys.stdout)
        if as_json:
            print(json.dumps(dataclasses.asdict(comp_report), indent=2))
        else:
            print(md_content)
        return 0 if comp_report.skill_pass_rate_pct >= 80.0 else 1
    else:
        from graph_fy.evals import run_evals
        report = run_evals(
            graph_path=target_graph_path,
            db_path=db_path,
            root_dir=root_dir,
            scenarios_path=scenarios_path,
            category_filter=category,
            limit=limit,
        )
        md_content = report.to_markdown()
        if report_path:
            Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(md_content, encoding="utf-8")
            print(f"Eval report written to {report_path}", file=sys.stderr if as_json else sys.stdout)
        if as_json:
            print(json.dumps(dataclasses.asdict(report), indent=2))
        else:
            print(md_content)
        return 0 if report.overall_score >= 80.0 else 1
