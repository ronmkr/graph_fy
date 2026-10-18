"""Handler for 'graph_fy arch-entities'."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_arch_entities(argv: list[str], default_graph_path_fn) -> int:
    """Execute whole-entity architectural synthesis."""
    from graph_fy.multi_repo import resolve_multi_graphs, synthesize_entity_architecture

    args = argv[2:]
    if not args:
        print("Usage: graph_fy arch-entities <Entity1> <Entity2> ... [--graph <path>] [--format mermaid|json|markdown] [--hops N]", file=sys.stderr)
        return 1

    entities: list[str] = []
    graph_paths: list[str] = []
    fmt = "markdown"
    hops = 1

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
        elif args[i] in ("--format", "-f") and i + 1 < len(args):
            fmt = args[i + 1].lower()
            i += 2
        elif args[i].startswith("--format="):
            fmt = args[i].split("=", 1)[1].lower()
            i += 1
        elif args[i] == "--hops" and i + 1 < len(args):
            try:
                hops = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif not args[i].startswith("-"):
            for ent in args[i].split(","):
                if ent.strip():
                    entities.append(ent.strip())
            i += 1
        else:
            i += 1

    if not entities:
        print("error: at least one entity name required", file=sys.stderr)
        return 1

    if not graph_paths:
        graph_paths = [str(default_graph_path_fn())]
    G = resolve_multi_graphs(graph_paths)
    arch = synthesize_entity_architecture(entities, G, max_hops=hops)

    if fmt == "json":
        print(json.dumps(arch, indent=2))
    elif fmt == "mermaid":
        print(arch["mermaid"])
    else:
        edges_cnt = arch.get("subgraph_edges_count", arch.get("edges_count", 0))
        print(f"### Entity Architecture Synthesis ({len(arch['entities'])} entities matched, {edges_cnt} relationships)\n")
        print("#### Matched Entities:")
        print("| Entity | Symbol Label | Source File | Location | Degree | Repo |")
        print("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for ent in arch["entities"]:
            print(f"| {ent['entity']} | `{ent['label']}` | `{ent['source_file']}` | {ent['source_location']} | {ent['degree']} | {ent['repo']} |")
        print("\n#### Whole-Entity Architectural Flow:")
        print("```mermaid")
        print(arch["mermaid"])
        print("```\n")
        if arch["matrix"]:
            print("#### Entity Interaction Matrix:")
            for src_ent, tgt_map in arch["matrix"].items():
                for tgt_ent, rels in tgt_map.items():
                    print(f"- **{src_ent}** -> **{tgt_ent}**: {', '.join(rels)}")
    return 0
