"""Tests for cross-format dependency resolution and end-to-end tracing (Phase 7)."""
from __future__ import annotations

from pathlib import Path

from graph_fy.build import build_from_json
from graph_fy.extract import extract_dockerfile, extract_helm, extract_kubernetes, extract_terraform
from graph_fy.retrieval import trace


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_cross_format_dependency_chain_and_trace(tmp_path):
    """Test full dependency chain:
    Terraform module -> Helm release -> Helm Chart -> K8s Deployment -> container Image -> Dockerfile
    """
    # 1. Terraform defining a module and a helm_release
    tf_code = """\
module "web_app" {
  source = "./modules/web"
}

resource "helm_release" "app_release" {
  name  = "app-service"
  chart = "app-chart"
}

resource "kubernetes_deployment" "api_server" {
  metadata {
    name = "api-server"
  }
}

resource "aws_dynamodb_table" "main_table" {
  name = "app-items"
}
"""
    _write(tmp_path, "main.tf", tf_code)

    # 2. Helm chart files
    chart_yaml = """\
apiVersion: v2
name: app-chart
description: Application Helm chart
version: 1.0.0
"""
    deploy_tpl = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-server
  namespace: default
spec:
  replicas: 2
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: app
          image: myrepo/app:2.1.0
"""
    _write(tmp_path, "charts/app-chart/Chart.yaml", chart_yaml)
    _write(tmp_path, "charts/app-chart/templates/deployment.yaml", deploy_tpl)

    # 3. Dockerfile
    dockerfile_content = """\
FROM python:3.12-slim AS base
WORKDIR /app
COPY . .
CMD ["python", "main.py"]
"""
    _write(tmp_path, "app/Dockerfile", dockerfile_content)

    # 4. CloudFormation template with matching DynamoDB table
    cfn_yaml = """\
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  ItemsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: app-items
      BillingMode: PAY_PER_REQUEST
"""
    _write(tmp_path, "cloudformation.yaml", cfn_yaml)

    # Extract all files
    from graph_fy.extract import extract_cloudformation
    ext_tf = extract_terraform(tmp_path / "main.tf")
    ext_helm = extract_helm(tmp_path / "charts/app-chart/Chart.yaml")
    ext_k8s = extract_kubernetes(tmp_path / "charts/app-chart/templates/deployment.yaml")
    ext_docker = extract_dockerfile(tmp_path / "app/Dockerfile")
    ext_cfn = extract_cloudformation(tmp_path / "cloudformation.yaml")

    all_nodes = (
        ext_tf["nodes"]
        + ext_helm["nodes"]
        + ext_k8s["nodes"]
        + ext_docker["nodes"]
        + ext_cfn["nodes"]
    )
    all_edges = (
        ext_tf["edges"]
        + ext_helm["edges"]
        + ext_k8s["edges"]
        + ext_docker["edges"]
        + ext_cfn["edges"]
    )

    # Build merged graph (runs cross-format resolution)
    G = build_from_json({"nodes": all_nodes, "edges": all_edges}, directed=False)

    # Find nodes
    label_to_id = {attrs.get("label"): nid for nid, attrs in G.nodes(data=True)}

    # 1. Terraform helm_release -> Helm:Chart:app-chart
    helm_release_id = label_to_id.get("Helm:helm_release.app_release")
    helm_chart_id = label_to_id.get("Helm:Chart:app-chart")
    assert helm_release_id is not None
    assert helm_chart_id is not None
    assert G.has_edge(helm_release_id, helm_chart_id)

    # 2. Terraform kubernetes_deployment -> K8s:Deployment:api-server
    tf_k8s_id = label_to_id.get("K8s:kubernetes_deployment.api_server")
    k8s_deploy_id = label_to_id.get("K8s:Deployment:api-server")
    assert tf_k8s_id is not None
    assert k8s_deploy_id is not None
    assert G.has_edge(tf_k8s_id, k8s_deploy_id)

    # 3. K8s container image -> Docker build
    img_id = label_to_id.get("Image:myrepo/app:2.1.0")
    docker_build_id = label_to_id.get("Docker:Build:app")
    assert img_id is not None
    assert docker_build_id is not None
    assert G.has_edge(img_id, docker_build_id)

    # 4. Terraform DynamoDB table -> CloudFormation DynamoDB table
    tf_table_id = label_to_id.get("AWS:aws_dynamodb_table.main_table")
    cfn_table_id = label_to_id.get("AWS:DynamoDB::Table:ItemsTable")
    assert tf_table_id is not None
    assert cfn_table_id is not None
    assert G.has_edge(tf_table_id, cfn_table_id)

    # 5. Shortest path trace across the cross-format dependency chain
    # Path from Terraform helm release to Dockerfile build
    path_trace = trace(G, "Helm:helm_release.app_release", "Docker:Build:app", directed=False)
    assert len(path_trace) >= 3
    nodes_in_path = [path_trace[0]["from_node"]] + [step["to_node"] for step in path_trace]
    labels_in_path = [G.nodes[nid]["label"] for nid in nodes_in_path]
    assert "Helm:helm_release.app_release" in labels_in_path
    assert "Helm:Chart:app-chart" in labels_in_path
    assert "Docker:Build:app" in labels_in_path


def test_github_actions_to_infra_deploy_linking(tmp_path: Path):
    """Test GHA deploy step running terraform apply or helm upgrade connects to infra."""
    from graph_fy.extractors.github_actions import extract_github_workflow
    from graph_fy.extractors.iac import extract_helm
    from graph_fy.cross_resolve import resolve_cross_format_dependencies

    wf_yaml = """\
