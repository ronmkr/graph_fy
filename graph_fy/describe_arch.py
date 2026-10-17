"""graph_fy describe arch — Rich, interactive architecture & stage-by-stage AST flow view.

Generates a self-contained, presentation-ready HTML application depicting
system architecture organized by operational lifecycle stages (e.g.,
Initiation -> Validation & Risk -> Core Processing -> Gateways -> Settlement -> Events),
with individual execution steps and deep AST symbol skeletons (signatures, docstrings,
callers, callees, and data sinks). 100% Mermaid-free: native responsive HTML5, CSS3,
and interactive vector SVG.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
import html as _html
import json
from pathlib import Path
import re
import sys
from typing import Any

from graph_fy.paths import graph_fy_OUT, graph_fy_OUT_NAME


# ─────────────────────────────────────────────────────────────────────────────
# 1. Stage Archetypes & Category Classifiers
# ─────────────────────────────────────────────────────────────────────────────

STAGE_ARCHETYPES = [
    {
        "id": "initiation",
        "default_name": "Stage 1: Intake & Ingress",
        "icon": "⚡",
        "color": "#38bdf8",  # sky
        "bg_color": "rgba(56, 189, 248, 0.12)",
        "border_color": "rgba(56, 189, 248, 0.35)",
        "description": "System entry points, API route intake, command dispatchers, CLI hooks, and incoming payload ingestion.",
        "keywords": {"initiat", "entry", "intake", "route", "api", "endpoint", "dispatch", "cli", "listen", "webhook", "main", "hook"},
        "node_roles": {"route", "cli", "consumer", "rpc"},
    },
    {
        "id": "validation",
        "default_name": "Stage 2: Validation, Auth & Guards",
        "icon": "🛡️",
        "color": "#fbbf24",  # amber
        "bg_color": "rgba(251, 191, 36, 0.12)",
        "border_color": "rgba(251, 191, 36, 0.35)",
        "description": "Authentication guards, permission checks, payload schema validation, precondition filters, and policy evaluation.",
        "keywords": {"validat", "auth", "guard", "schema", "verify", "permission", "limit", "idempotenc", "filter", "detect", "security", "check"},
        "node_roles": {"middleware", "controller"},
    },
    {
        "id": "processing",
        "default_name": "Stage 3: Core Domain & Processing",
        "icon": "⚙️",
        "color": "#34d399",  # emerald
        "bg_color": "rgba(52, 211, 153, 0.12)",
        "border_color": "rgba(52, 211, 153, 0.35)",
        "description": "Core business domain logic, workflow orchestration, state machine transitions, parsing, and transformation engines.",
        "keywords": {"process", "orchestrat", "engine", "service", "workflow", "state", "intent", "calc", "pipeline", "extract", "parse", "transform", "compile", "execute"},
        "node_roles": {"service"},
    },
    {
        "id": "gateway",
        "default_name": "Stage 4: Integrations & Remote Clients",
        "icon": "🌐",
        "color": "#c084fc",  # purple
        "bg_color": "rgba(192, 132, 252, 0.12)",
        "border_color": "rgba(192, 132, 252, 0.35)",
        "description": "Third-party APIs, remote services, external microservice clients, network protocols, and integration adapters.",
        "keywords": {"gateway", "client", "adapter", "external", "remote", "network", "protocol", "http", "fetch", "rpc", "mcp"},
        "node_roles": {"external_api"},
    },
    {
        "id": "settlement",
        "default_name": "Stage 5: Persistence & Storage",
        "icon": "💾",
        "color": "#f472b6",  # pink/rose
        "bg_color": "rgba(244, 114, 182, 0.12)",
        "border_color": "rgba(244, 114, 182, 0.35)",
        "description": "Relational and document databases, persistent repositories, models, file storage, index stores, and data caching.",
        "keywords": {"persist", "database", "table", "sql", "repository", "model", "cache", "store", "save", "write", "storage", "db"},
        "node_roles": {"database", "cache"},
    },
    {
        "id": "events",
        "default_name": "Stage 6: Outputs, Events & Exporters",
        "icon": "📦",
        "color": "#818cf8",  # indigo
        "bg_color": "rgba(129, 140, 248, 0.12)",
        "border_color": "rgba(129, 140, 248, 0.35)",
        "description": "Asynchronous event queues, message brokers, notifications, visual reports, and data exporters.",
        "keywords": {"event", "publish", "queue", "topic", "stream", "notify", "broadcast", "export", "report", "emit", "output", "render"},
        "node_roles": {"queue"},
    },
]


def classify_node_role(node: dict[str, Any], node_id: str = "") -> tuple[str, str]:
    """Classify a node into an architectural category and display badge."""
    label = str(node.get("label") or node_id).strip()
    label_lower = label.lower()
    nid_lower = str(node_id).lower()
    node_type = str(
        node.get("node_type")
        or node.get("kind")
        or node.get("type")
        or node.get("category")
        or ""
    ).lower()
    source_file = str(node.get("source_file") or "").lower()

    # 1. Databases / SQL Tables
    if (
        node_type in {"table", "sql_table", "database", "db", "sql", "repository", "entity", "model"}
        or source_file.endswith(".sql")
        or any(w in label_lower for w in ("table", "repository", "database", "sqlite", "postgres", "mysql", "dynamodb", "mongodb", "store", "model", "schema"))
    ):
        return "database", "DB"

    # 2. Caches
    if node_type in {"cache", "redis", "memcached"} or any(w in label_lower for w in ("redis", "memcached", "cache")):
        return "cache", "Cache"

    # 3. Queues / Topics / Event Producers
    if (
        node_type in {"kafka_topic", "topic", "queue", "kafka", "rabbitmq", "sqs", "pubsub"}
        or any(w in label_lower for w in ("kafka", "topic", "queue", "rabbitmq", "sqs", "event_stream", "producer", "publish"))
    ):
        return "queue", "Queue"

    # 4. External APIs / Gateways
    if (
        node_type in {"external_api", "external_http", "external", "remote_service", "gateway"}
        or label_lower.startswith(("http://", "https://", "external:"))
        or any(w in label_lower for w in ("gateway", "webhook", "external", "remote", "client", "sdk", "api_client"))
    ):
        return "external_api", "Gateway"

    # 5. Routes / API Endpoints
    http_verbs = ("get ", "post ", "put ", "delete ", "patch ")
    if (
        node_type in {"route", "endpoint", "http_route", "api_endpoint", "api", "rest_endpoint"}
        or label_lower.startswith(http_verbs)
        or "route:" in label_lower
        or "endpoint" in label_lower
        or node.get("http_method") is not None
    ):
        return "route", "Route"

    # 6. RPC Endpoints
    if node_type in {"rpc", "grpc", "grpc_rpc", "grpc_service"} or label_lower.startswith("rpc:") or "grpc" in label_lower:
        return "rpc", "RPC"

    # 7. CLI Entry Points
    if (
        node_type in {"cli", "cli_command", "command", "entry", "entry_point"}
        or label_lower.startswith(("cli:", "cmd:"))
        or label_lower in {"main", "main()", "cli()", "run()"}
        or any(w in label_lower for w in ("command", "click.command", "dispatch_command"))
    ):
        return "cli", "CLI"

    # 8. Consumers / Event Handlers
    if (
        node_type in {"consumer", "event_consumer", "listener", "subscriber", "worker", "job"}
        or any(w in label_lower for w in ("consumer", "listener", "subscriber", "on_message", "handle_event"))
    ):
        return "consumer", "Consumer"

    # 9. Middleware / Guards / Validators
    if (
        node_type in {"middleware", "guard", "interceptor", "filter", "validator"}
        or any(w in label_lower for w in ("guard", "auth", "validat", "risk", "fraud", "schema", "verify", "permission", "idempotenc"))
    ):
        return "middleware", "Validator"

    # 10. Controllers / Handlers
    if node_type in {"controller", "handler"} or any(w in label_lower for w in ("controller", "handler")):
        return "controller", "Handler"

    # 11. Services / Processors / Engines
    if (
        node_type in {"service", "manager", "processor", "usecase", "engine", "orchestrator"}
        or any(w in label_lower for w in ("service", "manager", "processor", "usecase", "engine", "orchestrat", "intent", "pipeline"))
    ):
        return "service", "Service"

    # 12. Classes / Types
    if node.get("_callable_class") or node_type in {"class", "struct", "interface"}:
        return "class", "Class"

    # 13. General Functions
    return "function", "Function"


# ─────────────────────────────────────────────────────────────────────────────
# 2. AST Skeleton Resolution
# ─────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1024)
def _extract_ast_skeleton(
    source_file: str,
    line_number: int | None,
    label: str,
    project_root: Path | None = None,
) -> tuple[str, str, str]:
    """Extract AST signature, docstring, and code skeleton for a symbol.

    Returns:
        tuple of (signature, docstring, skeleton_code)
    """
    clean_label = label.strip()
    if clean_label.endswith("()"):
        clean_label = clean_label[:-2]

    # Default fallback signature
    def_sig = f"{clean_label}(...)"
    def_doc = ""
    def_skel = f"// Symbol: {clean_label}\n// Defined in: {source_file}:{line_number or 1}\n\n{clean_label}"

    if not source_file or not line_number or line_number <= 0:
        return def_sig, def_doc, def_skel

    cand_paths = [p for p in ((project_root / source_file if project_root else None), Path(source_file), Path.cwd() / source_file) if p]
    target_file = next((p for p in cand_paths if p.is_file()), None)

    if not target_file:
        return def_sig, def_doc, def_skel

    try:
        from graph_fy.skeleton import get_symbol_code_from_file
        code = get_symbol_code_from_file(target_file, line_number, skeletonize=True)
        if code:
            lines = [line.strip() for line in code.splitlines() if line.strip()]
            sig = lines[0] if lines else def_sig
            # Extract docstring if present
            doc_lines = []
            in_doc = False
            for line in code.splitlines():
                if '"""' in line or "'''" in line:
                    doc_lines.append(line.replace('"""', '').replace("'''", '').strip())
                    if line.count('"""') == 2 or line.count("'''") == 2:
                        break
                    in_doc = not in_doc
                elif in_doc:
                    doc_lines.append(line.strip())
            doc = " ".join(doc_lines).strip()
            return sig, doc, code
    except Exception:
        pass

    return def_sig, def_doc, def_skel


