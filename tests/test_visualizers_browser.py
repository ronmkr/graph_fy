"""Visualizer template validation and browser smoke tests for graph_fy.

Verifies:
1. All extracted CSS and JS templates in graph_fy/templates/ are valid syntax.
2. Generated HTML dashboards (architecture, callflow, tree, dashboard) are well-formed.
3. Responsive viewport meta tags and media queries are present.
4. Optional headless browser verification when playwright is available.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from graph_fy.describe_arch_html import generate_architecture_html
from graph_fy.exporters.dashboard import generate_dashboard_html
from graph_fy.templates import get_template
from graph_fy.tree_html import emit_html


def test_templates_load_and_validate_syntax():
    """All decoupled templates must load and pass strict JS/CSS syntax validation."""
    template_names = [
        "architecture.css",
        "architecture.js",
        "callflow.css",
        "callflow.js",
        "tree.css",
        "tree.js",
        "dashboard.css",
        "dashboard.js",
    ]
    for name in template_names:
        content = get_template(name)
        assert len(content.strip()) > 0, f"Template {name} is empty"

    # Validate JavaScript syntax with Node if available
    node_bin = shutil.which("node")
    if node_bin:
        js_templates = {
            "callflow.js": get_template("callflow.js"),
            "architecture.js": get_template("architecture.js")
            .replace("__DATA_JSON__", "{}")
            .replace("__MERMAID_CODE_TD__", '""')
            .replace("__MERMAID_CODE_LR__", '""'),
            "tree.js": get_template("tree.js").replace("__DATA_JSON__", "{}"),
            "dashboard.js": get_template("dashboard.js").replace(
                "__CALLFLOW_FILE__", '""'
            ),
        }
        for js_name, js_code in js_templates.items():
            proc = subprocess.run(
                [node_bin, "--check", "-"],
                input=js_code,
                text=True,
                capture_output=True,
            )
            assert proc.returncode == 0, f"JS syntax error in {js_name}: {proc.stderr}"

    # Validate CSS brace balance
    css_templates = [
        "architecture.css",
        "callflow.css",
        "tree.css",
        "dashboard.css",
    ]
    for css_name in css_templates:
        css_text = get_template(css_name)
        assert css_text.count("{") == css_text.count("}"), (
            f"Brace mismatch in {css_name}: {css_text.count('{')} open vs {css_text.count('}')} close"
        )
        assert ":root" in css_text or "*" in css_text, f"{css_name} missing root/reset styles"


def test_generated_html_structure_and_viewport():
    """Generated visualizers must include responsive viewport meta and non-empty styles."""
    sample_arch = {
        "project_name": "TestProject",
        "summary": "Architecture summary for test.",
        "stats": {
            "total_files": 1,
            "total_symbols": 2,
            "total_edges": 1,
            "stages_count": 1,
            "steps_count": 1,
            "symbols_count": 2,
            "sinks_count": 0,
        },
        "stages": [
            {
                "id": "init",
                "name": "Initialization",
                "color": "#38bdf8",
                "icon": "🚀",
                "description": "System bootstrap",
                "files_count": 1,
                "symbols_count": 2,
                "steps": [
                    {
                        "id": "step_1",
                        "step_number": "1.1",
                        "name": "Load Config",
                        "description": "Loads config",
                        "input_type": "path",
                        "output_type": "dict",
                        "category_tag": "config",
                        "sinks": [],
                        "symbols": [
                            {
                                "id": "cfg",
                                "label": "load_cfg",
                                "kind": "function",
                                "file": "main.py",
                                "signature": "def load_cfg()",
                                "docstring": "Loads config file.",
                                "calls": [],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    arch_html = generate_architecture_html(sample_arch)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0">' in arch_html
    assert "<style>" in arch_html and "</style>" in arch_html
    assert "<script>" in arch_html and "</script>" in arch_html
    assert "TestProject" in arch_html

    # 2. Tree HTML
    sample_tree = {"name": "root", "total_count": 1, "children": []}
    tree_html = emit_html(sample_tree, title="Tree Test", header="Tree Test")
    assert '<meta charset="UTF-8">' in tree_html
    assert "<style>" in tree_html
    assert "d3.v7.min.js" in tree_html

    # 3. Dashboard HTML
    dash_html = generate_dashboard_html()
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0"/>' in dash_html
    assert '<iframe id="iframe-arch" src="architecture.html"></iframe>' in dash_html
    assert '<iframe id="iframe-graph" data-src="graph.html"></iframe>' in dash_html
    assert '<iframe id="iframe-callflow"' in dash_html
    assert '<iframe id="iframe-tree"' in dash_html


def test_dashboard_responsive_media_queries():
    """All visualizer CSS must define responsive breakpoints for mobile/tablet."""
    css_files = ["architecture.css", "callflow.css", "tree.css", "dashboard.css"]
    for name in css_files:
        css = get_template(name)
        assert "@media" in css, f"{name} must contain responsive media query breakpoints"


def test_browser_headless_smoke_test(tmp_path: Path):
    """Smoke test running headless browser if playwright is installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed; static DOM and syntax tests passed.")

    dash_file = tmp_path / "dashboard.html"
    dash_file.write_text(generate_dashboard_html(), encoding="utf-8")

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Test across standard responsive viewports
        viewports = [
            {"width": 390, "height": 844},   # Mobile (iPhone 14)
            {"width": 768, "height": 1024},  # Tablet (iPad)
            {"width": 1440, "height": 900},  # Desktop
            {"width": 2560, "height": 1440}, # Ultrawide
        ]
        for vp in viewports:
            page = browser.new_page(viewport=vp)
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(f"file://{dash_file.resolve()}")
            # Switch tabs and toggle split
            page.click("#tab-callflow")
            page.click("#splitBtn")
            page.close()
        browser.close()

    assert not errors, f"Browser console errors encountered: {errors}"
