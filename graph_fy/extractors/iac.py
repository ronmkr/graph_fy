"""Infrastructure-as-Code and configuration extractors: Kubernetes, Helm, AWS CloudFormation, and Kafka.

Fully local, deterministic extraction with zero network calls and zero model dependencies.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from graph_fy.extractors.base import _make_id


# Custom YAML loader that ignores unknown tags (e.g. AWS CloudFormation !Ref, !Sub, !GetAtt)
class _SafeLoaderIgnoreUnknown(yaml.SafeLoader):
    pass


def _any_constructor(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    else:
        return loader.construct_mapping(node)


def _cfn_tag_constructor(tag_name: str):
    def constructor(loader, node):
        if isinstance(node, yaml.ScalarNode):
            val = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            val = loader.construct_sequence(node)
        else:
            val = loader.construct_mapping(node)
        return {tag_name: val}
    return constructor


_SafeLoaderIgnoreUnknown.add_constructor(None, _any_constructor)
for _tag, _prop in [
    ("!Ref", "Ref"),
    ("!GetAtt", "Fn::GetAtt"),
    ("!Sub", "Fn::Sub"),
    ("!Join", "Fn::Join"),
    ("!Select", "Fn::Select"),
    ("!FindInMap", "Fn::FindInMap"),
    ("!ImportValue", "Fn::ImportValue"),
]:
    _SafeLoaderIgnoreUnknown.add_constructor(_tag, _cfn_tag_constructor(_prop))


def is_kubernetes_manifest(path: Path) -> bool:
    """Return True if path is a YAML file containing Kubernetes apiVersion and kind."""
    if path.suffix.lower() not in (".yaml", ".yml"):
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:4096]
        return bool(re.search(r"^\s*apiVersion:\s*\S+", content, re.MULTILINE) and
                    re.search(r"^\s*kind:\s*\S+", content, re.MULTILINE))
    except Exception:
        return False


def is_helm_chart_file(path: Path) -> bool:
    """Return True if path is a Helm Chart.yaml, values.yaml, or a template under templates/."""
    name = path.name.lower()
    if name in ("chart.yaml", "chart.yml", "values.yaml", "values.yml"):
        return True
    parts = [p.lower() for p in path.parts]
    if "templates" in parts and path.suffix.lower() in (".yaml", ".yml", ".tpl"):
        return True
    return False


def is_cloudformation_template(path: Path) -> bool:
    """Return True if path is an AWS CloudFormation or SAM template."""
    ext = path.suffix.lower()
    if ext not in (".yaml", ".yml", ".json"):
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:4096]
        if "AWSTemplateFormatVersion" in content:
            return True
        if "Transform: AWS::Serverless" in content or "Transform: 'AWS::Serverless" in content:
            return True
        if re.search(r"Type:\s*['\"]?AWS::", content):
            return True
        if '"Type": "AWS::' in content:
            return True
        return False
    except Exception:
        return False


def is_kafka_config(path: Path) -> bool:
    """Return True if path is a Kafka config (server.properties, topics, etc.)."""
    name = path.name.lower()
    if name in ("server.properties", "kafka.properties", "consumer.properties", "producer.properties"):
        return True
    if path.suffix.lower() == ".properties" and ("kafka" in str(path).lower() or "broker" in str(path).lower()):
        return True
    return False


# =====================================================================
# 1. Kubernetes Extractor
# =====================================================================

def extract_kubernetes(path: Path) -> dict[str, Any]:
    """Extract Kubernetes resources and intra-file relationships.

    Nodes: K8s:<kind>:<name> (Deployment, Service, ConfigMap, Secret, Ingress, Pod, etc.)
    Edges:
      - contains (file -> resource)
      - selects (Service -> Pod/Deployment matching selector labels)
      - mounts (Deployment/Pod -> ConfigMap / Secret)
      - uses_image (Deployment/Pod -> container image)
      - routes_to (Ingress -> Service)
      - owns (Controller -> child resource via ownerReferences)
    """
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)
    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": path.name,
        "type": "file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": path.name,
    }]
    edges: list[dict[str, Any]] = []
    seen_ids: set[str] = {file_nid}

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        docs = list(yaml.load_all(content, Loader=_SafeLoaderIgnoreUnknown))
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    # First pass: collect all resources and their metadata
    parsed_resources: list[dict[str, Any]] = []

    for doc_idx, doc in enumerate(docs):
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        if not kind or not isinstance(kind, str):
            continue
        metadata = doc.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue

        name = metadata.get("name") or f"unnamed-{doc_idx}"
        namespace = metadata.get("namespace", "default")
        labels = metadata.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}

        res_id = _make_id("k8s", kind.lower(), str(namespace), str(name))
        ns_prefix = f"{namespace}/" if namespace and namespace != "default" else ""
        res_label = f"K8s:{kind}:{ns_prefix}{name}"

        # Summary text for BM25 indexing
        desc = f"Kubernetes {kind} resource named {name} in namespace {namespace}"
        if labels:
            desc += f" with labels {', '.join(f'{k}={v}' for k, v in list(labels.items())[:5])}"

        if res_id not in seen_ids:
            seen_ids.add(res_id)
            nodes.append({
                "id": res_id,
                "label": res_label,
                "type": "k8s_resource",
                "file_type": "code",
                "resource_kind": kind,
                "resource_name": name,
                "namespace": namespace,
                "source_file": str_path,
                "source_location": f"doc-{doc_idx + 1}",
                "text": desc,
            })
            edges.append({
                "source": file_nid,
                "target": res_id,
                "relation": "contains",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"doc-{doc_idx + 1}",
            })

            # Ingest ConfigMap / Secret keys as config_decl nodes
            if kind in ("ConfigMap", "Secret"):
                data_dict = doc.get("data") if isinstance(doc.get("data"), dict) else {}
                sdata_dict = doc.get("stringData") if isinstance(doc.get("stringData"), dict) else {}
                all_cfg_keys = list(data_dict.keys()) + list(sdata_dict.keys())
                cfg_type = "configmap_key" if kind == "ConfigMap" else "secret_key"
                for k in all_cfg_keys:
                    k_str = str(k)
                    cfg_nid = _make_id("k8s_cfg", str_path, namespace, str(name), k_str)
                    if cfg_nid not in seen_ids:
                        seen_ids.add(cfg_nid)
                        nodes.append({
                            "id": cfg_nid,
                            "label": f"K8s:{kind}Key:{k_str}",
                            "type": "config_decl",
                            "file_type": "code",
                            "config_type": cfg_type,
                            "config_name": k_str,
                            "source_file": str_path,
                            "source_location": f"doc-{doc_idx + 1}",
                            "text": f"Kubernetes {kind} {name} key {k_str}",
                        })
                        edges.append({
                            "source": res_id,
                            "target": cfg_nid,
                            "relation": "declares_config",
                            "confidence": "EXTRACTED",
                            "weight": 1.0,
                            "source_file": str_path,
                            "source_location": f"doc-{doc_idx + 1}",
                        })

        parsed_resources.append({
            "id": res_id,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "doc": doc,
            "doc_idx": doc_idx,
        })

    # Second pass: cross-resource relationship resolution
    for res in parsed_resources:
        doc = res["doc"]
        kind = res["kind"]
        src_id = res["id"]
        loc = f"doc-{res['doc_idx'] + 1}"

        # 1. Service -> Pod/Deployment via spec.selector
        if kind == "Service":
            spec = doc.get("spec") or {}
            selector = spec.get("selector") if isinstance(spec, dict) else None
            if isinstance(selector, dict) and selector:
                for target_res in parsed_resources:
                    if target_res["id"] == src_id:
                        continue
                    # Check if target resource labels match selector
                    target_labels = target_res["labels"]
                    # Also check spec.template.metadata.labels for Deployments/StatefulSets
                    target_doc = target_res["doc"]
                    template_labels = {}
                    if isinstance(target_doc, dict):
                        tspec = target_doc.get("spec")
                        if isinstance(tspec, dict):
                            tmeta = tspec.get("template", {}).get("metadata", {})
                            if isinstance(tmeta, dict) and isinstance(tmeta.get("labels"), dict):
                                template_labels = tmeta["labels"]

                    all_target_labels = {**target_labels, **template_labels}
                    if all(all_target_labels.get(k) == str(v) for k, v in selector.items()):
                        edges.append({
                            "source": src_id,
                            "target": target_res["id"],
                            "relation": "selects",
                            "confidence": "EXTRACTED",
                            "weight": 1.0,
                            "source_file": str_path,
                            "source_location": loc,
                        })

        # 2. Ingress -> Service
        if kind == "Ingress":
            spec = doc.get("spec") or {}
            if isinstance(spec, dict):
                rules = spec.get("rules") or []
                for rule in rules:
                    if not isinstance(rule, dict):
                        continue
                    http = rule.get("http") or {}
                    paths = http.get("paths") or [] if isinstance(http, dict) else []
                    for p in paths:
                        if not isinstance(p, dict):
                            continue
                        backend = p.get("backend") or {}
                        svc_name = None
                        if isinstance(backend, dict):
                            if "service" in backend and isinstance(backend["service"], dict):
                                svc_name = backend["service"].get("name")
                            elif "serviceName" in backend:
                                svc_name = backend["serviceName"]
                        if svc_name:
                            target_svc_id = _make_id("k8s", "service", res["namespace"], str(svc_name))
                            edges.append({
                                "source": src_id,
                                "target": target_svc_id,
                                "relation": "routes_to",
                                "confidence": "EXTRACTED",
                                "weight": 1.0,
                                "source_file": str_path,
                                "source_location": loc,
                            })

        # 3. Workload (Deployment, StatefulSet, DaemonSet, Job, Pod) -> ConfigMap / Secret / Image
        spec = doc.get("spec") or {}
        if isinstance(spec, dict):
            pod_spec = spec.get("template", {}).get("spec") if "template" in spec else spec
            if isinstance(pod_spec, dict):
                # Volume mounts
                volumes = pod_spec.get("volumes") or []
                if isinstance(volumes, list):
                    for vol in volumes:
                        if not isinstance(vol, dict):
                            continue
                        if "configMap" in vol and isinstance(vol["configMap"], dict):
                            cm_name = vol["configMap"].get("name")
                            if cm_name:
                                cm_id = _make_id("k8s", "configmap", res["namespace"], str(cm_name))
                                edges.append({
                                    "source": src_id,
                                    "target": cm_id,
                                    "relation": "mounts",
                                    "confidence": "EXTRACTED",
                                    "weight": 1.0,
                                    "source_file": str_path,
                                    "source_location": loc,
                                })
                        if "secret" in vol and isinstance(vol["secret"], dict):
                            sec_name = vol["secret"].get("secretName")
                            if sec_name:
                                sec_id = _make_id("k8s", "secret", res["namespace"], str(sec_name))
                                edges.append({
                                    "source": src_id,
                                    "target": sec_id,
                                    "relation": "mounts",
                                    "confidence": "EXTRACTED",
                                    "weight": 1.0,
                                    "source_file": str_path,
                                    "source_location": loc,
                                })

                # Containers -> image and envFrom
                containers = pod_spec.get("containers") or []
                if isinstance(containers, list):
                    for c in containers:
                        if not isinstance(c, dict):
                            continue
                        # Image reference
                        img = c.get("image")
                        if img and isinstance(img, str):
                            img_id = _make_id("image", img)
                            if img_id not in seen_ids:
                                seen_ids.add(img_id)
                                nodes.append({
                                    "id": img_id,
                                    "label": f"Image:{img}",
                                    "type": "container_image",
                                    "file_type": "code",
                                    "source_file": str_path,
                                    "source_location": loc,
                                    "text": f"Container image {img}",
                                })
                            edges.append({
                                "source": src_id,
                                "target": img_id,
                                "relation": "uses_image",
                                "confidence": "EXTRACTED",
                                "weight": 1.0,
                                "source_file": str_path,
                                "source_location": loc,
                            })

                        # envFrom Secret/ConfigMap
                        env_from = c.get("envFrom") or []
                        if isinstance(env_from, list):
                            for ef in env_from:
                                if not isinstance(ef, dict):
                                    continue
                                if "configMapRef" in ef and isinstance(ef["configMapRef"], dict):
                                    c_name = ef["configMapRef"].get("name")
                                    if c_name:
                                        c_id = _make_id("k8s", "configmap", res["namespace"], str(c_name))
                                        edges.append({
                                            "source": src_id,
                                            "target": c_id,
                                            "relation": "mounts",
                                            "confidence": "EXTRACTED",
                                            "weight": 1.0,
                                            "source_file": str_path,
                                            "source_location": loc,
                                        })
                                if "secretRef" in ef and isinstance(ef["secretRef"], dict):
                                    s_name = ef["secretRef"].get("name")
                                    if s_name:
                                        s_id = _make_id("k8s", "secret", res["namespace"], str(s_name))
                                        edges.append({
                                            "source": src_id,
                                            "target": s_id,
                                            "relation": "mounts",
                                            "confidence": "EXTRACTED",
                                            "weight": 1.0,
                                            "source_file": str_path,
                                            "source_location": loc,
                                        })

                        # Container env declarations (config_decl)
                        env_items = c.get("env") or []
                        if isinstance(env_items, list):
                            for env_item in env_items:
                                if not isinstance(env_item, dict):
                                    continue
                                env_k = env_item.get("name")
                                if env_k:
                                    env_k_str = str(env_k)
                                    env_nid = _make_id("k8s_env", str_path, res["namespace"], res["name"], env_k_str)
                                    if env_nid not in seen_ids:
                                        seen_ids.add(env_nid)
                                        nodes.append({
                                            "id": env_nid,
                                            "label": f"K8s:Env:{env_k_str}",
                                            "type": "config_decl",
                                            "file_type": "code",
                                            "config_type": "env",
                                            "config_name": env_k_str,
                                            "source_file": str_path,
                                            "source_location": loc,
                                            "text": f"Kubernetes container env {env_k_str} in {res['name']}",
                                        })
                                    edges.append({
                                        "source": src_id,
                                        "target": env_nid,
                                        "relation": "declares_env",
                                        "confidence": "EXTRACTED",
                                        "weight": 1.0,
                                        "source_file": str_path,
                                        "source_location": loc,
                                    })

        # 4. ownerReferences
        meta = doc.get("metadata") or {}
        if isinstance(meta, dict):
            owner_refs = meta.get("ownerReferences") or []
            if isinstance(owner_refs, list):
                for oref in owner_refs:
                    if not isinstance(oref, dict):
                        continue
                    okind = oref.get("kind")
                    oname = oref.get("name")
                    if okind and oname:
                        owner_id = _make_id("k8s", str(okind).lower(), res["namespace"], str(oname))
                        edges.append({
                            "source": owner_id,
                            "target": src_id,
                            "relation": "owns",
                            "confidence": "EXTRACTED",
                            "weight": 1.0,
                            "source_file": str_path,
                            "source_location": loc,
                        })

    return {"nodes": nodes, "edges": edges}


# =====================================================================
# 2. Helm Extractor
# =====================================================================

def _mask_helm_templates(content: str) -> str:
    """Mask Go-template directives to allow safe YAML structural parsing."""
    masked_lines: list[str] = []
    for line in content.splitlines():
        # Comment out lines that are purely template directives (control flow, helper includes)
        if re.match(r'^\s*\{\{.*?\}\}\s*$', line):
            masked_lines.append('# ' + line)
        else:
            # Replace inline {{ ... }} with a safe scalar identifier without adding extra quotes
            cleaned = re.sub(r'\{\{.*?\}\}', '__HELM_EXPR__', line)
            masked_lines.append(cleaned)
    return "\n".join(masked_lines)


def extract_helm(path: Path) -> dict[str, Any]:
    """Extract Helm Chart.yaml metadata, values.yaml, and template definitions.

    Nodes: Helm:Chart:<name>, Helm:Values:<name>, and generated K8s resource nodes.
    Edges:
      - depends_on (Chart -> sub-chart)
      - defines (Chart -> K8s resources, Chart -> Values)
    """
    str_path = str(path.resolve())
    name = path.name.lower()
    file_nid = _make_id(str_path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": path.name,
        "type": "file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": path.name,
    }]
    edges: list[dict[str, Any]] = []
    seen_ids: set[str] = {file_nid}

    # Case 1: Chart.yaml
    if name in ("chart.yaml", "chart.yml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        except Exception as exc:
            return {"nodes": nodes, "edges": edges, "error": str(exc)}

        chart_name = data.get("name") or path.parent.name
        version = data.get("version", "0.1.0")
        app_version = data.get("appVersion", "")
        desc = data.get("description", "")

        chart_id = _make_id("helm_chart", chart_name)
        chart_node = {
            "id": chart_id,
            "label": f"Helm:Chart:{chart_name}",
            "type": "helm_chart",
            "file_type": "code",
            "version": version,
            "app_version": app_version,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Helm chart {chart_name} (v{version}): {desc}",
        }
        nodes.append(chart_node)
        edges.append({
            "source": file_nid,
            "target": chart_id,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

        # Chart dependencies
        deps = data.get("dependencies") or []
        if isinstance(deps, list):
            for dep in deps:
                if not isinstance(dep, dict):
                    continue
                sub_name = dep.get("name")
                if sub_name:
                    sub_id = _make_id("helm_chart", sub_name)
                    if sub_id not in seen_ids:
                        seen_ids.add(sub_id)
                        nodes.append({
                            "id": sub_id,
                            "label": f"Helm:Chart:{sub_name}",
                            "type": "helm_chart",
                            "file_type": "code",
                            "source_file": str_path,
                            "source_location": "L1",
                            "text": f"Helm subchart dependency {sub_name}",
                        })
                    edges.append({
                        "source": chart_id,
                        "target": sub_id,
                        "relation": "depends_on",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "L1",
                    })

        return {"nodes": nodes, "edges": edges}

    # Case 2: values.yaml
    if name in ("values.yaml", "values.yml"):
        chart_name = path.parent.name
        values_id = _make_id("helm_values", chart_name)
        nodes.append({
            "id": values_id,
            "label": f"Helm:Values:{chart_name}",
            "type": "helm_values",
            "file_type": "code",
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Helm default values configuration for chart {chart_name}",
        })
        edges.append({
            "source": file_nid,
            "target": values_id,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })
        chart_id = _make_id("helm_chart", chart_name)
        edges.append({
            "source": chart_id,
            "target": values_id,
            "relation": "defines",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })
        return {"nodes": nodes, "edges": edges}

    # Case 3: templates/*.yaml
    chart_dir = path.parent.parent if path.parent.name == "templates" else path.parent
    chart_name = chart_dir.name
    chart_id = _make_id("helm_chart", chart_name)

    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        masked = _mask_helm_templates(raw_text)
        docs = list(yaml.load_all(masked, Loader=_SafeLoaderIgnoreUnknown))
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    for doc_idx, doc in enumerate(docs):
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        if not kind or not isinstance(kind, str) or kind == "__HELM_EXPR__":
            continue
        metadata = doc.get("metadata") or {}
        raw_name = metadata.get("name") if isinstance(metadata, dict) else None
        res_name = path.stem if (not raw_name or raw_name == "__HELM_EXPR__") else str(raw_name)

        res_id = _make_id("k8s", kind.lower(), "default", res_name)
        res_label = f"K8s:{kind}:{res_name}"

        if res_id not in seen_ids:
            seen_ids.add(res_id)
            nodes.append({
                "id": res_id,
                "label": res_label,
                "type": "k8s_resource",
                "file_type": "code",
                "resource_kind": kind,
                "resource_name": res_name,
                "source_file": str_path,
                "source_location": f"doc-{doc_idx + 1}",
                "text": f"Kubernetes {kind} template defined by Helm chart {chart_name}",
            })
            edges.append({
                "source": file_nid,
                "target": res_id,
                "relation": "contains",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"doc-{doc_idx + 1}",
            })
            edges.append({
                "source": chart_id,
                "target": res_id,
                "relation": "defines",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"doc-{doc_idx + 1}",
            })

    return {"nodes": nodes, "edges": edges}


# =====================================================================
# 3. AWS CloudFormation & SAM Extractor
# =====================================================================

def extract_cloudformation(path: Path) -> dict[str, Any]:
    """Extract AWS CloudFormation / SAM template resources and references.

    Nodes: AWS:<ResourceType>:<LogicalId>
    Edges:
      - contains (file -> resource)
      - depends_on (explicit DependsOn)
      - references (Fn::Ref, Fn::GetAtt references between resources)
    """
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": path.name,
        "type": "file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": path.name,
    }]
    edges: list[dict[str, Any]] = []
    seen_ids: set[str] = {file_nid}

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".json":
            doc = json.loads(content)
        else:
            doc = yaml.load(content, Loader=_SafeLoaderIgnoreUnknown)  # nosec B506
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    if not isinstance(doc, dict):
        return {"nodes": nodes, "edges": edges}

    resources = doc.get("Resources") or {}
    if not isinstance(resources, dict):
        resources = {}

    params = doc.get("Parameters") or {}
    if not isinstance(params, dict):
        params = {}

    outputs = doc.get("Outputs") or {}
    if not isinstance(outputs, dict):
        outputs = {}

    # Track logical IDs
    logical_ids = set(resources.keys())
    param_ids = set(params.keys())

    # Parameters
    for param_id, pdata in params.items():
        param_nid = _make_id("aws_cfn_param", param_id)
        if param_nid not in seen_ids:
            seen_ids.add(param_nid)
            nodes.append({
                "id": param_nid,
                "label": f"AWS:Parameter:{param_id}",
                "type": "aws_parameter",
                "file_type": "code",
                "logical_id": param_id,
                "source_file": str_path,
                "source_location": "Parameters",
                "text": f"AWS CloudFormation parameter {param_id}",
            })
            edges.append({
                "source": file_nid,
                "target": param_nid,
                "relation": "contains",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "Parameters",
            })

    # Resources
    for logical_id, rdata in resources.items():
        if not isinstance(rdata, dict):
            continue
        rtype = rdata.get("Type", "Resource")
        clean_type = rtype[5:] if rtype.startswith("AWS::") else rtype
        res_id = _make_id("aws_cfn", logical_id)
        res_label = f"AWS:{clean_type}:{logical_id}"

        props = rdata.get("Properties") or {}
        physical_name = ""
        if isinstance(props, dict):
            for k in ("TableName", "BucketName", "QueueName", "TopicName", "FunctionName", "ClusterName", "Name", "RoleName"):
                if k in props and isinstance(props[k], str):
                    physical_name = props[k]
                    break

        if res_id not in seen_ids:
            seen_ids.add(res_id)
            nodes.append({
                "id": res_id,
                "label": res_label,
                "type": "aws_resource",
                "file_type": "code",
                "aws_type": rtype,
                "logical_id": logical_id,
                "physical_name": physical_name,
                "source_file": str_path,
                "source_location": "Resources",
                "text": f"AWS CloudFormation {rtype} resource with logical ID {logical_id}",
            })
            edges.append({
                "source": file_nid,
                "target": res_id,
                "relation": "contains",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "Resources",
            })

    # Outputs
    for output_id, odata in outputs.items():
        out_nid = _make_id("aws_cfn_output", output_id)
        if out_nid not in seen_ids:
            seen_ids.add(out_nid)
            nodes.append({
                "id": out_nid,
                "label": f"AWS:Output:{output_id}",
                "type": "aws_output",
                "file_type": "code",
                "logical_id": output_id,
                "source_file": str_path,
                "source_location": "Outputs",
                "text": f"AWS CloudFormation output {output_id}",
            })
            edges.append({
                "source": file_nid,
                "target": out_nid,
                "relation": "contains",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "Outputs",
            })

    # Reference tracing
    def _find_refs(obj, parent_id: str):
        if isinstance(obj, dict):
            # Fn::Ref or Ref
            ref_target = obj.get("Ref") or obj.get("Fn::Ref")
            if ref_target and isinstance(ref_target, str):
                if ref_target in logical_ids:
                    tgt_id = _make_id("aws_cfn", ref_target)
                    edges.append({
                        "source": parent_id,
                        "target": tgt_id,
                        "relation": "references",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "Resources",
                    })
                elif ref_target in param_ids:
                    tgt_id = _make_id("aws_cfn_param", ref_target)
                    edges.append({
                        "source": parent_id,
                        "target": tgt_id,
                        "relation": "references",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "Resources",
                    })
            # Fn::GetAtt
            getatt = obj.get("Fn::GetAtt")
            if getatt:
                target_name = None
                if isinstance(getatt, list) and len(getatt) > 0:
                    target_name = getatt[0]
                elif isinstance(getatt, str) and "." in getatt:
                    target_name = getatt.split(".")[0]
                if target_name and target_name in logical_ids:
                    tgt_id = _make_id("aws_cfn", target_name)
                    edges.append({
                        "source": parent_id,
                        "target": tgt_id,
                        "relation": "references",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "Resources",
                    })
            for val in obj.values():
                _find_refs(val, parent_id)
        elif isinstance(obj, list):
            for item in obj:
                _find_refs(item, parent_id)
        elif isinstance(obj, str):
            for sub_match in re.findall(r"\$\{([a-zA-Z0-9_]+)(?:\.[a-zA-Z0-9_]+)*\}", obj):
                if sub_match in logical_ids:
                    edges.append({
                        "source": parent_id,
                        "target": _make_id("aws_cfn", sub_match),
                        "relation": "references",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "Resources",
                    })
                elif sub_match in param_ids:
                    edges.append({
                        "source": parent_id,
                        "target": _make_id("aws_cfn_param", sub_match),
                        "relation": "references",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "Resources",
                    })

    for logical_id, rdata in resources.items():
        if not isinstance(rdata, dict):
            continue
        res_id = _make_id("aws_cfn", logical_id)

        # DependsOn
        deps = rdata.get("DependsOn")
        if deps:
            dep_list = [deps] if isinstance(deps, str) else (deps if isinstance(deps, list) else [])
            for dep in dep_list:
                if isinstance(dep, str) and dep in logical_ids:
                    dep_id = _make_id("aws_cfn", dep)
                    edges.append({
                        "source": res_id,
                        "target": dep_id,
                        "relation": "depends_on",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "DependsOn",
                    })

        # Properties references
        props = rdata.get("Properties")
        if props:
            _find_refs(props, res_id)

    # Trace references in Outputs
    for output_id, odata in outputs.items():
        if isinstance(odata, dict):
            out_nid = _make_id("aws_cfn_output", output_id)
            val = odata.get("Value")
            if val:
                _find_refs(val, out_nid)

    return {"nodes": nodes, "edges": edges}


# =====================================================================
# 4. Kafka Extractor (Configs, Avro, Protobuf)
# =====================================================================

def extract_kafka_properties(path: Path) -> dict[str, Any]:
    """Extract Kafka configuration (broker ID, listeners, topics)."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": path.name,
        "type": "file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": path.name,
    }]
    edges: list[dict[str, Any]] = []

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    props: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()

    broker_id = props.get("broker.id") or props.get("node.id") or "broker-0"
    listeners = props.get("listeners") or props.get("advertised.listeners", "")
    cluster_node_id = _make_id("kafka_broker", broker_id)

    nodes.append({
        "id": cluster_node_id,
        "label": f"Kafka:Broker:{broker_id}",
        "type": "kafka_broker",
        "file_type": "code",
        "broker_id": broker_id,
        "listeners": listeners,
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Kafka Broker {broker_id} listening on {listeners}",
    })
    edges.append({
        "source": file_nid,
        "target": cluster_node_id,
        "relation": "contains",
        "confidence": "EXTRACTED",
        "weight": 1.0,
        "source_file": str_path,
        "source_location": "L1",
    })

    zk = props.get("zookeeper.connect")
    if zk:
        zk_nid = _make_id("kafka_zk", zk)
        nodes.append({
            "id": zk_nid,
            "label": f"Kafka:Zookeeper:{zk}",
            "type": "kafka_zookeeper",
            "file_type": "code",
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Kafka Zookeeper connection {zk}",
        })
        edges.append({
            "source": cluster_node_id,
            "target": zk_nid,
            "relation": "connects_to",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    bs = props.get("bootstrap.servers")
    if bs:
        bs_nid = _make_id("kafka_bootstrap", bs)
        nodes.append({
            "id": bs_nid,
            "label": f"Kafka:Cluster:{bs}",
            "type": "kafka_cluster",
            "file_type": "code",
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Kafka bootstrap servers {bs}",
        })
        edges.append({
            "source": cluster_node_id,
            "target": bs_nid,
            "relation": "connects_to",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    # Schema Registry mappings
    sr_url = props.get("schema.registry.url") or props.get("schema_registry_url")
    sr_subj = props.get("schema.registry.subject") or props.get("schema_registry_subject")
    sr_arn = props.get("schema_arn") or props.get("schemaArn")
    topic = props.get("topic") or props.get("kafka.topic")
    schema_file = props.get("schema.file") or props.get("schema_file") or props.get("schema.path")

    reg_nid = ""
    if sr_url:
        reg_nid = _make_id("schema_registry", sr_url)
        nodes.append({
            "id": reg_nid,
            "label": f"SchemaRegistry:{sr_url}",
            "type": "schema_registry",
            "file_type": "code",
            "url": sr_url,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Schema Registry at {sr_url}",
        })
        edges.append({
            "source": file_nid,
            "target": reg_nid,
            "relation": "configures_registry",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })
    elif sr_arn:
        reg_nid = _make_id("aws_glue_registry", sr_arn)
        nodes.append({
            "id": reg_nid,
            "label": f"AWS:GlueSchemaRegistry:{sr_arn}",
            "type": "schema_registry",
            "file_type": "code",
            "schema_arn": sr_arn,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"AWS Glue Schema Registry {sr_arn}",
        })
        edges.append({
            "source": file_nid,
            "target": reg_nid,
            "relation": "configures_registry",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    subj_nid = ""
    if sr_subj:
        subj_nid = _make_id("schema_subject", sr_subj)
        nodes.append({
            "id": subj_nid,
            "label": f"Schema:Subject:{sr_subj}",
            "type": "schema_subject",
            "file_type": "code",
            "subject_name": sr_subj,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Schema Subject {sr_subj}",
        })
        edges.append({
            "source": file_nid,
            "target": subj_nid,
            "relation": "references-schema-subject",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })
        if reg_nid:
            edges.append({
                "source": reg_nid,
                "target": subj_nid,
                "relation": "registers_subject",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

    topic_nid = ""
    if topic:
        topic_nid = _make_id("kafka_topic", topic)
        nodes.append({
            "id": topic_nid,
            "label": f"Kafka:Topic:{topic}",
            "type": "kafka_topic",
            "file_type": "code",
            "topic_name": topic,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Kafka topic {topic}",
        })
        edges.append({
            "source": file_nid,
            "target": topic_nid,
            "relation": "references_topic",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })
        if subj_nid:
            edges.append({
                "source": topic_nid,
                "target": subj_nid,
                "relation": "references-schema-subject",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

    if schema_file:
        try:
            resolved_schema = (path.parent / schema_file).resolve()
            schema_file_nid = _make_id(str(resolved_schema))
            edges.append({
                "source": file_nid,
                "target": schema_file_nid,
                "relation": "conforms-to-schema",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })
            if topic_nid:
                edges.append({
                    "source": topic_nid,
                    "target": schema_file_nid,
                    "relation": "conforms-to-schema",
                    "confidence": "EXTRACTED",
                    "weight": 1.0,
                    "source_file": str_path,
                    "source_location": "L1",
                })
        except Exception:
            pass

    return {"nodes": nodes, "edges": edges}


def extract_avro(path: Path) -> dict[str, Any]:
    """Extract Avro schema (.avsc) records and field definitions."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": path.name,
        "type": "file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": path.name,
    }]
    edges: list[dict[str, Any]] = []

    try:
        schema = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    if isinstance(schema, dict) and schema.get("type") == "record":
        name = schema.get("name", path.stem)
        ns = schema.get("namespace", "")
        full_name = f"{ns}.{name}" if ns else name
        record_id = _make_id("avro_record", full_name)
        fields = schema.get("fields") or []
        field_names = [f.get("name") for f in fields if isinstance(f, dict) and "name" in f]

        nodes.append({
            "id": record_id,
            "label": f"Kafka:Avro:{full_name}",
            "type": "avro_record",
            "file_type": "code",
            "schema_name": name,
            "namespace": ns,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Avro record {full_name} with fields: {', '.join(field_names)}",
        })
        edges.append({
            "source": file_nid,
            "target": record_id,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    return {"nodes": nodes, "edges": edges}


# Re-export extract_protobuf from dedicated protobuf extractor
from graph_fy.extractors.protobuf import extract_protobuf  # noqa: F401

