"""tree_html — emit a D3 v7 collapsible-tree HTML view of a graph.

A self-contained printable / browseable tree-of-modules view
intended to complement the existing force-directed ``graph.html``.
Key visual elements:

  * Expand-all / collapse-all / reset-view buttons.
  * Multi-line label wrapping (``wrapText``) with separately-coloured
    name and descendant-count.
  * Depth-based colour palette (top-level directories get distinct
    accent colours; deeper levels follow a level-specific palette).
  * Click-to-toggle subtree.

Tree-data shape:

    {
      "name": "<root label>",
      "total_count": <int>,
      "children": [ { "name", "total_count", "children": [...] }, ... ]
    }

CLI: ``graph_fy tree [--graph PATH] [--output HTML] [--root PATH]
[--max-children N] [--label NAME]``.

Implementation notes:
  - ``total_count`` is the descendant leaf count, so collapsed nodes
    can show ``(Total Count: 95)`` without needing the children loaded.
  - ``--max-children`` (default 200) caps how many children render
    under any one node; a synthetic ``(+N more)`` leaf appears when the
    cap fires so very wide directories stay usable.
  - The first-level palette is auto-populated from the live top-level
    directories so each gets a stable accent colour.
"""

from __future__ import annotations

import html as _html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from graph_fy.templates import get_template

DEFAULT_MAX_CHILDREN = 200


# ── Tree builder (filesystem hierarchy → JSON) ──────────────────


def _common_root(paths: List[str]) -> str:
    if not paths:
        return ""
    parts = [Path(p).parts for p in paths if p]
    if not parts:
        return ""
    common = parts[0]
    for p in parts[1:]:
        i = 0
        while i < len(common) and i < len(p) and common[i] == p[i]:
            i += 1
        common = common[:i]
    return str(Path(*common)) if common else ""


def _make_truncation_leaf(extra: int) -> Dict[str, Any]:
    return {"name": f"(+{extra} more)", "total_count": extra, "children": []}


def build_tree(
    graph: Dict[str, Any],
    *,
    root: Optional[str] = None,
    max_children: int = DEFAULT_MAX_CHILDREN,
    project_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``{name, total_count, children}`` hierarchy.

    Each leaf is either a code symbol (class / top-level function) or
    a synthetic "(+N more)" placeholder for truncated wide directories.
    Each interior node carries ``total_count = sum of leaf counts``.

    Raises ``ValueError`` when an explicit ``root`` matches none of the
    graph's ``source_file`` paths — every file would silently attach flat
    to the root and the hierarchy would be lost (#2534).
    """
    nodes: List[Dict[str, Any]] = list(graph.get("nodes", []))
    file_nodes = [n for n in nodes if n.get("source_file")]
    if not file_nodes:
        return {"name": "(empty graph)", "total_count": 0, "children": []}

    explicit_root = root is not None
    if root is None:
        root = _common_root([n["source_file"] for n in file_nodes])
    root_path = Path(root)

    by_file: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for n in file_nodes:
        by_file[n["source_file"]].append(n)

    # Build dir tree.
    dir_index: Dict[str, Dict[str, Any]] = {}
    label_root = project_label or root_path.name or root or "/"
    root_node: Dict[str, Any] = {
        "name": label_root, "total_count": 0, "children": [],
    }
    dir_index[str(root_path)] = root_node

    def _ensure_dir(abs_path: Path) -> Dict[str, Any]:
        key = str(abs_path)
        if key in dir_index:
            return dir_index[key]
        if abs_path == abs_path.parent:
            return root_node
        parent = (_ensure_dir(abs_path.parent)
                  if abs_path.parent != abs_path else root_node)
        node = {"name": abs_path.name, "total_count": 0, "children": []}
        dir_index[key] = node
        parent["children"].append(node)
        return node

    matched_files = 0
    for src_file, syms in sorted(by_file.items()):
        src_path = Path(src_file)
        try:
            rel = src_path.relative_to(root_path)
            parent_path = (root_path / rel).parent
            matched_files += 1
        except ValueError:
            parent_path = root_path
        parent_dir = _ensure_dir(parent_path)

        # File node — children are the symbols.
        sym_children: List[Dict[str, Any]] = []
        for n in syms:
            label = n.get("label", n.get("id", "?"))
            # Skip the redundant file-name node graph_fy emits (bare basename or
            # the directory-qualified form from the #2032 disambiguation pass).
            if n.get("file_type") == "code":
                from graph_fy.build import _is_file_node_label
                if _is_file_node_label(label, src_file):
                    continue
            sym_children.append({
                "name": label,
                "total_count": 1,
                "children": [],
            })
        # Sort: code symbols first by name, then anything else.
        sym_children.sort(key=lambda c: (
            c["name"].startswith("_"),
            c["name"].lower(),
        ))
        if len(sym_children) > max_children:
            extra = len(sym_children) - max_children
            sym_children = sym_children[:max_children] + [
                _make_truncation_leaf(extra),
            ]
        file_node = {
            "name": src_path.name,
            "total_count": len(sym_children) or 1,
            "children": sym_children,
        }
        parent_dir["children"].append(file_node)

    # An explicit --root that matches NOTHING would silently flatten the whole
    # hierarchy (every file attaches to the root); the common case is passing an
    # absolute checkout path while source_file is stored repo-relative (#2534).
    # A PARTIAL match stays fine — files outside the root legitimately attach flat.
    if explicit_root and matched_files == 0:
        sample = min(by_file)
        raise ValueError(
            f"--root {root} matched 0 of {len(by_file)} source files "
            f"(source_file paths are stored repo-relative, e.g. {sample!r})"
        )

    # Sort each dir's children + propagate total_count up.
    def _finalise(d: Dict[str, Any]) -> int:
        kids = d.get("children") or []
        kids.sort(key=lambda c: (
            0 if (c.get("children") and len(c["children"]) > 0) else 1,
            c["name"].lower(),
        ))
        if not kids:
            return d.get("total_count") or 1
        n = 0
        for c in kids:
            n += _finalise(c)
        d["total_count"] = n or 1
        return d["total_count"]

    _finalise(root_node)
    return root_node


# ── HTML emitter (single-data-blob substitution) ──────────────────


# We emit a Python f-string with literal CSS/JS braces escaped as {{ }}.
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
  <style>
{tree_css}
  </style>
</head>
<body>
  <div class="app-header">
    <div class="brand-wrap">
      <span class="brand-badge">graph_fy // TREE</span>
      <h1>{header}</h1>
    </div>
    <button id="themeToggle" onclick="toggleTheme()" title="Toggle dark/light mode" style="padding:6px 12px; font-size:0.9rem;">🌓 Theme</button>
  </div>
  <div class="controls">
    <input id="tree-search" type="search" placeholder="Search tree (e.g. app, session)..." oninput="filterTree(this.value)" style="padding: 7px 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--text); font-size: 0.85rem; width: 260px; outline: none;" />
    <button onclick="expandAll()">Expand All</button>
    <button onclick="collapseAll()">Collapse All</button>
    <button onclick="resetView()">Reset View</button>
  </div>
  <div id="tree-container">
    <svg id="tree-svg" width="{svg_width}" height="{svg_height}"></svg>
  </div>

  <script src="https://d3js.org/d3.v7.min.js"></script>
  <script>
{tree_js}
  </script>
</body>
</html>
"""


def emit_html(
    tree: Dict[str, Any],
    *,
    title: str,
    header: str,
    svg_width: int = 6000,
    svg_height: int = 8000,
) -> str:
    # Escape </script> sequences so embedded JSON cannot break out of the
    # <script> tag, and HTML-escape values that land in <title>/<h1>.
    data_json = json.dumps(tree, ensure_ascii=True, separators=(",", ":")).replace("</", r"<\/")
    tree_css = get_template("tree.css")
    tree_js = get_template("tree.js").replace("__DATA_JSON__", data_json)
    return _HTML_TEMPLATE.format(
        title=_html.escape(title),
        header=_html.escape(header),
        svg_width=svg_width,
        svg_height=svg_height,
        tree_css=tree_css,
        tree_js=tree_js,
    )


def write_tree_html(
    graph_path: Path,
    output_path: Path,
    *,
    root: Optional[str] = None,
    max_children: int = DEFAULT_MAX_CHILDREN,
    project_label: Optional[str] = None,
    # kept for CLI compatibility with the older signature; ignored now
    top_k_edges: int = 0,
) -> Path:
    from graph_fy.security import check_graph_file_size_cap
    check_graph_file_size_cap(graph_path)
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    tree = build_tree(graph, root=root, max_children=max_children,
                      project_label=project_label)
    display_name = tree['name']
    if display_name in (".", "", "/"):
        if project_label:
            display_name = project_label
        elif root and Path(root).resolve().name:
            display_name = Path(root).resolve().name
        elif graph_path.parent.name == "graph_fy_out" and graph_path.parent.parent.name:
            display_name = graph_path.parent.parent.name
        else:
            display_name = "Project"
    title = f"{display_name} — graph_fy tree viewer"
    header = f"{display_name} — Knowledge Graph"
    html = emit_html(tree, title=title, header=header)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
