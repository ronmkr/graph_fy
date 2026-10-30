"""Benchmark & Verification Suite for Framework & Microservice Lineage Features.

Evaluates performance, extraction speed, token compression, and blast radius
traversal across framework lineage extractors:
1. Structural YAML Extraction (#68)
2. Pydantic V2 & SQLAlchemy Schema Lineage (#60)
3. Angular Component Template HTML-to-TypeScript Reactive Binding (#59)
4. Spring Cloud Feign & RPC Cross-Microservice Resolution (#58)
"""
from __future__ import annotations

import time
import tempfile
from pathlib import Path
import networkx as nx

from graph_fy.extractors.yaml_config import extract_yaml
from graph_fy.extractors.python import extract_python
from graph_fy.extractors.angular import extract_angular_template
from graph_fy.extractors.java import extract_java
from graph_fy.resolvers.feign_rpc import resolve_spring_feign_rpc
from graph_fy.affected import affected_nodes


CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def benchmark_yaml_extraction(tmp_path: Path) -> dict[str, float]:
    """Scenario 1: Structural YAML extraction with written spelling preservation & line accuracy."""
    doc = """
name: CI/CD Pipeline
on:
  push:
    branches: [main, release/*]
  pull_request:
    branches: [main]

env:
  NODE_ENV: production
  DEBUG: "false"

jobs:
  build:
    name: Build & Package
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Setup Node
        uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'
      - run: npm ci
      - run: npm run build
  test:
    name: Unit & Integration Tests
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run Tests
        run: npm test -- --coverage
""" * 5  # Realistic multi-job workflow

    f = tmp_path / "ci.yaml"
    f.write_text(doc, encoding="utf-8")

    t0 = time.perf_counter()
    res = extract_yaml(f)
    t1 = time.perf_counter()

    nodes = res.get("nodes", [])
    raw_tokens = estimate_tokens(doc)
    ast_tokens = estimate_tokens(str(nodes))

    return {
        "duration_ms": (t1 - t0) * 1000.0,
        "nodes_count": float(len(nodes)),
        "raw_tokens": float(raw_tokens),
        "ast_tokens": float(ast_tokens),
        "compression_ratio": raw_tokens / max(1, ast_tokens),
    }


def benchmark_pydantic_sqlalchemy_lineage(tmp_path: Path) -> dict[str, float]:
    """Scenario 2: Pydantic V2 and SQLAlchemy bidirectional schema lineage."""
    py_code = """
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class Organization(Base):
    __tablename__ = "organizations"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    users = relationship("User", back_populates="org")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"))
    username = Column(String(50), nullable=False)
    email = Column(String(100), nullable=False)
    org = relationship("Organization", back_populates="users")
    orders = relationship("Order", back_populates="user")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    total_amount = Column(Integer)
    user = relationship("User", back_populates="orders")

class UserRequestSchema(BaseModel):
    username: str
    email: str
    age: int

    @field_validator("username", "email")
    def validate_identity(cls, v: str) -> str:
        return v.strip().lower()

    @model_validator(mode="after")
    def validate_all(self):
        return self
"""
    f = tmp_path / "models.py"
    f.write_text(py_code, encoding="utf-8")

    t0 = time.perf_counter()
    res = extract_python(f)
    t1 = time.perf_counter()

    edges = res.get("edges", [])
    val_edges = [e for e in edges if e.get("relation") == "validates_field"]
    rel_edges = [e for e in edges if e.get("relation") == "relates_to_model"]
    fk_edges = [e for e in edges if e.get("relation") == "foreign_key_to"]

    raw_tokens = estimate_tokens(py_code)

    return {
        "duration_ms": (t1 - t0) * 1000.0,
        "validator_edges": float(len(val_edges)),
        "relationship_edges": float(len(rel_edges)),
        "foreign_key_edges": float(len(fk_edges)),
        "raw_tokens": float(raw_tokens),
    }


