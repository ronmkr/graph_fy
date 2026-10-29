"""Tests for Cross-Layer Configuration & Environment Variable Lineage (Issue #28)."""
from __future__ import annotations

from pathlib import Path

from graph_fy.extractors.docker import extract_dockerfile, extract_docker_compose
from graph_fy.extractors.iac import extract_kubernetes
from graph_fy.extractors.github_actions import extract_github_workflow
from graph_fy.extractors.terraform import extract_terraform
from graph_fy.extractors.python import extract_python
from graph_fy.extractors.javascript import extract_js
from graph_fy.extractors.go import extract_go
from graph_fy.extractors.rust import extract_rust
from graph_fy.extractors.java import extract_java
from graph_fy.resolvers.config_lineage import resolve_config_lineage


def _write(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_dockerfile_and_python_lineage(tmp_path: Path):
    dockerfile = _write(tmp_path, "Dockerfile", """\
FROM python:3.12-slim
ENV DATABASE_URL=postgres://user:pass@localhost:5432/db
ARG BUILD_ID=123
WORKDIR /app
COPY . .
CMD ["python", "main.py"]
""")

    py_file = _write(tmp_path, "main.py", """\
import os

def connect_db():
    db_url = os.environ["DATABASE_URL"]
    return db_url

def get_build():
    return os.getenv("BUILD_ID", "default")
""")

    ext_docker = extract_dockerfile(dockerfile)
    ext_py = extract_python(py_file)

    # Check config_decl nodes in Docker
    docker_decls = [n for n in ext_docker["nodes"] if n.get("type") == "config_decl"]
    decl_names = {n["config_name"] for n in docker_decls}
    assert "DATABASE_URL" in decl_names
    assert "BUILD_ID" in decl_names

    # Check config_key nodes in Python
    py_keys = [n for n in ext_py["nodes"] if n.get("type") == "config_key"]
    key_names = {n["config_name"] for n in py_keys}
    assert "DATABASE_URL" in key_names
    assert "BUILD_ID" in key_names

    # Run resolution
    all_nodes = ext_docker["nodes"] + ext_py["nodes"]
    all_edges = ext_docker["edges"] + ext_py["edges"]
    new_edges = resolve_config_lineage(all_nodes, all_edges, root=tmp_path)

    lineage_edges = [e for e in all_edges if e.get("relation") == "reads_config_from"]
    assert len(lineage_edges) >= 2

    targets = {e["target"] for e in lineage_edges}
    db_node = next(n for n in docker_decls if n["config_name"] == "DATABASE_URL")
    build_node = next(n for n in docker_decls if n["config_name"] == "BUILD_ID")

    assert db_node["id"] in targets
    assert build_node["id"] in targets


def test_docker_compose_and_ts_lineage(tmp_path: Path):
    compose_file = _write(tmp_path, "docker-compose.yml", """\
services:
  web:
    image: node:20
    environment:
      - API_PORT=8080
      - REDIS_URL=redis://localhost:6379
""")

    ts_file = _write(tmp_path, "server.ts", """\
export function startServer() {
    const port = process.env.API_PORT;
    const redis = process.env["REDIS_URL"];
    return { port, redis };
}
""")

    ext_compose = extract_docker_compose(compose_file)
    ext_ts = extract_js(ts_file)

    all_nodes = ext_compose["nodes"] + ext_ts["nodes"]
    all_edges = ext_compose["edges"] + ext_ts["edges"]
    resolve_config_lineage(all_nodes, all_edges, root=tmp_path)

    lineage_edges = [e for e in all_edges if e.get("relation") == "reads_config_from"]
    assert len(lineage_edges) >= 2

    compose_decls = [n for n in ext_compose["nodes"] if n.get("type") == "config_decl"]
    decl_ids = {n["id"] for n in compose_decls}
    target_ids = {e["target"] for e in lineage_edges}

    # Verify both API_PORT and REDIS_URL targets are in target_ids
    assert not decl_ids.isdisjoint(target_ids)


def test_kubernetes_and_go_lineage(tmp_path: Path):
    k8s_file = _write(tmp_path, "k8s.yaml", """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  namespace: default
data:
  LOG_LEVEL: "info"
  SERVICE_PORT: "9000"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend
  namespace: default
spec:
  template:
    spec:
      containers:
      - name: api
        image: backend:latest
        env:
        - name: K8S_POD_NAME
          value: "backend-pod"
""")

    go_file = _write(tmp_path, "main.go", """\
package main

import "os"

func InitLogger() string {
    level := os.Getenv("LOG_LEVEL")
    pod := os.Getenv("K8S_POD_NAME")
    return level + pod
}
""")

    ext_k8s = extract_kubernetes(k8s_file)
    ext_go = extract_go(go_file)

    all_nodes = ext_k8s["nodes"] + ext_go["nodes"]
    all_edges = ext_k8s["edges"] + ext_go["edges"]
    resolve_config_lineage(all_nodes, all_edges, root=tmp_path)

    lineage_edges = [e for e in all_edges if e.get("relation") == "reads_config_from"]
    assert len(lineage_edges) >= 2

    k8s_decls = {n["config_name"]: n["id"] for n in ext_k8s["nodes"] if n.get("type") == "config_decl"}
    targets = {e["target"] for e in lineage_edges}

    assert k8s_decls["LOG_LEVEL"] in targets
    assert k8s_decls["K8S_POD_NAME"] in targets


def test_terraform_and_rust_lineage(tmp_path: Path):
    tf_file = _write(tmp_path, "main.tf", """\
variable "database_url" {
  type    = string
  default = "postgres://localhost"
}

output "api_endpoint" {
  value = "https://api.example.com"
}
""")

    rs_file = _write(tmp_path, "main.rs", """\
use std::env;

fn run() {
    let db = env::var("DATABASE_URL").unwrap();
    let endpoint = std::env::var("API_ENDPOINT").unwrap();
}
""")

    ext_tf = extract_terraform(tf_file)
    ext_rs = extract_rust(rs_file)

    all_nodes = ext_tf["nodes"] + ext_rs["nodes"]
    all_edges = ext_tf["edges"] + ext_rs["edges"]
    resolve_config_lineage(all_nodes, all_edges, root=tmp_path)

    lineage_edges = [e for e in all_edges if e.get("relation") == "reads_config_from"]
    assert len(lineage_edges) >= 2


def test_github_actions_and_java_lineage(tmp_path: Path):
    gha_file = _write(tmp_path, ".github/workflows/deploy.yml", """\
name: Deploy
on: push
env:
  DEPLOY_ENV: staging
  APP_SECRET: secret123
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo deploy
""")

    java_file = _write(tmp_path, "Config.java", """\
package com.example;

public class Config {
    public void setup() {
        String env = System.getenv("DEPLOY_ENV");
        String secret = System.getenv("APP_SECRET");
    }
}
""")

    ext_gha = extract_github_workflow(gha_file)
    ext_java = extract_java(java_file)

    all_nodes = ext_gha["nodes"] + ext_java["nodes"]
    all_edges = ext_gha["edges"] + ext_java["edges"]
    resolve_config_lineage(all_nodes, all_edges, root=tmp_path)

    lineage_edges = [e for e in all_edges if e.get("relation") == "reads_config_from"]
    assert len(lineage_edges) >= 2


def test_spring_value_and_viper_lineage(tmp_path: Path):
    from graph_fy.extractors.argocd import extract_argocd

    argo_file = _write(tmp_path, "argo-app.yaml", """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: billing-service
spec:
  source:
    repoURL: https://github.com/org/repo
    path: charts/billing
    helm:
      parameters:
        - name: app.jwt.secret
          value: mysecret
        - name: database.port
          value: "5432"
""")

    java_file = _write(tmp_path, "BillingService.java", """\
package com.example.billing;

import org.springframework.beans.factory.annotation.Value;

public class BillingService {
    @Value("${app.jwt.secret:default-secret}")
    private String jwtSecret;
}
""")

    go_file = _write(tmp_path, "config.go", """\
package config

import "github.com/spf13/viper"

func GetPort() string {
    return viper.GetString("database.port")
}
""")

    ext_argo = extract_argocd(argo_file)
    ext_java = extract_java(java_file)
    ext_go = extract_go(go_file)

    all_nodes = ext_argo["nodes"] + ext_java["nodes"] + ext_go["nodes"]
    all_edges = ext_argo["edges"] + ext_java["edges"] + ext_go["edges"]
    resolve_config_lineage(all_nodes, all_edges, root=tmp_path)

    lineage_edges = [e for e in all_edges if e.get("relation") == "reads_config_from"]
    assert len(lineage_edges) >= 2
