"""Export command implementation for graph_fy CLI."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable


def cmd_export(
    argv: list[str],
    default_out_dir: str | Path,
    find_graph_path_fn: Callable[[str], Path],
) -> int:
    """Handle `graph_fy export <format>` subcommand."""
    subcmd = argv[2] if len(argv) > 2 else ""
    if subcmd not in (
        "html",
        "callflow-html",
        "tree-html",
        "tree",
        "dashboard",
        "dashboard-html",
        "obsidian",
        "wiki",
        "svg",
        "graphml",
        "neo4j",
        "falkordb",
    ):
        print("Usage: graph_fy export <format>", file=sys.stderr)
        print("  html      [--graph PATH] [--labels PATH] [--node-limit N] [--no-viz]", file=sys.stderr)
        print(
            "  callflow-html [GRAPH|DIR] [--graph PATH] [--labels PATH] [--report PATH] [--sections PATH] [--output HTML]",
            file=sys.stderr,
        )
        print("            [--lang auto|zh-CN|en] [--max-sections N] [--diagram-scale N]", file=sys.stderr)
        print("  tree-html [--graph PATH] [--output HTML]", file=sys.stderr)
        print("  dashboard [--output HTML]", file=sys.stderr)
        print("  obsidian  [--graph PATH] [--labels PATH] [--dir PATH]", file=sys.stderr)
        print("  wiki      [--graph PATH] [--labels PATH]", file=sys.stderr)
        print("  svg       [--graph PATH] [--labels PATH]", file=sys.stderr)
        print("  graphml   [--graph PATH]", file=sys.stderr)
        print("  neo4j     [--graph PATH] [--push URI] [--user U] [--password P]", file=sys.stderr)
        print("            (or set NEO4J_PASSWORD instead of --password to keep it off argv)", file=sys.stderr)
        print("  falkordb  [--graph PATH] [--push URI] [--user U] [--password P]", file=sys.stderr)
        print("            (or set FALKORDB_PASSWORD instead of --password to keep it off argv)", file=sys.stderr)
        return 1

    # Parse shared args
    args = argv[3:]
    out_dir = Path(default_out_dir)
    if not out_dir.exists() and Path("graph_fy_out").exists():
        out_dir = Path("graph_fy_out")
    graph_path = out_dir / "graph.json"
    graph_path_explicit = False
    labels_path = out_dir / ".graph_fy_labels.json"
    labels_path_explicit = False
    report_path = out_dir / "GRAPH_REPORT.md"
    report_path_explicit = False
    sections_path: Path | None = None
    callflow_output: Path | None = None
    callflow_lang = "auto"
    callflow_max_sections = 15
    callflow_diagram_scale = 1.0
    callflow_max_diagram_nodes = 18
    callflow_max_diagram_edges = 24
    analysis_path = out_dir / ".graph_fy_analysis.json"
    node_limit = 5000
    no_viz = False
    obsidian_dir = out_dir / "obsidian"
    custom_export_dir: Path | None = None
    push_uri: str | None = None
    push_user = "neo4j"
    push_password: str | None = (
        os.environ.get("FALKORDB_PASSWORD") if subcmd == "falkordb" else os.environ.get("NEO4J_PASSWORD")
    ) or None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--graph" and i + 1 < len(args):
            graph_path = Path(args[i + 1])
            graph_path_explicit = True
            i += 2
        elif a == "--labels" and i + 1 < len(args):
            labels_path = Path(args[i + 1])
            labels_path_explicit = True
            i += 2
        elif a == "--report" and i + 1 < len(args):
            report_path = Path(args[i + 1])
            report_path_explicit = True
            i += 2
        elif a == "--sections" and i + 1 < len(args):
            sections_path = Path(args[i + 1])
            i += 2
        elif a in ("--output", "--out", "-o") and i + 1 < len(args):
            callflow_output = Path(args[i + 1]).expanduser()
            if not callflow_output.is_absolute():
                callflow_output = Path.cwd() / callflow_output
            i += 2
        elif a in ("-r", "--root") and i + 1 < len(args):
            graph_path = find_graph_path_fn(args[i + 1])
            graph_path_explicit = True
            i += 2
        elif a == "--lang" and i + 1 < len(args):
            callflow_lang = args[i + 1]
            i += 2
        elif a == "--max-sections" and i + 1 < len(args):
            callflow_max_sections = int(args[i + 1])
            i += 2
        elif a == "--diagram-scale" and i + 1 < len(args):
            callflow_diagram_scale = float(args[i + 1])
            i += 2
        elif a == "--max-diagram-nodes" and i + 1 < len(args):
            callflow_max_diagram_nodes = int(args[i + 1])
            i += 2
        elif a == "--max-diagram-edges" and i + 1 < len(args):
            callflow_max_diagram_edges = int(args[i + 1])
            i += 2
        elif a in ("-h", "--help") and subcmd == "callflow-html":
            print("Usage: graph_fy export callflow-html [GRAPH|DIR] [--graph PATH] [--labels PATH]")
            print("  --report PATH          path to GRAPH_REPORT.md")
            print("  --sections PATH        JSON section definitions")
            print("  --output HTML          output path (default graph_fy_out/<project>-callflow.html)")
            print("  --lang LANG            auto, zh-CN, en, etc. (default auto)")
            print("  --max-sections N       maximum auto-derived sections (default 15)")
            print("  --diagram-scale N      Mermaid diagram scale (default 1.0)")
            print("  --max-diagram-nodes N  representative nodes per section (default 18)")
            print("  --max-diagram-edges N  representative edges per section (default 24)")
            return 0
        elif a == "--node-limit" and i + 1 < len(args):
            node_limit = int(args[i + 1])
            i += 2
        elif a == "--no-viz":
            no_viz = True
            i += 1
        elif a == "--dir" and i + 1 < len(args):
            custom_export_dir = Path(args[i + 1])
            obsidian_dir = custom_export_dir
            i += 2
        elif a == "--push" and i + 1 < len(args):
            push_uri = args[i + 1]
            i += 2
        elif a == "--user" and i + 1 < len(args):
            push_user = args[i + 1]
            i += 2
        elif a == "--password" and i + 1 < len(args):
            push_password = args[i + 1]
            i += 2
        elif not a.startswith("-") and not graph_path_explicit:
            graph_path = find_graph_path_fn(a)
            graph_path_explicit = True
            i += 1
        else:
            i += 1

    graph_path = graph_path.expanduser()
    if graph_path_explicit:
        graph_out_dir = graph_path.parent
        if not labels_path_explicit:
            labels_path = graph_out_dir / ".graph_fy_labels.json"
        if not report_path_explicit:
            report_path = graph_out_dir / "GRAPH_REPORT.md"
    labels_path = labels_path.expanduser()
    report_path = report_path.expanduser()

    if not graph_path.exists():
        print(f"error: graph not found: {graph_path}. Run /graph_fy <path> first.", file=sys.stderr)
        return 1

    if subcmd == "callflow-html":
        from graph_fy.callflow_html import write_callflow_html as _write_callflow_html

        out = _write_callflow_html(
            graph=graph_path,
            report=report_path,
            labels=labels_path,
            sections=sections_path,
            output=callflow_output,
            lang=callflow_lang,
            max_sections=callflow_max_sections,
            diagram_scale=callflow_diagram_scale,
            max_diagram_nodes=callflow_max_diagram_nodes,
            max_diagram_edges=callflow_max_diagram_edges,
            verbose=True,
        )
        print(f"callflow HTML written - open in any browser: {out}")
        return 0

    from networkx.readwrite import json_graph as _jg
    from graph_fy.security import check_graph_file_size_cap as _check_cap

    _over_cap = False
    try:
        _check_cap(graph_path)
    except ValueError as _cap_err:
        if subcmd == "html":
            _over_cap = True
            try:
                _over_cap_bytes = graph_path.stat().st_size
            except OSError:
                _over_cap_bytes = -1
            print(
                f"warning: graph.json exceeds cap ({_over_cap_bytes} bytes); "
                f"falling back to community-aggregation view (node_limit=5000)",
                file=sys.stderr,
            )
        else:
            print(f"error: {_cap_err}", file=sys.stderr)
            return 1
    _raw = json.loads(graph_path.read_text(encoding="utf-8"))
    if "links" not in _raw and "edges" in _raw:
        _raw = dict(_raw, links=_raw["edges"])
    try:
        G = _jg.node_link_graph(_raw, edges="links")
    except TypeError:
        G = _jg.node_link_graph(_raw)
    if isinstance(_raw.get("hyperedges"), list):
        G.graph["hyperedges"] = _raw["hyperedges"]

    # Load optional analysis/labels
    communities: dict[int, list[str]] = {}
    if analysis_path.exists():
        _an = json.loads(analysis_path.read_text(encoding="utf-8"))
        communities = {int(k): v for k, v in _an.get("communities", {}).items()}
        cohesion: dict[int, float] = {int(k): v for k, v in _an.get("cohesion", {}).items()}
        gods_data = _an.get("gods", [])
    else:
        cohesion = {}
        gods_data = []

    if not communities:
        reconstructed: dict[int, list[str]] = {}
        for node_id, data in G.nodes(data=True):
            cid_raw = data.get("community")
            if cid_raw is None:
                continue
            try:
                cid = int(cid_raw)
            except (TypeError, ValueError):
                continue
            reconstructed.setdefault(cid, []).append(str(node_id))
        if reconstructed:
            communities = reconstructed

    labels: dict[int, str] = {}
    if labels_path.exists():
        try:
            raw_labels = json.loads(labels_path.read_text(encoding="utf-8"))
            if isinstance(raw_labels, dict):
                source_dict = raw_labels.get("community_labels") or raw_labels.get("labels") or raw_labels
                for k, v in source_dict.items():
                    try:
                        labels[int(k)] = str(v)
                    except (ValueError, TypeError):
                        continue
        except Exception:
            labels = {}

    out_dir = graph_path.parent

    if subcmd == "html":
        from graph_fy.export import to_html as _to_html

        if no_viz:
            html_target = out_dir / "graph.html"
            if html_target.exists():
                html_target.unlink()
            print("--no-viz: skipped graph.html")
        else:
            _effective_node_limit = 5000 if _over_cap else node_limit
            _to_html(
                G,
                communities,
                str(out_dir / "graph.html"),
                community_labels=labels or None,
                node_limit=_effective_node_limit,
            )
            if G.number_of_nodes() <= _effective_node_limit:
                print("graph.html written - open in any browser, no server needed")
            if _over_cap:
                return 0

    elif subcmd == "obsidian":
        from graph_fy.export import to_obsidian as _to_obsidian, to_canvas as _to_canvas

        n = _to_obsidian(
            G,
            communities,
            str(obsidian_dir),
            community_labels=labels or None,
            cohesion=cohesion or None,
        )
        print(f"Obsidian vault: {n} notes in {obsidian_dir}/")
        _to_canvas(
            G,
            communities,
            str(obsidian_dir / "graph.canvas"),
            community_labels=labels or None,
        )
        print(f"Canvas: {obsidian_dir}/graph.canvas")
        print(f"Open {obsidian_dir}/ as a vault in Obsidian.")

    elif subcmd == "wiki":
        from graph_fy.wiki import to_wiki as _to_wiki
        from graph_fy.analyze import god_nodes as _god_nodes

        if not communities:
            print(
                "error: .graph_fy_analysis.json is missing or empty — refusing to export wiki to prevent data loss.\n"
                "Run `graph_fy extract .` (or `graph_fy cluster-only .`) to regenerate community data first.",
                file=sys.stderr,
            )
            return 1
        wiki_dir = custom_export_dir if custom_export_dir is not None else (out_dir / "wiki")
        if not gods_data:
            gods_data = _god_nodes(G)
        n = _to_wiki(
            G,
            communities,
            str(wiki_dir),
            community_labels=labels or None,
            cohesion=cohesion or None,
            god_nodes_data=gods_data,
        )
        print(f"Wiki: {n} articles written to {wiki_dir}/")
        print(f"  {wiki_dir}/index.md  ->  agent entry point")

    elif subcmd == "svg":
        from graph_fy.export import to_svg as _to_svg

        _to_svg(G, communities, str(out_dir / "graph.svg"), community_labels=labels or None)
        print("graph.svg written - embeds in Obsidian, Notion, GitHub READMEs")

    elif subcmd == "graphml":
        from graph_fy.export import to_graphml as _to_graphml

        _to_graphml(G, communities, str(out_dir / "graph.graphml"))
        print("graph.graphml written - open in Gephi, yEd, or any GraphML tool")

    elif subcmd == "neo4j":
        if push_uri:
            from graph_fy.export import push_to_neo4j as _push

            if push_password is None:
                print("error: --password required for --push", file=sys.stderr)
                return 1
            result = _push(
                G,
                uri=push_uri,
                user=push_user,
                password=push_password,
                communities=communities,
            )
            print(f"Pushed to Neo4j: {result['nodes']} nodes, {result['edges']} edges")
        else:
            from graph_fy.export import to_cypher as _to_cypher

            _to_cypher(G, str(out_dir / "cypher.txt"))
            print(f"cypher.txt written - import with: cypher-shell < {out_dir}/cypher.txt")

    elif subcmd == "falkordb":
        if push_uri:
            from graph_fy.export import push_to_falkordb as _push

            result = _push(
                G,
                uri=push_uri,
                user=push_user,
                password=push_password,
                communities=communities,
            )
            print(f"Pushed to FalkorDB: {result['nodes']} nodes, {result['edges']} edges")
        else:
            from graph_fy.export import to_cypher as _to_cypher

            _to_cypher(G, str(out_dir / "cypher.txt"))
            print(
                f"cypher.txt written ({out_dir}/cypher.txt) - statements are OpenCypher. "
                f"FalkorDB's GRAPH.QUERY runs one statement at a time (no bulk script "
                f"import), so load a graph with: graph_fy export falkordb --push "
                f"falkordb://localhost:6379"
            )

    elif subcmd in ("tree-html", "tree"):
        from graph_fy.tree_html import write_tree_html, DEFAULT_MAX_CHILDREN

        out_file = callflow_output or (graph_path.parent / "GRAPH_TREE.html")
        write_tree_html(
            graph_path=graph_path,
            output_path=out_file,
            max_children=DEFAULT_MAX_CHILDREN,
        )
        print(f"wrote {out_file}")
        return 0

    elif subcmd in ("dashboard", "dashboard-html"):
        from graph_fy.exporters.dashboard import export_dashboard

        out_file = callflow_output or (graph_path.parent / "dashboard.html")
        export_dashboard(out_file=out_file, graph_path=graph_path)
        print(f"wrote {out_file}")
        return 0

    return 0