def benchmark_angular_template_binding(tmp_path: Path) -> dict[str, float]:
    """Scenario 3: Angular HTML-to-TypeScript template reactive binding."""
    ts_code = """
import { Component, signal } from '@angular/core';

@Component({
  selector: 'app-pet-dashboard',
  templateUrl: './pet-dashboard.component.html'
})
export class PetDashboardComponent {
  pets = signal<any[]>([]);
  isLoading = signal<boolean>(false);
  filterTerm = '';

  onSelectPet(pet: any): void {}
  deletePet(id: number): void {}
  reloadList(): void {}
}
"""
    html_code = """
<div class="dashboard-container" *ngIf="!isLoading()">
  <header>
    <h1>Pet Directory</h1>
    <input [(ngModel)]="filterTerm" [placeholder]="'Search pets...'" />
    <button (click)="reloadList()">Refresh</button>
  </header>
  <main>
    <ul>
      <li *ngFor="let pet of pets()" [class.active]="pet.isSelected">
        <span (click)="onSelectPet(pet)">{{ pet.name }} ({{ pet.species }})</span>
        <button [disabled]="isLoading()" (click)="deletePet(pet.id)">Delete</button>
      </li>
    </ul>
  </main>
</div>
"""
    ts_f = tmp_path / "pet-dashboard.component.ts"
    html_f = tmp_path / "pet-dashboard.component.html"
    ts_f.write_text(ts_code, encoding="utf-8")
    html_f.write_text(html_code, encoding="utf-8")

    t0 = time.perf_counter()
    res = extract_angular_template(html_f)
    t1 = time.perf_counter()

    edges = res.get("edges", [])
    bind_edges = [e for e in edges if e.get("relation") == "template_binds_to"]

    # Test reverse blast radius speed
    g = nx.DiGraph()
    for n in res.get("nodes", []):
        g.add_node(n["id"], **n)
    for e in edges:
        g.add_edge(e["source"], e["target"], **e)

    target_id = bind_edges[0]["target"] if bind_edges else ""
    tb0 = time.perf_counter()
    hits = affected_nodes(g, target_id, depth=2) if target_id else []
    tb1 = time.perf_counter()

    return {
        "duration_ms": (t1 - t0) * 1000.0,
        "binding_edges": float(len(bind_edges)),
        "blast_radius_traversal_ms": (tb1 - tb0) * 1000.0,
        "affected_template_hits": float(len(hits)),
    }


def benchmark_spring_feign_rpc(tmp_path: Path) -> dict[str, float]:
    """Scenario 4: Spring Cloud Feign & RPC cross-microservice resolution."""
    feign_code = """
package com.example.client;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.*;

@FeignClient(name = "order-service", path = "/api/v1/orders")
public interface OrderClient {
    @GetMapping("/{orderId}")
    OrderDTO getOrder(@PathVariable("orderId") Long id);

    @PostMapping("/checkout")
    OrderDTO checkout(@RequestBody CartDTO cart);

    @DeleteMapping("/{orderId}")
    void cancelOrder(@PathVariable("orderId") Long id);
}
"""
    controller_code = """
package com.example.server;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/orders")
public class OrderApiController {
    @GetMapping("/{id}")
    public OrderDTO fetchOrder(@PathVariable Long id) { return null; }

    @PostMapping("/checkout")
    public OrderDTO processCheckout(@RequestBody CartDTO cart) { return null; }

    @DeleteMapping("/{id}")
    public void deleteOrder(@PathVariable Long id) {}
}
"""
    c_f = tmp_path / "OrderClient.java"
    s_f = tmp_path / "OrderApiController.java"
    c_f.write_text(feign_code, encoding="utf-8")
    s_f.write_text(controller_code, encoding="utf-8")

    c_res = extract_java(c_f)
    s_res = extract_java(s_f)
    nodes = list(c_res.get("nodes", [])) + list(s_res.get("nodes", []))
    edges = list(c_res.get("edges", [])) + list(s_res.get("edges", []))

    t0 = time.perf_counter()
    resolve_spring_feign_rpc(nodes, edges, root=tmp_path)
    t1 = time.perf_counter()

    feign_edges = [e for e in edges if e.get("relation") == "calls_feign_rpc"]

    # Reverse blast radius
    g = nx.DiGraph()
    for n in nodes:
        g.add_node(n["id"], **n)
    for e in edges:
        g.add_edge(e["source"], e["target"], **e)

    ctrl_target = feign_edges[0]["target"] if feign_edges else ""
    tb0 = time.perf_counter()
    hits = affected_nodes(g, ctrl_target, depth=2) if ctrl_target else []
    tb1 = time.perf_counter()

    return {
        "duration_ms": (t1 - t0) * 1000.0,
        "resolved_rpc_edges": float(len(feign_edges)),
        "blast_radius_traversal_ms": (tb1 - tb0) * 1000.0,
        "affected_client_hits": float(len(hits)),
    }


