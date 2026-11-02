"""graph_fy prime — Deterministic Codebase Priming & Cold-Start Accelerator (#103).

Generates a hyper-dense (~150-250 tokens), prompt-cache-friendly architectural header
summarizing bounded contexts, key entry points, and high-risk god nodes.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import networkx as nx

from graph_fy.affected import load_graph
from graph_fy.analyze import god_nodes, _is_file_node
from graph_fy.paths import default_graph_json

PRIME_START = "<!-- GRAPH_FY_PRIME_START -->"
PRIME_END = "<!-- GRAPH_FY_PRIME_END -->"


def generate_prime_header(graph: nx.Graph, root: Path | None = None) -> str:
    """Generate deterministic, ~150-250 token architectural briefing for AI agents.

    Synthesizes three critical structural signals from graph.json without LLM calls:
    1. Bounded Contexts: Top 3 community clusters by non-file node count.
    2. Key Entry Points: Bootstrap callables and dispatchers from primary source modules.
    3. High-Risk Hubs: Top god nodes by degree centrality (highest regression risk).
    """
    has_code_nodes = any(
        d.get("file_type") == "code" or not d.get("file_type") or d.get("category") == "code"
        for _, d in graph.nodes(data=True)
        if not _is_file_node(graph, _)
    )

    def _is_doc_or_noise(nid: str, ndata: dict) -> bool:
        if _is_file_node(graph, nid):
            return True
        if has_code_nodes:
            if ndata.get("file_type") in ("document", "paper", "image"):
                return True
            if ndata.get("node_kind") in ("heading", "section"):
                return True
        return False

    # 1. Bounded contexts: Louvain partition clusters ranked by functional symbol density
    # Exclude raw file/directory nodes and documentation headings when code nodes are present.
    communities: dict[int, list[str]] = {}
    for n, d in graph.nodes(data=True):
        if _is_doc_or_noise(n, d):
            continue
        c = d.get("community")
        if c is not None:
            try:
                cid = int(c)
                communities.setdefault(cid, []).append(n)
            except (ValueError, TypeError):
                pass

    top_cids = sorted(communities.keys(), key=lambda c: len(communities[c]), reverse=True)[:3]
    context_lines: list[str] = []
    for cid in top_cids:
        c_nodes = communities[cid]
        # Anchor context identity using top 2 highest-degree members rather than verbose listings
        c_sorted = sorted(c_nodes, key=lambda n: graph.degree(n), reverse=True)
        hubs = [f"`{graph.nodes[n].get('label', n)}`" for n in c_sorted[:2]]
        context_lines.append(f"  - Context {cid} ({len(c_nodes)} nodes): {', '.join(hubs)}")

    # 2. Key entry points: Heuristic scan for bootstrap routines across primary filenames.
    # Exclude test files, noisy markdown fragments, and excessively long labels.
    entry_candidates: list[str] = []
    for n, d in graph.nodes(data=True):
        src = str(d.get("source_file") or "").lower()
        lbl = str(d.get("label") or "").strip()
        if len(lbl) > 50 or lbl.startswith("#") or "\n" in lbl or "test" in src or "test" in lbl.lower():
            continue
        if any(e in src for e in ("__main__", "cli.py", "main.go", "main.rs", "index.ts", "app.py", "server.py")):
            if any(k in lbl.lower() for k in ("main", "cli", "dispatch", "run", "serve", "app", "cmd_")):
                entry_candidates.append(lbl)
    entry_points = sorted(list(dict.fromkeys(entry_candidates)))[:4]

    # 3. High-risk hubs: Top degree entities (god nodes) that propagate transitive blast radius.
    # When code nodes exist, prioritize code abstractions over documentation headings.
    all_gods = god_nodes(graph, top_n=10)
    filtered_gods = [
        g for g in all_gods
        if not _is_doc_or_noise(g["id"], graph.nodes.get(g["id"], {}))
    ] if has_code_nodes else all_gods
    effective_gods = filtered_gods if filtered_gods else all_gods
    hub_strs = [
        f"`{g['label']}` ({g['degree']} edges)"
        for g in effective_gods[:3]
    ]

    lines = [
        PRIME_START,
        "### graph_fy Prime Briefing (Zero-LLM Fast Path)",
        "- **Bounded Contexts**:",
    ]
    if context_lines:
        lines.extend(context_lines)
    else:
        lines.append("  - Monolithic context (single cluster)")

    if entry_points:
        lines.append(f"- **Key Entry Points**: {', '.join(f'`{e}`' for e in entry_points)}")

    if hub_strs:
        lines.append(f"- **High-Risk Hubs**: {', '.join(hub_strs)}")

    lines.extend([
        "- **Protocol**: Run `graph_fy context \"<task>\"` for AST skeletons & blast radius before edits.",
        PRIME_END,
    ])
    return "\n".join(lines)


def inject_prime_header(target_path: Path, header: str) -> None:
    """Idempotently inject or replace prime header in destination markdown/rules file.

    Guarantees:
    - If markers exist: replaces bounded content in-place without altering surrounding text.
    - If markers absent: prepends header cleanly with trailing newline spacing.
    - Creates parent directories automatically if absent.
    """
    if target_path.exists():
        content = target_path.read_text(encoding="utf-8")
        if PRIME_START in content and PRIME_END in content:
            pattern = re.compile(rf"{re.escape(PRIME_START)}.*?{re.escape(PRIME_END)}\n*", re.DOTALL)
            new_content = pattern.sub(f"{header}\n\n", content, count=1)
        else:
            new_content = f"{header}\n\n{content}"
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        new_content = f"{header}\n"

    target_path.write_text(new_content, encoding="utf-8")


def cmd_prime(argv: list[str]) -> int:
    """CLI handler for `graph_fy prime`."""
    target_mode = "stdout"
    output_file: Path | None = None
    graph_path = Path(default_graph_json())
    root_dir = Path(".")
    as_json = False

    args = argv[2:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--target", "-t") and i + 1 < len(args):
            target_mode = args[i + 1].lower()
            i += 2
        elif arg.startswith(("--target=", "-t=")):
            target_mode = arg.split("=", 1)[1].lower()
            i += 1
        elif arg in ("--output", "-o") and i + 1 < len(args):
            output_file = Path(args[i + 1])
            i += 2
        elif arg.startswith(("--output=", "-o=")):
            output_file = Path(arg.split("=", 1)[1])
            i += 1
        elif arg in ("--graph", "-g") and i + 1 < len(args):
            graph_path = Path(args[i + 1])
            i += 2
        elif arg.startswith(("--graph=", "-g=")):
            graph_path = Path(arg.split("=", 1)[1])
            i += 1
        elif arg in ("--root", "-r", "--dir", "-d") and i + 1 < len(args):
            root_dir = Path(args[i + 1])
            i += 2
        elif arg.startswith(("--root=", "--dir=")):
            root_dir = Path(arg.split("=", 1)[1])
            i += 1
        elif arg == "--json":
            as_json = True
            i += 1
        else:
            i += 1

    if not graph_path.exists():
        # Fall back to search in root_dir
        cand = root_dir / "graph_fy_out" / "graph.json"
        if cand.exists():
            graph_path = cand
        else:
            print(f"error: graph file not found at {graph_path}. Run `graph_fy build` first.", file=sys.stderr)
            return 1

    try:
        graph = load_graph(graph_path)
    except Exception as exc:
        print(f"error: could not load graph: {exc}", file=sys.stderr)
        return 1

    header = generate_prime_header(graph, root=root_dir)
    approx_tokens = int(len(header.split()) * 1.3)

    if as_json:
        result = {
            "header": header,
            "approx_tokens": approx_tokens,
            "target": target_mode,
        }
        print(json.dumps(result, indent=2))
        return 0

    if output_file is not None:
        inject_prime_header(output_file, header)
        print(f"Injected prime header into {output_file} (~{approx_tokens} tokens)")
        return 0

    target_map = {
        "agents": "AGENTS.md",
        "claude": "CLAUDE.md",
        "gemini": "GEMINI.md",
        "cursor": ".cursorrules",
        "copilot": ".github/copilot-instructions.md",
    }

    if target_mode == "stdout":
        print(header)
    elif target_mode == "all":
        synced = []
        for name, rel_path in target_map.items():
            target_f = root_dir / rel_path
            if target_f.exists():
                inject_prime_header(target_f, header)
                synced.append(str(target_f))
        if not synced:
            # Fall back to syncing default AGENTS.md and CLAUDE.md if neither exists
            for def_name in ("AGENTS.md", "CLAUDE.md"):
                target_f = root_dir / def_name
                inject_prime_header(target_f, header)
                synced.append(str(target_f))
        print(f"Synchronized prime header with {len(synced)} file(s): {', '.join(synced)} (~{approx_tokens} tokens)")
    elif target_mode in target_map:
        target_f = root_dir / target_map[target_mode]
        inject_prime_header(target_f, header)
        print(f"Synchronized prime header with {target_f} (~{approx_tokens} tokens)")
    else:
        print(f"error: unknown target '{target_mode}'. Use stdout, agents, claude, gemini, cursor, copilot, or all.", file=sys.stderr)
        return 1

    return 0
