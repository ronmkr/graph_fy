"""Unit tests for full-stack contract resolution in cross_resolve.py (Track 5).

Tests:
1. HTTP Method Awareness: Strict separation of GET vs POST vs DELETE vs PUT.
2. BaseURL Resolution: Axios/client instance baseURL prefixing and relative path matching.
3. OpenAPI & Schema Integration: Canonical contract matching via operationId and OpenAPI nodes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graph_fy.cross_resolve import (
    _normalize_route_path,
    _split_method_and_path,
    resolve_cross_boundary_api_routes,
    resolve_cross_format_dependencies,
)


def _write(tmp_path: Path, rel_path: str, content: str) -> Path:
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_split_method_and_path_and_normalization():
    """Verify method splitting and parameter normalization."""
    meth, path = _split_method_and_path("GET /api/v1/users")
    assert meth == "GET"
    assert path == "/api/v1/users"

    meth, path = _split_method_and_path("POST   /checkout/pay")
    assert meth == "POST"
    assert path == "/checkout/pay"

    meth, path = _split_method_and_path("/no/method/here")
    assert meth is None
    assert path == "/no/method/here"

    # Route path normalization
    assert _normalize_route_path("GET /users/:id") == "/users/{}"
    assert _normalize_route_path("POST /users/{userId}") == "/users/{}"
    assert _normalize_route_path("/api/v1/items/${item.id}") == "/api/v1/items/{}"
    assert _normalize_route_path("https://api.example.com/v1/orders/12345?query=active") == "/v1/orders/{}"


def test_http_method_strict_matching_prevents_read_mutation_collision():
    """Verify that POST /users does not link to GET /users handler, but links to POST handler."""
    nodes = [
        # Backend handlers
        {
            "id": "backend_get_users",
            "label": "list_users()",
            "type": "route",
            "http_method": "GET",
            "http_path": "/api/v1/users",
            "source_file": "backend/api.py",
        },
        {
            "id": "backend_post_users",
            "label": "create_user()",
            "type": "route",
            "http_method": "POST",
            "http_path": "/api/v1/users",
            "source_file": "backend/api.py",
        },
        # Frontend callers
        {
            "id": "frontend_read_call",
            "label": "loadUsers",
            "type": "function",
            "text": "const res = await axios.get('/api/v1/users');",
            "source_file": "frontend/users.ts",
        },
        {
            "id": "frontend_mutation_call",
            "label": "submitUser",
            "type": "function",
            "text": "return axios.post('/api/v1/users', payload);",
            "source_file": "frontend/users.ts",
        },
    ]
    edges: list[dict[str, Any]] = []

    new_edges = resolve_cross_boundary_api_routes(nodes, edges)

    read_targets = [e["target"] for e in edges if e["source"] == "frontend_read_call" and e["relation"] == "routes_to"]
    mutation_targets = [e["target"] for e in edges if e["source"] == "frontend_mutation_call" and e["relation"] == "routes_to"]

    # GET must route strictly to backend_get_users
    assert "backend_get_users" in read_targets
    assert "backend_post_users" not in read_targets

    # POST must route strictly to backend_post_users
    assert "backend_post_users" in mutation_targets
    assert "backend_get_users" not in mutation_targets

    # HTTP method and context attributes annotated on edges
    read_edge = next(e for e in edges if e["source"] == "frontend_read_call" and e["target"] == "backend_get_users")
    assert read_edge["http_method"] == "GET"
    assert read_edge["context"] == "routes_to:GET"

    mutation_edge = next(e for e in edges if e["source"] == "frontend_mutation_call" and e["target"] == "backend_post_users")
    assert mutation_edge["http_method"] == "POST"
    assert mutation_edge["context"] == "routes_to:POST"


def test_base_url_and_client_instance_resolution(tmp_path):
    """Verify that client instance baseURL (/api/v1) is resolved when caller uses relative path (/users)."""
    # 1. Frontend file defining client instance with baseURL
    fe_code = """\
import axios from 'axios';

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 5000,
});

export function fetchUsers() {
  return api.get('/users');
}
"""
    fe_file = _write(tmp_path, "src/api/client.ts", fe_code)

    # 2. Backend file defining route
    be_code = """\
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")

@router.get("/users")
def get_all_users():
    return []
