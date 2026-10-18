"""Tests for graph_fy.exporters.dashboard."""
from __future__ import annotations

import html.parser
import shutil
import subprocess
from pathlib import Path

import pytest

from graph_fy.exporters.dashboard import export_dashboard, generate_dashboard_html


class _DashboardHTMLValidator(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.iframes = []
        self.buttons = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attr_dict = dict(attrs)
        if tag == "iframe":
            self.iframes.append(attr_dict)
        elif tag == "button":
            self.buttons.append(attr_dict)


def test_generate_dashboard_html_structure():
    content = generate_dashboard_html(callflow_file="custom-callflow.html", project_name="my_repo")
    assert "<!DOCTYPE html>" in content
    assert "Unified Multi-View Architecture Dashboard" in content
    assert "custom-callflow.html" in content
    assert "data-src=\"graph.html\"" in content
    assert "data-src=\"GRAPH_TREE.html\"" in content
    assert "src=\"architecture.html\"" in content
    assert "id=\"iframe-callflow\"" in content
    assert "toggleSplit()" in content
    assert "switchTab('arch')" in content
    assert "↗ Open in New Tab" in content

    # Validate HTML parser well-formedness
    parser = _DashboardHTMLValidator()
    parser.feed(content)
    assert len(parser.iframes) == 4
    # Check that 1 iframe has src and 3 have lazy data-src
    src_count = sum(1 for ifr in parser.iframes if "src" in ifr)
    data_src_count = sum(1 for ifr in parser.iframes if "data-src" in ifr)
    assert src_count == 1
    assert data_src_count == 3


def test_export_dashboard_file_generation(tmp_path: Path):
    out_dir = tmp_path / "graph_fy_out"
    out_dir.mkdir(parents=True)
    (out_dir / "custom-callflow.html").write_text("<div>callflow</div>", encoding="utf-8")

    out_file = out_dir / "dashboard.html"
    result_path = export_dashboard(out_file=out_file)

    assert result_path == out_file
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "custom-callflow.html" in content
    assert "view-container" in content


def test_export_dashboard_headless_chromium_smoke(tmp_path: Path):
    """Smoke test dashboard HTML inside headless Chromium if standalone CLI binary exists."""
    browser_bin = shutil.which("chromium") or shutil.which("google-chrome")

    if not browser_bin:
        pytest.skip("No standalone Chromium CLI binary in PATH")

    out_file = tmp_path / "dashboard.html"
    export_dashboard(out_file=out_file)

    proc = subprocess.run(
        [
            browser_bin,
            "--headless=new",
            "--disable-gpu",
            "--dump-dom",
            f"file://{out_file.resolve()}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0
    dom_output = proc.stdout
    assert "Unified Multi-View Architecture Dashboard" in dom_output
    assert "mainViewport" in dom_output
    assert "iframe-arch" in dom_output
