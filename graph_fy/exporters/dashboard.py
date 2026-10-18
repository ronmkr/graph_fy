"""Unified multi-view architecture dashboard exporter for graph_fy.

Stitches together:
  1. Architecture Map (architecture.html)
  2. Force Graph (graph.html)
  3. Execution Callflow (<project>-callflow.html / graph_fy-callflow.html)
  4. Symbol Tree (GRAPH_TREE.html)
into a unified responsive dashboard supporting single-tab navigation and 2x2 split-screen view.
"""
from __future__ import annotations

from pathlib import Path
from graph_fy.templates import get_template


def generate_dashboard_html(callflow_file: str = "graph_fy-callflow.html", project_name: str = "project") -> str:
    """Generate the unified multi-view architecture dashboard HTML."""
    dash_css = get_template("dashboard.css")
    dash_js = get_template("dashboard.js").replace("__CALLFLOW_FILE__", callflow_file)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>graph_fy Unified Multi-View Architecture Dashboard</title>
<style>
{dash_css}
</style>
</head>
<body>
  <header>
    <a class="brand" href="#">
      <span>graph_fy</span>
      <span class="brand-pill">Unified Multi-View</span>
    </a>
    <div class="tabs">
      <button id="tab-arch" class="tab-btn active" onclick="switchTab('arch')">🗺️ Architecture Map</button>
      <button id="tab-graph" class="tab-btn" onclick="switchTab('graph')">🌐 Force Graph</button>
      <button id="tab-callflow" class="tab-btn" onclick="switchTab('callflow')">🔀 Execution Callflow</button>
      <button id="tab-tree" class="tab-btn" onclick="switchTab('tree')">🌲 Symbol Tree</button>
    </div>
    <div class="controls">
      <a id="extLink" class="ext-link" href="architecture.html" target="_blank" title="Open current view in new window/tab">↗ Open in New Tab</a>
      <button id="splitBtn" class="split-btn" onclick="toggleSplit()">⊞ Split 2×2 View</button>
    </div>
  </header>
  <main id="mainViewport">
    <div id="view-arch" class="view-container active">
      <span class="grid-badge">1. Architecture Map</span>
      <iframe id="iframe-arch" src="architecture.html"></iframe>
    </div>
    <div id="view-graph" class="view-container">
      <span class="grid-badge">2. Force Graph</span>
      <iframe id="iframe-graph" data-src="graph.html"></iframe>
    </div>
    <div id="view-callflow" class="view-container">
      <span class="grid-badge">3. Execution Callflow</span>
      <iframe id="iframe-callflow" data-src="{callflow_file}"></iframe>
    </div>
    <div id="view-tree" class="view-container">
      <span class="grid-badge">4. Symbol Tree</span>
      <iframe id="iframe-tree" data-src="GRAPH_TREE.html"></iframe>
    </div>
  </main>
  <script>
{dash_js}
</script>
</body>
</html>
"""


def export_dashboard(out_file: Path | str, graph_path: Path | str | None = None) -> Path:
    """Generate and write the unified multi-view architecture dashboard HTML file."""
    out_path = Path(out_file)
    out_dir = out_path.parent
    graph_p = Path(graph_path) if graph_path else out_dir / "graph.json"

    proj_name = out_dir.parent.name or "project"
    callflow_file = "graph_fy-callflow.html"
    for cand in [f"{proj_name}-callflow.html", "graph_fy-callflow.html", "callflow.html"]:
        if (out_dir / cand).exists():
            callflow_file = cand
            break
    else:
        any_callflow = sorted(out_dir.glob("*-callflow.html"))
        if any_callflow:
            callflow_file = any_callflow[0].name

    html_content = generate_dashboard_html(callflow_file=callflow_file, project_name=proj_name)
    out_path.write_text(html_content, encoding="utf-8")
    return out_path