"""
    be_file = _write(tmp_path, "backend/routes.py", be_code)

    nodes = [
        {
            "id": "backend_get_all_users",
            "label": "get_all_users()",
            "_callable": True,
            "source_file": str(be_file),
            "source_location": "L6",
        },
        {
            "id": "frontend_fetch_users",
            "label": "fetchUsers()",
            "_callable": True,
            "source_file": str(fe_file),
            "source_location": "L8",
        },
    ]
    edges: list[dict[str, Any]] = []

    new_edges = resolve_cross_boundary_api_routes(nodes, edges)
    route_edges = [(e["source"], e["target"], e["relation"]) for e in edges if e.get("relation") == "routes_to"]

    assert ("frontend_fetch_users", "backend_get_all_users", "routes_to") in route_edges


def test_nestjs_controller_route_resolution(tmp_path):
    """Verify NestJS @Controller prefix + @Get/@Post decorators match frontend calls."""
    nest_code = """\
import { Controller, Get, Param } from '@nestjs/common';

@Controller('api/v1/orders')
export class OrdersController {
  @Get(':id')
  findOne(@Param('id') id: string) {
    return { orderId: id };
  }
}
"""
    nest_file = _write(tmp_path, "backend/src/orders.controller.ts", nest_code)

    nodes = [
        {
            "id": "nestjs_find_one",
            "label": "findOne()",
            "_callable": True,
            "source_file": str(nest_file),
            "source_location": "L6",
        },
        {
            "id": "frontend_get_order",
            "label": "getOrder",
            "type": "function",
            "text": "const res = await fetch('/api/v1/orders/992');",
            "source_file": "frontend/orders.ts",
        },
    ]
    edges: list[dict[str, Any]] = []

    new_edges = resolve_cross_boundary_api_routes(nodes, edges)
    route_edges = [(e["source"], e["target"], e["relation"]) for e in edges if e.get("relation") == "routes_to"]

    assert ("frontend_get_order", "nestjs_find_one", "routes_to") in route_edges


def test_openapi_contract_integration_implements_and_routes_to():
    """Verify that OpenAPI operation nodes link to backend handlers (implements) and client calls (routes_to)."""
    nodes = [
        # OpenAPI Operation Node
        {
            "id": "openapi_get_users_op",
            "label": "API:GET /api/v1/users",
            "type": "api_operation",
            "method": "GET",
            "path": "/api/v1/users",
            "operation_id": "list_users",
            "source_file": "docs/openapi.json",
        },
        # Backend handler implementing the operation
        {
            "id": "backend_list_users",
            "label": "list_users()",
            "type": "route",
            "http_method": "GET",
            "http_path": "/api/v1/users",
            "source_file": "backend/users.py",
        },
        # Generated client SDK method matching operationId
        {
            "id": "frontend_sdk_list_users",
            "label": "listUsers()",
            "type": "function",
            "source_file": "frontend/src/sdk/usersService.ts",
            "source_location": "L12",
        },
        # Direct fetch caller
        {
            "id": "frontend_direct_fetch",
            "label": "loadUsersComponent",
            "type": "component",
            "text": "fetch('/api/v1/users');",
            "source_file": "frontend/src/UsersView.tsx",
        },
    ]
    edges: list[dict[str, Any]] = []

    res_nodes, res_edges = resolve_cross_format_dependencies(nodes, edges)

    # 1. Backend handler implements OpenAPI operation
    implements_edges = [
        (e["source"], e["target"], e["relation"])
        for e in res_edges
        if e.get("relation") == "implements"
    ]
    assert ("backend_list_users", "openapi_get_users_op", "implements") in implements_edges

    # 2. Client SDK function routes to OpenAPI operation by operationId
    route_edges = [
        (e["source"], e["target"], e["relation"])
        for e in res_edges
        if e.get("relation") == "routes_to"
    ]
    assert ("frontend_sdk_list_users", "openapi_get_users_op", "routes_to") in route_edges

    # 3. Direct fetch routes to backend handler and OpenAPI operation
    assert ("frontend_direct_fetch", "backend_list_users", "routes_to") in route_edges
    assert ("frontend_direct_fetch", "openapi_get_users_op", "routes_to") in route_edges


def test_env_variable_base_url_resolution(tmp_path: Path):
    """Verify that baseURL with import.meta.env.VITE_API_URL resolves via .env file."""
    # Write .env file
    env_content = """\
