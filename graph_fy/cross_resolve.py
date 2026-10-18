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


_HTTP_METHODS: frozenset[str] = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"})


def _split_method_and_path(raw: str) -> tuple[str | None, str]:
    """Extract leading HTTP method (if present) and clean path."""
    import re
    cleaned = raw.strip().strip("'\"`")
    m = re.match(r"^(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\s+(.*)$", cleaned, re.IGNORECASE)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return None, cleaned


def _normalize_route_path(path: str) -> str:
    """Normalize route path: strip method, protocol, host, query params, trailing slash, and unify :id / {id} / ${id} param syntax."""
    import re
    if not path:
        return ""
    _, path = _split_method_and_path(path)
    p = path.strip().strip("'\"`")
    p = p.split("?")[0]
    if "://" in p:
        after_scheme = p.split("://", 1)[1]
        p = "/" + after_scheme.split("/", 1)[-1] if "/" in after_scheme else "/"
    p = re.sub(r"\$\{[a-zA-Z0-9_.]+\}", "{}", p)
    p = re.sub(r"[:<][a-zA-Z0-9_]+>?", "{}", p)
    p = re.sub(r"\{[a-zA-Z0-9_]+\}", "{}", p)
    p = re.sub(r"/[0-9]+(?=/|$)", "/{}", p)
    p = re.sub(r"/[0-9a-fA-F\-]{16,}(?=/|$)", "/{}", p)
    p = "/" + p.strip("/")
    return p.lower()


def _discover_env_vars(candidate_dirs: list[Path]) -> dict[str, str]:
    """Scan candidate directories for .env* and docker-compose*.yml files to extract API and base URL configurations."""
    from urllib.parse import urlsplit
    env_vars: dict[str, str] = {}
    checked_dirs: set[Path] = set()

    for d in candidate_dirs:
        try:
            curr: Path | None = d.resolve() if d.is_dir() else d.parent.resolve()
        except Exception:
            continue
        depth = 0
        while curr and depth < 3 and curr not in checked_dirs:
            checked_dirs.add(curr)
            # Scan .env, .env.example, .env.local, .env.development, .env.production, etc.
            try:
                for env_file in curr.glob(".env*"):
                    if env_file.is_file():
                        try:
                            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                                line = line.strip()
                                if not line or line.startswith("#") or "=" not in line:
                                    continue
                                k, v = line.split("=", 1)
                                k = k.strip()
                                v = v.strip().strip("'\"`")
                                if any(t in k.upper() for t in ("API", "URL", "BASE", "PREFIX", "BACKEND", "GATEWAY")):
                                    parsed = urlsplit(v)
                                    path = parsed.path if parsed.netloc else v
                                    if path:
                                        env_vars[k] = "/" + path.strip("/")
                        except Exception:
                            pass
            except Exception:
                pass

            # Scan docker-compose.yml / yaml
            for compose_name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
                compose_file = curr / compose_name
                if compose_file.is_file():
                    try:
                        for line in compose_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                            line = line.strip().lstrip("- ")
                            if "=" in line:
                                k, v = line.split("=", 1)
                            elif ":" in line and not line.startswith("http"):
                                k, v = line.split(":", 1)
                            else:
                                continue
                            k = k.strip()
                            v = v.strip().strip("'\"`")
                            if any(t in k.upper() for t in ("API", "URL", "BASE", "PREFIX", "BACKEND", "GATEWAY")):
                                parsed = urlsplit(v)
                                path = parsed.path if parsed.netloc else v
                                if path:
                                    env_vars[k] = "/" + path.strip("/")
                    except Exception:
                        pass

            parent = curr.parent
            if parent == curr:
                break
            curr = parent
            depth += 1

    return env_vars


