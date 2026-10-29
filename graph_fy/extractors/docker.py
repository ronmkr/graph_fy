"""Dockerfile extractor.

Extracts Dockerfile directives:
- Base images (FROM <image> [AS <stage>])
- Built images
- Exposed ports
- Dependencies and references
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.ids import make_id


def _make_id(*parts: str) -> str:
    clean = [re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(p)).strip("_") for p in parts if p]
    return "_".join(clean)


def extract_dockerfile(path: Path) -> dict[str, Any]:
    """Extract Dockerfile directives into graph nodes and edges."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)
    file_name = path.name

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": file_name,
        "type": "dockerfile",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Dockerfile {file_name}",
    }]
    edges: list[dict[str, Any]] = []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    # Determine default built image name from directory
    dir_name = path.parent.name
    built_image_name = dir_name if dir_name not in (".", "/", "") else "app"

    # Primary build target node
    build_nid = _make_id("docker_build", str_path)
    build_node = {
        "id": build_nid,
        "label": f"Docker:Build:{built_image_name}",
        "type": "docker_build",
        "file_type": "code",
        "image_name": built_image_name,
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Docker build for {built_image_name} from {file_name}",
    }
    nodes.append(build_node)
    edges.append({
        "source": file_nid,
        "target": build_nid,
        "relation": "contains",
        "confidence": "EXTRACTED",
        "weight": 1.0,
        "source_file": str_path,
        "source_location": "L1",
    })

    lines = content.splitlines()
    for line_idx, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # FROM <image>[:<tag>] [AS <stage>]
        from_match = re.match(r"^FROM\s+([^\s]+)(?:\s+[aA][sS]\s+([^\s]+))?", line)
        if from_match:
            base_img = from_match.group(1)
            stage_name = from_match.group(2)

            base_nid = _make_id("image", base_img)
            nodes.append({
                "id": base_nid,
                "label": f"Image:{base_img}",
                "type": "image_ref",
                "file_type": "code",
                "image_tag": base_img,
                "source_file": str_path,
                "source_location": f"L{line_idx}",
                "text": f"Base container image {base_img}",
            })
            edges.append({
                "source": build_nid,
                "target": base_nid,
                "relation": "from_image",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line_idx}",
            })

            if stage_name:
                stage_nid = _make_id("docker_stage", str_path, stage_name)
                nodes.append({
                    "id": stage_nid,
                    "label": f"Docker:Stage:{stage_name}",
                    "type": "docker_stage",
                    "file_type": "code",
                    "stage_name": stage_name,
                    "source_file": str_path,
                    "source_location": f"L{line_idx}",
                    "text": f"Docker build stage {stage_name}",
                })
                edges.append({
                    "source": build_nid,
                    "target": stage_nid,
                    "relation": "has_stage",
                    "confidence": "EXTRACTED",
                    "weight": 1.0,
                    "source_file": str_path,
                    "source_location": f"L{line_idx}",
                })

        # ENV KEY=VALUE or ENV KEY VALUE
        env_match = re.match(r"^ENV\s+(.+)$", line)
        if env_match:
            rest = env_match.group(1).strip()
            if "=" in rest:
                pairs = re.findall(r'([A-Za-z_][A-Za-z0-9_]*)=', rest)
                for var_name in pairs:
                    env_nid = _make_id("docker_env", str_path, var_name)
                    nodes.append({
                        "id": env_nid,
                        "label": f"Docker:Env:{var_name}",
                        "type": "config_decl",
                        "file_type": "code",
                        "config_type": "env",
                        "config_name": var_name,
                        "source_file": str_path,
                        "source_location": f"L{line_idx}",
                        "text": f"Docker environment variable {var_name}",
                    })
                    edges.append({
                        "source": build_nid,
                        "target": env_nid,
                        "relation": "declares_env",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": f"L{line_idx}",
                    })
            else:
                parts = rest.split(None, 1)
                if parts:
                    var_name = parts[0]
                    env_nid = _make_id("docker_env", str_path, var_name)
                    nodes.append({
                        "id": env_nid,
                        "label": f"Docker:Env:{var_name}",
                        "type": "config_decl",
                        "file_type": "code",
                        "config_type": "env",
                        "config_name": var_name,
                        "source_file": str_path,
                        "source_location": f"L{line_idx}",
                        "text": f"Docker environment variable {var_name}",
                    })
                    edges.append({
                        "source": build_nid,
                        "target": env_nid,
                        "relation": "declares_env",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": f"L{line_idx}",
                    })

        # ARG KEY or ARG KEY=VALUE
        arg_match = re.match(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if arg_match:
            arg_name = arg_match.group(1)
            arg_nid = _make_id("docker_arg", str_path, arg_name)
            nodes.append({
                "id": arg_nid,
                "label": f"Docker:Arg:{arg_name}",
                "type": "config_decl",
                "file_type": "code",
                "config_type": "arg",
                "config_name": arg_name,
                "source_file": str_path,
                "source_location": f"L{line_idx}",
                "text": f"Docker build argument {arg_name}",
            })
            edges.append({
                "source": build_nid,
                "target": arg_nid,
                "relation": "declares_arg",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line_idx}",
            })

    return {"nodes": nodes, "edges": edges}


def is_docker_compose(path: Path) -> bool:
    """Return True if path is a docker-compose or compose YAML manifest."""
    name = path.name.lower()
    return (
        name.startswith("docker-compose") or name.startswith("compose")
    ) and (name.endswith(".yml") or name.endswith(".yaml"))


def extract_docker_compose(path: Path) -> dict[str, Any]:
    """Extract docker-compose.yml services, depends_on, networks, volumes into graph nodes and edges."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)
    file_name = path.name

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": f"Compose:{file_name}",
        "type": "docker_compose",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Docker Compose {file_name}",
    }]
    edges: list[dict[str, Any]] = []

    try:
        import yaml
        content = path.read_text(encoding="utf-8", errors="replace")
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return {"nodes": nodes, "edges": edges}
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    services = data.get("services") or {}
    if not isinstance(services, dict):
        return {"nodes": nodes, "edges": edges}

    for svc_name, svc_data in services.items():
        if not isinstance(svc_data, dict):
            svc_data = {}
        svc_nid = _make_id("compose_service", str_path, str(svc_name))

        image_name = svc_data.get("image")
        build_cfg = svc_data.get("build")
        depends_on = svc_data.get("depends_on") or []
        networks = svc_data.get("networks") or []
        volumes = svc_data.get("volumes") or []
        env_raw = svc_data.get("environment") or []
        secrets_raw = svc_data.get("secrets") or []

        # Parse env var names
        env_vars: list[str] = []
        if isinstance(env_raw, dict):
            env_vars = [str(k) for k in env_raw.keys()]
        elif isinstance(env_raw, list):
            for e in env_raw:
                if isinstance(e, str) and "=" in e:
                    env_vars.append(e.split("=", 1)[0].strip())
                elif isinstance(e, str):
                    env_vars.append(e.strip())

        # Parse secrets names
        secrets: list[str] = []
        if isinstance(secrets_raw, dict):
            secrets = [str(k) for k in secrets_raw.keys()]
        elif isinstance(secrets_raw, list):
            for s in secrets_raw:
                if isinstance(s, str):
                    secrets.append(s.strip())
                elif isinstance(s, dict) and "source" in s:
                    secrets.append(str(s["source"]))

        svc_node: dict[str, Any] = {
            "id": svc_nid,
            "label": f"Docker:Service:{svc_name}",
            "type": "docker_service",
            "file_type": "code",
            "service_name": str(svc_name),
            "image": str(image_name) if image_name else "",
            "env_vars": env_vars,
            "secrets": secrets,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Docker Compose service {svc_name}",
        }
        nodes.append(svc_node)
        edges.append({
            "source": file_nid,
            "target": svc_nid,
            "relation": "defines",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })
        for ev in env_vars:
            ev_nid = _make_id("compose_env", str_path, str(svc_name), ev)
            nodes.append({
                "id": ev_nid,
                "label": f"Compose:Env:{ev}",
                "type": "config_decl",
                "file_type": "code",
                "config_type": "env",
                "config_name": ev,
                "source_file": str_path,
                "source_location": "L1",
                "text": f"Compose service {svc_name} environment variable {ev}",
            })
            edges.append({
                "source": svc_nid,
                "target": ev_nid,
                "relation": "declares_env",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

        for sec in secrets:
            sec_nid = _make_id("secret", sec)
            nodes.append({
                "id": sec_nid,
                "label": f"Secret:{sec}",
                "type": "secret_ref",
                "file_type": "code",
                "secret_name": sec,
                "source_file": str_path,
                "source_location": "L1",
                "text": f"Secret {sec}",
            })
            edges.append({
                "source": svc_nid,
                "target": sec_nid,
                "relation": "references_secret",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

        # Image reference
        if image_name:
            clean_img = str(image_name).strip()
            img_nid = _make_id("image", clean_img)
            nodes.append({
                "id": img_nid,
                "label": f"Image:{clean_img}",
                "type": "image_ref",
                "file_type": "code",
                "image_tag": clean_img,
                "source_file": str_path,
                "source_location": "L1",
                "text": f"Container image {clean_img}",
            })
            edges.append({
                "source": svc_nid,
                "target": img_nid,
                "relation": "uses_image",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

        # Build context reference
        if build_cfg:
            build_str = build_cfg if isinstance(build_cfg, str) else (build_cfg.get("context") or ".")
            build_nid = _make_id("docker_build", str_path, str(svc_name))
            nodes.append({
                "id": build_nid,
                "label": f"Docker:Build:{svc_name}",
                "type": "docker_build",
                "file_type": "code",
                "build_context": str(build_str),
                "source_file": str_path,
                "source_location": "L1",
                "text": f"Docker build context {build_str} for service {svc_name}",
            })
            edges.append({
                "source": svc_nid,
                "target": build_nid,
                "relation": "builds_with",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

        # Dependencies
        dep_list: list[str] = []
        if isinstance(depends_on, list):
            dep_list = [str(d) for d in depends_on]
        elif isinstance(depends_on, dict):
            dep_list = [str(d) for d in depends_on.keys()]
        for dep in dep_list:
            dep_nid = _make_id("compose_service", str_path, dep)
            edges.append({
                "source": svc_nid,
                "target": dep_nid,
                "relation": "depends_on",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

        # Networks
        net_list: list[str] = []
        if isinstance(networks, list):
            net_list = [str(n) for n in networks]
        elif isinstance(networks, dict):
            net_list = [str(n) for n in networks.keys()]
        for net in net_list:
            net_nid = _make_id("compose_network", str_path, net)
            nodes.append({
                "id": net_nid,
                "label": f"Docker:Network:{net}",
                "type": "docker_network",
                "file_type": "code",
                "network_name": net,
                "source_file": str_path,
                "source_location": "L1",
                "text": f"Docker network {net}",
            })
            edges.append({
                "source": svc_nid,
                "target": net_nid,
                "relation": "connects_to",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

        # Volumes
        vol_list: list[str] = []
        if isinstance(volumes, list):
            for v in volumes:
                if isinstance(v, str):
                    host_part = v.split(":", 1)[0].strip()
                    vol_list.append(host_part)
                elif isinstance(v, dict) and "source" in v:
                    vol_list.append(str(v["source"]).strip())
        for vol in vol_list:
            vol_nid = _make_id("compose_volume", str_path, vol)
            nodes.append({
                "id": vol_nid,
                "label": f"Docker:Volume:{vol}",
                "type": "docker_volume",
                "file_type": "code",
                "volume_name": vol,
                "source_file": str_path,
                "source_location": "L1",
                "text": f"Docker volume {vol}",
            })
            edges.append({
                "source": svc_nid,
                "target": vol_nid,
                "relation": "mounts",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

    return {"nodes": nodes, "edges": edges}
