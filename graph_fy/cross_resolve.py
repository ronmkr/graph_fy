"""Cross-format dependency resolution (Terraform, Helm, Kubernetes, AWS, Docker).

Discovers and connects cross-format dependency chains across infrastructure
and code definitions:
1. Terraform helm_release / kubernetes_* -> Helm Chart / K8s nodes.
2. Helm Chart -> K8s resources defined by its templates.
3. K8s Deployment container image: -> Dockerfile / build / ECR resources.
4. CloudFormation and Terraform AWS resources sharing physical names / ARNs.
5. Terraform module -> child module resources.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graph_fy.extractors.protobuf import resolve_grpc_openapi_edges
from graph_fy.extractors.rtk import resolve_rtk_backend_edges


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return s.strip().strip('"\'').lower()


def resolve_cross_format_dependencies(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Inspect nodes and edges, resolving cross-format references in-place.

    Returns the mutated (nodes, edges) lists.
    """
    seen_edges: set[tuple[str, str, str]] = {
        (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        for e in edges
    }

    def _add_edge(src: str, tgt: str, relation: str, sf: str = "", loc: str = "L1") -> None:
        if not src or not tgt or src == tgt:
            return
        key = (src, tgt, relation)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": sf,
            "source_location": loc,
        })

    # Indices
    nodes_by_id = {n["id"]: n for n in nodes if "id" in n}

    # 1. Index Helm charts
    helm_charts: dict[str, str] = {}  # chart_name -> node_id
    for n in nodes:
        lbl = n.get("label", "")
        if lbl.startswith("Helm:Chart:"):
            chart_name = _norm(lbl.split(":", 2)[2])
            helm_charts[chart_name] = n["id"]
        elif n.get("type") == "helm_chart":
            c_name = _norm(n.get("chart_name") or lbl)
            if c_name:
                helm_charts[c_name] = n["id"]

    # 2. Index Kubernetes resources: (kind_lower, name_lower) -> node_id
    k8s_resources: dict[tuple[str, str], str] = {}
    k8s_nodes: list[dict[str, Any]] = []
    for n in nodes:
        lbl = n.get("label", "")
        if lbl.startswith("K8s:") or n.get("type") == "k8s_resource":
            k8s_nodes.append(n)
            kind = _norm(n.get("resource_kind", ""))
            name = _norm(n.get("resource_name", ""))
            if not kind or not name:
                parts = lbl.split(":")
                if len(parts) >= 3:
                    kind = _norm(parts[1])
                    name = _norm(parts[2].split("/")[-1])
            if kind and name:
                k8s_resources[(kind, name)] = n["id"]

    # 3. Index Dockerfile and build nodes
    docker_builds: list[dict[str, Any]] = []
    for n in nodes:
        if (
            n.get("type") in ("dockerfile", "docker_build")
            or n.get("label", "").startswith("Docker:")
            or str(n.get("source_file", "")).endswith("Dockerfile")
        ):
            docker_builds.append(n)

    # 4. Resolve Terraform -> Helm release and K8s resources
    for n in nodes:
        lbl = n.get("label", "")
        res_type = _norm(n.get("resource_type", ""))
        res_name = _norm(n.get("resource_name", ""))
        sf = n.get("source_file", "")
        loc = n.get("source_location", "L1")

        # Terraform helm_release
        if (
            res_type == "helm_release"
            or lbl.startswith("Helm:helm_release.")
            or lbl.startswith("helm_release.")
        ):
            chart_ref = _norm(n.get("chart"))
            chart_name = Path(chart_ref).name if chart_ref else res_name
            if not chart_name and "." in lbl:
                chart_name = _norm(lbl.split(".")[-1])
            if chart_name in helm_charts:
                _add_edge(n["id"], helm_charts[chart_name], "deploys", sf, loc)

        # Terraform kubernetes_* resources
        if res_type.startswith("kubernetes_") or "kubernetes_" in lbl:
            kind_cand = res_type.replace("kubernetes_", "").replace("_", "")
            if not kind_cand and "kubernetes_" in lbl:
                prefix = lbl.split("kubernetes_")[-1]
                kind_cand = prefix.split(".")[0].replace("_", "")
            target_name = _norm(n.get("name") or res_name)
            target_slug = target_name.replace("-", "_")
            for (k_kind, k_name), k_id in k8s_resources.items():
                if k_kind == kind_cand and (k_name == target_name or k_name.replace("-", "_") == target_slug):
                    _add_edge(n["id"], k_id, "deploys", sf, loc)

    # 4b. Helm chart -> K8s resources defined in templates
    for k_node in k8s_nodes:
        k_sf = str(k_node.get("source_file", "")).replace("\\", "/")
        if "/templates/" in k_sf:
            parts = k_sf.split("/templates/")[0].split("/")
            if parts:
                chart_dir_name = _norm(parts[-1])
                if chart_dir_name in helm_charts:
                    _add_edge(helm_charts[chart_dir_name], k_node["id"], "defines", k_node.get("source_file", ""), "L1")

    # 5. Connect K8s Deployment container image: -> Dockerfile / Image Build
    image_nodes: list[dict[str, Any]] = [
        n for n in nodes
        if n.get("type") == "image_ref" or n.get("label", "").startswith("Image:")
    ]
    target_builds = [db for db in docker_builds if db.get("type") == "docker_build"] or docker_builds
    for img_node in image_nodes:
        raw_tag = img_node.get("image_tag") or img_node.get("label", "").replace("Image:", "")
        # Parse image name e.g. "myrepo/app:2.1.0" -> "app"
        tag_no_version = raw_tag.split(":")[0] if ":" in raw_tag else raw_tag
        image_name = _norm(tag_no_version.split("/")[-1])
        sf = img_node.get("source_file", "")
        loc = img_node.get("source_location", "L1")

        # Match to Docker build
        for db in target_builds:
            db_sf = str(db.get("source_file", ""))
            db_img = _norm(db.get("image_name", ""))
            parent_dir = _norm(Path(db_sf).parent.name)

            # Match criteria: directory name matches image name or explicit image_name matches,
            # or single Dockerfile in repository
            if (
                image_name == db_img
                or image_name == parent_dir
                or (len(target_builds) <= 2 and parent_dir in (".", "", "graph_fy"))
            ):
                _add_edge(img_node["id"], db["id"], "built_by", sf, loc)
                break

    # 6. Connect CloudFormation and Terraform AWS resources
    tf_aws_nodes = [
        n for n in nodes
        if n.get("label", "").startswith("AWS:aws_") or (n.get("resource_type", "").startswith("aws_"))
    ]
    cfn_aws_nodes = [
        n for n in nodes
        if (
            (n.get("type") == "aws_resource" or n.get("label", "").startswith("AWS:"))
            and not n.get("label", "").startswith("AWS:aws_")
        )
    ]

    for tf_n in tf_aws_nodes:
        tf_name = _norm(
            tf_n.get("table_name")
            or tf_n.get("bucket")
            or tf_n.get("name")
            or tf_n.get("resource_name")
        )
        tf_sf = tf_n.get("source_file", "")
        tf_loc = tf_n.get("source_location", "L1")
        if not tf_name:
            continue

        for cfn_n in cfn_aws_nodes:
            cfn_phys = _norm(cfn_n.get("physical_name"))
            cfn_logic = _norm(cfn_n.get("logical_id") or cfn_n.get("resource_name"))
            cfn_lbl = _norm(cfn_n.get("label", ""))
            # Check if physical name, logical name, or label matches
            if (
                (cfn_phys and (tf_name == cfn_phys or tf_name.replace("-", "_") == cfn_phys.replace("-", "_")))
                or (cfn_logic and (tf_name == cfn_logic or tf_name.replace("-", "_") == cfn_logic.replace("-", "_")))
                or (tf_name and tf_name in cfn_lbl)
            ):
                _add_edge(tf_n["id"], cfn_n["id"], "shares_resource", tf_sf, tf_loc)

    # 7. Terraform module -> resources inside module directory
    module_nodes = [
        n for n in nodes
        if n.get("label", "").startswith("module.") or n.get("type") == "terraform_module"
    ]
    for mod_node in module_nodes:
        mod_src = mod_node.get("module_source")
        if not mod_src:
            continue
        mod_dir = Path(mod_src).name.lower()
        mod_sf = mod_node.get("source_file", "")
        mod_loc = mod_node.get("source_location", "L1")
        for target_node in nodes:
            tgt_sf = target_node.get("source_file", "").lower()
            if f"/{mod_dir}/" in tgt_sf or tgt_sf.endswith(f"/{mod_dir}"):
                if target_node["id"] != mod_node["id"]:
                    _add_edge(mod_node["id"], target_node["id"], "contains", mod_sf, mod_loc)

    # 8. Link CI/CD pipelines to infra (GitHub Actions -> Terraform / Helm)
    tf_roots = [
        n for n in nodes
        if (
            n.get("type") == "module" and n.get("language") == "terraform"
        ) or n.get("label", "").startswith("Terraform module:")
    ]
    workflow_steps = [
        n for n in nodes
        if n.get("type") == "workflow_step" or "run" in n or n.get("label", "").startswith("Step:")
    ]

    for step in workflow_steps:
        cmd = str(step.get("run") or "").lower()
        step_sf = step.get("source_file", "")
        step_loc = step.get("source_location", "L1")
        if not cmd:
            continue

        # Terraform deploy steps: `terraform apply`, `terraform init`, etc.
        if "terraform apply" in cmd or "terraform init" in cmd or "terraform plan" in cmd:
            chdir_match = None
            if "-chdir=" in cmd:
                parts = cmd.split("-chdir=", 1)[1].split()
                if parts:
                    chdir_match = _norm(parts[0])

            matched_tf = False
            if chdir_match:
                for tf_node in tf_roots:
                    tf_dir = _norm(tf_node.get("_terraform_directory", ""))
                    if chdir_match in tf_dir or tf_dir.endswith(chdir_match):
                        _add_edge(step["id"], tf_node["id"], "deploys", step_sf, step_loc)
                        matched_tf = True
                        break

            # Fallback: link to first/root Terraform module
            if not matched_tf and tf_roots:
                _add_edge(step["id"], tf_roots[0]["id"], "deploys", step_sf, step_loc)

        # Helm deploy steps: `helm upgrade`, `helm install`, `helm template`
        if "helm upgrade" in cmd or "helm install" in cmd:
            matched_helm = False
            for chart_name, chart_id in helm_charts.items():
                if chart_name and chart_name in cmd:
                    _add_edge(step["id"], chart_id, "deploys", step_sf, step_loc)
                    matched_helm = True
                    break
            # Fallback if only 1 chart exists
            if not matched_helm and len(helm_charts) == 1:
                single_chart_id = next(iter(helm_charts.values()))
                _add_edge(step["id"], single_chart_id, "deploys", step_sf, step_loc)

    # 8b. Link Argo CD Applications to Helm charts and K8s manifests
    argo_apps = [
        n for n in nodes
        if n.get("type") == "argocd_application" or n.get("label", "").startswith("Argo:Application:")
    ]
    for app in argo_apps:
        src_path = _norm(app.get("source_path", ""))
        app_sf = app.get("source_file", "")
        app_loc = app.get("source_location", "L1")
        if not src_path:
            continue

        # Match to Helm chart
        for chart_name, chart_id in helm_charts.items():
            if chart_name in src_path or src_path.endswith(chart_name):
                _add_edge(app["id"], chart_id, "manages", app_sf, app_loc)

        # Match to K8s resources by path
        for k_node in k8s_nodes:
            k_sf = _norm(k_node.get("source_file", ""))
            if src_path in k_sf:
                _add_edge(app["id"], k_node["id"], "manages", app_sf, app_loc)

    # 9. Secret & Environment Cross-Referencing
    # Canonical hub nodes for secrets and env vars across all formats
    canonical_secrets: dict[str, str] = {}  # norm(name) -> node_id
    canonical_envs: dict[str, str] = {}     # norm(name) -> node_id

    for n in nodes:
        lbl = n.get("label", "")
        norm_lbl = _norm(lbl)
        nid = n.get("id", "")
        if lbl.startswith("Secret:") or n.get("type") == "secret_ref":
            sec_name = _norm(n.get("secret_name") or lbl.replace("Secret:", ""))
            if sec_name:
                canonical_secrets.setdefault(sec_name, nid)
        elif lbl.startswith("Env:") or n.get("type") == "env_ref":
            env_name = _norm(n.get("env_name") or lbl.replace("Env:", ""))
            if env_name:
                canonical_envs.setdefault(env_name, nid)

    # Cross-link K8s Secrets to canonical secrets
    for k_node in k8s_nodes:
        kind = _norm(k_node.get("resource_kind", ""))
        name = _norm(k_node.get("resource_name", ""))
        if kind == "secret" and name:
            sec_nid = canonical_secrets.setdefault(name, k_node["id"])
            if sec_nid != k_node["id"]:
                _add_edge(k_node["id"], sec_nid, "defines_secret", k_node.get("source_file", ""), "L1")

        if kind == "configmap" and name:
            env_nid = canonical_envs.setdefault(name, k_node["id"])
            if env_nid != k_node["id"]:
                _add_edge(k_node["id"], env_nid, "defines_env", k_node.get("source_file", ""), "L1")

    # Cross-link Terraform secret resources to canonical secrets
    for n in nodes:
        res_type = _norm(n.get("resource_type", ""))
        res_name = _norm(n.get("resource_name", ""))
        if "secret" in res_type and res_name:
            clean_name = res_name.replace("_", "-")
            for sec_norm, sec_nid in canonical_secrets.items():
                if sec_norm == res_name or sec_norm == clean_name or sec_norm in res_name:
                    _add_edge(n["id"], sec_nid, "defines_secret", n.get("source_file", ""), "L1")

    # Cross-link Docker services and GHA steps to canonical secrets & envs
    for n in nodes:
        sf = n.get("source_file", "")
        loc = n.get("source_location", "L1")
        # Docker Compose services
        for s in n.get("secrets", []):
            norm_s = _norm(s)
            if norm_s in canonical_secrets:
                _add_edge(n["id"], canonical_secrets[norm_s], "references_secret", sf, loc)
        for ev in n.get("env_vars", []):
            ev_clean = ev.split("=")[0].strip() if "=" in ev else ev.strip()
            norm_ev = _norm(ev_clean)
            if norm_ev in canonical_envs:
                _add_edge(n["id"], canonical_envs[norm_ev], "references_env", sf, loc)

    # 7. Cross-resolve gRPC to OpenAPI gateway endpoints
    resolve_grpc_openapi_edges(nodes, edges)

    # 8. Cross-resolve RTK Query endpoints to backend routes
    resolve_rtk_backend_edges(nodes, edges)

    # 9. Cross-resolve generic frontend HTTP client calls (fetch, axios) to backend routes
    resolve_cross_boundary_api_routes(nodes, edges)

    return nodes, edges