name: Deploy Infra
on: [push]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Terraform Apply
        run: terraform apply -chdir=terraform/prod -auto-approve
      - name: Deploy Helm
        run: helm upgrade --install my-app ./charts/my-app
"""
    _write(tmp_path, ".github/workflows/deploy.yml", wf_yaml)

    # Helm chart
    chart_yaml = """\
apiVersion: v2
name: my-app
version: 1.0.0
"""
    _write(tmp_path, "charts/my-app/Chart.yaml", chart_yaml)

    # Terraform dummy node
    tf_node = {
        "id": "tf_mod_prod",
        "label": "Terraform module: terraform/prod",
        "type": "module",
        "language": "terraform",
        "file_type": "code",
        "_terraform_directory": "terraform/prod",
        "source_file": str(tmp_path / "terraform/prod/main.tf"),
    }

    gha_res = extract_github_workflow(tmp_path / ".github/workflows/deploy.yml")
    helm_res = extract_helm(tmp_path / "charts/my-app/Chart.yaml")

    all_nodes = gha_res["nodes"] + helm_res["nodes"] + [tf_node]
    all_edges = gha_res["edges"] + helm_res["edges"]

    nodes, edges = resolve_cross_format_dependencies(all_nodes, all_edges)
    relations = {(e["source"], e["target"], e["relation"]) for e in edges}

    # Step:Terraform Apply -> tf_mod_prod with deploys
    tf_step = next(n for n in nodes if "Terraform Apply" in n.get("label", ""))
    assert (tf_step["id"], "tf_mod_prod", "deploys") in relations

    # Step:Deploy Helm -> Helm:Chart:my-app with deploys
    helm_step = next(n for n in nodes if "Deploy Helm" in n.get("label", ""))
    helm_chart = next(n for n in nodes if "Helm:Chart:my-app" in n.get("label", ""))
    assert (helm_step["id"], helm_chart["id"], "deploys") in relations


def test_secret_and_env_cross_referencing(tmp_path: Path):
    """Test secret and env variable cross-referencing across GHA, Docker Compose, and K8s."""
    from graph_fy.extractors.github_actions import extract_github_workflow
    from graph_fy.extractors.docker import extract_docker_compose
    from graph_fy.cross_resolve import (
        resolve_cross_format_dependencies,
        find_secret_and_env_cross_references,
    )

    wf_yaml = """\
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        env:
          DATABASE_URL: ${{ env.DB_HOST }}
          SECRET_TOKEN: ${{ secrets.AUTH_SECRET }}
        run: npm run build
"""
    _write(tmp_path, ".github/workflows/ci.yml", wf_yaml)

    compose_yaml = """\
version: '3.8'
services:
  web:
    image: node:18
    environment:
      - DATABASE_URL=postgres://localhost:5432
    secrets:
      - auth_secret
secrets:
  auth_secret:
    file: ./secrets/auth_secret.txt
