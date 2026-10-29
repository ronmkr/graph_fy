"""Argo CD Application and ApplicationSet extractor (GitOps).

Extracts:
1. Argo CD Application (apiVersion: argoproj.io/v1alpha1, kind: Application):
   - spec.source: repoURL, path, targetRevision, helm/kustomize configs
   - spec.destination: server, namespace, name
   - syncPolicy: automated (prune, selfHeal)
2. Argo CD ApplicationSet (apiVersion: argoproj.io/v1alpha1, kind: ApplicationSet):
   - generators: git, list, matrix, cluster
   - template: application metadata, source, destination
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def _make_id(*parts: str) -> str:
    clean = [re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(p)).strip("_") for p in parts if p]
    return "_".join(clean)


def is_argocd_manifest(path: Path) -> bool:
    """Return True if path is an Argo CD Application or ApplicationSet YAML file."""
    ext = path.suffix.lower()
    if ext not in (".yaml", ".yml"):
        return False

    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:1500]
        if "argoproj.io" in sample and ("Application" in sample or "ApplicationSet" in sample):
            return True
    except Exception:
        pass
    return False


def extract_argocd(path: Path) -> dict[str, Any]:
    """Extract Argo CD Application or ApplicationSet into graph nodes and edges."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": f"Argo:{path.name}",
        "type": "argocd_file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Argo CD manifest {path.name}",
    }]
    edges: list[dict[str, Any]] = []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        documents = list(yaml.safe_load_all(content))
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    for doc_idx, doc in enumerate(documents, start=1):
        if not isinstance(doc, dict):
            continue

        api_version = str(doc.get("apiVersion", "")).lower()
        kind = str(doc.get("kind", ""))

        if not ("argoproj.io" in api_version):
            continue

        metadata = doc.get("metadata") or {}
        name = str(metadata.get("name") or f"argocd_doc_{doc_idx}")
        namespace = str(metadata.get("namespace") or "argocd")
        spec = doc.get("spec") or {}

        if kind == "Application":
            _extract_application(str_path, file_nid, name, namespace, spec, nodes, edges)
        elif kind == "ApplicationSet":
            _extract_applicationset(str_path, file_nid, name, namespace, spec, nodes, edges)

    return {"nodes": nodes, "edges": edges}


def _extract_application(
    str_path: str,
    file_nid: str,
    app_name: str,
    app_namespace: str,
    spec: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    app_nid = _make_id("argocd_app", app_name)

    project = str(spec.get("project") or "default")
    source = spec.get("source") or {}
    destination = spec.get("destination") or {}
    sync_policy = spec.get("syncPolicy") or {}

    repo_url = str(source.get("repoURL") or "")
    source_path = str(source.get("path") or "")
    target_revision = str(source.get("targetRevision") or "HEAD")

    dest_server = str(destination.get("server") or "")
    dest_namespace = str(destination.get("namespace") or "default")

    app_node = {
        "id": app_nid,
        "label": f"Argo:Application:{app_name}",
        "type": "argocd_application",
        "file_type": "code",
        "app_name": app_name,
        "namespace": app_namespace,
        "project": project,
        "repo_url": repo_url,
        "source_path": source_path,
        "target_revision": target_revision,
        "dest_server": dest_server,
        "dest_namespace": dest_namespace,
        "automated_sync": bool(sync_policy.get("automated")),
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Argo CD Application {app_name} syncing from {source_path or repo_url} to {dest_namespace}",
    }
    nodes.append(app_node)
    edges.append({
        "source": file_nid,
        "target": app_nid,
        "relation": "defines_application",
        "confidence": "EXTRACTED",
        "weight": 1.0,
        "source_file": str_path,
        "source_location": "L1",
    })

    # Source link
    if source_path or repo_url:
        src_label = source_path if source_path else repo_url
        src_nid = _make_id("argocd_source", src_label)
        nodes.append({
            "id": src_nid,
            "label": f"Argo:Source:{src_label}",
            "type": "argocd_source",
            "file_type": "code",
            "repo_url": repo_url,
            "path": source_path,
            "target_revision": target_revision,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Argo CD source {src_label} (branch: {target_revision})",
        })
        edges.append({
            "source": app_nid,
            "target": src_nid,
            "relation": "syncs_from",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    # Destination cluster / namespace link
    if dest_namespace:
        dest_nid = _make_id("argocd_dest", dest_server, dest_namespace)
        nodes.append({
            "id": dest_nid,
            "label": f"Argo:Destination:{dest_namespace}",
            "type": "argocd_destination",
            "file_type": "code",
            "server": dest_server,
            "namespace": dest_namespace,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Argo CD destination namespace {dest_namespace} on {dest_server or 'in-cluster'}",
        })
        edges.append({
            "source": app_nid,
            "target": dest_nid,
            "relation": "deploys_to",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    # Helm parameters / config values
    helm = source.get("helm") or {}
    if isinstance(helm, dict):
        params = helm.get("parameters") or []
        if isinstance(params, list):
            for p in params:
                if isinstance(p, dict) and p.get("name"):
                    pname = str(p["name"])
                    p_nid = _make_id("argocd_param", str_path, app_name, pname)
                    nodes.append({
                        "id": p_nid,
                        "label": f"Argo:Param:{pname}",
                        "type": "config_decl",
                        "file_type": "code",
                        "config_type": "env",
                        "config_name": pname,
                        "source_file": str_path,
                        "source_location": "L1",
                        "text": f"Argo CD Application {app_name} parameter {pname}",
                    })
                    edges.append({
                        "source": app_nid,
                        "target": p_nid,
                        "relation": "declares_config",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "L1",
                    })


def _extract_applicationset(
    str_path: str,
    file_nid: str,
    appset_name: str,
    appset_namespace: str,
    spec: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    appset_nid = _make_id("argocd_appset", appset_name)
    generators = spec.get("generators") or []

    gen_types: list[str] = []
    if isinstance(generators, list):
        for gen in generators:
            if isinstance(gen, dict):
                gen_types.extend(str(k) for k in gen.keys())

    template = spec.get("template") or {}
    tpl_meta = template.get("metadata") or {}
    tpl_name = str(tpl_meta.get("name") or f"{appset_name}-{{{{name}}}}")

    appset_node = {
        "id": appset_nid,
        "label": f"Argo:ApplicationSet:{appset_name}",
        "type": "argocd_applicationset",
        "file_type": "code",
        "appset_name": appset_name,
        "namespace": appset_namespace,
        "generator_types": gen_types,
        "template_name": tpl_name,
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Argo CD ApplicationSet {appset_name} with generator(s): {', '.join(gen_types)}",
    }
    nodes.append(appset_node)
    edges.append({
        "source": file_nid,
        "target": appset_nid,
        "relation": "defines_applicationset",
        "confidence": "EXTRACTED",
        "weight": 1.0,
        "source_file": str_path,
        "source_location": "L1",
    })

    # Extract generator nodes
    for gen_type in gen_types:
        gen_nid = _make_id("argocd_gen", appset_name, gen_type)
        nodes.append({
            "id": gen_nid,
            "label": f"Argo:Generator:{gen_type}",
            "type": "argocd_generator",
            "file_type": "code",
            "generator_type": gen_type,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Argo CD {gen_type} generator for {appset_name}",
        })
        edges.append({
            "source": appset_nid,
            "target": gen_nid,
            "relation": "uses_generator",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })
