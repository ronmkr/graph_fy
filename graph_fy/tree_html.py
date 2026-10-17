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
    :root {{
      --bg: #f8f9fa;
      --surface: #ffffff;
      --border: #dadce0;
      --text: #202124;
      --muted: #5f6368;
      --accent: #0b57d0;
      --accent-hover: #0842a0;
      --accent-subtle: #e8f0fe;
    }}
    [data-theme="dark"] {{
      --bg: #121212;
      --surface: #1e1e1e;
      --border: #333333;
      --text: #e8eaed;
      --muted: #9aa0a6;
      --accent: #8ab4f8;
      --accent-hover: #aecbfa;
      --accent-subtle: #1f2b3e;
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) {{
        --bg: #121212;
        --surface: #1e1e1e;
        --border: #333333;
        --text: #e8eaed;
        --muted: #9aa0a6;
        --accent: #8ab4f8;
        --accent-hover: #aecbfa;
        --accent-subtle: #1f2b3e;
      }}
    }}
    body {{
      font-family: Roboto, "Google Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      margin: 0;
      padding: 0;
      background-color: var(--bg);
      color: var(--text);
      overflow-x: hidden;
    }}
    .app-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 24px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
      gap: 16px;
      box-shadow: 0 1px 2px 0 rgba(60,64,67,0.3), 0 1px 3px 1px rgba(60,64,67,0.15);
    }}
    .brand-wrap {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand-badge {{
      background: #0b57d0;
      color: #ffffff;
      font-size: 11px;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 6px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      font-family: Roboto, "Google Sans", sans-serif;
    }}
    h1 {{
      margin: 0;
      font-size: 1.25rem;
      font-weight: 500;
      font-family: Roboto, "Google Sans", sans-serif;
      color: var(--text);
      letter-spacing: -0.01em;
    }}
    .controls {{
      margin: 16px 24px 0 24px;
      display: flex;
      gap: 8px;
    }}
    button {{
      padding: 8px 16px;
      background: var(--surface);
      color: var(--accent);
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 0.85rem;
      font-weight: 500;
      font-family: Roboto, "Google Sans", sans-serif;
      cursor: pointer;
      transition: all 0.15s ease;
      box-shadow: 0 1px 2px rgba(60,64,67,0.08);
    }}
    button:hover {{
      border-color: var(--accent);
      color: var(--accent);
      background: var(--surface);
      box-shadow: 0 1px 3px rgba(60,64,67,0.15);
    }}
    button:active {{
      background: var(--accent-subtle);
      transform: translateY(1px);
    }}
    #tree-container {{
      width: calc(100vw - 48px);
      height: 80vh;
      overflow: auto;
      border-radius: 12px;
      background: var(--surface);
      margin: 16px 24px 24px 24px;
      box-shadow: 0 1px 3px rgba(60,64,67,0.12), 0 1px 2px rgba(60,64,67,0.24);
      border: 1px solid var(--border);
      position: relative;
    }}
    svg {{
      background: var(--surface);
      border-radius: 12px;
      display: block;
    }}
    .node circle {{
      stroke-width: 2px;
      transition: all 0.2s ease;
    }}
    .node text {{
      font: 12px Roboto, -apple-system, sans-serif;
      fill: var(--text);
      paint-order: stroke fill;
      stroke: var(--surface);
      stroke-width: 3.5px;
      stroke-linejoin: round;
      stroke-opacity: 0.95;
    }}
    .link {{
      fill: none;
      stroke: var(--border);
      stroke-opacity: 0.7;
      stroke-width: 1.5px;
    }}
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
    const initialJsonData = {data_json};

    function transformData(jsonData) {{
        // Helper function to recursively build the children structure
        function processNode(node, parentL1StageName) {{
            let displayName = node.name;
            // Append total_count if it exists and is not already in the name
            if (node.total_count !== undefined) {{
                if (!/\(Total Count: \d+\)$/.test(displayName)) {{
                    displayName += ` (Total Count: ${{node.total_count}})`;
                }}
            }}

            const newNode = {{ name: displayName }};

            if (parentL1StageName === "Root") {{
                 newNode.originalStageName = node.name;
            }} else {{
                newNode.originalStageName = parentL1StageName;
            }}

            if (node.children && node.children.length > 0) {{
                const stageNameToPass = (parentL1StageName === "Root") ? node.name : parentL1StageName;
                newNode.children = node.children.map(child => processNode(child, stageNameToPass));
            }}

            return newNode;
        }}

        let rootDisplayName = jsonData.name;
        if (jsonData.total_count !== undefined && !/\(Total Count: \d+\)$/.test(rootDisplayName)) {{
            rootDisplayName += ` (Total Count: ${{jsonData.total_count}})`;
        }}

        return {{
            name: rootDisplayName,
            originalStageName: "Root",
            children: (jsonData.children || []).map(child => processNode(child, "Root"))
        }};
    }}

    const treeData = transformData(initialJsonData);

    // Auto-populated phaseColors: every depth-1 child of the root gets
    // a stable colour from a bigger palette so all top-level dirs are
    // distinguishable.
    const PALETTE = [
      ["#1a73e8","#174ea6","#d2e3fc"],
      ["#1e8e3e","#137333","#ceead6"],
      ["#d93025","#a50e0e","#fad2cf"],
      ["#f9ab00","#b06000","#feefc3"],
      ["#9334e6","#681da8","#f3e8fd"],
      ["#0097a7","#006064","#b2ebf2"],
      ["#e8710a","#b34700","#fedfc8"],
      ["#e52592","#9c166f","#fce4ec"],
      ["#3f51b5","#283593","#c5cae9"],
      ["#00acc1","#00838f","#e0f7fa"],
      ["#5f6368","#3c4043","#dadce0"],
      ["#c2185b","#880e4f","#f8bbd0"],
      ["#1565c0","#0d47a1","#bbdefb"],
      ["#2e7d32","#1b5e20","#c8e6c9"],
      ["#7b1fa2","#4a148c","#e1bee7"],
    ];
    const phaseColors = {{ "Root": {{ fill: "#1a73e8", stroke: "#174ea6", collapsedFill: "#d2e3fc" }},
                          "Default": {{ fill: "#bdc1c6", stroke: "#9aa0a6", collapsedFill: "#e8eaed" }} }};
    (initialJsonData.children || []).forEach((c, i) => {{
      const pal = PALETTE[i % PALETTE.length];
      phaseColors[c.name] = {{ fill: pal[0], stroke: pal[1], collapsedFill: pal[2] }};
    }});

    const getPalette = depth => {{
      const pal = PALETTE[depth % PALETTE.length];
      return {{ fill: pal[0], stroke: pal[1], collapsedFill: pal[2] }};
    }};

    const svgElement = d3.select("#tree-svg");
    const initialSvgWidth = +svgElement.attr("width");
    const initialSvgHeight = +svgElement.attr("height");
    const margin = {{ top: 40, right: 120, bottom: 80, left: 450 }};
    let width = initialSvgWidth - margin.left - margin.right;
    let height = initialSvgHeight - margin.top - margin.bottom;
    const duration = 500;
    let nodeCounter = 0;
    const g = svgElement.append("g").attr("transform", `translate(${{margin.left}},${{margin.top}})`);
    const treemap = d3.tree().nodeSize([40, 0]);
    let rootNode = d3.hierarchy(treeData, d => d.children);
    rootNode.x0 = 0;
    rootNode.y0 = 0;

    if (rootNode.children) {{
      rootNode.children.forEach(d_child => {{
        if (d_child.children) {{ collapseBranch(d_child); }}
      }});
    }}
    updateTree(rootNode);

    function collapseBranch(d) {{ if (d.children) {{ d._children = d.children; d._children.forEach(collapseBranch); d.children = null; }} }}
    function expandBranch(d) {{ if (d._children) {{ d.children = d._children; d._children = null; }} if (d.children) {{ d.children.forEach(expandBranch); }} }}
    window.expandAll = () => {{ expandBranch(rootNode); updateTree(rootNode); }};
    window.collapseAll = () => {{ if (rootNode.children) {{ rootNode.children.forEach(collapseBranch); }} updateTree(rootNode); }};
    window.resetView = () => {{ if (rootNode.children) {{ rootNode.children.forEach(d_child => {{ if (d_child.children || d_child._children) {{ collapseBranch(d_child); }} }}); }} if (rootNode._children && !rootNode.children) {{ rootNode.children = rootNode._children; rootNode._children = null; }} updateTree(rootNode); }};

    window.filterTree = (query) => {{
      const q = (query || '').toLowerCase().trim();
      try {{
        if (history.replaceState) {{
          const url = new URL(window.location);
          if (q) url.searchParams.set('search', q);
          else url.searchParams.delete('search');
          history.replaceState(null, '', url);
        }}
      }} catch(e) {{}}
      if (!q) {{
        window.resetView();
        return;
      }}
      function searchAndExpand(d) {{
        let hasMatch = (d.data.name || '').toLowerCase().includes(q);
        const children = d.children || d._children || [];
        for (const child of children) {{
          if (searchAndExpand(child)) hasMatch = true;
        }}
        if (hasMatch && d._children) {{
          d.children = d._children;
          d._children = null;
        }}
        return hasMatch;
      }}
      searchAndExpand(rootNode);
      updateTree(rootNode);
    }};

    // Initialize from URL deep link
    (function() {{
      try {{
        const params = new URLSearchParams(window.location.search);
        const q = params.get('search') || params.get('q') || (window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '');
        const searchInput = document.getElementById('tree-search');
        if (q && searchInput) {{
          searchInput.value = q;
          window.filterTree(q);
        }}
      }} catch(e) {{}}
    }})();

    function updateTree(source) {{
      const treeLayoutData = treemap(rootNode);
      let nodes = treeLayoutData.descendants();
      let links = treeLayoutData.descendants().slice(1);

      let minX = 0;
      let maxX = 0;
      if (nodes.length > 0) {{
        minX = d3.min(nodes, d => d.x);
        maxX = d3.max(nodes, d => d.x);
      }}

      let neededHeight = Math.max(initialSvgHeight, maxX - minX + margin.top + margin.bottom + 100);
      svgElement.transition().duration(duration / 2).attr("height", neededHeight);
      g.transition().duration(duration / 2).attr("transform", `translate(${{margin.left}},${{margin.top - minX + 40}})`);

      nodes.forEach(d => {{ d.y = d.depth * 400; }}); // Adjust horizontal separation if needed

      const node = g.selectAll('g.node').data(nodes, d => d.id || (d.id = ++nodeCounter));
      const nodeEnter = node.enter().append('g')
        .attr('class', d => "node" + (d.children || d._children ? " node--internal" : " node--leaf") + (d._children ? " _children" : ""))
        .attr('transform', d => `translate(${{source.y0}},${{source.x0}})`)
        .on('click', (event, d) => {{ if (d.children) {{ d._children = d.children; d.children = null; }} else if (d._children) {{ d.children = d._children; d._children = null; }} updateTree(d); }})
        .style('cursor', d => (d.children || d._children) ? 'pointer' : 'default');

      nodeEnter.append('circle').attr('r', 1e-6);

      nodeEnter.append('text')
        .attr('dy', '.35em')
        .attr('x', d => d.children || d._children ? -14 : 14)
        .attr('text-anchor', d => d.children || d._children ? 'end' : 'start')
        .style("fill-opacity", 1e-6)
        .call(wrapText, 380);

      const nodeUpdate = nodeEnter.merge(node);
      nodeUpdate.transition().duration(duration)
        .attr('transform', d => `translate(${{d.y}},${{d.x}})`)
        .attr('class', d => "node" + (d.children ? " node--internal" : " node--leaf") + (d._children ? " node--internal _children" : ""));

      nodeUpdate.select('circle').attr('r', 8.5)
        .style('fill', d => {{
            const palette = (d.depth === 1) ? (phaseColors[d.data.originalStageName] || phaseColors.Default) : getPalette(d.depth);
            if (d._children) return palette.collapsedFill;
            if (d.children) return palette.fill;
            return "#ffffff";
        }})
        .style('stroke', d => {{
            const palette = (d.depth === 1) ? (phaseColors[d.data.originalStageName] || phaseColors.Default) : getPalette(d.depth);
            return palette.stroke;
        }});
      nodeUpdate.select('text').style("fill-opacity", 1).call(wrapText, 380);

      const nodeExit = node.exit().transition().duration(duration).attr('transform', d => `translate(${{source.y}},${{source.x}})`).remove();
      nodeExit.select('circle').attr('r', 1e-6);
      nodeExit.select('text').style('fill-opacity', 1e-6);

      const link = g.selectAll('path.link').data(links, d => d.id);
      const linkEnter = link.enter().insert('path', "g").attr('class', 'link').attr('d', d => {{ const o = {{ x: source.x0, y: source.y0 }}; return diagonal(o, o); }});

      linkEnter.merge(link).transition().duration(duration).attr('d', d => diagonal(d, d.parent))
        .style('stroke', d => {{
            const sourceNode = d.parent;
            if (!sourceNode) return phaseColors.Default.stroke;
            const l1AncestorName = sourceNode.data.originalStageName;
            const colorPalette = phaseColors[l1AncestorName] || phaseColors.Default;
            return colorPalette.stroke;
        }});
      link.exit().transition().duration(duration).attr('d', d => {{ const o = {{ x: source.x, y: source.y }}; return diagonal(o, o); }}).remove();
      nodes.forEach(d => {{ d.x0 = d.x; d.y0 = d.y; }});
    }}

    function diagonal(s, d) {{ return `M ${{s.y}} ${{s.x}} C ${{(s.y + d.y) / 2}} ${{s.x}}, ${{(s.y + d.y) / 2}} ${{d.x}}, ${{d.y}} ${{d.x}}`; }}

    function wrapText(textElements, maxWidth) {{
        const textPartColors = {{
            name: 'currentColor',
            count: 'var(--accent)'
        }};
        const countRegex = /(\s\(Total Count: \d+\))$/;

        textElements.each(function () {{
            const textD3 = d3.select(this);
            const originalNodeText = textD3.datum().data.name;
            const x = parseFloat(textD3.attr("x") || 0);
            const initialDy = textD3.attr("dy");
            const textAnchor = textD3.attr("text-anchor");
            const lineHeight = 1.1;

            textD3.text(null);

            let namePart = originalNodeText;
            let countPartText = "";

            const countMatch = originalNodeText.match(countRegex);
            if (countMatch && originalNodeText.endsWith(countMatch[0])) {{
                namePart = originalNodeText.substring(0, originalNodeText.length - countMatch[0].length).trim();
                countPartText = countMatch[0].trim();
            }}

            const tokens = [];
            namePart.split(/\s+/).filter(Boolean).forEach(word => {{
                tokens.push({{ text: word, type: 'name' }});
            }});
            if (countPartText) {{
                tokens.push({{ text: countPartText, type: 'count' }});
            }}

            if (tokens.length === 0 && originalNodeText) {{
                tokens.push({{ text: originalNodeText, type: 'name' }});
            }}

            let currentTspan = textD3.append("tspan").attr("x", x).attr("dy", initialDy);
            if (textAnchor === "end") currentTspan.attr("text-anchor", "end");

            let lineTokens = [];

            for (let i = 0; i < tokens.length; i++) {{
                const tokenObj = tokens[i];

                lineTokens.push(tokenObj);
                currentTspan.text(lineTokens.map(t => t.text).join(" "));

                if (currentTspan.node().getComputedTextLength() > maxWidth && lineTokens.length > 1) {{
                    lineTokens.pop();

                    currentTspan.text(null);
                    lineTokens.forEach((prevToken, idx) => {{
                        currentTspan.append("tspan")
                            .text((idx > 0 ? " " : "") + prevToken.text)
                            .style("fill", textPartColors[prevToken.type] || textPartColors.name)
                            .style("font-weight", prevToken.type === 'count' ? "bold" : "normal");
                    }});

                    lineTokens = [tokenObj];
                    currentTspan = textD3.append("tspan").attr("x", x).attr("dy", lineHeight + "em");
                    if (textAnchor === "end") currentTspan.attr("text-anchor", "end");
                }}
            }}

            currentTspan.text(null);
            lineTokens.forEach((token, idx) => {{
                currentTspan.append("tspan")
                    .text((idx > 0 ? " " : "") + token.text)
                    .style("fill", textPartColors[token.type] || textPartColors.name)
                    .style("font-weight", token.type === 'count' ? "bold" : "normal");
            }});

            if (textD3.selectAll("tspan > tspan").empty() && textD3.select("tspan").text().length === 0 && originalNodeText) {{
                let t = textD3.select("tspan");
                let displayText = originalNodeText;
                t.text(displayText).style("fill", textPartColors.name);
                if (t.node() && t.node().getComputedTextLength() > maxWidth && displayText.length > 20) {{
                    let estimatedChars = Math.floor(maxWidth / (t.node().getComputedTextLength()/displayText.length) );
                    displayText = displayText.substring(0, Math.max(0, estimatedChars - 3)) + "...";
                    t.text(displayText);
                }}
            }}
        }});
    }}
  </script>
  <script>
    function toggleTheme() {{
      const current = document.documentElement.getAttribute('data-theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try {{ localStorage.setItem('graph_fy_theme', next); }} catch(e){{}}
    }}
    (function() {{
      try {{
        const saved = localStorage.getItem('graph_fy_theme');
        if (saved) document.documentElement.setAttribute('data-theme', saved);
      }} catch(e){{}}
    }})();
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
    data_json = json.dumps(tree, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    return _HTML_TEMPLATE.format(
        title=_html.escape(title),
        header=_html.escape(header),
        svg_width=svg_width,
        svg_height=svg_height,
        data_json=data_json,
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