def _normalize_route_path(path: str) -> str:
    """Normalize route path: strip protocol, host, query params, trailing slash, and unify :id / {id} param syntax."""
    import re
    p = path.strip().strip("'\"`")
    p = p.split("?")[0]
    if "://" in p:
        p = "/" + p.split("://", 1)[1].split("/", 1)[-1] if "/" in p.split("://", 1)[1] else "/"
    p = re.sub(r"[:<][a-zA-Z0-9_]+>?", "{}", p)
    p = re.sub(r"\{[a-zA-Z0-9_]+\}", "{}", p)
    p = re.sub(r"/[0-9]+(?=/|$)", "/{}", p)
    p = re.sub(r"/[0-9a-fA-F\-]{16,}(?=/|$)", "/{}", p)
    p = "/" + p.strip("/")
    return p.lower()


def resolve_cross_boundary_api_routes(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Connect frontend HTTP client calls (fetch, axios, sdk) to backend route endpoints.

    Discovers:
    1. Backend routes: OpenAPI operations, FastAPI/Flask/Express/NestJS decorated endpoints.
    2. Frontend client calls: fetch('/api/...'), axios.get('/api/...'), apiClient.post(...)
    When normalized paths match, adds `routes_to` edges with confidence `INFERRED`.
    """
    import re
    from pathlib import Path

    seen_edges: set[tuple[str, str, str]] = {
        (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        for e in edges
    }
    new_edges: list[dict[str, Any]] = []

    file_lines_cache: dict[str, list[str]] = {}

    def _get_file_lines(sf: str) -> list[str]:
        if sf not in file_lines_cache:
            p = Path(sf)
            if not p.is_file():
                if Path("graph_fy_out/.graphify_root").exists():
                    try:
                        groot = Path("graph_fy_out/.graphify_root").read_text(encoding="utf-8").strip()
                        if (Path(groot) / sf).is_file():
                            p = Path(groot) / sf
                    except Exception:
                        pass
            if p.is_file():
                try:
                    file_lines_cache[sf] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except Exception:
                    file_lines_cache[sf] = []
            else:
                file_lines_cache[sf] = []
        return file_lines_cache[sf]

    # 1. Collect backend route providers: (node, norm_path, method, file_stem)
    backend_routes: list[tuple[dict[str, Any], str, str, str]] = []
    for n in nodes:
        if n.get("file_type") == "rationale":
            continue
        ntype = str(n.get("type", "")).lower()
        lbl = str(n.get("label", ""))
        meta = n.get("metadata") or {}
        route_path = (
            n.get("path")
            or n.get("route")
            or n.get("http_route")
            or n.get("http_path")
            or meta.get("http_route")
            or meta.get("route")
            or meta.get("path")
            or ""
        )
        method = str(n.get("http_method") or n.get("method") or meta.get("http_method") or "").upper()
        sf = n.get("source_file", "")
        stem = Path(sf).stem if sf else ""

        if ntype in ("openapi_operation", "openapi_path") or lbl.startswith("OpenAPI:"):
            if not route_path and ":" in lbl:
                route_path = lbl.split(":")[-1]
            if route_path:
                backend_routes.append((n, _normalize_route_path(route_path), method or "GET", stem))
        elif any(k in ntype for k in ("route", "endpoint", "api_handler", "rpc")):
            if route_path:
                backend_routes.append((n, _normalize_route_path(route_path), method or "GET", stem))
        elif any(lbl.startswith(prefix) for prefix in ("Route:", "Endpoint:", "API:")):
            if not route_path:
                route_path = lbl.split(":", 1)[-1]
            backend_routes.append((n, _normalize_route_path(route_path), method or "GET", stem))
        elif sf and (lbl.endswith("()") or n.get("_callable")):
            # Inspect definition line for route decorator (FastAPI, Flask, NestJS, Express, etc.)
            sloc = n.get("source_location", "")
            if sloc.startswith("L"):
                try:
                    lineno = int(sloc[1:]) - 1
                    lines = _get_file_lines(sf)
                    if lines and 0 <= lineno < len(lines):
                        start = max(0, lineno - 6)
                        snippet = "\n".join(lines[start:lineno + 1])
                        m = re.search(
                            r"""@(?:router|api_router|app|blueprint|bp)\.(get|post|put|delete|patch|options|head|route|api_route)\s*\(\s*["']([^"']+)["']""",
                            snippet,
                            re.IGNORECASE,
                        )
                        if m:
                            deco_meth = m.group(1).upper()
                            if deco_meth in ("ROUTE", "API_ROUTE"):
                                m_meth = re.search(r"""methods\s*=\s*\[\s*["'](GET|POST|PUT|DELETE|PATCH)["']""", snippet, re.IGNORECASE)
                                deco_meth = m_meth.group(1).upper() if m_meth else "GET"
                            backend_routes.append((n, _normalize_route_path(m.group(2)), method or deco_meth, stem))
                except Exception:
                    pass

    if not backend_routes:
        return new_edges

    # 2. Discover client callers with route paths: (node, client_path, method)
    client_callers: list[tuple[dict[str, Any], str, str]] = []
    for n in nodes:
        if n.get("file_type") == "rationale":
            continue
        meta = n.get("metadata") or {}
        client_path = (
            n.get("target_url")
            or n.get("http_path")
            or n.get("api_path")
            or meta.get("target_url")
            or meta.get("api_path")
            or ""
        )
        method = str(n.get("http_method") or n.get("method") or meta.get("http_method") or "GET").upper()
        if not client_path:
            txt = str(n.get("text", "")) or str(n.get("label", ""))
            fetch_match = re.search(
                r"""(?:fetch|axios(?:\.(?:get|post|put|delete|patch))?)\s*\(\s*["'`](\/[a-zA-Z0-9_\-\/{}:]+)["'`]""",
                txt,
            )
            if fetch_match:
                client_path = fetch_match.group(1)
                if "axios." in txt:
                    method_match = re.search(r"axios\.(get|post|put|delete|patch)", txt)
                    if method_match:
                        method = method_match.group(1).upper()

        sf = n.get("source_file", "")
        lbl = n.get("label", "")
        if not client_path and sf and lbl.endswith("()") and "types" not in Path(sf).name.lower():
            sloc = n.get("source_location", "")
            if sloc.startswith("L"):
                try:
                    lineno = int(sloc[1:]) - 1
                    lines = _get_file_lines(sf)
                    if lines and 0 <= lineno < len(lines):
                        end = min(len(lines), lineno + 25)
                        body = "\n".join(lines[lineno:end])
                        m_url = re.search(r"""url:\s*['"`](\/[a-zA-Z0-9_\-\/{}:]+)['"`]""", body)
                        if m_url:
                            client_path = m_url.group(1)
                            m_meth = re.search(r"""\.(get|post|put|delete|patch)\b""", body, re.IGNORECASE)
                            if m_meth:
                                method = m_meth.group(1).upper()
                        elif "fetch" in body:
                            m_fetch = re.search(r"""fetch\s*\(\s*['"`](\/[a-zA-Z0-9_\-\/{}:]+)['"`]""", body)
                            if m_fetch:
                                client_path = m_fetch.group(1)
                                m_meth = re.search(r"""method:\s*['"`](GET|POST|PUT|DELETE|PATCH)['"`]""", body, re.IGNORECASE)
                                method = m_meth.group(1).upper() if m_meth else "GET"
                        elif "axios" in body:
                            m_axios = re.search(r"""axios\.(get|post|put|delete|patch)\s*\(\s*['"`](\/[a-zA-Z0-9_\-\/{}:]+)['"`]""", body)
                            if m_axios:
                                method = m_axios.group(1).upper()
                                client_path = m_axios.group(2)
                except Exception:
                    pass

        if client_path:
            client_callers.append((n, client_path, method))

    def _routes_match(c_path: str, b_path: str, b_stem: str) -> bool:
        norm_c = _normalize_route_path(c_path)
        norm_b = _normalize_route_path(b_path)
        if norm_c == norm_b:
            return True
        non_param_b = [p for p in norm_b.split("/") if p and p != "{}"]
        if norm_c.endswith(norm_b) and (non_param_b or (b_stem and b_stem in norm_c)):
            return True
        if norm_b.endswith(norm_c) and (non_param_b or (b_stem and b_stem in norm_b)):
            return True
        if b_stem:
            norm_b_prefixed = _normalize_route_path(f"/{b_stem}{b_path}")
            if norm_c == norm_b_prefixed or norm_c.endswith(norm_b_prefixed):
                return True
        return False

    for n, client_path, method in client_callers:
        for b_node, norm_b, b_method, b_stem in backend_routes:
            if _routes_match(client_path, norm_b, b_stem):
                if not method or not b_method or method == b_method or method == "ALL" or b_method == "ALL":
                    key = (n["id"], b_node["id"], "routes_to")
                    if key not in seen_edges and n["id"] != b_node["id"]:
                        seen_edges.add(key)
                        edge = {
                            "source": n["id"],
                            "target": b_node["id"],
                            "relation": "routes_to",
                            "confidence": "INFERRED",
                            "weight": 1.0,
                            "source_file": n.get("source_file", ""),
                            "source_location": n.get("source_location", "L1"),
                        }
                        edges.append(edge)
                        new_edges.append(edge)

    return new_edges


def find_secret_and_env_cross_references(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Index all defined and referenced secrets and environment variables across formats."""
    defined_secrets: set[str] = set()
    referenced_secrets: set[str] = set()
    defined_envs: set[str] = set()
    referenced_envs: set[str] = set()

    for n in nodes:
        lbl = n.get("label", "")
        # K8s
        if n.get("resource_kind", "").lower() == "secret":
            defined_secrets.add(_norm(n.get("resource_name", "")))
        if n.get("resource_kind", "").lower() == "configmap":
            defined_envs.add(_norm(n.get("resource_name", "")))
        # Terraform
        res_type = _norm(n.get("resource_type", ""))
        res_name = _norm(n.get("resource_name", ""))
        if "secret" in res_type and res_name:
            defined_secrets.add(res_name)

        # Docker Compose / GHA declarations
        if lbl.startswith("Secret:"):
            sec_name = _norm(lbl.replace("Secret:", ""))
            referenced_secrets.add(sec_name)
        if lbl.startswith("Env:"):
            env_name = _norm(lbl.replace("Env:", ""))
            referenced_envs.add(env_name)

    for e in edges:
        rel = e.get("relation", "")
        if rel == "defines_secret":
            defined_secrets.add(_norm(str(e.get("target", ""))))
        elif rel == "references_secret":
            referenced_secrets.add(_norm(str(e.get("target", ""))))
        elif rel == "defines_env":
            defined_envs.add(_norm(str(e.get("target", ""))))
        elif rel == "references_env":
            referenced_envs.add(_norm(str(e.get("target", ""))))

    return {
        "secrets": {
            "defined": sorted(defined_secrets),
            "referenced": sorted(referenced_secrets),
            "missing": sorted(referenced_secrets - defined_secrets),
        },
        "envs": {
            "defined": sorted(defined_envs),
            "referenced": sorted(referenced_envs),
            "missing": sorted(referenced_envs - defined_envs),
        },
    }