# ─────────────────────────────────────────────────────────────────────────────
# 3. Architecture Model Synthesis
# ─────────────────────────────────────────────────────────────────────────────

def _map_node_to_stage(
    node: dict[str, Any],
    role: str,
    focus: str | None = None,
) -> str:
    """Determine which architectural stage a node belongs to."""
    label_lower = str(node.get("label", "")).lower()
    source_lower = str(node.get("source_file", "")).lower()
    comm_name = str(node.get("community_name", "")).lower()


    # 1. Structural Node Roles take precedence
    if role == "queue":
        return "events"
    if role in {"database", "cache"}:
        return "settlement"
    if role == "external_api":
        return "gateway"
    if role in {"route", "cli", "rpc"}:
        return "initiation"

    if role in {"middleware", "controller"}:
        return "validation"
    if role == "service":
        return "processing"

    # 2. Match archetype keywords
    for arch in STAGE_ARCHETYPES:
        if any(k in label_lower or k in source_lower for k in arch["keywords"]):
            return arch["id"]

    return "processing"


def build_architecture_model(
    graph_data: dict[str, Any],
    labels: dict[str, str] | None = None,
    project_root: Path | None = None,
    focus: str | None = None,
    max_steps_per_stage: int = 6,
) -> dict[str, Any]:
    """Analyze AST graph and synthesize an end-to-end architecture pipeline model."""
    nodes = list(graph_data.get("nodes", []))
    links = list(graph_data.get("links", graph_data.get("edges", [])))
    labels = labels or {}

    project_name = (
        graph_data.get("metadata", {}).get("project_name")
        or graph_data.get("graph", {}).get("project_name")
        or (project_root.name if project_root else "Repository")
    )

    # Filter / spotlight if focus term is provided
    if focus:
        f_norm = focus.lower()
        matched_nids = set()
        for n in nodes:
            label_lower = str(n.get("label", "")).lower()
            src_lower = str(n.get("source_file", "")).lower()
            if f_norm in label_lower or f_norm in src_lower:
                matched_nids.add(n.get("id"))
        # Include 1-hop neighbors
        for l in links:
            s, t = str(l.get("source", "")), str(l.get("target", ""))
            if s in matched_nids or t in matched_nids:
                matched_nids.add(s)
                matched_nids.add(t)
        if matched_nids:
            nodes = [n for n in nodes if n.get("id") in matched_nids]

    # Index nodes & links
    node_map = {str(n.get("id")): n for n in nodes}
    out_edges = defaultdict(list)
    in_edges = defaultdict(list)

    for l in links:
        src = str(l.get("source", ""))
        tgt = str(l.get("target", ""))
        rel = str(l.get("relation", "calls"))
        if src in node_map and tgt in node_map:
            out_edges[src].append((tgt, rel))
            in_edges[tgt].append((src, rel))

    # Group nodes by stage
    stage_nodes: dict[str, list[dict]] = defaultdict(list)
    node_roles: dict[str, tuple[str, str]] = {}

    for n in nodes:
        nid = str(n.get("id"))
        role, tag = classify_node_role(n, nid)
        node_roles[nid] = (role, tag)
        stage_id = _map_node_to_stage(n, role, focus)
        stage_nodes[stage_id].append(n)

    # Build Stage & Step structures
    stages_output = []
    all_sinks = set()
    total_symbols = 0

    stage_order = ["initiation", "validation", "processing", "gateway", "settlement", "events"]

    for s_idx, st_id in enumerate(stage_order, 1):
        st_meta = next((item for item in STAGE_ARCHETYPES if item["id"] == st_id), STAGE_ARCHETYPES[0])
        raw_nodes = stage_nodes.get(st_id, [])

        if not raw_nodes and not focus:
            continue

        # Dynamically discover dominant community or source directory for this stage in the target repo
        comm_counts = defaultdict(int)
        dir_counts = defaultdict(int)
        for n in raw_nodes:
            cid = str(n.get("community_id", n.get("community", "")))
            if cid in labels and labels[cid]:
                comm_counts[labels[cid]] += 1
            elif n.get("community_name"):
                comm_counts[str(n["community_name"])] += 1

            src = str(n.get("source_file", ""))
            if src:
                parts = Path(src).parts
                top_dir = parts[0] if len(parts) > 1 else Path(src).stem
                dir_counts[top_dir] += 1

        top_comm = max(comm_counts, key=comm_counts.get) if comm_counts else None
        top_dir = max(dir_counts, key=dir_counts.get) if dir_counts else None
        st_subsystem = top_comm or (top_dir if top_dir else "")

        if top_comm:
            st_name = f"{st_meta['default_name']} — {top_comm}"
        elif top_dir:
            st_name = f"{st_meta['default_name']} ({top_dir})"
        else:
            st_name = st_meta["default_name"]

        # Sort nodes by connectivity / hub importance
        raw_nodes.sort(
            key=lambda n: len(out_edges[str(n.get("id"))]) + len(in_edges[str(n.get("id"))]),
            reverse=True,
        )

        # Synthesize sequential steps within this stage
        steps = []
        step_chunk_size = max(1, len(raw_nodes) // max_steps_per_stage) if raw_nodes else 1
        chunks = [raw_nodes[i:i + step_chunk_size] for i in range(0, len(raw_nodes), step_chunk_size)][:max_steps_per_stage]

        for step_idx, chunk in enumerate(chunks, 1):
            if not chunk:
                continue
            rep_node = chunk[0]
            rep_id = str(rep_node.get("id"))
            role, tag = node_roles.get(rep_id, ("function", "Function"))
            rep_label = str(rep_node.get("label") or rep_id)

            step_num = f"{s_idx}.{step_idx}"

            # Step title synthesis
            if role == "route":
                step_title = f"Intake: {rep_label}"
                desc = "Receive and parse inbound protocol request payload."
                inp = "Inbound Payload"
                out = "Parsed Request Data"
            elif role in {"middleware", "controller"}:
                step_title = f"Verify: {rep_label}"
                desc = "Validate request schema, authentication tokens, and preconditions."
                inp = "Request Context"
                out = "Authorized Context / Token"
            elif role == "service":
                step_title = f"Execute: {rep_label}"
                desc = "Orchestrate core business domain workflow and transformations."
                inp = "Domain Entities"
                out = "Execution Outcome / Intent"
            elif role == "external_api":
                step_title = f"Client: {rep_label}"
                desc = "Dispatch remote client call or communicate with external service."
                inp = "Outbound Payload"
                out = "Remote Response"
            elif role in {"database", "cache"}:
                step_title = f"Persist: {rep_label}"
                desc = "Commit state changes, write persistent records, or update cached data."
                inp = "Data Records"
                out = "Committed State"
            elif role == "queue":
                step_title = f"Publish: {rep_label}"
                desc = "Emit asynchronous broadcast event and notify downstream subscribers."
                inp = "Domain Event"
                out = "Emitted Message"
            else:
                step_title = f"Step: {rep_label}"
                desc = f"Execute {role} operation within {st_name}."
                inp = "Input Parameters"
                out = "Operation Result"

            # Resolve detailed AST symbols for this step
            step_symbols = []
            step_sinks = set()
            first_doc = ""

            for idx, node in enumerate(chunk[:4]):  # Top 4 symbols per step
                nid = str(node.get("id"))
                n_label = str(node.get("label") or nid)
                src_file = str(node.get("source_file") or "")
                src_loc = str(node.get("source_location") or "")
                line_match = re.search(r"\d+", src_loc)
                line_num = int(line_match.group()) if line_match else None

                sig, doc, skel = _extract_ast_skeleton(src_file, line_num, n_label, project_root)
                if idx == 0 and doc:
                    first_doc = doc

                # Inbound callers
                callers = []
                for caller_id, rel in in_edges[nid][:5]:
                    c_node = node_map.get(caller_id, {})
                    callers.append({
                        "id": caller_id,
                        "label": str(c_node.get("label") or caller_id),
                        "file": str(c_node.get("source_file") or ""),
                        "relation": rel,
                    })

                # Outbound callees
                callees = []
                for callee_id, rel in out_edges[nid][:5]:
                    t_node = node_map.get(callee_id, {})
                    c_role, _ = node_roles.get(callee_id, ("", ""))
                    if c_role in {"database", "cache", "queue", "external_api"}:
                        step_sinks.add(str(t_node.get("label") or callee_id))
                        all_sinks.add(str(t_node.get("label") or callee_id))
                    callees.append({
                        "id": callee_id,
                        "label": str(t_node.get("label") or callee_id),
                        "file": str(t_node.get("source_file") or ""),
                        "relation": rel,
                    })

                sym_detail = {
                    "id": nid,
                    "label": n_label,
                    "kind": node_roles.get(nid, ("function", "Function"))[1],
                    "source_file": src_file,
                    "source_location": src_loc,
                    "line_number": line_num,
                    "signature": sig,
                    "docstring": doc,
                    "skeleton": skel,
                    "callers": callers,
                    "callees": callees,
                    "sinks": list(step_sinks),
                }
                step_symbols.append(sym_detail)
                total_symbols += 1

            if first_doc:
                desc = first_doc

            steps.append({
                "id": f"{st_id}-step-{step_idx}",
                "step_number": step_num,
                "name": step_title,
                "category": role,
                "category_tag": tag,
                "description": desc,
                "input_type": inp,
                "output_type": out,
                "symbols": step_symbols,
                "sinks": list(step_sinks),
            })

        # Calculate files touched
        unique_files = {str(n.get("source_file", "")) for n in raw_nodes if n.get("source_file")}

        stages_output.append({
            "id": st_id,
            "index": s_idx,
            "name": st_name,
            "subsystem": st_subsystem,
            "category": st_id,
            "icon": st_meta["icon"],
            "color": st_meta["color"],
            "bg_color": st_meta["bg_color"],
            "border_color": st_meta["border_color"],
            "description": st_meta["description"],
            "steps": steps,
            "symbols_count": len(raw_nodes),
            "files_count": len(unique_files),
        })

    # High-level architecture summary narrative
    total_steps = sum(len(s["steps"]) for s in stages_output)
    summary_text = (
        f"Operational architecture model for <strong>{_html.escape(project_name)}</strong> decomposed into "
        f"<strong>{len(stages_output)} lifecycle stages</strong> across <strong>{total_steps} sequential execution steps</strong>. "
        f"Indexed <strong>{total_symbols} concrete AST symbols</strong> with live signatures, docstrings, and "
        f"bidirectional execution contracts connecting to <strong>{len(all_sinks)} data sinks</strong>."
    )

    return {
        "project_name": project_name,
        "focus": focus,
        "summary": summary_text,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "stats": {
            "stages_count": len(stages_output),
            "steps_count": total_steps,
            "symbols_count": total_symbols,
            "nodes_total": len(nodes),
            "edges_total": len(links),
            "sinks_count": len(all_sinks),
        },
        "stages": stages_output,
        "all_sinks": sorted(list(all_sinks)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Presentation Layer (Imported from describe_arch_html)
# ─────────────────────────────────────────────────────────────────────────────

from graph_fy.describe_arch_html import (
    generate_architecture_html,
    generate_architecture_mermaid,
    generate_architecture_svg,
)


# ─────────────────────────────────────────────────────────────────────────────
# 6. CLI Entry Point & High-Level Function
# ─────────────────────────────────────────────────────────────────────────────

def describe_architecture(
    project_or_graph: str | Path | None = None,
    *,
    graph_path: str | Path | None = None,
    labels_path: str | Path | None = None,
    output_path: str | Path | None = None,
    focus: str | None = None,
    title: str | None = None,
    as_json: bool = False,
    open_browser: bool = False,
    verbose: bool = True,
) -> Path | dict[str, Any]:
    """Execute `describe arch` pipeline, creating interactive HTML or emitting JSON."""
    # Resolve graph.json path
    cand = Path(project_or_graph).expanduser() if project_or_graph else Path.cwd()

    if graph_path:
        gp = Path(graph_path).expanduser().resolve()
    elif cand.is_file() and cand.suffix == ".json":
        gp = cand.resolve()
    else:
        from graph_fy.paths import default_graph_json
        gp = next(
            (p.resolve() for p in (cand / "graph.json", cand / graph_fy_OUT / "graph.json", cand / "graph_fy_out" / "graph.json") if p.is_file()),
            Path(default_graph_json()).resolve(),
        )

    if not gp.is_file():
        raise FileNotFoundError(f"graph.json not found at {gp}. Run 'graph_fy <directory>' first.")

    # Project root
    project_root = gp.parent.parent if gp.parent.name in (graph_fy_OUT_NAME, "graph_fy_out", "graphify_out") else gp.parent

    # Resolve labels path
    lp = Path(labels_path).expanduser().resolve() if labels_path else next(
        (p for p in (gp.parent / ".graph_fy_labels.json", gp.parent / ".graphify_labels.json") if p.is_file()),
        gp.parent / ".graph_fy_labels.json",
    )

    labels_data = {}
    if lp.is_file():
        try:
            raw_labels = json.loads(lp.read_text(encoding="utf-8"))
            if isinstance(raw_labels, dict):
                labels_data = raw_labels.get("labels", raw_labels)
        except Exception:
            pass

    # Load graph data
    try:
        from graph_fy.paths import load_node_link_graph
        raw_dict = json.loads(gp.read_text(encoding="utf-8"))
        G = load_node_link_graph({**raw_dict, "directed": True, "multigraph": True})
        nodes = [dict(attrs, id=nid) for nid, attrs in G.nodes(data=True)]
        links = []
        for u, v, attrs in G.edges(data=True):
            links.append(dict(attrs, source=attrs.get("_src", u), target=attrs.get("_tgt", v)))
        graph_data = {"nodes": nodes, "links": links, "graph": raw_dict.get("graph", {}), "metadata": raw_dict.get("metadata", {})}
    except Exception:
        raw_dict = json.loads(gp.read_text(encoding="utf-8"))
        graph_data = raw_dict

    # Build architecture model
    arch_model = build_architecture_model(
        graph_data,
        labels=labels_data,
        project_root=project_root,
        focus=focus,
    )

    if as_json:
        if verbose:
            print(json.dumps(arch_model, indent=2, ensure_ascii=False))
        return arch_model

    # Determine output path
    if output_path:
        out_p = Path(output_path).expanduser().resolve()
    else:
        out_p = gp.parent / "architecture.html"

    out_p.parent.mkdir(parents=True, exist_ok=True)
    html_content = generate_architecture_html(arch_model, title=title)
    out_p.write_text(html_content, encoding="utf-8")

    if verbose:
        print(f"Architecture HTML view written: {out_p}")
        print(f"  Stages: {arch_model['stats']['stages_count']}  |  Steps: {arch_model['stats']['steps_count']}  |  AST Symbols: {arch_model['stats']['symbols_count']}  |  Sinks: {arch_model['stats']['sinks_count']}")
        print(f"  Repo: {arch_model['project_name']}  |  Native responsive SVG, Mermaid flowchart & AST code drawer")

    if open_browser:
        try:
            import webbrowser
            webbrowser.open(out_p.as_uri())
        except Exception:
            pass

    return out_p


_DESCRIBE_USAGE = """Usage: graph_fy describe arch [DIR|GRAPH] [options]

Deconstruct system architecture into lifecycle stages, sequential execution steps,
and deep AST skeletons (signatures, docstrings, callers, callees, sinks) in an
interactive, presentation-ready HTML application with Native SVG and Mermaid views.

Options:
  --graph PATH       path to graph.json (default graph_fy_out/graph.json)
  --labels PATH      path to .graph_fy_labels.json
  --output HTML      output HTML path (default graph_fy_out/architecture.html)
  --focus TERM       spotlight specific subsystem or symbol (e.g. cli, extract, auth)
  --title TITLE      override project title in report
  --json             emit structured architecture JSON instead of HTML
  --open             open generated HTML in default web browser"""


def cli_describe(argv: list[str]) -> None:
    """CLI dispatcher for `graph_fy describe`."""
    if argv and argv[0] in ("-h", "--help"):
        print(_DESCRIBE_USAGE)
        return

    # Allow bare `graph_fy describe [options]` as well as `graph_fy describe arch [options]`
    if argv and argv[0] in ("arch", "architecture"):
        remaining = argv[1:]
    else:
        remaining = argv

    parser = argparse.ArgumentParser(prog="graph_fy describe", add_help=False)
    parser.add_argument("target", nargs="?", default=None)
    parser.add_argument("--graph", default=None)
    parser.add_argument("--labels", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--focus", default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")

    args, unknown = parser.parse_known_args(remaining)
    if args.help:
        print(_DESCRIBE_USAGE)
        return

    try:
        describe_architecture(
            args.target,
            graph_path=args.graph,
            labels_path=args.labels,
            output_path=args.output,
            focus=args.focus,
            title=args.title,
            as_json=args.json,
            open_browser=args.open,
            verbose=True,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
