"""test_select.py — Native Test Selector for modified code hunks.

Maps git diff hunks to enclosing AST functions and executes reverse-BFS traversal
to select the minimal, highest-confidence subset of affected tests (unit and integration),
leveraging test-to-code coverage edges where available.
Outputs ready-to-run copy-pasteable test execution commands for pytest, cargo, npm/vitest,
go, and maven/gradle.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import networkx as nx

from graph_fy.impact import _is_test_node
from graph_fy.paths import out_path


def _parse_git_diff_hunks(
    base_ref: str,
    root_dir: Path | str = ".",
) -> dict[str, list[tuple[int, int]]]:
    """Execute git diff and parse modified line ranges per file."""
    root_path = Path(root_dir)
    diff_text = ""

    # Primary diff against base_ref
    try:
        proc = subprocess.run(
            ["git", "diff", "-U0", base_ref],
            cwd=str(root_path),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        diff_text = proc.stdout or ""
    except Exception:
        diff_text = ""

    # If diff is empty and ref is HEAD, also check uncommitted / unstaged changes
    if not diff_text.strip() and base_ref in ("HEAD", "HEAD~0"):
        try:
            proc2 = subprocess.run(
                ["git", "diff", "-U0"],
                cwd=str(root_path),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            diff_text = proc2.stdout or ""
        except Exception:
            pass

    file_changes: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            if current_file not in file_changes:
                file_changes[current_file] = []
        elif line.startswith("@@ ") and current_file:
            m = re.search(r"@@\s+-[0-9]+(?:,[0-9]+)?\s+\+([0-9]+)(?:,([0-9]+))?\s+@@", line)
            if m:
                start_l = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                end_l = start_l + max(count, 1) - 1
                file_changes[current_file].append((start_l, end_l))

    return file_changes


def _detect_runner(root_dir: Path, explicit_runner: str = "auto") -> str:
    """Detect the appropriate test runner based on project files."""
    if explicit_runner != "auto":
        return explicit_runner

    if (root_dir / "Cargo.toml").exists():
        return "cargo"
    if (root_dir / "pyproject.toml").exists() or (root_dir / "pytest.ini").exists() or (root_dir / "setup.py").exists():
        return "pytest"
    if (root_dir / "package.json").exists():
        try:
            pkg_data = json.loads((root_dir / "package.json").read_text(encoding="utf-8"))
            dev_deps = {**pkg_data.get("dependencies", {}), **pkg_data.get("devDependencies", {})}
            if "vitest" in dev_deps:
                return "vitest"
            if "jest" in dev_deps:
                return "jest"
        except Exception:
            pass
        return "npm"
    if (root_dir / "go.mod").exists():
        return "go"
    if (root_dir / "pom.xml").exists():
        return "mvn"
    if (root_dir / "build.gradle").exists() or (root_dir / "build.gradle.kts").exists():
        return "gradle"

    return "pytest"


def _format_test_command(runner: str, selected_tests: list[dict[str, Any]], root_dir: Path) -> str:
    """Generate ready-to-run test execution command."""
    if not selected_tests:
        return ""

    test_files: list[str] = []
    test_symbols: list[str] = []
    seen_files: set[str] = set()

    for t in selected_tests:
        sf = t.get("source_file") or ""
        lbl = t.get("label") or ""
        if sf and sf not in seen_files:
            seen_files.add(sf)
            test_files.append(sf)
        if lbl and lbl not in test_symbols:
            test_symbols.append(lbl)

    if runner == "pytest":
        files_str = " ".join(test_files[:10])
        if test_symbols and len(test_symbols) <= 8:
            k_expr = " or ".join(test_symbols)
            return f"pytest {files_str} -k \"{k_expr}\""
        return f"pytest {files_str}"

    elif runner == "cargo":
        # Check if tests are in tests/<name>.rs
        tests_args: list[str] = []
        for tf in test_files:
            p = Path(tf)
            if "tests" in p.parts and p.suffix == ".rs":
                tests_args.append(f"--test {p.stem}")
        if tests_args:
            sub = " ".join(tests_args[:4])
            if test_symbols and len(test_symbols) <= 4:
                return f"cargo test {sub} {test_symbols[0]}"
            return f"cargo test {sub}"
        if test_symbols:
            return f"cargo test {test_symbols[0]}"
        return "cargo test"

    elif runner in ("vitest", "npx vitest"):
        files_str = " ".join(test_files[:8])
        if test_symbols and len(test_symbols) <= 4:
            t_expr = "|".join(test_symbols)
            return f"npx vitest run {files_str} -t \"{t_expr}\""
        return f"npx vitest run {files_str}"

    elif runner in ("jest", "npx jest"):
        files_str = " ".join(test_files[:8])
        if test_symbols and len(test_symbols) <= 4:
            t_expr = "|".join(test_symbols)
            return f"npx jest {files_str} -t \"{t_expr}\""
        return f"npx jest {files_str}"

    elif runner == "npm":
        files_str = " ".join(test_files[:8])
        return f"npm test -- {files_str}"

    elif runner == "go":
        if test_symbols:
            pat = "^(" + "|".join(test_symbols[:8]) + ")$"
            return f"go test -v ./... -run \"{pat}\""
        return "go test -v ./..."

    elif runner == "mvn":
        if test_symbols:
            t_arg = ",".join(test_symbols[:6])
            return f"mvn test -Dtest={t_arg}"
        return "mvn test"

    elif runner == "gradle":
        if test_symbols:
            args = " ".join(f"--tests {s}" for s in test_symbols[:6])
            return f"./gradlew test {args}"
        return "./gradlew test"

    return f"pytest {' '.join(test_files[:10])}"


def select_affected_tests(
    graph: nx.Graph,
    since: str = "HEAD~1",
    root_dir: str | Path = ".",
    coverage_file: str | Path | None = None,
    runner: str = "auto",
    max_tests: int = 50,
) -> dict[str, Any]:
    """Select the minimal, highest-confidence subset of tests affected by git changes."""
    root_path = Path(root_dir)

    # Ingest coverage if provided or auto-discovered
    cov_meta: dict[str, Any] = {}
    try:
        from graph_fy.coverage_ingest import ingest_coverage
        cov_res = ingest_coverage(graph, coverage_path=coverage_file, root_dir=root_path)
        if cov_res.get("files_covered", 0) > 0:
            cov_meta = cov_res
    except Exception:
        pass

    # Step 1: Parse git diff
    file_changes = _parse_git_diff_hunks(base_ref=since, root_dir=root_path)
    if not file_changes:
        return {
            "since": since,
            "modified_files": [],
            "modified_functions": [],
            "selected_tests": [],
            "runner": _detect_runner(root_path, runner),
            "command": "",
            "coverage_meta": cov_meta,
            "summary": "No modified files detected.",
        }

    norm_changes = {Path(p).as_posix(): ranges for p, ranges in file_changes.items()}

    # Step 2: Map changed lines to graph nodes
    modified_nodes: list[str] = []
    direct_test_nodes: list[str] = []

    for nid, data in graph.nodes(data=True):
        src_file = data.get("source_file") or data.get("source_path") or ""
        if not src_file:
            continue
        norm_src = Path(src_file).as_posix()

        for cf, ranges in norm_changes.items():
            if norm_src == cf or norm_src.endswith("/" + cf) or cf.endswith("/" + norm_src):
                # Check line overlap
                start_l = data.get("start_line") or data.get("line_start")
                end_l = data.get("end_line") or data.get("line_end")
                if start_l is None:
                    loc = str(data.get("source_location") or "")
                    lm = re.search(r"L?(\d+)", loc)
                    start_l = int(lm.group(1)) if lm else None

                if start_l is not None:
                    end_l = end_l or start_l
                    if any(max(start_l, r_start) <= min(end_l, r_end) for r_start, r_end in ranges):
                        if _is_test_node(data):
                            direct_test_nodes.append(nid)
                        else:
                            modified_nodes.append(nid)
                elif not ranges:  # whole file change
                    if _is_test_node(data):
                        direct_test_nodes.append(nid)
                    else:
                        modified_nodes.append(nid)
                break

    # Step 3: Reverse-BFS traversal to tests
    candidate_tests: dict[str, dict[str, Any]] = {}

    # Directly modified tests get top score
    for tn in direct_test_nodes:
        t_data = graph.nodes[tn]
        candidate_tests[tn] = {
            "node_id": tn,
            "label": str(t_data.get("label") or tn),
            "source_file": str(t_data.get("source_file") or ""),
            "line": t_data.get("start_line") or t_data.get("line_start") or 1,
            "score": 1.0,
            "distance": 0,
            "reason": "directly_modified_test",
            "triggered_by": [tn],
        }

    # BFS from modified functions
    for mn in modified_nodes:
        mn_label = str(graph.nodes[mn].get("label") or mn)

        # 1. Direct coverage edges & callers (check both directions)
        neighbors_to_check = set()
        if graph.is_directed():
            neighbors_to_check.update(graph.predecessors(mn))
            neighbors_to_check.update(graph.successors(mn))
        else:
            neighbors_to_check.update(graph.neighbors(mn))

        for nb in neighbors_to_check:
            ed1 = graph.get_edge_data(nb, mn) or {}
            if isinstance(ed1, dict) and 0 in ed1:
                ed1 = ed1[0]
            ed2 = graph.get_edge_data(mn, nb) or {}
            if isinstance(ed2, dict) and 0 in ed2:
                ed2 = ed2[0]
            rel1 = str(ed1.get("relation") or "").lower()
            rel2 = str(ed2.get("relation") or "").lower()
            is_cov = rel1 in ("covered_by_test", "covers") or rel2 in ("covered_by_test", "covers")
            nb_data = graph.nodes[nb]
            if is_cov or _is_test_node(nb_data):
                if _is_test_node(nb_data):
                    entry = candidate_tests.setdefault(nb, {
                        "node_id": nb,
                        "label": str(nb_data.get("label") or nb),
                        "source_file": str(nb_data.get("source_file") or ""),
                        "line": nb_data.get("start_line") or nb_data.get("line_start") or 1,
                        "score": 0.0,
                        "distance": 1,
                        "reason": "direct_coverage_edge" if is_cov else "direct_caller_test",
                        "triggered_by": [],
                    })
                    entry["score"] = max(entry["score"], 0.95 if is_cov else 0.85)
                    if mn_label not in entry["triggered_by"]:
                        entry["triggered_by"].append(mn_label)

        # 2. Transitive callers (2 hops)
        visited: set[str] = {mn}
        queue: list[tuple[str, int, float]] = [(mn, 0, 1.0)]
        while queue:
            curr, dist, conf = queue.pop(0)
            if dist >= 2:
                continue

            preds = list(graph.predecessors(curr) if graph.is_directed() else graph.neighbors(curr))
            for p in preds:
                if p in visited:
                    continue
                visited.add(p)
                p_data = graph.nodes[p]
                new_conf = conf * 0.85

                if _is_test_node(p_data):
                    entry = candidate_tests.setdefault(p, {
                        "node_id": p,
                        "label": str(p_data.get("label") or p),
                        "source_file": str(p_data.get("source_file") or ""),
                        "line": p_data.get("start_line") or p_data.get("line_start") or 1,
                        "score": 0.0,
                        "distance": dist + 1,
                        "reason": f"{dist + 1}-hop_transitive_caller",
                        "triggered_by": [],
                    })
                    entry["score"] = max(entry["score"], new_conf)
                    if mn_label not in entry["triggered_by"]:
                        entry["triggered_by"].append(mn_label)
                else:
                    queue.append((p, dist + 1, new_conf))

    # Sort selected tests by score descending, then distance ascending
    sorted_tests = sorted(
        candidate_tests.values(),
        key=lambda x: (-x["score"], x["distance"], x["label"]),
    )[:max_tests]

    effective_runner = _detect_runner(root_path, runner)
    cmd = _format_test_command(effective_runner, sorted_tests, root_path)

    return {
        "since": since,
        "modified_files": list(file_changes.keys()),
        "modified_functions": [str(graph.nodes[n].get("label") or n) for n in modified_nodes],
        "selected_tests": sorted_tests,
        "runner": effective_runner,
        "command": cmd,
        "coverage_meta": cov_meta,
        "summary": f"Selected {len(sorted_tests)} tests for {len(modified_nodes)} modified functions across {len(file_changes)} files.",
    }


def cmd_test_select(argv: list[str]) -> int:
    """CLI handler for `graph_fy test-select`."""
    from graph_fy.affected import load_graph

    since = "HEAD~1"
    root_dir = "."
    graph_path: str | Path | None = None
    coverage_path: str | Path | None = None
    runner = "auto"
    max_tests = 50
    as_json = "--json" in argv

    args = argv[2:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--since", "-s") and i + 1 < len(args):
            since = args[i + 1]
            i += 2
        elif a.startswith("--since="):
            since = a.split("=", 1)[1]
            i += 1
        elif a in ("--dir", "-d", "--root", "-r") and i + 1 < len(args):
            root_dir = args[i + 1]
            i += 2
        elif a.startswith("--dir=") or a.startswith("--root="):
            root_dir = a.split("=", 1)[1]
            i += 1
        elif a in ("--graph", "-g") and i + 1 < len(args):
            graph_path = args[i + 1]
            i += 2
        elif a.startswith("--graph="):
            graph_path = a.split("=", 1)[1]
            i += 1
        elif a in ("--coverage", "-c") and i + 1 < len(args):
            coverage_path = args[i + 1]
            i += 2
        elif a.startswith("--coverage="):
            coverage_path = a.split("=", 1)[1]
            i += 1
        elif a in ("--runner",) and i + 1 < len(args):
            runner = args[i + 1]
            i += 2
        elif a.startswith("--runner="):
            runner = a.split("=", 1)[1]
            i += 1
        elif a in ("--max-tests", "-m") and i + 1 < len(args):
            try:
                max_tests = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif a.startswith("--max-tests="):
            try:
                max_tests = int(a.split("=", 1)[1])
            except ValueError:
                pass
            i += 1
        elif not a.startswith("-"):
            since = a
            i += 1
        else:
            i += 1

    root_p = Path(root_dir)
    gp: Path
    if graph_path:
        gp = Path(graph_path)
    elif (root_p / "graph_fy_out" / "graph.json").exists():
        gp = root_p / "graph_fy_out" / "graph.json"
    elif (root_p / "graph.json").exists():
        gp = root_p / "graph.json"
    else:
        gp = out_path("graph.json")

    if not gp.exists():
        print(f"Error: Knowledge graph not found at {gp}. Run `graph_fy extract` first.", file=sys.stderr)
        return 1

    try:
        graph = load_graph(gp)
    except Exception as exc:
        print(f"Error loading graph from {gp}: {exc}", file=sys.stderr)
        return 1

    res = select_affected_tests(
        graph=graph,
        since=since,
        root_dir=root_p,
        coverage_file=coverage_path,
        runner=runner,
        max_tests=max_tests,
    )

    if as_json:
        print(json.dumps(res, indent=2))
        return 0

    # Human-readable Caveman output
    print(f"\n🎯 [graph_fy test-select] {res['summary']}")
    if res.get("coverage_meta", {}).get("files_covered", 0) > 0:
        cm = res["coverage_meta"]
        print(f"📊 Coverage Ingested: {cm['files_covered']} files, {cm['overall_coverage_pct']}% overall line coverage ({cm['edges_added']} edges added)")

    if res.get("modified_functions"):
        print("\nChanged Functions:")
        for fn in res["modified_functions"][:10]:
            print(f"  • {fn}")
        if len(res["modified_functions"]) > 10:
            print(f"  ... and {len(res['modified_functions']) - 10} more")

    tests = res.get("selected_tests", [])
    if tests:
        print(f"\nSelected Tests ({len(tests)} targets):")
        print(f"  {'TEST SYMBOL':<36} {'DISTANCE':<10} {'CONFIDENCE':<12} {'REASON'}")
        print("  " + "-" * 75)
        for t in tests[:15]:
            lbl = t["label"][:34]
            dist = f"{t['distance']} hop"
            conf = f"{t['score']:.2f}"
            print(f"  {lbl:<36} {dist:<10} {conf:<12} {t['reason']}")
        if len(tests) > 15:
            print(f"  ... and {len(tests) - 15} more tests")

        cmd = res.get("command", "")
        if cmd:
            print("\n🚀 Ready-to-Run Test Command:")
            print(f"   {cmd}\n")
    else:
        print("\nNo tests found directly or transitively covering the modified lines.\n")

    return 0


def cmd_coverage(argv: list[str]) -> int:
    """CLI handler for `graph_fy coverage` / `graph_fy ingest-coverage`."""
    from graph_fy.affected import load_graph
    from graph_fy.coverage_ingest import ingest_coverage

    root_dir = "."
    graph_path: str | Path | None = None
    coverage_path: str | Path | None = None
    as_json = "--json" in argv

    args = argv[2:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--dir", "-d", "--root", "-r") and i + 1 < len(args):
            root_dir = args[i + 1]
            i += 2
        elif a.startswith("--dir=") or a.startswith("--root="):
            root_dir = a.split("=", 1)[1]
            i += 1
        elif a in ("--graph", "-g") and i + 1 < len(args):
            graph_path = args[i + 1]
            i += 2
        elif a.startswith("--graph="):
            graph_path = a.split("=", 1)[1]
            i += 1
        elif not a.startswith("-"):
            coverage_path = a
            i += 1
        else:
            i += 1

    root_p = Path(root_dir)
    gp: Path
    if graph_path:
        gp = Path(graph_path)
    elif (root_p / "graph_fy_out" / "graph.json").exists():
        gp = root_p / "graph_fy_out" / "graph.json"
    elif (root_p / "graph.json").exists():
        gp = root_p / "graph.json"
    else:
        gp = out_path("graph.json")

    if not gp.exists():
        print(f"Error: Knowledge graph not found at {gp}. Run `graph_fy extract` first.", file=sys.stderr)
        return 1

    try:
        graph = load_graph(gp)
    except Exception as exc:
        print(f"Error loading graph from {gp}: {exc}", file=sys.stderr)
        return 1

    res = ingest_coverage(graph, coverage_path=coverage_path, root_dir=root_p)

    if as_json:
        print(json.dumps(res, indent=2))
        return 0

    if res.get("error"):
        print(f"❌ [graph_fy coverage] {res['error']}", file=sys.stderr)
        return 1

    print(f"\n📊 [graph_fy coverage] Successfully ingested {res.get('coverage_file', '')}")
    print(f"  • Files covered: {res['files_covered']}")
    print(f"  • AST nodes annotated: {res['nodes_annotated']}")
    print(f"  • Test-to-code edges added: {res['edges_added']}")
    print(f"  • Overall repository line coverage: {res['overall_coverage_pct']}%\n")
    return 0
