"""Phase 3 resolver for Cross-Layer Configuration & Environment Variable Lineage.

Connects runtime configuration reads across languages (Python, JS/TS, Rust, Go, Java):
- os.environ["X"], os.getenv("X")
- process.env.X, process.env["X"]
- std::env::var("X")
- viper.GetString("X"), os.Getenv("X")
- @Value("${X}"), System.getenv("X")
to infrastructure declarations:
- Dockerfile (ENV, ARG) & Docker Compose (environment)
- Kubernetes manifests (env:, ConfigMap, Secret) & Argo CD
- GitHub Actions workflows (env:)
- Terraform declarations (variable, output)
via `reads_config_from` directed edges.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.config_keys import extract_config_keys_from_source


def _norm_key(name: str) -> str:
    """Normalize a config or environment variable key for robust matching."""
    cleaned = str(name).strip().strip("'\"`")
    if cleaned.startswith("var."):
        cleaned = cleaned[4:]
    elif cleaned.startswith("output."):
        cleaned = cleaned[7:]
    elif cleaned.startswith("Docker:Env:"):
        cleaned = cleaned[11:]
    elif cleaned.startswith("Docker:Arg:"):
        cleaned = cleaned[11:]
    elif cleaned.startswith("Compose:Env:"):
        cleaned = cleaned[12:]
    elif cleaned.startswith("K8s:Env:"):
        cleaned = cleaned[8:]
    elif cleaned.startswith("K8s:ConfigMapKey:"):
        cleaned = cleaned[17:]
    elif cleaned.startswith("K8s:SecretKey:"):
        cleaned = cleaned[14:]
    elif cleaned.startswith("GHA:Env:"):
        cleaned = cleaned[8:]
    elif cleaned.startswith("Argo:Param:"):
        cleaned = cleaned[11:]
    elif cleaned.startswith("ConfigKey:"):
        cleaned = cleaned[10:]
    return re.sub(r"[^a-zA-Z0-9]+", "_", cleaned).lower().strip("_")


def resolve_config_lineage(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Cross-resolve configuration key reads to infrastructure declarations via `reads_config_from` edges."""
    # 1. Ensure config_key nodes are extracted from scanned code files if not present
    existing_key_nodes = [n for n in nodes if n.get("type") == "config_key" or str(n.get("label", "")).startswith("ConfigKey:")]
    if not existing_key_nodes:
        # Scan code files represented in nodes
        scanned_files: set[str] = set()
        file_to_nodes: dict[str, list[dict[str, Any]]] = {}
        for n in nodes:
            sf = n.get("source_file")
            if sf:
                file_to_nodes.setdefault(sf, []).append(n)

        for sf in file_to_nodes:
            p = Path(sf)
            if not p.is_file() and root:
                candidate = Path(root) / sf
                if candidate.is_file():
                    p = candidate
            if p.is_file() and p.suffix.lower() in (
                ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
                ".mjs", ".cjs", ".rs", ".go", ".java", ".kt", ".scala",
            ):
                try:
                    src_text = p.read_text(encoding="utf-8", errors="replace")
                    wrapper = {"nodes": nodes, "edges": edges}
                    extract_config_keys_from_source(p, src_text, wrapper)
                except Exception:
                    pass

    # 2. Collect all configuration declarations
    # exact_name -> list[node_id]
    exact_decls: dict[str, list[dict[str, Any]]] = {}
    # norm_name -> list[node_id]
    norm_decls: dict[str, list[dict[str, Any]]] = {}

    for n in nodes:
        ntype = str(n.get("type", ""))
        lbl = str(n.get("label", ""))
        cname = n.get("config_name")

        if not cname:
            if lbl.startswith((
                "Docker:Env:", "Docker:Arg:", "Compose:Env:",
                "K8s:Env:", "K8s:ConfigMapKey:", "K8s:SecretKey:",
                "GHA:Env:", "Argo:Param:",
            )):
                cname = lbl.split(":", 2)[-1]
            elif lbl.startswith("var."):
                cname = lbl[4:]
            elif lbl.startswith("output."):
                cname = lbl[7:]
            elif ntype in ("terraform_variable", "terraform_output"):
                cname = n.get("resource_name") or lbl.split(".", 1)[-1]
            elif ntype == "k8s_resource" and n.get("resource_kind") in ("ConfigMap", "Secret"):
                cname = n.get("resource_name")

        if cname:
            cname_str = str(cname).strip()
            exact_decls.setdefault(cname_str, []).append(n)
            norm_k = _norm_key(cname_str)
            if norm_k:
                norm_decls.setdefault(norm_k, []).append(n)

    # 3. Track callers that read config keys: config_key_id -> list[caller_id]
    key_to_callers: dict[str, list[str]] = {}
    for e in edges:
        if e.get("relation") == "reads_config":
            src = str(e.get("source"))
            tgt = str(e.get("target"))
            key_to_callers.setdefault(tgt, []).append(src)

    seen_edges: set[tuple[str, str, str]] = {
        (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        for e in edges
    }
    new_edges: list[dict[str, Any]] = []

    def _add_edge(src_id: str, tgt_id: str, sf: str, loc: str, conf: str = "EXTRACTED") -> None:
        if src_id == tgt_id:
            return
        edge_key = (src_id, tgt_id, "reads_config_from")
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            edge_dict = {
                "source": src_id,
                "target": tgt_id,
                "relation": "reads_config_from",
                "confidence": conf,
                "weight": 1.0,
                "context": "config_lineage",
                "source_file": sf,
                "source_location": loc,
            }
            edges.append(edge_dict)
            new_edges.append(edge_dict)

    # 4. Connect config_key nodes and their callers to matching declarations
    for n in nodes:
        if n.get("type") != "config_key" and not str(n.get("label", "")).startswith("ConfigKey:"):
            continue

        raw_key = n.get("config_name") or str(n.get("label", "")).replace("ConfigKey:", "").strip()
        if not raw_key:
            continue

        sf = str(n.get("source_file") or "")
        loc = str(n.get("source_location") or "L1")
        nid = str(n.get("id"))

        # Find matching declarations: exact match first, then normalized
        matching_nodes: list[dict[str, Any]] = []
        is_exact = False
        if raw_key in exact_decls:
            matching_nodes = exact_decls[raw_key]
            is_exact = True
        else:
            norm_k = _norm_key(raw_key)
            if norm_k in norm_decls:
                matching_nodes = norm_decls[norm_k]

        conf = "EXTRACTED" if is_exact else "INFERRED"
        for decl in matching_nodes:
            decl_id = str(decl["id"])
            # Edge 1: config_key node -> declaration
            _add_edge(nid, decl_id, sf, loc, conf)

            # Edge 2: caller function/file -> declaration
            callers = key_to_callers.get(nid, [])
            for c_id in callers:
                _add_edge(c_id, decl_id, sf, loc, conf)

    return new_edges
