from __future__ import annotations

from collections import Counter
import difflib
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import networkx as nx

from graph_fy.paths import load_node_link_graph, out_path
from graph_fy.retrieval_format import _find_callers_and_callees, _format_source_location, format_rag_prompt
from graph_fy.retrieval_index import get_default_index_path, split_subtokens
from graph_fy.skeleton import compact_signature, get_symbol_code_from_file

_KEYWORDS_AND_BUILTINS = frozenset({
    # Python
    "def", "class", "return", "import", "from", "as", "if", "elif", "else",
    "for", "while", "try", "except", "finally", "with", "yield", "lambda",
    "pass", "break", "continue", "raise", "assert", "global", "nonlocal",
    "async", "await", "true", "false", "none", "self", "cls", "int", "str",
    "float", "bool", "list", "dict", "set", "tuple", "print", "len", "range",
    "isinstance", "issubclass", "hasattr", "getattr", "setattr", "delattr",
    # JS/TS
    "const", "let", "var", "function", "export", "default",
    "extends", "implements", "interface", "type", "new", "this",
    "throw", "catch", "switch", "case", "null", "undefined",
    "void", "any", "number", "string", "boolean", "console", "log",
})


def diff_context(
    graph: nx.Graph | None = None,
    base_ref: str = "HEAD",
    root_dir: str | Path = ".",
    token_budget: int = 2000,
    graph_path: Path | str | None = None,
    format: str = "default",
) -> dict[str, Any]:
    """Retrieve git diff-scoped ego-network context for code review / PR analysis.

    1. Executes `git diff -U0 <base_ref>` to identify modified files and line ranges.
    2. Maps changed line ranges to graph nodes via source_file & source_location.
    3. Finds 1-hop callers, affected tests, and api routes.
    4. Packs node skeletons and impact summary into `token_budget`.
    """
    root_path = Path(root_dir)

    diff_text = ""
    try:
        proc = subprocess.run(
            ["git", "diff", "-U0", base_ref],
            cwd=str(root_path),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        diff_text = proc.stdout or ""
    except Exception:
        diff_text = ""

    if not diff_text.strip() and base_ref == "HEAD":
        try:
            proc2 = subprocess.run(
                ["git", "diff", "-U0"],
                cwd=str(root_path),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
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

    if graph is None:
        if graph_path is not None:
            gp = Path(graph_path)
        elif (root_path / "graph_fy_out" / "graph.json").exists():
            gp = root_path / "graph_fy_out" / "graph.json"
        elif (root_path / "graph.json").exists():
            gp = root_path / "graph.json"
        else:
            gp = out_path("graph.json")
        if not gp.exists():
            return {
                "base_ref": base_ref,
                "modified_files": list(file_changes.keys()),
                "changed_nodes": [],
                "callers": [],
                "affected_tests": [],
                "endpoints": [],
                "prompt_context": "No knowledge graph found.",
            }
        try:
            graph = load_node_link_graph(gp)
        except Exception:
            return {
                "base_ref": base_ref,
                "modified_files": list(file_changes.keys()),
                "changed_nodes": [],
                "callers": [],
                "affected_tests": [],
                "endpoints": [],
                "prompt_context": "Error parsing knowledge graph.",
            }

    changed_nodes: list[str] = []
    norm_changes = {Path(p).as_posix(): ranges for p, ranges in file_changes.items()}

    file_to_nodes: dict[str, list[tuple[str, int | None]]] = {}
    for nid, data in graph.nodes(data=True):
        src_file = data.get("source_file") or data.get("source_path") or ""
        if not src_file:
            continue
        norm_src = Path(src_file).as_posix()
        for cf in norm_changes:
            if norm_src == cf or norm_src.endswith("/" + cf) or cf.endswith("/" + norm_src):
                src_loc = str(data.get("source_location") or "")
                lm = re.search(r"L?(\d+)", src_loc)
                line_num = int(lm.group(1)) if lm else None
                file_to_nodes.setdefault(cf, []).append((nid, line_num))
                break

    for cf, matched_ranges in norm_changes.items():
        nodes_in_file = file_to_nodes.get(cf, [])
        if not nodes_in_file:
            continue
        numbered = [(nid, lnum) for nid, lnum in nodes_in_file if lnum is not None]
        numbered.sort(key=lambda x: x[1])

        for idx, (nid, lnum) in enumerate(numbered):
            next_lnum = numbered[idx + 1][1] - 1 if idx + 1 < len(numbered) else 999999
            node_start = lnum
            node_end = next_lnum
            for start_l, end_l in matched_ranges:
                if max(node_start, start_l) <= min(node_end, end_l):
                    changed_nodes.append(nid)
                    break

        for nid, lnum in nodes_in_file:
            if lnum is None and nid not in changed_nodes:
                changed_nodes.append(nid)

    callers: list[str] = []
    affected_tests: list[str] = []
    endpoints: list[str] = []
    seen_callers: set[str] = set()
    seen_tests: set[str] = set()

    for cn in changed_nodes:
        c_list, _ = _find_callers_and_callees(graph, cn)
        for c in c_list:
            if c not in seen_callers and c not in changed_nodes:
                seen_callers.add(c)
                callers.append(c)

        for nb in (graph.neighbors(cn) if cn in graph else []):
            nb_data = graph.nodes[nb]
            nb_src = str(nb_data.get("source_file") or nb_data.get("source_path") or "")
            nb_lbl = str(nb_data.get("label") or nb)

            if ("test" in nb.lower() or "test" in nb_src.lower()) and nb not in seen_tests:
                seen_tests.add(nb)
                affected_tests.append(nb_lbl)

            ed = graph.get_edge_data(cn, nb) or {}
            if isinstance(ed, dict) and 0 in ed:
                ed = ed[0]
            rel = str(ed.get("relation") or "").lower()
            if rel in ("route", "endpoint", "api") or "route" in nb.lower():
                endpoints.append(nb_lbl)

    risk_tier = "LOW"
    risk_score = 0.0
    downstream_impact_count = 0
    try:
        from graph_fy.impact import compute_impact
        for cn in changed_nodes[:5]:
            imp = compute_impact(graph, cn, max_depth=3)
            r_score = float(imp.get("risk_score", 0.0))
            if r_score > risk_score:
                risk_score = r_score
                risk_tier = str(imp.get("risk_tier", "LOW"))
            for at in imp.get("affected_tests", []):
                t_lbl = str(at.get("label", at.get("node", at))) if isinstance(at, dict) else str(at)
                t_node = str(at.get("node", t_lbl)) if isinstance(at, dict) else str(at)
                if t_node not in seen_tests:
                    seen_tests.add(t_node)
                    affected_tests.append(t_lbl)
            for ap in imp.get("affected_apis", []):
                ap_lbl = str(ap.get("label", ap.get("node", ap))) if isinstance(ap, dict) else str(ap)
                if ap_lbl not in endpoints:
                    endpoints.append(ap_lbl)
    except Exception:
        pass

    co_changed_files: list[tuple[str, float]] = []
    if file_changes and root_path.is_dir():
        try:
            log_proc = subprocess.run(
                ["git", "log", "--pretty=format:---COMMIT---", "--name-only", "-n", "50"],
                cwd=str(root_path),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            commits = log_proc.stdout.split("---COMMIT---")
            target_set = {Path(f).as_posix() for f in file_changes.keys()}
            co_counts: Counter[str] = Counter()
            target_commits = 0
            for c in commits:
                files = {Path(f.strip()).as_posix() for f in c.splitlines() if f.strip()}
                if target_set.intersection(files):
                    target_commits += 1
                    for f in files:
                        if f not in target_set:
                            co_counts[f] += 1
            if target_commits > 0:
                for f, count in co_counts.most_common(4):
                    freq = round(count / target_commits, 2)
                    if freq >= 0.2:
                        co_changed_files.append((f, freq))
        except Exception:
            pass

    # Dynamic Language Blindspot & Confidence Detection
    dynamic_risks: set[str] = set()
    for mf in file_changes.keys():
        p = Path(mf) if Path(mf).is_absolute() else (root_path / mf)
        if p.is_file():
            try:
                code_text = p.read_text(encoding="utf-8", errors="ignore")
                if "getattr(" in code_text or "hasattr(" in code_text or "setattr(" in code_text:
                    dynamic_risks.add("reflection/getattr")
                if "importlib" in code_text or "__import__" in code_text:
                    dynamic_risks.add("dynamic-import")
                if "Depends(" in code_text:
                    dynamic_risks.add("fastapi-di")
                if "@pytest.fixture" in code_text:
                    dynamic_risks.add("pytest-fixture")
                if "**kwargs" in code_text:
                    dynamic_risks.add("kwargs-forwarding")
            except OSError:
                pass

    confidence_tier = (
        f"[DYNAMIC RISK DETECTED] ({', '.join(sorted(dynamic_risks))})"
        if dynamic_risks
        else "[HIGH CONFIDENCE] (Static AST Verified)"
    )

    lines: list[str] = [
        f"### Diff-Scoped Ego Context (base: {base_ref})",
        f"- Blast Radius Risk: {risk_tier} (Score: {risk_score:.2f}, {downstream_impact_count} downstream dependents)",
        f"- AST Confidence: {confidence_tier}",
        f"- Modified files: {len(file_changes)} ({', '.join(file_changes.keys()) or 'none'})",
        f"- Directly touched symbols: {len(changed_nodes)}",
    ]
    if callers:
        lines.append(f"- Impacted callers (1-hop): {', '.join(callers[:10])}")
    if affected_tests:
        lines.append(f"- Potentially affected tests: {', '.join(affected_tests[:10])}")
    if endpoints:
        lines.append(f"- API / Route Endpoints: {', '.join(endpoints[:10])}")
    if co_changed_files:
        lines.append(f"- Historically Co-Changed: {', '.join(f'{f} ({int(freq*100)}%)' for f, freq in co_changed_files)}")

    char_budget = token_budget * 4
    current_chars = sum(len(l) + 1 for l in lines)

    lines.append("\n#### Changed Symbol AST Skeletons:")
    for cn in changed_nodes:
        if current_chars >= char_budget:
            break
        cdata = graph.nodes[cn]
        lbl = cdata.get("label") or cn
        spath = cdata.get("source_file") or cdata.get("source_path") or ""
        sloc = cdata.get("source_location") or ""
        skel = cdata.get("skeleton") or ""

        if not skel and spath and sloc and root_path.is_dir():
            target_f = root_path / spath
            if target_f.exists():
                try:
                    skel = get_symbol_code_from_file(target_f, sloc, skeleton=True)
                except Exception:
                    pass

        header = f"// Symbol: {lbl} ({spath}:{sloc})"
        if skel:
            block = f"{header}\n{skel}\n"
            if current_chars + len(block) <= char_budget:
                lines.append(block)
                current_chars += len(block)
            else:
                sig = compact_signature(skel)
                block = f"{header}\n{sig}\n"
                lines.append(block)
                current_chars += len(block)
                break
        else:
            block = f"{header}\n"
            lines.append(block)
            current_chars += len(block)

    if format == "github-pr":
        risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "CRITICAL": "🚨"}.get(risk_tier, "⚪")
        pr_lines: list[str] = [
            "## 🔍 graph_fy AST Blast Radius Review",
            "",
            "| Metric | Blast Radius Analysis |",
            "| :--- | :--- |",
            f"| **Risk Tier** | {risk_emoji} **{risk_tier}** (Score: `{risk_score:.2f}`, `{downstream_impact_count}` downstream dependents) |",
            f"| **Confidence** | {confidence_tier} |",
            f"| **Modified Files** | `{len(file_changes)}` file(s) (`{', '.join(sorted(file_changes.keys())) or 'none'}`) |",
            f"| **Directly Touched Symbols** | `{len(changed_nodes)}` symbol(s) |",
            f"| **Impacted Callers (1-Hop)** | `{len(callers)}` caller(s) |",
            f"| **Affected Test Targets** | `{len(affected_tests)}` test suite(s) |",
            "",
            "> ℹ️ **Static Blast Radius Grounding**: Blast radius represents statically resolvable dependencies from the AST. Dynamic dispatch, runtime reflection (`getattr`), and framework DI are noted in confidence scoring.",
            "",
        ]

        if changed_nodes:
            pr_lines.extend([
                f"<details><summary><b>Touched AST Symbols & Skeletons ({len(changed_nodes)})</b></summary>",
                "",
                "```python",
            ])
            for cn in changed_nodes[:8]:
                c_data = graph.nodes[cn]
                spath = c_data.get("source_file") or c_data.get("source_path")
                sloc = c_data.get("source_location")
                lbl = c_data.get("label", cn)
                if spath:
                    res_path = Path(spath) if Path(spath).is_absolute() else (root_path / spath)
                    if res_path.is_file():
                        lm = re.search(r"L?(\d+)", str(sloc or ""))
                        lnum = int(lm.group(1)) if lm else 1
                        skel = get_symbol_code_from_file(res_path, lnum, skeletonize=True, compact_docstrings=True)
                        if skel:
                            pr_lines.append(f"// Symbol: {lbl} ({spath}:{sloc})\n{skel}")
            pr_lines.extend(["```", "", "</details>", ""])

        if callers:
            caller_items = "\n".join(f"- `{c}`" for c in callers[:15])
            pr_lines.extend([
                f"<details><summary><b>Impacted Callers ({len(callers)})</b></summary>",
                "",
                caller_items,
                "",
                "</details>",
                "",
            ])

        if affected_tests:
            test_items = "\n".join(f"- `{t}`" for t in affected_tests[:10])
            pr_lines.extend([
                f"<details><summary><b>Recommended Test Verification Targets ({len(affected_tests)})</b></summary>",
                "",
                test_items,
                "",
                "</details>",
                "",
            ])

        if co_changed_files:
            co_items = "\n".join(f"- `{f}` ({int(freq*100)}% co-change)" for f, freq in co_changed_files)
            pr_lines.extend([
                f"<details><summary><b>Historically Co-Changed Files (Git)</b></summary>",
                "",
                co_items,
                "",
                "</details>",
                "",
            ])

        prompt_context = "\n".join(pr_lines)
    else:
        prompt_context = "\n".join(lines)

    return {
        "base_ref": base_ref,
        "risk_tier": risk_tier,
        "risk_score": round(risk_score, 2),
        "confidence_tier": confidence_tier,
        "dynamic_risks": sorted(dynamic_risks),
        "downstream_impact_count": downstream_impact_count,
        "modified_files": list(file_changes.keys()),
        "changed_nodes": changed_nodes,
        "callers": callers,
        "affected_tests": affected_tests,
        "endpoints": endpoints,
        "historically_co_changed": co_changed_files,
        "prompt_context": prompt_context,
    }


def refine_query(
    code_draft: str,
    graph: nx.Graph | None = None,
    db_path: Path | str | None = None,
    *,
    root_dir: str | Path = ".",
    token_budget: int = 2000,
    graph_path: Path | str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Iterative feedback querying (RepoCoder pattern).

    Extracts referenced identifiers from an agent's incomplete code draft,
    queries FTS5 BM25 index, excludes draft-local symbols, and retrieves
    missing dependency skeletons and type signatures packed within token budget.
    """
    from graph_fy.retrieval import query

    root_path = Path(root_dir)
    if graph_path is None:
        if (root_path / "graph_fy_out" / "graph.json").exists():
            graph_path = root_path / "graph_fy_out" / "graph.json"
        elif (root_path / "graph_fy_out" / "graph.json").exists():
            graph_path = root_path / "graph_fy_out" / "graph.json"
    if db_path is None:
        if (root_path / "graph_fy_out" / "index.db").exists():
            db_path = root_path / "graph_fy_out" / "index.db"
        elif (root_path / "graph_fy_out" / "index.db").exists():
            db_path = root_path / "graph_fy_out" / "index.db"

    if not code_draft or not code_draft.strip():
        return {
            "extracted_identifiers": [],
            "resolved_dependencies": [],
            "prompt_context": "",
        }

    defined_in_draft: set[str] = set()
    for m in re.finditer(r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", code_draft):
        defined_in_draft.add(m.group(1))
    for m in re.finditer(r"\b(?:function|class|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)", code_draft):
        defined_in_draft.add(m.group(1))
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", code_draft):
        defined_in_draft.add(m.group(1))

    all_raw_idents = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", code_draft)
    unresolved_idents: list[str] = []
    seen = set()
    for ident in all_raw_idents:
        if ident in defined_in_draft or ident.lower() in _KEYWORDS_AND_BUILTINS:
            continue
        if ident not in seen:
            seen.add(ident)
            unresolved_idents.append(ident)

    if not unresolved_idents:
        return {
            "extracted_identifiers": [],
            "resolved_dependencies": [],
            "prompt_context": "",
        }

    q_tokens: list[str] = []
    for uid in unresolved_idents[:20]:
        sub = split_subtokens(uid)
        q_tokens.extend(sub)
        q_tokens.append(uid)
    q_tokens = list(dict.fromkeys(q_tokens))
    query_str = " ".join(q_tokens)

    results = query(
        query_str,
        graph=graph,
        db_path=db_path,
        limit=limit,
        graph_path=graph_path,
        skeleton=True,
    )
    if isinstance(results, str):
        candidates = []
    else:
        candidates = results

    resolved: list[dict[str, Any]] = [
        c for c in candidates
        if c.get("label") not in defined_in_draft and c.get("node_id") not in defined_in_draft
    ]

    formatted_context = format_rag_prompt(resolved, token_budget=token_budget, question="Draft dependency resolution")

    return {
        "extracted_identifiers": unresolved_idents,
        "resolved_dependencies": resolved,
        "prompt_context": formatted_context,
    }


def suggest_close_symbols(
    query_terms: list[str],
    graph: nx.Graph | None = None,
    db_path: Path | str | None = None,
    *,
    limit: int = 3,
    cutoff: float = 0.65,
) -> list[str]:
    """Find close verified AST symbol names using standard difflib matching."""
    labels: set[str] = set()
    if graph is not None:
        for nid, d in graph.nodes(data=True):
            lbl = d.get("label") or (nid.split("::")[-1] if isinstance(nid, str) else "")
            if lbl:
                labels.add(str(lbl))
    elif db_path is not None and Path(db_path).exists():
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            cur = conn.cursor()
            for row in cur.execute("SELECT DISTINCT title FROM nodes_fts LIMIT 5000"):
                if row[0]:
                    labels.add(str(row[0]))
            conn.close()
        except Exception:
            pass

    if not labels:
        return []

    lower_map: dict[str, str] = {}
    for s in labels:
        clean = s.strip("().").strip()
        if clean:
            lower_map[clean.lower()] = clean

    stop_words = _KEYWORDS_AND_BUILTINS.union({
        "find", "get", "set", "fix", "check", "run", "test", "issue", "bug",
        "implement", "update", "create", "delete", "remove", "add", "call", "show", "view"
    })
    candidates_found: list[str] = []
    for term in query_terms:
        term_clean = term.strip("().").strip().lower()
        if len(term_clean) < 3 or term_clean in stop_words:
            continue
        close_keys = difflib.get_close_matches(term_clean, lower_map.keys(), n=limit, cutoff=cutoff)
        for k in close_keys:
            candidates_found.append(lower_map[k])

    return list(dict.fromkeys(candidates_found))[: limit * 2]


def task_context(
    task: str,
    graph: nx.Graph | None = None,
    db_path: Path | str | None = None,
    *,
    root_dir: str | Path = ".",
    token_budget: int = 1500,
    graph_path: Path | str | None = None,
    include_diagram: bool = True,
    limit: int = 6,
) -> dict[str, Any]:
    """Generate a holistic, deterministic pre-agent architectural briefing.

    Combines:
    - Target subsystem & architectural community identification
    - Compact Tier-1 AST skeletons with 1-line docstrings and type annotations
    - Bidirectional execution flow (callers/callees with compaction)
    - Downstream blast radius risk scoring & affected test targets
    - Historical git temporal co-changes
    - Mermaid architectural flow diagram
    - Token economy savings metric
    """
    from graph_fy.retrieval import query

    root_path = Path(root_dir)
    if graph_path is None:
        if (root_path / "graph_fy_out" / "graph.json").exists():
            graph_path = root_path / "graph_fy_out" / "graph.json"
        elif (root_path / "graph.json").exists():
            graph_path = root_path / "graph.json"
    if db_path is None:
        if graph_path is not None and (Path(graph_path).parent / "index.db").exists():
            db_path = Path(graph_path).parent / "index.db"
        elif graph is None and (root_path / "graph_fy_out" / "index.db").exists():
            db_path = root_path / "graph_fy_out" / "index.db"
        elif graph is None and (root_path / "index.db").exists():
            db_path = root_path / "index.db"

    if graph is None and graph_path is not None and Path(graph_path).exists():
        try:
            graph = load_node_link_graph(graph_path)
        except Exception:
            graph = None

    if not task or not task.strip():
        return {
            "task": task,
            "subsystems": [],
            "symbols": [],
            "did_you_mean": [],
            "risk_tier": "LOW",
            "risk_score": 0.0,
            "affected_tests": [],
            "historically_co_changed": [],
            "architecture_diagram": "",
            "tokens_saved": 0,
            "prompt_context": "No task provided.",
        }

    # 1. Expand query terms using sub-tokens
    raw_terms = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]+\b", task)
    expanded_terms: list[str] = []
    for t in raw_terms:
        if t.lower() not in _KEYWORDS_AND_BUILTINS and len(t) > 1:
            expanded_terms.extend(split_subtokens(t))
            expanded_terms.append(t)
    query_str = " ".join(list(dict.fromkeys(expanded_terms))) if expanded_terms else task

    # 2. Retrieve candidate symbols with AST skeletons and boundary pruning
    res = query(
        query_str,
        graph=graph,
        db_path=db_path,
        limit=limit,
        graph_path=graph_path,
        skeleton=True,
        boundary_pruning=True,
        root=root_path,
    )
    candidates: list[dict[str, Any]] = res if isinstance(res, list) else []

    # Grounded Anti-Hallucination Filter:
    # Ensure candidates match at least one significant user query term to reject false-positive stem hits.
    # If explicit code identifiers (e.g. foo_bar, MyClass) are present, require grounding on code identifiers.
    code_identifiers = [
        t.lower() for t in raw_terms
        if ("_" in t or any(c.isupper() for c in t[1:]))
        and len(t) >= 4
        and t.lower() not in _KEYWORDS_AND_BUILTINS
    ]

    significant_terms = [
        t.lower() for t in raw_terms
        if len(t) >= 3 and t.lower() not in _KEYWORDS_AND_BUILTINS and t.lower() not in (
            "service", "class", "method", "function", "impl", "type", "test", "helper",
            "manage", "manager", "data", "inspect", "search", "find", "check", "call", "show",
            "trace", "audit", "across", "usage", "generation"
        )
    ]
    filter_terms = code_identifiers if code_identifiers else significant_terms
    if filter_terms:
        grounded_candidates = []
        for c in candidates:
            c_text = f"{c.get('node_id', '')} {c.get('label', '')} {c.get('source_path', '')}".lower()
            if any(term in c_text for term in filter_terms):
                grounded_candidates.append(c)
        candidates = grounded_candidates

    if not candidates:
        did_you_mean = suggest_close_symbols(significant_terms or raw_terms, graph=graph, db_path=db_path)
        did_you_mean_msg = ""
        if did_you_mean:
            did_you_mean_msg = f"\n> Did you mean: {', '.join(f'`{s}`' for s in did_you_mean)}?"

        return {
            "task": task,
            "subsystems": [],
            "symbols": [],
            "did_you_mean": did_you_mean,
            "risk_tier": "LOW",
            "risk_score": 0.0,
            "downstream_impact_count": 0,
            "affected_tests": [],
            "historically_co_changed": [],
            "architecture_diagram": "",
            "raw_tokens": 0,
            "briefing_tokens": 0,
            "compression_ratio": 1.0,
            "prompt_context": (
                f"### graph_fy Pre-Agent Architectural Briefing\n"
                f"**Task**: {task}\n\n"
                f"> [!WARNING]\n"
                f"> Target Symbol Not Found: No symbols matching '{task}' exist in the knowledge graph.{did_you_mean_msg}\n"
                f"> Grounded AST Invariant: Do NOT hallucinate non-existent APIs, classes, methods, or imports. Verify the exact identifier."
            ),
        }

    # 3. Subsystem / Community Detection
    subsystems_map: Counter[str] = Counter()
    candidate_node_ids = [c["node_id"] for c in candidates if "node_id" in c]
    candidate_files: set[str] = set()

    for c in candidates:
        cf = c.get("source_path") or c.get("source_file")
        if cf:
            candidate_files.add(Path(cf).as_posix())

    if graph is not None:
        for nid in candidate_node_ids:
            if nid in graph:
                ndata = graph.nodes[nid]
                comm = ndata.get("community_name") or ndata.get("community")
                if comm is not None:
                    comm_str = f"Community {comm}" if isinstance(comm, (int, str)) and not str(comm).startswith("Community") else str(comm)
                    subsystems_map[comm_str] += 1
                f = ndata.get("source_file") or ndata.get("source_path")
                if f:
                    candidate_files.add(Path(f).as_posix())

    subsystems = [s for s, _ in subsystems_map.most_common(3)]
    subsystem_str = ", ".join(subsystems) if subsystems else "General Subsystem"

    # 4. Downstream Impact & Blast Radius
    risk_tier = "LOW"
    risk_score = 0.0
    downstream_impact_count = 0
    affected_tests: list[str] = []
    seen_tests: set[str] = set()

    if graph is not None:
        try:
            from graph_fy.impact import compute_impact
            for nid in candidate_node_ids[:3]:
                if nid in graph:
                    imp = compute_impact(graph, nid, max_depth=3, root=root_path)
                    r_score = float(imp.get("risk_score", 0.0))
                    if r_score > risk_score:
                        risk_score = r_score
                        risk_tier = str(imp.get("risk_tier", "LOW"))
                    downstream_impact_count += int(imp.get("downstream_count", 0))
                    for at in imp.get("affected_tests", []):
                        t_lbl = str(at.get("label", at.get("node", at))) if isinstance(at, dict) else str(at)
                        t_node = str(at.get("node", t_lbl)) if isinstance(at, dict) else str(at)
                        if t_node not in seen_tests:
                            seen_tests.add(t_node)
                            affected_tests.append(t_lbl)
        except Exception:
            pass

    # Dynamic Language Blindspot Detection
    dynamic_risks: set[str] = set()
    for cf in candidate_files:
        p = Path(cf) if Path(cf).is_absolute() else (root_path / cf)
        if p.is_file():
            try:
                code_text = p.read_text(encoding="utf-8", errors="ignore")
                if "getattr(" in code_text or "hasattr(" in code_text or "setattr(" in code_text:
                    dynamic_risks.add("reflection/getattr")
                if "importlib" in code_text or "__import__" in code_text:
                    dynamic_risks.add("dynamic-import")
                if "Depends(" in code_text:
                    dynamic_risks.add("fastapi-di")
                if "@pytest.fixture" in code_text:
                    dynamic_risks.add("pytest-fixture")
                if "**kwargs" in code_text:
                    dynamic_risks.add("kwargs-forwarding")
            except OSError:
                pass

    confidence_tier = (
        f"STATIC_VERIFIED with DYNAMIC_RISK ({', '.join(sorted(dynamic_risks))})"
        if dynamic_risks
        else "HIGH_CONFIDENCE (Static AST Verified)"
    )

    # 5. Historical Co-Change Mining & Hotspot Tracking (Git)
    co_changed_files: list[tuple[str, float]] = []
    hotspot_files: list[dict[str, Any]] = []

    target_set = {Path(f).as_posix() for f in candidate_files}

    # First check graph nodes for precomputed co_changes_with edges & churn annotations
    if graph is not None and target_set:
        seen_co: set[str] = set()
        for nid, data in graph.nodes(data=True):
            sf = Path(str(data.get("source_file", ""))).as_posix()
            if sf in target_set:
                c_count = data.get("commit_count")
                c_churn = data.get("churn_30d")
                if c_count or c_churn:
                    hotspot_files.append({
                        "file": sf,
                        "commit_count": c_count or 0,
                        "churn_30d": c_churn or 0,
                        "primary_authors": data.get("primary_authors", []),
                    })
                # Check neighbors for co_changes_with
                for nbr in graph.neighbors(nid):
                    edata = graph.get_edge_data(nid, nbr) or {}
                    ed_list = edata.values() if isinstance(edata, dict) and any(isinstance(k, int) for k in edata) else [edata]
                    for ed in ed_list:
                        if ed.get("relation") == "co_changes_with":
                            nbr_sf = Path(str(graph.nodes[nbr].get("source_file", ""))).as_posix()
                            if nbr_sf and nbr_sf not in target_set and nbr_sf not in seen_co:
                                seen_co.add(nbr_sf)
                                weight = float(ed.get("weight") or 0.5)
                                co_changed_files.append((nbr_sf, weight))

    # Fallback to local git repository log mining if graph had no precomputed co-change edges
    if not co_changed_files and target_set and root_path.is_dir():
        try:
            from graph_fy.git_history import extract_git_history, compute_cochanges, compute_file_churn
            git_commits = extract_git_history(root_path, max_commits=200)
            if git_commits:
                co_pairs = compute_cochanges(git_commits, min_co_commits=2, min_confidence=0.3)
                seen_co = set()
                for pair in co_pairs:
                    if pair.file_a in target_set and pair.file_b not in target_set and pair.file_b not in seen_co:
                        seen_co.add(pair.file_b)
                        co_changed_files.append((pair.file_b, pair.jaccard))
                    elif pair.file_b in target_set and pair.file_a not in target_set and pair.file_a not in seen_co:
                        seen_co.add(pair.file_a)
                        co_changed_files.append((pair.file_a, pair.jaccard))
                if not hotspot_files:
                    churns = compute_file_churn(git_commits)
                    for tf in target_set:
                        if tf in churns:
                            ch = churns[tf]
                            hotspot_files.append({
                                "file": tf,
                                "commit_count": ch.commit_count,
                                "churn_30d": ch.churn_30d,
                                "primary_authors": ch.primary_authors,
                            })
        except Exception:
            pass

    # 6. Architectural Flow Diagram (Mermaid)
    diagram_lines: list[str] = ["flowchart TD"]
    edge_tuples: list[tuple[str, str, str]] = []
    diagram_nodes: dict[str, str] = {}

    for c in candidates[:6]:
        nid = str(c.get("node_id", ""))
        lbl = str(c.get("label") or nid)
        node_key = re.sub(r"[^A-Za-z0-9_]", "_", lbl).strip("_")
        if not node_key:
            node_key = re.sub(r"[^A-Za-z0-9_]", "_", nid).strip("_") or f"node_{abs(hash(nid)) % 10000}"
        safe_lbl = lbl.replace('"', "'")
        diagram_nodes[node_key] = safe_lbl

        callers = c.get("callers", [])
        for clr in callers[:3]:
            clr_key = re.sub(r"[^A-Za-z0-9_]", "_", clr).strip("_") or f"node_{abs(hash(clr)) % 10000}"
            diagram_nodes[clr_key] = clr.replace('"', "'")
            edge_tuples.append((clr_key, node_key, "calls"))

        callees = c.get("callees", [])
        for clee in callees[:3]:
            clee_key = re.sub(r"[^A-Za-z0-9_]", "_", clee).strip("_") or f"node_{abs(hash(clee)) % 10000}"
            diagram_nodes[clee_key] = clee.replace('"', "'")
            edge_tuples.append((node_key, clee_key, "calls"))

    seen_edges: set[tuple[str, str]] = set()
    selected_edges: list[tuple[str, str]] = []
    for u, v, rel in edge_tuples:
        if u != v and (u, v) not in seen_edges and u in diagram_nodes and v in diagram_nodes:
            seen_edges.add((u, v))
            selected_edges.append((u, v))
            if len(selected_edges) >= 16:
                break

    used_nodes: set[str] = {u for u, v in selected_edges} | {v for u, v in selected_edges}
    for snid in sorted(used_nodes):
        if snid in diagram_nodes:
            diagram_lines.append(f'    {snid}["{diagram_nodes[snid]}"]')

    for u, v in selected_edges:
        diagram_lines.append(f"    {u} --> {v}")

    mermaid_diagram = "\n".join(diagram_lines) if len(selected_edges) > 0 else ""

    # 7. Token Economy Estimation
    total_raw_chars = 0
    for cf in candidate_files:
        p = Path(cf) if Path(cf).is_absolute() else (root_path / cf)
        if p.is_file():
            try:
                total_raw_chars += len(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    raw_tokens = max(1, total_raw_chars // 4)

    # Detect explicit code-like targets mentioned in prompt that are absent from verified AST
    missing_explicit_symbols: list[str] = []
    explicit_targets = set()
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(", task):
        explicit_targets.add(m.group(1))
    for m in re.finditer(r"\b([A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b", task):
        explicit_targets.add(m.group(1))
    for m in re.finditer(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)+([A-Za-z_][A-Za-z0-9_]{2,})\b", task):
        explicit_targets.add(m.group(1))

    stop_targets = {"get", "set", "def", "class", "True", "False", "None", "self", "args", "kwargs", "test", "Flask", "SQLAlchemy", "Alembic", "Click", "Jinja2", "Python", "HTTP", "REST", "API", "JSON", "CORS"}
    explicit_targets = {t for t in explicit_targets if t not in stop_targets and len(t) > 3}

    if explicit_targets:
        cand_text = " ".join(f"{c.get('node_id', '')} {c.get('label', '')}".lower() for c in candidates)
        for target in explicit_targets:
            t_low = target.lower()
            if t_low not in cand_text:
                if graph is None or target not in graph:
                    missing_explicit_symbols.append(target)

    # 8. Assemble Prompt Context with Deterministic Prefix for Prompt-Cache Priming
    lines: list[str] = [
        "<!-- graph_fy:agent_context:v1 -->",
        "### graph_fy Pre-Agent Architectural Briefing",
        "> [!IMPORTANT]",
        "> Grounded AST Invariant: All symbols, signatures, and dependencies below are extracted directly from the verified AST. Do NOT hallucinate non-existent methods, APIs, or imports. If a symbol or parameter is not present here, inspect the indicated source file before making assumptions.",
        f"**Target Subsystem**: {subsystem_str}",
        f"**AST Analysis Confidence**: {confidence_tier}",
    ]

    char_budget = token_budget * 4
    current_chars = sum(len(l) + 1 for l in lines)

    if include_diagram and mermaid_diagram:
        diag_block = f"\n#### Subsystem Architecture Diagram:\n```mermaid\n{mermaid_diagram}\n```\n"
        if current_chars + len(diag_block) <= char_budget:
            lines.append(diag_block)
            current_chars += len(diag_block)

    lines.append("\n#### Targeted Symbol Skeletons (Tier 1 AST):")
    for c in candidates:
        if current_chars >= char_budget:
            break
        nid = c.get("node_id", "")
        lbl = c.get("label", "")
        spath = c.get("source_path", "")
        sloc = c.get("source_location", "")
        skel = c.get("skeleton", "")
        callers = c.get("callers", [])
        callees = c.get("callees", [])

        meta_parts = [f"// Symbol: {lbl} ({spath}:{sloc})"]
        if nid:
            meta_parts.append(f'// [On-Demand Body: get_symbol_implementation("{nid}")]')
        if callers:
            c_str = ", ".join(callers[:5]) + (f" (+{len(callers)-5} more)" if len(callers) > 5 else "")
            meta_parts.append(f"// Callers: {c_str}")
        if callees:
            meta_parts.append(f"// Callees: {', '.join(callees[:5])}")

        meta_hdr = "\n".join(meta_parts)
        if skel:
            if str(spath).lower().endswith((".md", ".txt", ".rst", ".doc", ".pdf")):
                dlines = [dl for dl in skel.splitlines() if dl.strip()]
                skel_preview = "\n".join(dlines[:3]) + ("\n..." if len(dlines) > 3 else "")
                block = f"{meta_hdr}\n{skel_preview}\n"
            else:
                block = f"{meta_hdr}\n{skel}\n"
            if current_chars + len(block) <= char_budget:
                lines.append(block)
                current_chars += len(block)
            else:
                sig = compact_signature(skel)
                block = f"{meta_hdr}\n{sig}\n"
                lines.append(block)
                current_chars += len(block)
                break
        elif spath and sloc:
            block = f"{meta_hdr}\n"
            lines.append(block)
            current_chars += len(block)

    # Query Impact & Verification Targets (Dynamic Tail)
    lines.extend([
        "\n#### Query Impact & Verification Targets:",
        f"**Task**: {task}",
        f"**Blast Radius Risk**: {risk_tier} (Risk Score: {risk_score:.2f}, {downstream_impact_count} downstream dependents)",
    ])
    if affected_tests:
        lines.append(f"**Verification Targets (Tests)**: {', '.join(sorted(affected_tests)[:6])}")
    if co_changed_files:
        co_str = ", ".join(f"`{f}` ({int(freq*100)}% co-change)" for f, freq in co_changed_files)
        lines.append(f"**Historically Co-Changed Files**: {co_str}")
    if hotspot_files:
        hot_str = ", ".join(
            f"`{h['file']}` ({h['commit_count']} commits, {h['churn_30d']} lines churn/30d"
            + (f", author: {h['primary_authors'][0]}" if h.get("primary_authors") else "")
            + ")"
            for h in hotspot_files[:3]
        )
        lines.append(f"**High-Churn Hotspots**: {hot_str}")

    if missing_explicit_symbols:
        lines.extend([
            "> [!WARNING]",
            f"> Target Symbol Not Found: The following symbol(s) mentioned in the task do not exist in the verified AST: {', '.join(sorted(missing_explicit_symbols))}.",
            "> Grounded AST Invariant: Do NOT hallucinate implementations or APIs for these non-existent symbols. Verify source code before proceeding.",
        ])

    prompt_context = "\n".join(lines)
    briefing_tokens = max(1, len(prompt_context) // 4)
    compression_ratio = round(raw_tokens / max(1, briefing_tokens), 1)

    return {
        "task": task,
        "subsystems": subsystems,
        "symbols": candidates,
        "did_you_mean": [],
        "risk_tier": risk_tier,
        "risk_score": round(risk_score, 2),
        "downstream_impact_count": downstream_impact_count,
        "affected_tests": affected_tests,
        "historically_co_changed": co_changed_files,
        "hotspots": hotspot_files,
        "architecture_diagram": mermaid_diagram,
        "raw_tokens": raw_tokens,
        "briefing_tokens": briefing_tokens,
        "compression_ratio": compression_ratio,
        "prompt_context": prompt_context,
    }