def test_framework_lineage_benchmark_suite(tmp_path: Path) -> None:
    yaml_bench = benchmark_yaml_extraction(tmp_path)
    py_bench = benchmark_pydantic_sqlalchemy_lineage(tmp_path)
    ng_bench = benchmark_angular_template_binding(tmp_path)
    rpc_bench = benchmark_spring_feign_rpc(tmp_path)

    # Assertions on functional correctness & speed
    assert yaml_bench["duration_ms"] < 50.0
    assert yaml_bench["nodes_count"] >= 10.0

    assert py_bench["duration_ms"] < 100.0
    assert py_bench["validator_edges"] >= 2.0
    assert py_bench["relationship_edges"] >= 6.0
    assert py_bench["foreign_key_edges"] >= 2.0

    assert ng_bench["duration_ms"] < 25.0
    assert ng_bench["binding_edges"] >= 6.0
    assert ng_bench["blast_radius_traversal_ms"] < 5.0
    assert ng_bench["affected_template_hits"] >= 1.0

    assert rpc_bench["duration_ms"] < 25.0
    assert rpc_bench["resolved_rpc_edges"] == 3.0
    assert rpc_bench["blast_radius_traversal_ms"] < 5.0
    assert rpc_bench["affected_client_hits"] >= 1.0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        y = benchmark_yaml_extraction(p)
        py = benchmark_pydantic_sqlalchemy_lineage(p)
        ng = benchmark_angular_template_binding(p)
        rpc = benchmark_spring_feign_rpc(p)

        print("\n=== Framework & Microservice Lineage Benchmark Results ===\n")
        print("1. Structural YAML Extraction (#68):")
        print(f"   - Extraction Latency: {y['duration_ms']:.2f} ms")
        print(f"   - Nodes Extracted: {int(y['nodes_count'])}")
        print(f"   - Raw vs AST Tokens: {int(y['raw_tokens'])} -> {int(y['ast_tokens'])} ({y['compression_ratio']:.1f}x compression)")

        print("\n2. Pydantic V2 & SQLAlchemy Schema Lineage (#60):")
        print(f"   - Extraction Latency: {py['duration_ms']:.2f} ms")
        print(f"   - Validator Edges (validates_field): {int(py['validator_edges'])}")
        print(f"   - Bidirectional Relationship Edges (relates_to_model): {int(py['relationship_edges'])}")
        print(f"   - Foreign Key Edges (foreign_key_to): {int(py['foreign_key_edges'])}")

        print("\n3. Angular Template Reactive Binding (#59):")
        print(f"   - Extraction Latency: {ng['duration_ms']:.2f} ms")
        print(f"   - Binding Edges (template_binds_to): {int(ng['binding_edges'])}")
        print(f"   - Blast Radius Traversal Latency: {ng['blast_radius_traversal_ms']:.3f} ms")
        print(f"   - Affected Template Hits: {int(ng['affected_template_hits'])}")

        print("\n4. Spring Cloud Feign & RPC Cross-Microservice Resolution (#58):")
        print(f"   - Resolution Latency: {rpc['duration_ms']:.2f} ms")
        print(f"   - Resolved RPC Edges (calls_feign_rpc): {int(rpc['resolved_rpc_edges'])}")
        print(f"   - Blast Radius Traversal Latency: {rpc['blast_radius_traversal_ms']:.3f} ms")
        print(f"   - Affected Client Caller Hits: {int(rpc['affected_client_hits'])}")