# Environment settings
VITE_API_URL=http://localhost:5173/api/v2
PORT=3000
"""
    _write(tmp_path, ".env", env_content)

    # Write frontend client with dynamic baseURL
    fe_code = """\
import axios from 'axios';

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL,
});

export function listItems() {
  return apiClient.get('/items');
}
"""
    fe_file = _write(tmp_path, "src/api/items.ts", fe_code)

    # Write backend FastAPI router
    be_code = """\
from fastapi import APIRouter

router = APIRouter(prefix="/api/v2")

@router.get("/items")
def get_items():
    return []
"""
    be_file = _write(tmp_path, "backend/routes/items.py", be_code)

    nodes = [
        {
            "id": "backend_get_items",
            "label": "get_items()",
            "_callable": True,
            "source_file": str(be_file),
            "source_location": "L6",
        },
        {
            "id": "frontend_list_items",
            "label": "listItems()",
            "_callable": True,
            "source_file": str(fe_file),
            "source_location": "L8",
        },
    ]
    edges: list[dict[str, Any]] = []

    resolve_cross_boundary_api_routes(nodes, edges)
    route_edges = [(e["source"], e["target"], e["relation"]) for e in edges if e.get("relation") == "routes_to"]

    assert ("frontend_list_items", "backend_get_items", "routes_to") in route_edges


def test_docker_compose_and_fallback_base_url_resolution(tmp_path: Path):
    """Verify that baseURL with fallback or docker-compose environment resolves correctly."""
    # Write docker-compose.yml
    compose_content = """\
version: '3.8'
services:
  web:
    image: node:20
    environment:
      - BACKEND_API_URL=http://api-service:8080/api/v3
"""
    _write(tmp_path, "docker-compose.yml", compose_content)

    # Write frontend client with process.env and fallback
    fe_code = """\
import axios from 'axios';

const client = axios.create({
  baseURL: process.env.BACKEND_API_URL || '/api/fallback',
});

export function getOrders() {
  return client.get('/orders');
}
"""
    fe_file = _write(tmp_path, "frontend/client.js", fe_code)

    # Write backend router
    be_code = """\
from fastapi import APIRouter

router = APIRouter(prefix="/api/v3")

@router.get("/orders")
def fetch_orders():
    return []
"""
    be_file = _write(tmp_path, "server/orders.py", be_code)

    nodes = [
        {
            "id": "backend_fetch_orders",
            "label": "fetch_orders()",
            "_callable": True,
            "source_file": str(be_file),
            "source_location": "L6",
        },
        {
            "id": "frontend_get_orders",
            "label": "getOrders()",
            "_callable": True,
            "source_file": str(fe_file),
            "source_location": "L8",
        },
    ]
    edges: list[dict[str, Any]] = []

    resolve_cross_boundary_api_routes(nodes, edges)
    route_edges = [(e["source"], e["target"], e["relation"]) for e in edges if e.get("relation") == "routes_to"]

    assert ("frontend_get_orders", "backend_fetch_orders", "routes_to") in route_edges


def test_client_path_leading_env_variable(tmp_path: Path):
    """Verify that direct fetch calls with leading env variable resolve against backend."""
    _write(tmp_path, ".env", "REACT_APP_API_BASE=/api/v1\n")

    be_code = """\
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")

@router.post("/checkout")
def process_checkout():
    return {"status": "ok"}
"""
    be_file = _write(tmp_path, "backend/checkout.py", be_code)

    nodes = [
        {
            "id": "backend_process_checkout",
            "label": "process_checkout()",
            "_callable": True,
            "source_file": str(be_file),
            "source_location": "L6",
        },
        {
            "id": "frontend_submit_checkout",
            "label": "submitCheckout()",
            "type": "function",
            "text": "fetch(`${process.env.REACT_APP_API_BASE}/checkout`, { method: 'POST' });",
            "source_file": str(tmp_path / "frontend/Checkout.tsx"),
        },
    ]
    edges: list[dict[str, Any]] = []

    resolve_cross_boundary_api_routes(nodes, edges)
    route_edges = [(e["source"], e["target"], e["relation"]) for e in edges if e.get("relation") == "routes_to"]

    assert ("frontend_submit_checkout", "backend_process_checkout", "routes_to") in route_edges