def _resolve_base_url_expr(expr: str, env_vars: dict[str, str]) -> str | None:
    """Resolve string literal, process.env / import.meta.env, or fallback in baseURL expression."""
    import re
    from urllib.parse import urlsplit

    if not expr:
        return None
    expr = expr.strip()

    # 1. String literal: '/api/v1' or "http://localhost:8000/api"
    m_lit = re.match(r"""^['"`]([^'"`]+)['"`]$""", expr)
    if m_lit:
        raw = m_lit.group(1).strip()
        parsed = urlsplit(raw)
        path = parsed.path if parsed.netloc else raw
        return "/" + path.strip("/") if path else None

    # 2. Dynamic env variable reference: process.env.VITE_API_URL or import.meta.env.NEXT_PUBLIC_API_URL
    m_env = re.search(r"""(?:process\.env|import\.meta\.env)\.([A-Za-z0-9_]+)""", expr)
    if m_env:
        var_name = m_env.group(1)
        if var_name in env_vars:
            return env_vars[var_name]

    # 3. Fallback expression: process.env.API || '/api/v1' or ?? '/api/v1'
    m_fb = re.search(r"""(?:\|\||\?\?)\s*['"`]([^'"`]+)['"`]""", expr)
    if m_fb:
        raw = m_fb.group(1).strip()
        parsed = urlsplit(raw)
        path = parsed.path if parsed.netloc else raw
        return "/" + path.strip("/") if path else None

    return None


