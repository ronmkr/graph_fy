"""Lint and diagnose command implementations for graph_fy CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from graph_fy.diagnostics import (
    diagnose_file,
    format_diagnostic_json,
    format_diagnostic_report,
)
from graph_fy.lint import run_lint


def cmd_lint(
    argv: list[str],
    default_graph_path_fn: Callable[[], str],
    find_graph_path_fn: Callable[[str | Path | None], Path],
) -> int:
    """Handle `graph_fy lint` command."""
    graph_path = Path(default_graph_path_fn())
    json_output = False
    target_path: Path | None = None

    flag_cycles = False
    flag_layers = False
    flag_dead = False
    flag_orphans = False
    flag_actions = False
    flag_drift = False
    flag_secrets = False
    flag_all = False
    base_graph_path: Path | None = None

    i = 2
    while i < len(argv):
        arg = argv[i]
        if arg in ("--graph", "-g") and i + 1 < len(argv):
            i += 1
            graph_path = Path(argv[i])
        elif arg.startswith("--graph="):
            graph_path = Path(arg.split("=", 1)[1])
        elif arg in ("--base", "--base-graph") and i + 1 < len(argv):
            i += 1
            base_graph_path = Path(argv[i])
        elif arg.startswith(("--base=", "--base-graph=")):
            base_graph_path = Path(arg.split("=", 1)[1])
        elif arg in ("--root", "-r", "--dir", "-d") and i + 1 < len(argv):
            i += 1
            graph_path = find_graph_path_fn(argv[i])
        elif arg.startswith(("--root=", "--dir=")):
            graph_path = find_graph_path_fn(arg.split("=", 1)[1])
        elif arg == "--json":
            json_output = True
        elif arg in ("--cycles", "--cycle"):
            flag_cycles = True
        elif arg in ("--layers", "--layer"):
            flag_layers = True
        elif arg in ("--dead", "--dead-code"):
            flag_dead = True
        elif arg in ("--orphans", "--orphan"):
            flag_orphans = True
        elif arg in ("--actions", "--action"):
            flag_actions = True
        elif arg == "--drift":
            flag_drift = True
        elif arg in ("--secrets", "--secret"):
            flag_secrets = True
        elif arg == "--all":
            flag_all = True
        elif not arg.startswith("-") and target_path is None:
            target_path = Path(arg)
        i += 1

    if target_path:
        graph_path = find_graph_path_fn(target_path)

    if not graph_path.exists():
        print(f"error: graph file not found at {graph_path}. Run `graph_fy build` first.", file=sys.stderr)
        return 1

    try:
        with open(graph_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"error: failed to read {graph_path}: {exc}", file=sys.stderr)
        return 1

    base_graph = None
    if base_graph_path and base_graph_path.exists():
        try:
            with open(base_graph_path, encoding="utf-8") as f:
                base_graph = json.load(f)
        except Exception:
            pass

    has_specific = any([flag_cycles, flag_layers, flag_dead, flag_orphans, flag_actions, flag_drift, flag_secrets])
    if flag_all:
        result = run_lint(
            data,
            check_actions=True,
            check_drift=True,
            check_secrets=True,
            check_orphans=True,
            check_rtk=True,
            check_dead=True,
            check_cycles=True,
            check_layers=True,
            check_context_managers=True,
            base_graph=base_graph,
        )
    elif has_specific:
        result = run_lint(
            data,
            check_actions=flag_actions,
            check_drift=flag_drift,
            check_secrets=flag_secrets,
            check_orphans=flag_orphans,
            check_rtk=False,
            check_dead=flag_dead,
            check_cycles=flag_cycles,
            check_layers=flag_layers,
            check_context_managers=True,
            base_graph=base_graph,
        )
    else:
        result = run_lint(
            data,
            check_actions=True,
            check_drift=True,
            check_secrets=True,
            check_orphans=True,
            check_rtk=True,
            check_dead=False,
            check_cycles=True,
            check_layers=True,
            check_context_managers=True,
            base_graph=base_graph,
        )
    if json_output:
        print(json.dumps(result, indent=2))
    else:
        summary = result["summary"]
        print(f"graph_fy Lint: {result['status'].upper()}")
        print(f"Summary: {summary['errors']} errors, {summary['warnings']} warnings, {summary['info']} info")
        for item in result["findings"]:
            sev = item.get("severity", "info").upper()
            rule = item.get("rule", "")
            msg = item.get("message", "")
            print(f"[{sev}] [{rule}] {msg}")
    return 1 if result["summary"]["errors"] > 0 else 0


def cmd_diagnose(
    argv: list[str],
    default_graph_path_fn: Callable[[], str],
) -> int:
    """Handle `graph_fy diagnose` command."""
    subcmd = argv[2] if len(argv) > 2 and not argv[2].startswith("-") else ""
    if subcmd != "multigraph":
        usage_cmd = "diagnose multigraph"
        print(
            f"Usage: graph_fy {usage_cmd} "
            "[--graph path] [--json] [--max-examples N] "
            "[--directed] [--undirected] [--extract-path path]",
            file=sys.stderr,
        )
        return 1

    graph_path = Path(default_graph_path_fn())
    max_examples = 5
    directed: bool | None = None
    direction_flag: str | None = None
    json_output = False
    extract_path: Path | None = None

    i = 2 if (len(argv) > 2 and argv[2].startswith("-")) else 3
    while i < len(argv):
        arg = argv[i]
        if arg == "--graph":
            i += 1
            if i >= len(argv):
                print("error: --graph requires a path", file=sys.stderr)
                return 1
            graph_path = Path(argv[i])
        elif arg == "--json":
            json_output = True
        elif arg == "--max-examples":
            i += 1
            if i >= len(argv):
                print("error: --max-examples requires an integer", file=sys.stderr)
                return 1
            try:
                max_examples = int(argv[i])
            except ValueError:
                print("error: --max-examples requires an integer", file=sys.stderr)
                return 1
            if max_examples < 0:
                print("error: --max-examples must be >= 0", file=sys.stderr)
                return 1
        elif arg == "--directed":
            if direction_flag == "undirected":
                print(
                    "error: --directed and --undirected are mutually exclusive",
                    file=sys.stderr,
                )
                return 1
            direction_flag = "directed"
            directed = True
        elif arg == "--undirected":
            if direction_flag == "directed":
                print(
                    "error: --directed and --undirected are mutually exclusive",
                    file=sys.stderr,
                )
                return 1
            direction_flag = "undirected"
            directed = False
        elif arg == "--extract-path":
            i += 1
            if i >= len(argv):
                print("error: --extract-path requires a path", file=sys.stderr)
                return 1
            extract_path = Path(argv[i])
        else:
            print(f"error: unknown diagnose option {arg}", file=sys.stderr)
            return 1
        i += 1

    try:
        summary = diagnose_file(
            graph_path,
            directed=directed,
            root=Path(".").resolve(),
            max_examples=max_examples,
            extract_path=extract_path,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if json_output:
        print(json.dumps(format_diagnostic_json(summary), indent=2))
    else:
        print(format_diagnostic_report(summary))
    return 0