"""
    _write(tmp_path, "docker-compose.yml", compose_yaml)

    # K8s Secret node
    k8s_secret_node = {
        "id": "k8s_secret_auth_secret",
        "label": "K8s:Secret:auth-secret",
        "resource_kind": "secret",
        "resource_name": "auth-secret",
        "type": "k8s_resource",
        "file_type": "code",
        "source_file": "k8s/secret.yaml",
    }

    gha_res = extract_github_workflow(tmp_path / ".github/workflows/ci.yml")
    docker_res = extract_docker_compose(tmp_path / "docker-compose.yml")

    all_nodes = gha_res["nodes"] + docker_res["nodes"] + [k8s_secret_node]
    all_edges = gha_res["edges"] + docker_res["edges"]

    nodes, edges = resolve_cross_format_dependencies(all_nodes, all_edges)
    ref_data = find_secret_and_env_cross_references(nodes, edges)

    # Secret references
    assert "auth_secret" in ref_data["secrets"]["referenced"] or "auth-secret" in ref_data["secrets"]["referenced"]
    assert "auth-secret" in ref_data["secrets"]["defined"] or "auth_secret" in ref_data["secrets"]["defined"]

    # Env references
    assert "database_url" in ref_data["envs"]["referenced"] or "db_host" in ref_data["envs"]["referenced"]


def test_resolve_cross_boundary_api_routes():
    """Verify that frontend fetch/axios calls resolve to backend route handlers via routes_to edges."""
    from graph_fy.cross_resolve import resolve_cross_format_dependencies

    nodes = [
        # Backend FastAPI/OpenAPI route
        {
            "id": "backend_get_users",
            "label": "get_user_by_id",
            "type": "route",
            "http_method": "GET",
            "http_path": "/api/v1/users/{id}",
            "source_file": "backend/api/users.py",
        },
        # Frontend component calling fetch
        {
            "id": "frontend_user_profile",
            "label": "UserProfileComponent",
            "type": "component",
            "text": "const res = await fetch('/api/v1/users/42');",
            "source_file": "frontend/src/UserProfile.tsx",
        },
        # Frontend component calling axios
        {
            "id": "frontend_edit_profile",
            "label": "editProfile",
            "type": "function",
            "text": "return axios.get('/api/v1/users/:id');",
            "source_file": "frontend/src/api.ts",
        },
    ]
    edges = []

    res_nodes, res_edges = resolve_cross_format_dependencies(nodes, edges)
    route_edges = [
        (e["source"], e["target"], e["relation"])
        for e in res_edges
        if e.get("relation") == "routes_to"
    ]

    assert ("frontend_user_profile", "backend_get_users", "routes_to") in route_edges
    assert ("frontend_edit_profile", "backend_get_users", "routes_to") in route_edges


def test_cross_boundary_full_stack_monorepo_route_resolution(tmp_path):
    """Test full-stack monorepo route resolution between Python backend and TS frontend client."""
    from graph_fy.cross_resolve import resolve_cross_boundary_api_routes

    # 1. Backend Python router file
    py_code = """\
from fastapi import APIRouter

router = APIRouter(tags=["auth"])

@router.post("/login/access-token")
def login_access_token(form_data):
    return {"token": "xyz"}
"""
    py_file = _write(tmp_path, "backend/app/api/routes/login.py", py_code)

    # 2. Frontend TypeScript SDK file
    ts_code = """\
export class LoginService {
    public static loginAccessToken(options) {
        return client.post({
            url: '/api/v1/login/access-token',
            ...options,
        });
    }
}
"""
    ts_file = _write(tmp_path, "frontend/src/client/sdk.gen.ts", ts_code)

    nodes = [
        {
            "id": "backend_login_access_token",
            "label": "login_access_token()",
            "_callable": True,
            "source_file": str(py_file),
            "source_location": "L6",
        },
        {
            "id": "frontend_login_service_login_access_token",
            "label": ".loginAccessToken()",
            "_callable": True,
            "source_file": str(ts_file),
            "source_location": "L2",
        },
    ]
    edges = []

    new_edges = resolve_cross_boundary_api_routes(nodes, edges)
    route_edges = [
        (e["source"], e["target"], e["relation"])
        for e in edges
        if e.get("relation") == "routes_to"
    ]

    assert ("frontend_login_service_login_access_token", "backend_login_access_token", "routes_to") in route_edges