def resolve_cross_boundary_api_routes(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Connect frontend HTTP client calls (fetch, axios, sdk) to backend route endpoints and OpenAPI operations.

    Discovers:
    1. Backend routes: OpenAPI operations, FastAPI/Flask/Express/NestJS decorated endpoints.
    2. Frontend client calls: fetch('/api/...'), axios.get('/api/...'), apiClient.post(...)
    When normalized paths and HTTP methods match, adds `routes_to` edges with confidence `INFERRED`
    (or `EXTRACTED` for direct OpenAPI matches) and annotates `http_method` and `context`.
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
    openapi_ops: list[tuple[dict[str, Any], str, str, str]] = []  # (node, method, path, op_id)

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

        if not method and route_path:
            m_meth, r_clean = _split_method_and_path(route_path)
            if m_meth:
                method = m_meth
                route_path = r_clean

        if not method and any(lbl.startswith(p) for p in ("Route:", "Endpoint:", "API:", "OpenAPI:")):
            rest = lbl.split(":", 1)[-1].strip()
            m_meth, rest_clean = _split_method_and_path(rest)
            if m_meth:
                method = m_meth
                if not route_path:
                    route_path = rest_clean

        # OpenAPI / API spec operations
        if ntype in ("openapi_operation", "openapi_path", "api_operation", "api_path") or lbl.startswith(("OpenAPI:", "API:")):
            if not route_path and ":" in lbl:
                rest = lbl.split(":", 1)[-1].strip()
                _, route_path = _split_method_and_path(rest)
            if route_path:
                norm_p = _normalize_route_path(route_path)
                m_eff = method or "GET"
                op_id = str(n.get("operation_id") or n.get("operationId") or "").lower()
                backend_routes.append((n, norm_p, m_eff, stem))
                openapi_ops.append((n, m_eff, norm_p, op_id))
        elif any(k in ntype for k in ("route", "endpoint", "api_handler", "rpc")):
            if route_path:
                backend_routes.append((n, _normalize_route_path(route_path), method or "GET", stem))
        elif any(lbl.startswith(prefix) for prefix in ("Route:", "Endpoint:")):
            if not route_path:
                rest = lbl.split(":", 1)[-1].strip()
                _, route_path = _split_method_and_path(rest)
            backend_routes.append((n, _normalize_route_path(route_path), method or "GET", stem))
        elif sf and (lbl.endswith("()") or n.get("_callable")):
            # Inspect definition line for route decorator (FastAPI, Flask, NestJS, Express, etc.)
            sloc = n.get("source_location", "")
            if sloc.startswith("L"):
                try:
                    lineno = int(sloc[1:]) - 1
                    lines = _get_file_lines(sf)
                    if lines and 0 <= lineno < len(lines):
                        start = max(0, lineno - 8)
                        snippet = "\n".join(lines[start:lineno + 1])
                        # Check FastAPI / Flask decorators
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
                            deco_path = m.group(2)
                            # Check for router prefix in file: APIRouter(prefix="/...")
                            full_file = "\n".join(lines[:lineno])
                            m_pref = re.search(r"""(?:APIRouter|Blueprint)\s*\([^)]*prefix\s*=\s*["']([^"']+)["']""", full_file)
                            if m_pref:
                                pref = m_pref.group(1).strip("/")
                                if not deco_path.strip("/").startswith(pref):
                                    deco_path = f"/{pref}/{deco_path.lstrip('/')}"
                            backend_routes.append((n, _normalize_route_path(deco_path), method or deco_meth, stem))
                        else:
                            # Check NestJS decorators: @Get('/path'), @Post('/path'), etc.
                            m_nest = re.search(
                                r"""@(Get|Post|Put|Delete|Patch|Options|Head)\s*\(\s*["']?([^"']*)["']?\s*\)""",
                                snippet,
                                re.IGNORECASE,
                            )
                            if m_nest:
                                deco_meth = m_nest.group(1).upper()
                                deco_path = m_nest.group(2) or "/"
                                full_file = "\n".join(lines[:lineno])
                                m_ctrl = re.search(r"""@Controller\s*\(\s*["']([^"']+)["']\s*\)""", full_file)
                                if m_ctrl:
                                    ctrl_prefix = m_ctrl.group(1).strip("/")
                                    deco_path = f"/{ctrl_prefix}/{deco_path.lstrip('/')}"
                                backend_routes.append((n, _normalize_route_path(deco_path), method or deco_meth, stem))
                            else:
                                # Check Express: (app|router).(get|post|put|delete)('/path', ...)
                                m_exp = re.search(
                                    r"""(?:app|router)\.(get|post|put|delete|patch|options|head)\s*\(\s*["']([^"']+)["']""",
                                    snippet,
                                    re.IGNORECASE,
                                )
                                if m_exp:
                                    deco_meth = m_exp.group(1).upper()
                                    deco_path = m_exp.group(2)
                                    backend_routes.append((n, _normalize_route_path(deco_path), method or deco_meth, stem))
                except Exception:
                    pass

    # 2. Track Client BaseURL & Instance configurations (with dynamic config and .env resolution)
    candidate_dirs: list[Path] = [Path.cwd()]
    if Path("graph_fy_out/.graphify_root").exists():
        try:
            groot = Path("graph_fy_out/.graphify_root").read_text(encoding="utf-8").strip()
            if groot:
                candidate_dirs.append(Path(groot))
        except Exception:
            pass
    for n in nodes:
        sf_cand = n.get("source_file", "")
        if sf_cand:
            candidate_dirs.append(Path(sf_cand))

    env_vars = _discover_env_vars(candidate_dirs)

    client_base_urls: dict[str, str] = {}
    file_base_urls: dict[str, str] = {}
    global_base_url: str = ""

    scanned_files: set[str] = set()
    for n in nodes:
        sf = n.get("source_file", "")
        if not sf or sf in scanned_files:
            continue
        scanned_files.add(sf)
        ext = Path(sf).suffix.lower()
        if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"):
            lines = _get_file_lines(sf)
            if not lines:
                continue
            text = "\n".join(lines)
            for m_inst in re.finditer(
                r"""(?:const|let|var|export\s+const)\s+([A-Za-z0-9_]+)\s*=\s*(?:axios\.create|createClient|new\s+\w*(?:Client|Api|Service))\s*\(\s*\{[^}]*base(?:URL|Path)\s*:\s*([^,\n}]+)""",
                text,
                re.DOTALL,
            ):
                var_name = m_inst.group(1)
                raw_expr = m_inst.group(2).strip()
                b_url = _resolve_base_url_expr(raw_expr, env_vars)
                if b_url:
                    client_base_urls[var_name] = b_url
                    file_base_urls[sf] = b_url

            m_glob = re.search(r"""(?:axios\.defaults\.baseURL|OpenAPI\.BASE)\s*=\s*([^;\n]+)""", text)
            if m_glob:
                raw_expr = m_glob.group(1).strip()
                b_url = _resolve_base_url_expr(raw_expr, env_vars)
                if b_url:
                    global_base_url = b_url

    # 3. Discover client callers: (node, client_path, method, resolved_path)
    client_callers: list[tuple[dict[str, Any], str, str, str]] = []
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
        method = str(n.get("http_method") or n.get("method") or meta.get("http_method") or "").upper()
        instance_var = ""

        if not client_path:
            txt = str(n.get("text", "")) or str(n.get("label", ""))
            client_match = re.search(
                r"""(?:fetch|([A-Za-z0-9_]+)\.(?:get|post|put|delete|patch|options|head)|axios(?:\.(?:get|post|put|delete|patch|options|head))?)\s*\(\s*[`'"]((?:\$\{[^}]+\}|\/|[a-zA-Z0-9_\-\/{}:])+)[`'"]""",
                txt,
            )
            if client_match:
                if client_match.group(1):
                    instance_var = client_match.group(1)
                client_path = client_match.group(2)
                method_match = re.search(r"\.(get|post|put|delete|patch|options|head)\b", txt, re.IGNORECASE)
                if method_match and not method:
                    method = method_match.group(1).upper()
                m_opt_meth = re.search(r"""method:\s*['"`](GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)['"`]""", txt, re.IGNORECASE)
                if m_opt_meth and not method:
                    method = m_opt_meth.group(1).upper()

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
                            m_meth = re.search(r"""\.(get|post|put|delete|patch|options|head)\b""", body, re.IGNORECASE)
                            if m_meth and not method:
                                method = m_meth.group(1).upper()
                            m_opt_meth = re.search(r"""method:\s*['"`](GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)['"`]""", body, re.IGNORECASE)
                            if m_opt_meth:
                                method = m_opt_meth.group(1).upper()
                        else:
                            m_fetch = re.search(r"""\bfetch\s*\(\s*['"`](\/[a-zA-Z0-9_\-\/{}:]+)['"`]""", body)
                            if m_fetch:
                                client_path = m_fetch.group(1)
                                m_meth = re.search(r"""method:\s*['"`](GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)['"`]""", body, re.IGNORECASE)
                                method = m_meth.group(1).upper() if m_meth else (method or "GET")
                            else:
                                m_call = re.search(
                                    r"""([A-Za-z0-9_]+)\.(get|post|put|delete|patch|options|head)\s*\(\s*['"`](\/[a-zA-Z0-9_\-\/{}:]+)['"`]""",
                                    body,
                                    re.IGNORECASE,
                                )
                                if m_call:
                                    instance_var = m_call.group(1)
                                    method = m_call.group(2).upper()
                                    client_path = m_call.group(3)
                except Exception:
                    pass

        if client_path:
            eff_method = method or "GET"
            # Check for leading environment variable reference in client_path
            m_lead = re.match(r"""^(?:\$\{)?(?:process\.env|import\.meta\.env)\.([A-Za-z0-9_]+)\}?(.*)""", client_path)
            if m_lead:
                vname = m_lead.group(1)
                tail = m_lead.group(2).lstrip("/")
                if vname in env_vars:
                    base = env_vars[vname].rstrip("/")
                    client_path = f"{base}/{tail}"
            # Resolve BaseURL if configured
            eff_base = client_base_urls.get(instance_var) or file_base_urls.get(sf) or global_base_url
            if eff_base and not client_path.startswith(eff_base):
                resolved_path = f"{eff_base.rstrip('/')}/{client_path.lstrip('/')}"
            else:
                resolved_path = client_path
            client_callers.append((n, client_path, eff_method, resolved_path))

    # Helper: Route matching
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

    # 4. Step 5.3: Link Backend Handlers to OpenAPI Operations via 'implements' edges
    if openapi_ops:
        for b_node, norm_b, b_method, b_stem in backend_routes:
            if b_node.get("type") in ("api_operation", "openapi_operation") or b_node.get("label", "").startswith(("API:", "OpenAPI:")):
                continue  # Skip OpenAPI spec nodes themselves
            b_lbl = b_node.get("label", "").lower().rstrip("()")
            for op_node, op_method, op_norm_path, op_id in openapi_ops:
                matched = False
                conf = "INFERRED"
                if (norm_b == op_norm_path or norm_b.endswith(op_norm_path) or op_norm_path.endswith(norm_b)):
                    if b_method in (op_method, "ALL") or op_method == "ALL":
                        matched = True
                if not matched and op_id and len(op_id) > 2:
                    op_clean = op_id.replace("_", "")
                    b_clean = b_lbl.replace("_", "")
                    if op_clean in b_clean or b_clean in op_clean:
                        matched = True
                        conf = "EXTRACTED"
                if matched:
                    key = (b_node["id"], op_node["id"], "implements")
                    if key not in seen_edges and b_node["id"] != op_node["id"]:
                        seen_edges.add(key)
                        edge = {
                            "source": b_node["id"],
                            "target": op_node["id"],
                            "relation": "implements",
                            "http_method": op_method,
                            "context": f"implements:{op_method}",
                            "confidence": conf,
                            "weight": 1.0,
                            "source_file": b_node.get("source_file", ""),
                            "source_location": b_node.get("source_location", "L1"),
                        }
                        edges.append(edge)
                        new_edges.append(edge)

    # 5. Connect Client Callers to Backend Routes & OpenAPI Operations
    for n, client_path, method, resolved_path in client_callers:
        for b_node, norm_b, b_method, b_stem in backend_routes:
            if n["id"] == b_node["id"]:
                continue
            matches_route = _routes_match(client_path, norm_b, b_stem) or _routes_match(resolved_path, norm_b, b_stem)
            if matches_route:
                # Step 5.1: Strict HTTP method compatibility
                method_compat = (
                    not method
                    or not b_method
                    or method == "ALL"
                    or b_method == "ALL"
                    or method == b_method
                )
                if method_compat:
                    key = (n["id"], b_node["id"], "routes_to")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        eff_verb = method if method and method != "ALL" else (b_method if b_method and b_method != "ALL" else "GET")
                        edge = {
                            "source": n["id"],
                            "target": b_node["id"],
                            "relation": "routes_to",
                            "http_method": eff_verb,
                            "context": f"routes_to:{eff_verb}",
                            "confidence": "INFERRED",
                            "weight": 1.0,
                            "source_file": n.get("source_file", ""),
                            "source_location": n.get("source_location", "L1"),
                        }
                        edges.append(edge)
                        new_edges.append(edge)

    # 6. Step 5.3: Connect Client SDK Functions to OpenAPI operations by operationId
    if openapi_ops:
        for n in nodes:
            if n.get("file_type") == "rationale":
                continue
            lbl = n.get("label", "").rstrip("()").lstrip(".")
            if not lbl or len(lbl) < 3:
                continue
            clean_lbl = lbl.lower().replace("_", "")
            sf = n.get("source_file", "").lower()
            # Only client/frontend/sdk files
            if not any(k in sf for k in ("client", "sdk", "frontend", "src", "api", "service")):
                continue
            for op_node, op_method, op_norm_path, op_id in openapi_ops:
                if not op_id or len(op_id) < 3:
                    continue
                clean_op_id = op_id.lower().replace("_", "")
                if clean_lbl == clean_op_id or clean_lbl.endswith(clean_op_id):
                    key = (n["id"], op_node["id"], "routes_to")
                    if key not in seen_edges and n["id"] != op_node["id"]:
                        seen_edges.add(key)
                        edge = {
                            "source": n["id"],
                            "target": op_node["id"],
                            "relation": "routes_to",
                            "http_method": op_method,
                            "context": f"routes_to:{op_method}",
                            "confidence": "EXTRACTED",
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

