"""Tests for Track 4: Dynamic language realism, blast radius confidence tiers, and framework extractors."""
from __future__ import annotations

from pathlib import Path
import networkx as nx
import pytest

from graph_fy.extractors.frameworks import (
    extract_js_frameworks,
    extract_python_frameworks,
)
from graph_fy.impact import compute_impact, format_impact_report
from graph_fy.retrieval import diff_context


def test_fastapi_and_pytest_framework_extraction(tmp_path: Path):
    py_code = '''
from fastapi import FastAPI, Depends

app = FastAPI()

def get_db():
    return None

def get_current_user():
    return "admin"

@app.get("/users")
def list_users(db = Depends(get_db), current_user = Depends(get_current_user)):
    return []

def test_user_flow(client, db_session):
    assert True
'''
    py_file = tmp_path / "api.py"
    py_file.write_text(py_code, encoding="utf-8")

    result: dict = {
        "nodes": [
            {"id": "api::list_users", "label": "list_users", "file_type": "function", "source_file": "api.py", "source_location": "L12"},
            {"id": "api::test_user_flow", "label": "test_user_flow", "file_type": "function", "source_file": "api.py", "source_location": "L16"},
        ],
        "edges": [],
    }

    extract_python_frameworks(py_file, py_code, result)

    # FastAPI Depends extracted
    di_edges = [e for e in result["edges"] if e.get("context") == "fastapi_di"]
    assert len(di_edges) == 2
    targets = {e["target"] for e in di_edges}
    assert "get_db" in targets
    assert "get_current_user" in targets
    for e in di_edges:
        assert e["relation"] == "depends_on"

    # Pytest fixtures extracted
    fixture_edges = [e for e in result["edges"] if e.get("context") == "pytest_fixture"]
    assert len(fixture_edges) == 2
    fixture_targets = {e["target"] for e in fixture_edges}
    assert "client" in fixture_targets
    assert "db_session" in fixture_targets


def test_express_and_react_framework_extraction(tmp_path: Path):
    js_code = '''
const express = require('express');
const app = express();

app.use(authMiddleware);
app.use(corsHandler);

function UserDashboard() {
    const auth = useAuth();
    const session = useSession();
    const [count, setCount] = useState(0);
    return null;
}
'''
    js_file = tmp_path / "app.js"
    js_file.write_text(js_code, encoding="utf-8")

    result: dict = {
        "nodes": [
            {"id": "app_js", "label": "app.js", "file_type": "file", "source_file": "app.js", "source_location": "L1"},
            {"id": "app::UserDashboard", "label": "UserDashboard", "file_type": "function", "source_file": "app.js", "source_location": "L7"},
        ],
        "edges": [],
    }

    extract_js_frameworks(js_file, js_code, result)

    # Express middleware
    mw_edges = [e for e in result["edges"] if e.get("context") == "express_middleware"]
    assert len(mw_edges) == 2
    mw_targets = {e["target"] for e in mw_edges}
    assert "authMiddleware" in mw_targets
    assert "corsHandler" in mw_targets

    # React custom hooks (excluding useState)
    hook_edges = [e for e in result["edges"] if e.get("context") == "react_hook"]
    assert len(hook_edges) == 2
    hook_targets = {e["target"] for e in hook_edges}
    assert "useAuth" in hook_targets
    assert "useSession" in hook_targets
    assert "useState" not in hook_targets


def test_compute_impact_confidence_scoring(tmp_path: Path):
    G = nx.Graph()
    G.add_node("mod::safe_func", label="safe_func", source_file="safe.py")
    G.add_node("mod::caller", label="caller", source_file="caller.py")
    G.add_edge("mod::caller", "mod::safe_func", relation="calls")

    # Pure static path -> HIGH_CONFIDENCE
    impact_safe = compute_impact(G, "mod::safe_func", root=tmp_path)
    assert impact_safe["confidence_rating"] == "HIGH_CONFIDENCE"
    assert impact_safe["confidence_score"] == 1.0

    report_safe = format_impact_report(impact_safe)
    assert "[HIGH CONFIDENCE]" in report_safe

    # Dynamic reflection node -> DYNAMIC_RISK_DETECTED
    G.add_node("mod::dynamic_hub", label="getattr_dispatch", source_file="dynamic.py")
    G.add_edge("mod::dynamic_hub", "mod::safe_func", relation="calls")

    impact_dyn = compute_impact(G, "mod::safe_func", root=tmp_path)
    assert impact_dyn["confidence_rating"] == "DYNAMIC_RISK_DETECTED"
    assert impact_dyn["confidence_score"] < 1.0
    assert any("getattr" in r for r in impact_dyn["dynamic_risks"])

    report_dyn = format_impact_report(impact_dyn)
    assert "[DYNAMIC RISK DETECTED]" in report_dyn


def test_diff_context_confidence_and_disclaimer(tmp_path: Path):
    G = nx.Graph()
    G.add_node("app::route", label="register_route", source_file="app.py", source_location="L10")
    G.add_node("app::handler", label="handle_request", source_file="app.py", source_location="L30")
    G.add_edge("app::handler", "app::route", relation="calls")

    # Default format
    res_def = diff_context(graph=G, root_dir=tmp_path, format="default")
    assert "AST Confidence:" in res_def["prompt_context"]
    assert "confidence_tier" in res_def

    # GitHub PR format
    res_pr = diff_context(graph=G, root_dir=tmp_path, format="github-pr")
    ctx = res_pr["prompt_context"]
    assert "| **Confidence** |" in ctx
    assert "Static Blast Radius Grounding" in ctx
    assert "Dynamic dispatch, runtime reflection (`getattr`), and framework DI" in ctx
