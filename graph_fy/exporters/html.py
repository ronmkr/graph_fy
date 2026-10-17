"""html — moved verbatim from graph_fy/export.py."""
from __future__ import annotations

from pathlib import Path
import html as _html
from graph_fy.analyze import _node_community_map
from graph_fy.paths import COMMUNITY_COLORS, write_text_atomic
import json
import networkx as nx
from graph_fy.security import sanitize_label


MAX_NODES_FOR_VIZ = 5_000
_HTML_STALE_MARKER = ".graph.html.stale"

def _viz_node_limit() -> int:
    """Return the effective viz node limit, honoring graph_fy_VIZ_NODE_LIMIT env var.

    Falls back to MAX_NODES_FOR_VIZ when the env var is unset, empty, or non-integer.
    Set to 0 to disable HTML viz unconditionally (useful for CI runners).
    """
    import os
    raw = os.environ.get("graph_fy_VIZ_NODE_LIMIT")
    if raw is None or not raw.strip():
        return MAX_NODES_FOR_VIZ
    try:
        return int(raw)
    except ValueError:
        return MAX_NODES_FOR_VIZ

def _html_styles() -> str:
    return """<style>
  :root {
    --bg: #f8f9fa;
    --surface: #ffffff;
    --surface-variant: #f1f3f4;
    --border: #dadce0;
    --border-accent: #dadce0;
    --text: #202124;
    --muted: #5f6368;
    --accent: #0b57d0;
    --accent-hover: #0842a0;
    --accent-glow: rgba(11, 87, 208, 0.15);
    --coral: #d93025;
    --success: #137333;
    --warning: #b06000;
    --purple: #9334e6;
  }
  [data-theme="dark"] {
    --bg: #121212;
    --surface: #1e1e1e;
    --surface-variant: #282828;
    --border: #333333;
    --border-accent: #444444;
    --text: #e8eaed;
    --muted: #9aa0a6;
    --accent: #8ab4f8;
    --accent-hover: #aecbfa;
    --accent-glow: rgba(138, 180, 248, 0.25);
    --coral: #f28b82;
    --success: #81c995;
    --warning: #fdd663;
    --purple: #c58af9;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #121212;
      --surface: #1e1e1e;
      --surface-variant: #282828;
      --border: #333333;
      --border-accent: #444444;
      --text: #e8eaed;
      --muted: #9aa0a6;
      --accent: #8ab4f8;
      --accent-hover: #aecbfa;
      --accent-glow: rgba(138, 180, 248, 0.25);
      --coral: #f28b82;
      --success: #81c995;
      --warning: #fdd663;
      --purple: #c58af9;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background-color: var(--bg);
    color: var(--text);
    font-family: Roboto, "Google Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    display: flex;
    height: 100vh;
    overflow: hidden;
  }
  #graph { flex: 1; background: var(--surface); }
  #sidebar {
    width: 360px;
    min-width: 300px;
    max-width: 480px;
    background: var(--surface);
    border-left: 1px solid var(--border);
    box-shadow: -2px 0 8px rgba(60,64,67,0.08);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  #sidebar-tabs {
    display: flex;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .tab-btn {
    flex: 1;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--muted);
    padding: 12px 4px;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
    text-align: center;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-family: Roboto, "Google Sans", sans-serif;
    transition: all 0.15s ease;
    white-space: nowrap;
  }
  .tab-btn:hover { color: var(--text); background: var(--surface-variant); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); background: transparent; font-weight: 600; }
  .tab-pane { display: none; flex: 1; flex-direction: column; overflow-y: auto; padding: 14px; background: var(--surface); }
  .tab-pane.active { display: flex; }
  #search-wrap { padding: 0 0 10px 0; border-bottom: 1px solid var(--border); }
  #search {
    width: 100%;
    background: var(--surface-variant);
    border: 1px solid transparent;
    color: var(--text);
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 12.5px;
    font-family: Roboto, sans-serif;
    outline: none;
    transition: all 0.15s;
  }
  #search:focus {
    background: var(--surface);
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-glow);
  }
  #search-results { max-height: 140px; overflow-y: auto; padding: 4px 0; border-bottom: 1px solid var(--border); display: none; }
  .search-item { padding: 6px 10px; cursor: pointer; border-radius: 6px; font-size: 12px; font-family: Roboto, sans-serif; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text); }
  .search-item:hover { background: #e8f0fe; color: var(--accent); }
  #info-panel { padding: 12px 0; min-height: 140px; }
  #info-panel h3 { font-size: 12px; color: var(--accent); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.04em; font-family: Roboto, "Google Sans", sans-serif; font-weight: 600; }
  #info-content { font-size: 12.5px; color: var(--muted); line-height: 1.6; }
  #info-content .field { margin-bottom: 6px; font-family: Roboto, sans-serif; color: var(--muted); }
  #info-content .field b { color: var(--text); font-weight: 500; }
  #info-content .empty { color: #80868b; font-style: italic; }
  .neighbor-link { display: block; padding: 5px 8px; margin: 3px 0; border-radius: 6px; cursor: pointer; font-size: 11.5px; font-family: Roboto, sans-serif; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-left: 3px solid var(--accent); background: #f8f9fa; color: var(--text); }
  .neighbor-link:hover { background: #e8f0fe; color: var(--accent); }
  #neighbors-list { max-height: 160px; overflow-y: auto; margin-top: 6px; }
  #legend-wrap { flex: 1; overflow-y: auto; padding: 0; }
  #legend-wrap h3 { font-size: 12px; color: var(--accent); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.04em; font-family: Roboto, "Google Sans", sans-serif; font-weight: 600; }
  .legend-item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; cursor: pointer; border-radius: 6px; font-size: 12px; font-family: Roboto, sans-serif; color: var(--text); }
  .legend-item:hover { background: #f1f3f4; }
  .legend-item.dimmed { opacity: 0.35; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .legend-count { color: var(--muted); font-size: 11px; }
  #stats { padding: 10px 14px; border-top: 1px solid var(--border); font-size: 11px; font-family: Roboto, sans-serif; color: var(--muted); flex-shrink: 0; background: #f8f9fa; }
  #legend-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; padding: 4px 0; }
  #legend-controls label { display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 11.5px; font-family: Roboto, sans-serif; color: var(--muted); user-select: none; }
  #legend-controls label:hover { color: var(--text); }
  .legend-cb, #select-all-cb { appearance: none; -webkit-appearance: none; width: 16px; height: 16px; border: 1.5px solid var(--border); border-radius: 4px; background: #ffffff; cursor: pointer; position: relative; flex-shrink: 0; }
  .legend-cb:checked, #select-all-cb:checked { background: var(--accent); border-color: var(--accent); }
  .legend-cb:checked::after, #select-all-cb:checked::after { content: ''; position: absolute; left: 4.5px; top: 2px; width: 4px; height: 7px; border: solid #fff; border-width: 0 2px 2px 0; transform: rotate(45deg); }
  #select-all-cb:indeterminate { background: var(--accent); border-color: var(--accent); }
  #select-all-cb:indeterminate::after { content: ''; position: absolute; left: 3px; top: 6px; width: 8px; height: 2px; background: #fff; border: none; transform: none; }
  .action-btn { background: #ffffff; color: var(--accent); border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px; font-size: 11.5px; font-weight: 500; font-family: Roboto, "Google Sans", sans-serif; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 4px; transition: all 0.15s; }
  .action-btn:hover { background: #f8fafd; border-color: var(--accent); }
  .action-btn.primary { background: var(--accent); border-color: transparent; color: #fff; font-weight: 500; }
  .action-btn.primary:hover { background: var(--accent-hover); box-shadow: 0 1px 3px rgba(60,64,67,0.3); }
  .schema-box { background: var(--surface-variant); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-family: ui-monospace, monospace; font-size: 10.5px; color: var(--accent); margin-bottom: 8px; max-height: 120px; overflow-y: auto; white-space: pre-wrap; line-height: 1.4; }
  #sql-input { width: 100%; background: var(--surface-variant); border: 1px solid var(--border); color: var(--text); font-family: ui-monospace, monospace; font-size: 11.5px; padding: 8px 10px; border-radius: 8px; resize: vertical; outline: none; margin-bottom: 6px; }
  #sql-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-glow); background: var(--surface); }
  .results-table-wrap { max-height: 180px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; margin-top: 6px; background: var(--surface); }
  .query-table { width: 100%; border-collapse: collapse; font-size: 11.5px; font-family: Roboto, sans-serif; text-align: left; }
  .query-table th { background: var(--surface-variant); color: var(--text); font-weight: 600; padding: 6px 10px; border-bottom: 1px solid var(--border); position: sticky; top: 0; text-transform: uppercase; font-size: 10px; }
  .query-table td { padding: 6px 10px; border-bottom: 1px solid var(--border); color: var(--text); max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .query-table tr:hover td { background: var(--surface-variant); cursor: pointer; color: var(--accent); }
  .risk-badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; font-family: Roboto, sans-serif; }
  .risk-LOW { background: #ceead6; color: #137333; border: 1px solid #a8dab5; }
  .risk-MEDIUM { background: #feefc3; color: #b06000; border: 1px solid #fdd663; }
  .risk-HIGH { background: #fedfc8; color: #b34700; border: 1px solid #fcbb8b; }
  .risk-CRITICAL { background: #fad2cf; color: #c5221f; border: 1px solid #f28b82; }
  .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0; }
  .metric-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px; text-align: center; box-shadow: 0 1px 2px rgba(60,64,67,0.08); }
  .metric-val { font-size: 16px; font-weight: 600; color: var(--accent); font-family: Roboto, "Google Sans", sans-serif; }
  .metric-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin-top: 2px; }
  .closure-item { padding: 5px 8px; font-size: 11px; font-family: Roboto, sans-serif; border-left: 3px solid var(--accent); margin: 3px 0; border-radius: 4px; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; background: var(--surface-variant); color: var(--text); }
  .closure-item:hover { background: var(--surface-variant); color: var(--accent); }
  .closure-item.is-test { border-left-color: #1a73e8; }
  .closure-item.is-api { border-left-color: #d93025; }
  .closure-item.is-iac { border-left-color: #ea8600; }
</style>"""

def _hyperedge_script(hyperedges_json: str) -> str:
    return f"""<script>
// Render hyperedges as shaded regions
const hyperedges = {hyperedges_json};
// afterDrawing passes ctx already transformed to network coordinate space.
// Draw node positions raw — no manual pan/zoom/DPR math needed.

// Andrew's monotone chain. Returns the hull in counter-clockwise order, which
// is what the perimeter must be traced in. Collinear and duplicate points
// collapse to the extremes, so degenerate member sets render as a segment
// rather than a zero-area crossed path.
function convexHull(pts) {{
    const p = pts.slice().sort((a, b) => (a.x - b.x) || (a.y - b.y));
    if (p.length < 3) return p;
    const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
    const build = seq => {{
        const out = [];
        for (const q of seq) {{
            while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], q) <= 0) out.pop();
            out.push(q);
        }}
        out.pop();
        return out;
    }};
    const hull = build(p).concat(build(p.slice().reverse()));
    return hull.length >= 3 ? hull : p;
}}
network.on('afterDrawing', function(ctx) {{
    hyperedges.forEach(h => {{
        const positions = h.nodes
            .map(nid => network.getPositions([nid])[nid])
            .filter(p => p !== undefined);
        if (positions.length < 2) return;
        ctx.save();
        ctx.globalAlpha = 0.12;
        ctx.fillStyle = '#6366f1';
        ctx.strokeStyle = '#6366f1';
        ctx.lineWidth = 2;
        ctx.beginPath();
        // Centroid and expanded hull in network coordinates.
        // The perimeter must follow hull order, not h.nodes order: tracing the
        // raw member order self-intersects whenever the layout does not happen
        // to place members in angular order, filling as crossed wedges.
        const cx = positions.reduce((s, p) => s + p.x, 0) / positions.length;
        const cy = positions.reduce((s, p) => s + p.y, 0) / positions.length;
        const hull = convexHull(positions);
        const expanded = hull.map(p => ({{
            x: cx + (p.x - cx) * 1.15,
            y: cy + (p.y - cy) * 1.15
        }}));
        ctx.moveTo(expanded[0].x, expanded[0].y);
        expanded.slice(1).forEach(p => ctx.lineTo(p.x, p.y));
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 0.4;
        ctx.stroke();
        // Label
        ctx.globalAlpha = 0.8;
        ctx.fillStyle = '#4f46e5';
        ctx.font = 'bold 11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(h.label, cx, cy - 5);
        ctx.restore();
    }});
}});
</script>"""

def _html_script(nodes_json: str, edges_json: str, legend_json: str) -> str:
    return f"""<script>
const RAW_NODES = {nodes_json};
const RAW_EDGES = {edges_json};
const LEGEND = {legend_json};

// Embedded Database Table Schema Definition (Zero-Server In-Browser SQL Explorer)
const DB_SCHEMA = {{
  version: "1.0",
  engine: "Client-Side WASM / In-Browser Relational Graph Query Engine",
  tables: {{
    nodes: {{
      columns: {{
        id: "TEXT PRIMARY KEY",
        label: "TEXT",
        file_type: "TEXT",
        type: "TEXT (alias for file_type)",
        community: "INTEGER",
        community_name: "TEXT",
        source_file: "TEXT",
        degree: "INTEGER",
        size: "REAL"
      }}
    }},
    edges: {{
      columns: {{
        id: "INTEGER PRIMARY KEY",
        from: "TEXT",
        source: "TEXT (alias for from)",
        to: "TEXT",
        target: "TEXT (alias for to)",
        relation: "TEXT",
        confidence: "TEXT",
        width: "INTEGER"
      }}
    }}
  }}
}};

// Pre-index graph edges for downstream transitive impact traversal
const inEdges = new Map();
const outEdges = new Map();
RAW_EDGES.forEach((e, idx) => {{
  const s = String(e.from);
  const t = String(e.to);
  const rel = String(e.label || '').toLowerCase();
  if (!inEdges.has(t)) inEdges.set(t, []);
  inEdges.get(t).push({{ source: s, relation: rel, edge: e, index: idx }});
  if (!outEdges.has(s)) outEdges.set(s, []);
  outEdges.get(s).push({{ target: t, relation: rel, edge: e, index: idx }});
}});

// HTML-escape helper — prevents XSS when injecting graph data into innerHTML
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

// Build vis datasets
const nodesDS = new vis.DataSet(RAW_NODES.map((n, i) => ({{
  id: n.id, label: n.label, color: n.color, size: n.size,
  font: n.font, title: n.title,
  x: 30 * Math.sqrt(i) * Math.cos(i * 2.4),
  y: 30 * Math.sqrt(i) * Math.sin(i * 2.4),
  _community: n.community, _community_name: n.community_name,
  _source_file: n.source_file, _file_type: n.file_type, _degree: n.degree,
  _repo: n.repo,
}})));

const edgesDS = new vis.DataSet(RAW_EDGES.map((e, i) => ({{
  id: i, from: e.from, to: e.to,
  label: '',
  title: e.title,
  dashes: e.dashes,
  width: e.width,
  color: e.color,
  arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }},
}})));

const container = document.getElementById('graph');
const network = new vis.Network(container, {{ nodes: nodesDS, edges: edgesDS }}, {{
  physics: {{
    enabled: true,
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {{
      gravitationalConstant: -60,
      centralGravity: 0.005,
      springLength: 120,
      springConstant: 0.08,
      damping: 0.4,
      avoidOverlap: 0.8,
    }},
    stabilization: {{ iterations: 200, fit: true }},
  }},
  interaction: {{
    hover: true,
    tooltipDelay: 100,
    hideEdgesOnDrag: true,
    navigationButtons: false,
    keyboard: false,
  }},
  nodes: {{ shape: 'dot', borderWidth: 1.5 }},
  edges: {{ smooth: {{ type: 'continuous', roundness: 0.2 }}, selectionWidth: 3 }},
}});

network.once('stabilizationIterationsDone', () => {{
  network.setOptions({{ physics: {{ enabled: false }} }});
}});

let currentSelectedNodeId = null;

function showInfo(nodeId) {{
  const n = nodesDS.get(nodeId);
  if (!n) return;
  currentSelectedNodeId = nodeId;

  // Sync to blast target dropdown & slice panel
  const blastSel = document.getElementById('blast-target-select');
  if (blastSel) blastSel.value = String(nodeId);
  const sliceLabel = document.getElementById('slice-node-label');
  if (sliceLabel) sliceLabel.textContent = n.label;

  const neighborIds = network.getConnectedNodes(nodeId);
  const neighborItems = neighborIds.map(nid => {{
    const nb = nodesDS.get(nid);
    const color = nb ? nb.color.background : '#555';
    const repoBadge = (nb && nb._repo && nb._repo !== n._repo) ? ` <small style="opacity:0.8; font-size:9.5px; background:var(--surface-variant); padding:1px 4px; border-radius:4px;">📦 ${{esc(nb._repo)}}</small>` : '';
    return `<span class="neighbor-link" style="border-left-color:${{esc(color)}}" data-nid="${{esc(nid)}}">${{esc(nb ? nb.label : nid)}}${{repoBadge}}</span>`;
  }}).join('');
  const repoChip = n._repo ? `<div class="field"><span class="repo-badge" style="background:var(--accent-subtle); color:var(--accent); border:1px solid var(--border-accent); padding:2px 8px; border-radius:12px; font-size:11px; font-weight:600;">📦 ${{esc(n._repo)}}</span></div>` : '';
  document.getElementById('info-content').innerHTML = `
    <div class="field"><b>${{esc(n.label)}}</b></div>
    ${{repoChip}}
    <div class="field">Type: ${{esc(n._file_type || 'unknown')}}</div>
    <div class="field">Community: ${{esc(n._community_name)}}</div>
    <div class="field">Source: ${{esc(n._source_file || '-')}}</div>
    <div class="field">Degree: ${{n._degree}}</div>
    <div style="display:flex; gap:4px; margin:8px 0;">
      <button class="action-btn primary btn-info-blast" data-nid="${{esc(nodeId)}}">💥 Blast Radius</button>
      <button class="action-btn btn-info-slice" data-nid="${{esc(nodeId)}}" data-hops="1">1-Hop</button>
      <button class="action-btn btn-info-slice" data-nid="${{esc(nodeId)}}" data-hops="2">2-Hop</button>
    </div>
    ${{neighborIds.length ? `<div class="field" style="margin-top:8px;color:var(--muted);font-size:11px">Neighbors (${{neighborIds.length}})</div><div id="neighbors-list">${{neighborItems}}</div>` : ''}}
  `;
}}

function focusNode(nodeId) {{
  network.focus(nodeId, {{ scale: 1.4, animation: true }});
  network.selectNodes([nodeId]);
  showInfo(nodeId);
}}

// Tab navigation
function switchTab(tabId) {{
  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  }});
  document.querySelectorAll('.tab-pane').forEach(pane => {{
    pane.classList.toggle('active', pane.id === tabId);
  }});
}}
document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
}});

// Toggle schema card
const toggleSchemaBtn = document.getElementById('toggle-schema-btn');
const dbSchemaCard = document.getElementById('db-schema-card');
if (toggleSchemaBtn && dbSchemaCard) {{
  toggleSchemaBtn.addEventListener('click', () => {{
    const cur = dbSchemaCard.style.display;
    dbSchemaCard.style.display = cur === 'none' ? 'block' : 'none';
  }});
}}

// Neighbor & Action link delegation
document.addEventListener('click', e => {{
  const el = e.target.closest('.neighbor-link');
  if (el && el.dataset.nid !== undefined) focusNode(el.dataset.nid);

  const blastBtn = e.target.closest('.btn-info-blast');
  if (blastBtn && blastBtn.dataset.nid !== undefined) {{
    switchTab('tab-blast');
    const sel = document.getElementById('blast-target-select');
    if (sel) sel.value = blastBtn.dataset.nid;
    runBlastRadius(blastBtn.dataset.nid);
  }}

  const sliceBtn = e.target.closest('.btn-info-slice');
  if (sliceBtn && sliceBtn.dataset.nid !== undefined) {{
    switchTab('tab-slice');
    const hops = parseInt(sliceBtn.dataset.hops, 10);
    document.querySelectorAll('.slice-hop-btn').forEach(b => {{
      b.classList.toggle('primary', b.dataset.hops === String(hops));
    }});
    sliceNeighborhood(sliceBtn.dataset.nid, hops);
  }}

  const closureItem = e.target.closest('.closure-item');
  if (closureItem && closureItem.dataset.nid !== undefined) {{
    focusNode(closureItem.dataset.nid);
  }}
}});

// Track hovered node
let hoveredNodeId = null;
network.on('hoverNode', params => {{
  hoveredNodeId = params.node;
  container.style.cursor = 'pointer';
}});
network.on('blurNode', () => {{
  hoveredNodeId = null;
  container.style.cursor = 'default';
}});
container.addEventListener('click', () => {{
  if (hoveredNodeId !== null) {{
    showInfo(hoveredNodeId);
    network.selectNodes([hoveredNodeId]);
  }}
}});
function showDefaultInfo() {{
  const topNodes = [...RAW_NODES].sort((a, b) => (b.size || 0) - (a.size || 0)).slice(0, 8);
  let html = '<div style="margin-top:10px;"><p style="font-size:12px;color:var(--muted);margin-bottom:8px;">Click a node to inspect it, or select a key architectural hub:</p>';
  html += '<div style="display:flex;flex-wrap:wrap;gap:6px;">';
  topNodes.forEach(n => {{
    html += `<button class="action-btn" style="font-size:11px;padding:4px 8px;cursor:pointer;border-radius:12px;border:1px solid var(--border);" onclick="network.focus('${{n.id}}', {{scale:1.4, animation:true}}); network.selectNodes(['${{n.id}}']); showInfo('${{n.id}}');">${{n.label}}</button>`;
  }});
  html += '</div></div>';
  const infoEl = document.getElementById('info-content');
  if (infoEl) infoEl.innerHTML = html;
}}

network.on('click', params => {{
  if (params.nodes.length > 0) {{
    showInfo(params.nodes[0]);
  }} else if (hoveredNodeId === null) {{
    showDefaultInfo();
  }}
}});
showDefaultInfo();

// Search Box
const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', () => {{
  const q = searchInput.value.toLowerCase().trim();
  searchResults.innerHTML = '';
  try {{
    if (history.replaceState) {{
      const url = new URL(window.location);
      if (q) url.searchParams.set('search', q);
      else url.searchParams.delete('search');
      history.replaceState(null, '', url);
    }}
  }} catch(e){{}}
  if (!q) {{ searchResults.style.display = 'none'; return; }}
  const matches = RAW_NODES.filter(n => n.label.toLowerCase().includes(q)).slice(0, 20);
  if (!matches.length) {{ searchResults.style.display = 'none'; return; }}
  searchResults.style.display = 'block';
  matches.forEach(n => {{
    const el = document.createElement('div');
    el.className = 'search-item';
    el.textContent = n.label;
    el.style.borderLeft = `3px solid ${{n.color.background}}`;
    el.style.paddingLeft = '8px';
    el.onclick = () => {{
      network.focus(n.id, {{ scale: 1.5, animation: true }});
      network.selectNodes([n.id]);
      showInfo(n.id);
      searchResults.style.display = 'none';
      try {{
        if (history.replaceState) {{
          const url = new URL(window.location);
          url.searchParams.set('focus', n.id);
          history.replaceState(null, '', url);
        }}
      }} catch(e){{}}
    }};
    searchResults.appendChild(el);
  }});
}});
document.addEventListener('click', e => {{
  if (!searchResults.contains(e.target) && e.target !== searchInput)
    searchResults.style.display = 'none';
}});

// Initialize from URL deep link
(function() {{
  try {{
    const params = new URLSearchParams(window.location.search);
    const q = params.get('search') || params.get('q') || (window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '');
    const theme = params.get('theme');
    if (theme === 'dark' || theme === 'light') {{
      document.documentElement.setAttribute('data-theme', theme);
      try {{ localStorage.setItem('graph_fy_theme', theme); }} catch(e){{}}
    }}
    if (q && searchInput) {{
      searchInput.value = q;
      searchInput.dispatchEvent(new Event('input'));
    }}
    const focusId = params.get('focus');
    if (focusId && typeof network !== 'undefined') {{
      setTimeout(() => {{
        network.focus(focusId, {{ scale: 1.5, animation: true }});
        network.selectNodes([focusId]);
        showInfo(focusId);
      }}, 300);
    }}
  }} catch(e){{}}
}})();

// Client-Side Zero-Server SQL / Query Engine
function executeSqlQuery(sql) {{
  if (!sql || !sql.trim()) return {{ error: "Empty SQL query" }};
  const q = sql.trim();
  const m = q.match(/^SELECT\\s+(.+?)\\s+FROM\\s+([a-zA-Z0-9_]+)(?:\\s+WHERE\\s+(.+?))?(?:\\s+ORDER\\s+BY\\s+([a-zA-Z0-9_]+)(?:\\s+(ASC|DESC))?)?(?:\\s+LIMIT\\s+(\\d+))?\\s*;?$/i);
  if (!m) {{
    return {{ error: "Syntax error. Supported: SELECT <cols|*> FROM <nodes|edges> [WHERE cond] [ORDER BY col [ASC|DESC]] [LIMIT n]" }};
  }}
  const rawCols = m[1].trim();
  const table = m[2].trim().toLowerCase();
  const whereClause = m[3] ? m[3].trim() : null;
  const orderCol = m[4] ? m[4].trim() : null;
  const orderDir = m[5] ? m[5].toUpperCase() : 'ASC';
  const limit = m[6] ? parseInt(m[6], 10) : null;

  let dataset = [];
  if (table === 'nodes') {{
    dataset = RAW_NODES.map(n => ({{
      id: String(n.id),
      label: String(n.label || ''),
      file_type: String(n.file_type || ''),
      type: String(n.file_type || ''),
      community: Number(n.community !== undefined ? n.community : 0),
      community_name: String(n.community_name || ''),
      source_file: String(n.source_file || ''),
      degree: Number(n.degree || 0),
      size: Number(n.size || 0),
    }}));
  }} else if (table === 'edges') {{
    dataset = RAW_EDGES.map((e, idx) => ({{
      id: idx,
      from: String(e.from),
      source: String(e.from),
      to: String(e.to),
      target: String(e.to),
      relation: String(e.label || ''),
      label: String(e.label || ''),
      confidence: String(e.confidence || ''),
      width: Number(e.width || 1),
    }}));
  }} else {{
    return {{ error: `Unknown table '${{table}}'. Available tables: nodes, edges` }};
  }}

  let filtered = dataset;
  if (whereClause) {{
    try {{
      filtered = dataset.filter(row => evaluateWhere(row, whereClause));
    }} catch (err) {{
      return {{ error: "Evaluation error in WHERE clause: " + err.message }};
    }}
  }}

  if (orderCol) {{
    const c = orderCol.toLowerCase();
    filtered.sort((a, b) => {{
      let va = a[c];
      let vb = b[c];
      if (va === undefined) va = '';
      if (vb === undefined) vb = '';
      if (typeof va === 'number' && typeof vb === 'number') {{
        return orderDir === 'DESC' ? vb - va : va - vb;
      }}
      return orderDir === 'DESC' ? String(vb).localeCompare(String(va)) : String(va).localeCompare(String(vb));
    }});
  }}

  if (limit !== null && !isNaN(limit)) {{
    filtered = filtered.slice(0, limit);
  }}

  let cols = [];
  if (rawCols === '*') {{
    cols = table === 'nodes'
      ? ['id', 'label', 'file_type', 'community', 'degree', 'source_file']
      : ['id', 'from', 'to', 'relation', 'confidence'];
  }} else {{
    cols = rawCols.split(',').map(c => c.trim().toLowerCase());
  }}

  const rows = filtered.map(row => {{
    const obj = {{}};
    cols.forEach(c => {{
      obj[c] = row[c] !== undefined ? row[c] : '';
    }});
    return obj;
  }});

  return {{ table, cols, rows, count: rows.length, total: dataset.length, rawMatches: filtered }};
}}

function evaluateWhere(row, clause) {{
  const orParts = clause.split(/\\s+OR\\s+/i);
  return orParts.some(orPart => {{
    const andParts = orPart.split(/\\s+AND\\s+/i);
    return andParts.every(cond => evaluateSimpleCondition(row, cond.trim()));
  }});
}}

function evaluateSimpleCondition(row, cond) {{
  cond = cond.trim();
  if (cond.startsWith('(') && cond.endsWith(')')) {{
    return evaluateWhere(row, cond.slice(1, -1));
  }}
  const m = cond.match(/^([a-zA-Z0-9_]+)\\s*(=|!=|<>|>=|<=|>|<|LIKE|NOT\\s+LIKE)\\s*(.+)$/i);
  if (!m) return true;
  const col = m[1].trim().toLowerCase();
  const op = m[2].toUpperCase().replace(/\\s+/g, ' ');
  let rawVal = m[3].trim();

  let val = rawVal;
  if ((rawVal.startsWith("'") && rawVal.endsWith("'")) || (rawVal.startsWith('"') && rawVal.endsWith('"'))) {{
    val = rawVal.slice(1, -1);
  }} else if (!isNaN(Number(rawVal))) {{
    val = Number(rawVal);
  }}

  let rowVal = row[col];
  if (rowVal === undefined) {{
    if (col === 'type') rowVal = row['file_type'];
    else if (col === 'source') rowVal = row['from'];
    else if (col === 'target') rowVal = row['to'];
    else if (col === 'name') rowVal = row['label'];
    else return false;
  }}

  if (op === 'LIKE' || op === 'NOT LIKE') {{
    const pattern = String(val).replace(/%/g, '.*').replace(/_/g, '.');
    const re = new RegExp('^' + pattern + '$', 'i');
    const matched = re.test(String(rowVal));
    return op === 'LIKE' ? matched : !matched;
  }}

  if (typeof val === 'number' && typeof rowVal === 'number') {{
    if (op === '=') return rowVal === val;
    if (op === '!=' || op === '<>') return rowVal !== val;
    if (op === '>') return rowVal > val;
    if (op === '>=') return rowVal >= val;
    if (op === '<') return rowVal < val;
    if (op === '<=') return rowVal <= val;
  }}

  const sRow = String(rowVal).toLowerCase();
  const sVal = String(val).toLowerCase();
  if (op === '=') return sRow === sVal;
  if (op === '!=' || op === '<>') return sRow !== sVal;
  if (op === '>') return sRow > sVal;
  if (op === '>=') return sRow >= sVal;
  if (op === '<') return sRow < sVal;
  if (op === '<=') return sRow <= sVal;
  return false;
}}

// Render SQL Query Results Table
let lastQueryMatches = [];
function renderQueryResults(res) {{
  const statusEl = document.getElementById('query-status');
  const resultsEl = document.getElementById('query-results');
  const actionsEl = document.getElementById('query-actions');

  if (res.error) {{
    statusEl.innerHTML = `<span style="color:#f87171;">${{esc(res.error)}}</span>`;
    resultsEl.style.display = 'none';
    actionsEl.style.display = 'none';
    lastQueryMatches = [];
    return;
  }}

  lastQueryMatches = res.rawMatches;
  statusEl.innerHTML = `Returned <b>${{res.count}}</b> of ${{res.total}} records in <i>${{res.table}}</i>`;
  if (res.count === 0) {{
    resultsEl.innerHTML = '<div style="padding:8px; color:#666; font-style:italic;">No records match the predicate.</div>';
    resultsEl.style.display = 'block';
    actionsEl.style.display = 'none';
    return;
  }}

  let ths = res.cols.map(c => `<th>${{esc(c)}}</th>`).join('');
  let trs = res.rows.map(r => {{
    let tds = res.cols.map(c => `<td>${{esc(r[c])}}</td>`).join('');
    let rowId = r.id || r.from || '';
    return `<tr data-rowid="${{esc(rowId)}}" data-table="${{res.table}}">${{tds}}</tr>`;
  }}).join('');

  resultsEl.innerHTML = `<table class="query-table"><thead><tr>${{ths}}</tr></thead><tbody>${{trs}}</tbody></table>`;
  resultsEl.style.display = 'block';
  actionsEl.style.display = res.table === 'nodes' ? 'flex' : 'none';

  // Row click to focus
  resultsEl.querySelectorAll('tr[data-rowid]').forEach(tr => {{
    tr.onclick = () => {{
      const rid = tr.dataset.rowid;
      if (rid && nodesDS.get(rid)) focusNode(rid);
    }};
  }});
}}

const sqlInput = document.getElementById('sql-input');
const btnRunSql = document.getElementById('btn-run-sql');
const btnClearSql = document.getElementById('btn-clear-sql');
const sqlPresets = document.getElementById('sql-presets');

btnRunSql.addEventListener('click', () => {{
  const res = executeSqlQuery(sqlInput.value);
  renderQueryResults(res);
}});

btnClearSql.addEventListener('click', () => {{
  sqlInput.value = '';
  document.getElementById('query-results').style.display = 'none';
  document.getElementById('query-actions').style.display = 'none';
  document.getElementById('query-status').textContent = 'Ready. Enter query or choose preset.';
  lastQueryMatches = [];
}});

sqlPresets.addEventListener('change', () => {{
  if (sqlPresets.value) {{
    sqlInput.value = sqlPresets.value;
    btnRunSql.click();
  }}
}});

// Ad-Hoc Predicate Filter Form
const btnApplyFilter = document.getElementById('btn-apply-filter');
const btnResetFilter = document.getElementById('btn-reset-filter');
btnApplyFilter.addEventListener('click', () => {{
  const lbl = document.getElementById('filter-label').value.trim();
  const typ = document.getElementById('filter-type').value.trim();
  const comm = document.getElementById('filter-comm-select').value;

  const whereParts = [];
  if (lbl) whereParts.push(`label LIKE '%${{lbl.replace(/'/g, "")}}%'`);
  if (typ) whereParts.push(`file_type = '${{typ.replace(/'/g, "")}}'`);
  if (comm !== "") whereParts.push(`community = ${{comm}}`);

  const sql = "SELECT id, label, file_type, community, degree FROM nodes" +
    (whereParts.length ? " WHERE " + whereParts.join(" AND ") : "");
  sqlInput.value = sql;
  btnRunSql.click();
}});

btnResetFilter.addEventListener('click', () => {{
  document.getElementById('filter-label').value = '';
  document.getElementById('filter-type').value = '';
  document.getElementById('filter-comm-select').value = '';
  btnClearSql.click();
  nodesDS.update(RAW_NODES.map(n => ({{ id: n.id, hidden: hiddenCommunities.has(n.community) }})));
}});

const btnHighlightSql = document.getElementById('btn-highlight-sql');
const btnIsolateSql = document.getElementById('btn-isolate-sql');
btnHighlightSql.addEventListener('click', () => {{
  if (!lastQueryMatches.length) return;
  const matchIds = new Set(lastQueryMatches.map(m => String(m.id)));
  nodesDS.update(RAW_NODES.map(n => {{
    const isMatch = matchIds.has(String(n.id));
    return {{
      id: n.id,
      color: isMatch ? {{ background: '#0b57d0', border: '#1a73e8' }} : {{ background: n.color.background, border: n.color.border, opacity: 0.15 }},
      borderWidth: isMatch ? 3 : 1
    }};
  }}));
}});

btnIsolateSql.addEventListener('click', () => {{
  if (!lastQueryMatches.length) return;
  const matchIds = new Set(lastQueryMatches.map(m => String(m.id)));
  nodesDS.update(RAW_NODES.map(n => ({{
    id: n.id,
    hidden: !matchIds.has(String(n.id))
  }})));
}});

// Blast Radius & Predictive Impact Analysis Engine
const IMPACT_RELATIONS = new Set([
  'calls', 'imports', 'inherits', 'implements', 'references', 'depends_on', 'subscribes_to'
]);

function isTestNode(data) {{
  const src = (data.source_file || data._source_file || '').toLowerCase();
  const lbl = (data.label || '').toLowerCase();
  return src.includes('test_') || src.includes('_test') || src.includes('tests/') ||
         src.startsWith('tests/') || lbl.includes('test_') || lbl.includes('_test') || lbl.startsWith('test');
}}

function isApiNode(data) {{
  const type = (data.file_type || data._file_type || data.type || '').toLowerCase();
  if (['api', 'rpc', 'endpoint'].includes(type)) return true;
  const lbl = String(data.label || '');
  const methods = ['GET ', 'POST ', 'PUT ', 'DELETE ', 'PATCH ', 'HEAD ', 'OPTIONS '];
  return methods.some(m => lbl.startsWith(m) || lbl.includes(' ' + m));
}}

function isIacNode(data) {{
  const type = (data.file_type || data._file_type || data.type || '').toLowerCase();
  if (['k8s', 'terraform', 'helm', 'kafka', 'docker'].includes(type)) return true;
  const src = (data.source_file || data._source_file || '').toLowerCase();
  return ['dockerfile', 'docker-compose', 'terraform', '.tf', 'helm', 'k8s'].some(k => src.includes(k));
}}

function computeBlastRadius(targetId, maxDepth = 10) {{
  const targetNode = RAW_NODES.find(n => String(n.id) === String(targetId));
  if (!targetNode) return null;

  const seen = new Set([String(targetId)]);
  const queue = [{{ id: String(targetId), depth: 0 }}];
  const downstream = [];

  const containerMembers = outEdges.get(String(targetId)) || [];
  containerMembers.forEach(out => {{
    if (out.relation === 'method' || out.relation === 'contains') {{
      if (!seen.has(out.target)) {{
        seen.add(out.target);
        queue.push({{ id: out.target, depth: 0 }});
      }}
    }}
  }});

  while (queue.length > 0) {{
    const {{ id: curr, depth: d }} = queue.shift();
    if (d >= maxDepth) continue;

    const incoming = inEdges.get(curr) || [];
    for (const inEdge of incoming) {{
      const rel = inEdge.relation;
      const isRelevant = !rel || IMPACT_RELATIONS.has(rel) || inEdge.edge.confidence === 'AGGREGATED';
      if (!isRelevant) continue;

      const u = inEdge.source;
      if (!seen.has(u)) {{
        seen.add(u);
        const uNode = RAW_NODES.find(n => String(n.id) === u);
        if (uNode) {{
          downstream.push({{
            id: u,
            label: uNode.label,
            depth: d + 1,
            relation: rel || inEdge.edge.label || 'connected',
            source_file: uNode.source_file || '',
            community: uNode.community,
            file_type: uNode.file_type || '',
            degree: uNode.degree || 1
          }});
          queue.push({{ id: u, depth: d + 1 }});
        }}
      }}
    }}

    if (RAW_EDGES.some(e => e.confidence === 'AGGREGATED')) {{
      const outgoing = outEdges.get(curr) || [];
      for (const outEdge of outgoing) {{
        const u = outEdge.target;
        if (!seen.has(u)) {{
          seen.add(u);
          const uNode = RAW_NODES.find(n => String(n.id) === u);
          if (uNode) {{
            downstream.push({{
              id: u,
              label: uNode.label,
              depth: d + 1,
              relation: outEdge.relation || 'connected',
              source_file: uNode.source_file || '',
              community: uNode.community,
              file_type: uNode.file_type || '',
              degree: uNode.degree || 1
            }});
            queue.push({{ id: u, depth: d + 1 }});
          }}
        }}
      }}
    }}
  }}

  const affectedTests = [];
  const affectedApis = [];
  const affectedIac = [];
  const communitiesCrossed = new Set();
  if (targetNode.community !== undefined) communitiesCrossed.add(targetNode.community);

  const degrees = RAW_NODES.map(n => n.degree || 0).sort((a, b) => b - a);
  const p95Idx = Math.max(0, Math.floor(degrees.length * 0.05) - 1);
  const godThreshold = Math.max(10, degrees[p95Idx] || 10);
  const godNodesImpacted = [];

  downstream.forEach(item => {{
    if (item.community !== undefined) communitiesCrossed.add(item.community);
    if (isTestNode(item)) affectedTests.push(item);
    if (isApiNode(item)) affectedApis.push(item);
    if (isIacNode(item)) affectedIac.push(item);
    if (item.degree >= godThreshold) godNodesImpacted.push(item);
  }});

  const downstreamCount = downstream.length;
  const countScore = Math.min(0.40, 0.40 * (Math.log(downstreamCount + 1) / Math.log(50)));
  const godScore = Math.min(0.25, godNodesImpacted.length * 0.125);
  const commScore = Math.min(0.20, 0.05 * communitiesCrossed.size);
  const maxDeg = degrees[0] || 1;
  const centralityScore = 0.15 * Math.min(1.0, (targetNode.degree || 1) / maxDeg);

  let totalScore = +(countScore + godScore + commScore + centralityScore).toFixed(3);
  totalScore = Math.max(0.0, Math.min(1.0, totalScore));

  let tier = 'LOW';
  if (totalScore >= 0.70) tier = 'CRITICAL';
  else if (totalScore >= 0.45) tier = 'HIGH';
  else if (totalScore >= 0.20) tier = 'MEDIUM';

  const explanation = [
    `${{downstreamCount}} downstream dependent${{downstreamCount === 1 ? '' : 's'}} within depth ${{maxDepth}}.`,
    godNodesImpacted.length ? `Touches ${{godNodesImpacted.length}} architectural god hub(s).` : '',
    communitiesCrossed.size > 1 ? `Crosses ${{communitiesCrossed.size}} community boundaries.` : '',
    affectedApis.length ? `${{affectedApis.length}} API endpoint(s) impacted.` : '',
    affectedTests.length ? `${{affectedTests.length}} test suite(s) cover this dependency path.` : 'No test coverage detected on dependency path.'
  ].filter(Boolean).join(' ');

  return {{
    target: targetNode,
    downstream,
    downstreamCount,
    affectedTests,
    affectedApis,
    affectedIac,
    godNodesImpacted,
    communitiesCrossed: Array.from(communitiesCrossed),
    riskScore: totalScore,
    riskTier: tier,
    explanation
  }};
}}

function runBlastRadius(targetId) {{
  if (!targetId) return;
  const impact = computeBlastRadius(targetId);
  if (!impact) return;

  const panel = document.getElementById('blast-results-panel');
  if (panel) panel.style.display = 'block';

  document.getElementById('blast-target-name').textContent = impact.target.label;
  const badge = document.getElementById('blast-risk-badge');
  badge.textContent = impact.riskTier;
  badge.className = `risk-badge risk-${{impact.riskTier}}`;

  const scoreBar = document.getElementById('blast-score-bar');
  scoreBar.style.width = `${{impact.riskScore * 100}}%`;
  scoreBar.style.background = impact.riskTier === 'CRITICAL' ? '#d93025' : (impact.riskTier === 'HIGH' ? '#ea8600' : (impact.riskTier === 'MEDIUM' ? '#f9ab00' : '#137333'));
  document.getElementById('blast-score-val').textContent = impact.riskScore.toFixed(3);

  document.getElementById('blast-downstream-count').textContent = impact.downstreamCount;
  document.getElementById('blast-comm-count').textContent = impact.communitiesCrossed.length;
  document.getElementById('blast-test-count').textContent = impact.affectedTests.length;
  document.getElementById('blast-api-count').textContent = impact.affectedApis.length;
  document.getElementById('blast-explanation').textContent = impact.explanation;

  const apisSec = document.getElementById('blast-apis-section');
  const apisList = document.getElementById('blast-apis-list');
  if (impact.affectedApis.length) {{
    apisSec.style.display = 'block';
    document.getElementById('blast-apis-num').textContent = impact.affectedApis.length;
    apisList.innerHTML = impact.affectedApis.map(a => `
      <div class="closure-item is-api" data-nid="${{esc(a.id)}}">${{esc(a.label)}} (depth ${{a.depth}})</div>
    `).join('');
  }} else {{
    apisSec.style.display = 'none';
  }}

  const testsSec = document.getElementById('blast-tests-section');
  const testsList = document.getElementById('blast-tests-list');
  if (impact.affectedTests.length) {{
    testsSec.style.display = 'block';
    document.getElementById('blast-tests-num').textContent = impact.affectedTests.length;
    testsList.innerHTML = impact.affectedTests.map(t => `
      <div class="closure-item is-test" data-nid="${{esc(t.id)}}">${{esc(t.label)}} (depth ${{t.depth}})</div>
    `).join('');
  }} else {{
    testsSec.style.display = 'none';
  }}

  const closureList = document.getElementById('blast-closure-list');
  document.getElementById('blast-closure-num').textContent = impact.downstreamCount;
  closureList.innerHTML = impact.downstream.slice(0, 50).map(d => `
    <div class="closure-item" data-nid="${{esc(d.id)}}">[${{esc(d.relation)}}] ${{esc(d.label)}} (depth ${{d.depth}})</div>
  `).join('');

  // Graph styling updates
  const affectedSet = new Set(impact.downstream.map(d => String(d.id)));
  affectedSet.add(String(targetId));

  const updates = RAW_NODES.map(n => {{
    const sId = String(n.id);
    if (sId === String(targetId)) {{
      return {{ id: n.id, color: {{ background: '#f9ab00', border: '#1a73e8' }}, borderWidth: 4 }};
    }} else if (affectedSet.has(sId)) {{
      const isTest = impact.affectedTests.some(t => String(t.id) === sId);
      const isApi = impact.affectedApis.some(a => String(a.id) === sId);
      const bColor = isTest ? '#1a73e8' : (isApi ? '#d93025' : '#ea8600');
      return {{ id: n.id, color: {{ background: n.color.background, border: bColor }}, borderWidth: 3 }};
    }} else {{
      return {{ id: n.id, color: {{ background: n.color.background, border: n.color.border, opacity: 0.15 }} }};
    }}
  }});
  nodesDS.update(updates);
}}

function clearBlastRadius() {{
  nodesDS.update(RAW_NODES.map(n => ({{
    id: n.id,
    color: n.color,
    borderWidth: 1.5,
    hidden: hiddenCommunities.has(n.community)
  }})));
  const panel = document.getElementById('blast-results-panel');
  if (panel) panel.style.display = 'none';
}}

const blastTargetSelect = document.getElementById('blast-target-select');
RAW_NODES.forEach(n => {{
  const opt = document.createElement('option');
  opt.value = n.id;
  opt.textContent = `${{n.label}} (${{n.file_type || 'node'}})`;
  blastTargetSelect.appendChild(opt);
}});

document.getElementById('btn-calc-blast').addEventListener('click', () => {{
  const val = blastTargetSelect.value;
  if (val) runBlastRadius(val);
}});

document.getElementById('btn-clear-blast').addEventListener('click', clearBlastRadius);

// Neighborhood & Community Slicing
let currentSliceNodeId = null;

function sliceNeighborhood(nodeId, hops) {{
  currentSliceNodeId = nodeId;
  const labelEl = document.getElementById('slice-node-label');
  const targetNode = RAW_NODES.find(n => String(n.id) === String(nodeId));
  if (labelEl) labelEl.textContent = targetNode ? targetNode.label : nodeId;

  if (hops === 0) {{
    nodesDS.update(RAW_NODES.map(n => ({{ id: n.id, hidden: hiddenCommunities.has(n.community) }})));
    return;
  }}

  const visited = new Set([String(nodeId)]);
  let currentFrontier = [String(nodeId)];

  for (let step = 0; step < hops; step++) {{
    const nextFrontier = [];
    currentFrontier.forEach(nid => {{
      const neighbors = network.getConnectedNodes(nid);
      neighbors.forEach(nbId => {{
        const sNbId = String(nbId);
        if (!visited.has(sNbId)) {{
          visited.add(sNbId);
          nextFrontier.push(sNbId);
        }}
      }});
    }});
    currentFrontier = nextFrontier;
  }}

  nodesDS.update(RAW_NODES.map(n => ({{
    id: n.id,
    hidden: !visited.has(String(n.id)) || hiddenCommunities.has(n.community)
  }})));
}}

function isolateCommunity(cid) {{
  const targetCid = Number(cid);
  nodesDS.update(RAW_NODES.map(n => ({{
    id: n.id,
    hidden: n.community !== targetCid
  }})));
}}

function resetSlicing() {{
  nodesDS.update(RAW_NODES.map(n => ({{
    id: n.id,
    hidden: hiddenCommunities.has(n.community)
  }})));
  const labelEl = document.getElementById('slice-node-label');
  if (labelEl) labelEl.textContent = '(None selected)';
  document.querySelectorAll('.slice-hop-btn').forEach(b => b.classList.remove('primary'));
}}

document.querySelectorAll('.slice-hop-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.slice-hop-btn').forEach(b => b.classList.remove('primary'));
    btn.classList.add('primary');
    const hops = parseInt(btn.dataset.hops, 10);
    const target = currentSelectedNodeId || (RAW_NODES[0] ? RAW_NODES[0].id : null);
    if (target) sliceNeighborhood(target, hops);
  }});
}});

const sliceCommSelect = document.getElementById('slice-comm-select');
const filterCommSelect = document.getElementById('filter-comm-select');
LEGEND.forEach(c => {{
  const opt1 = document.createElement('option');
  opt1.value = c.cid;
  opt1.textContent = `${{c.label}} (#${{c.cid}})`;
  sliceCommSelect.appendChild(opt1);

  const opt2 = document.createElement('option');
  opt2.value = c.cid;
  opt2.textContent = `${{c.label}} (#${{c.cid}})`;
  filterCommSelect.appendChild(opt2);
}});

document.getElementById('btn-isolate-comm').addEventListener('click', () => {{
  const cid = sliceCommSelect.value;
  if (cid !== '') isolateCommunity(cid);
}});

document.getElementById('btn-reset-slice').addEventListener('click', resetSlicing);

// Community Legend controls
const hiddenCommunities = new Set();
const selectAllCb = document.getElementById('select-all-cb');

function updateSelectAllState() {{
  const total = LEGEND.length;
  const hidden = hiddenCommunities.size;
  selectAllCb.checked = hidden === 0;
  selectAllCb.indeterminate = hidden > 0 && hidden < total;
}}

function toggleAllCommunities(hide) {{
  document.querySelectorAll('.legend-item').forEach(item => {{
    hide ? item.classList.add('dimmed') : item.classList.remove('dimmed');
  }});
  document.querySelectorAll('.legend-cb').forEach(cb => {{
    cb.checked = !hide;
  }});
  LEGEND.forEach(c => {{
    if (hide) hiddenCommunities.add(c.cid); else hiddenCommunities.delete(c.cid);
  }});
  const updates = RAW_NODES.map(n => ({{ id: n.id, hidden: hide }}));
  nodesDS.update(updates);
  updateSelectAllState();
}}

const legendEl = document.getElementById('legend');
LEGEND.forEach(c => {{
  const item = document.createElement('div');
  item.className = 'legend-item';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'legend-cb';
  cb.checked = true;
  cb.addEventListener('change', (e) => {{
    e.stopPropagation();
    if (cb.checked) {{
      hiddenCommunities.delete(c.cid);
      item.classList.remove('dimmed');
    }} else {{
      hiddenCommunities.add(c.cid);
      item.classList.add('dimmed');
    }}
    const updates = RAW_NODES
      .filter(n => n.community === c.cid)
      .map(n => ({{ id: n.id, hidden: !cb.checked }}));
    nodesDS.update(updates);
    updateSelectAllState();
  }});
  item.innerHTML = `<div class="legend-dot" style="background:${{c.color}}"></div>
    <span class="legend-label">${{c.label}}</span>
    <span class="legend-count">${{c.count}}</span>`;
  item.prepend(cb);
  item.onclick = (e) => {{
    if (e.target === cb) return;
    cb.checked = !cb.checked;
    cb.dispatchEvent(new Event('change'));
  }};
  legendEl.appendChild(item);
}});

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
</script>"""  # nosec B608


def _html_document_title(output_path: str) -> str:
    """Return a portable label for the graph.html <title>.

    Tracked artifacts must not embed the generator host absolute path
    (regression of #433; reported again as #2598 on Windows). Keep from the
    configured output-dir bare name (``graph_fy_out`` / ``graph_fy_OUT``
    basename) onward — portable in every case; otherwise fall back to a
    cwd-relative label, and finally the filename only.
    """
    from graph_fy.paths import graph_fy_OUT_NAME

    raw = str(output_path).replace("\\", "/")
    # Drop Windows drive prefix so Path parts are comparable on any OS.
    if len(raw) >= 3 and raw[1] == ":" and raw[0].isalpha() and raw[2] == "/":
        raw = raw[2:]  # "/Users/..." style after drive strip
    p = Path(raw)

    parts = list(Path(raw).parts)
    # Path("C:/Users/..") on POSIX may keep "C:" as first part — strip it.
    if parts and len(parts[0]) == 2 and parts[0][1] == ":" and parts[0][0].isalpha():
        parts = parts[1:]
    # Prefer keeping from the output-dir marker onward: portable in every
    # case, whereas a cwd-relative path still leaks host/user segments when
    # the graph is built from a directory ABOVE the project (#2598 follow-up).
    marker = graph_fy_OUT_NAME
    for i, part in enumerate(parts):
        if part == marker or part.startswith("graph_fy_out"):
            return "/".join(parts[i:])

    # No standard out-dir marker (fully custom output path): fall back to a
    # cwd-relative label when the target is under cwd, else the bare filename.
    try:
        resolved = p if p.is_absolute() else (Path.cwd() / p)
        rel = resolved.resolve().relative_to(Path.cwd().resolve())
        label = rel.as_posix()
        if label and label != ".":
            return label
    except (ValueError, OSError, RuntimeError):
        pass

    name = p.name
    return name if name else "graph.html"

def to_html(
    G: nx.Graph,
    communities: dict[int, list[str]],
    output_path: str,
    community_labels: dict[int, str] | None = None,
    member_counts: dict[int, int] | None = None,
    node_limit: int | None = None,
    learning_overlay: dict | None = None,
) -> bool:
    """Generate an interactive vis.js HTML visualization of the graph.

    Features: node size by degree, click-to-inspect panel, search box,
    community filter, physics clustering by community, confidence-styled edges.
    Raises ValueError if graph exceeds MAX_NODES_FOR_VIZ.

    If member_counts is provided (aggregated community view), node sizes are
    based on community member counts rather than graph degree.

    If node_limit is set and the graph exceeds it, automatically builds an
    aggregated community-level meta-graph instead of raising ValueError.

    Returns True when the output was written. Returns False when an aggregated
    view would contain fewer than two communities and is intentionally skipped.
    """
    limit = node_limit if node_limit is not None else _viz_node_limit()
    if G.number_of_nodes() > limit:
        if node_limit is not None:
            # Build aggregated community meta-graph
            from collections import Counter as _Counter
            import networkx as _nx
            print(f"Graph has {G.number_of_nodes()} nodes (above {limit} limit). Building aggregated community view...")
            node_to_community = {nid: cid for cid, members in communities.items() for nid in members}
            meta = _nx.Graph()
            for cid, members in communities.items():
                meta.add_node(str(cid), label=(community_labels or {}).get(cid, f"Community {cid}"))
            edge_counts = _Counter()
            for u, v in G.edges():
                cu, cv = node_to_community.get(u), node_to_community.get(v)
                if cu is not None and cv is not None and cu != cv:
                    edge_counts[(min(cu, cv), max(cu, cv))] += 1
            for (cu, cv), w in edge_counts.items():
                meta.add_edge(str(cu), str(cv), weight=w,
                              relation=f"{w} cross-community edges", confidence="AGGREGATED")
            if meta.number_of_nodes() <= 1:
                print("Single community - aggregated view not useful. Skipping graph.html.")
                return False
            meta_communities = {cid: [str(cid)] for cid in communities}
            mc = {cid: len(members) for cid, members in communities.items()}
            # Remap hyperedges from semantic node IDs to community IDs
            raw_hyperedges = G.graph.get("hyperedges", [])
            if raw_hyperedges:
                remapped = []
                for he in raw_hyperedges:
                    he_members = he.get("nodes", [])
                    comm_ids, seen = [], set()
                    for nid in he_members:
                        c = node_to_community.get(nid)
                        if c is None:
                            continue
                        s = str(c)
                        if s in seen:
                            continue
                        seen.add(s)
                        comm_ids.append(s)
                    if len(comm_ids) < 2:
                        continue
                    remapped.append({
                        "id": he.get("id", ""),
                        "label": he.get("label") or he.get("relation", "").replace("_", " "),
                        "nodes": comm_ids,
                    })
                meta.graph["hyperedges"] = remapped
            written = to_html(meta, meta_communities, output_path,
                              community_labels=community_labels, member_counts=mc)
            if not written:
                return False
            print(f"graph.html written (aggregated: {meta.number_of_nodes()} community nodes, {meta.number_of_edges()} cross-community edges)")
            print("Tip: run with --obsidian for full node-level detail.")
            return True
        raise ValueError(
            f"Graph has {G.number_of_nodes()} nodes - too large for HTML viz "
            f"(limit: {limit}). Use --no-viz, raise graph_fy_VIZ_NODE_LIMIT, "
            f"or reduce input size."
        )

    node_community = _node_community_map(communities)
    degree = dict(G.degree())
    max_deg = max(degree.values(), default=1) or 1
    max_mc = (max(member_counts.values(), default=1) or 1) if member_counts else 1

    # Work-memory overlay (derived sidecar). When not passed explicitly, load it
    # best-effort from the sibling .graph_fy_learning.json next to the output
    # graph.html (which lives beside graph.json). Empty/missing => no learning
    # fields, so the un-annotated render is byte-identical to pre-feature.
    if learning_overlay is None:
        learning_overlay = {}
        try:
            from graph_fy.reflect import load_learning_overlay as _llo
            learning_overlay = _llo(Path(output_path))
        except Exception:
            learning_overlay = {}
    # Status -> ring color. preferred=green, contested=amber. Tentative gets no
    # ring (it's not yet trustworthy enough to highlight in the map).
    _RING = {"preferred": "#22c55e", "contested": "#f59e0b"}

    # Build nodes list for vis.js
    vis_nodes = []
    for node_id, data in G.nodes(data=True):
        cid = node_community.get(node_id, 0)
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        label = sanitize_label(data.get("label", node_id))
        deg = degree.get(node_id, 1)
        if member_counts:
            mc = member_counts.get(cid, 1)
            size = 10 + 30 * (mc / max_mc)
            font_size = 12
        else:
            size = 10 + 30 * (deg / max_deg)
            # Only show label for high-degree nodes by default; others show on hover
            font_size = 12 if deg >= max_deg * 0.15 else 0
        node = {
            "id": node_id,
            "label": label,
            "color": {"background": color, "border": color, "highlight": {"background": "#ffffff", "border": color}},
            "size": round(size, 1),
            "font": {"size": font_size, "color": "#202124", "strokeWidth": 2, "strokeColor": "#ffffff"},
            "title": label,
            "community": cid,
            "community_name": sanitize_label((community_labels or {}).get(cid, f"Community {cid}")),
            "source_file": sanitize_label(str(data.get("source_file") or "")),
            "file_type": data.get("file_type", ""),
            "degree": deg,
            "repo": sanitize_label(str(data.get("repo") or "")),
        }
        # Conditional learning fields — only present for annotated nodes, so
        # un-annotated output keeps the exact pre-feature node dict shape.
        entry = learning_overlay.get(str(node_id)) if learning_overlay else None
        if entry:
            status = sanitize_label(str(entry.get("status", "")))
            stale = bool(entry.get("stale"))
            node["learning_status"] = status
            node["learning_stale"] = stale
            ring = _RING.get(status)
            if ring:
                # Status-colored ring via the border; stale => desaturated +
                # dashed (vis.js supports per-node `shapeProperties.borderDashes`).
                if stale:
                    ring = "#9ca3af"
                    node["shapeProperties"] = {"borderDashes": [4, 4]}
                node["borderWidth"] = 3
                node["color"] = {
                    "background": color, "border": ring,
                    "highlight": {"background": "#ffffff", "border": ring},
                }
            # Lesson line appended to the hover title.
            if status == "contested":
                lesson = f"Lesson: contested (useful {entry.get('uses', 0)} / dead-end {entry.get('neg', 0)})"
            elif status == "preferred":
                lesson = f"Lesson: preferred source ({entry.get('uses', 0)} useful, score={entry.get('score', 0)})"
            else:
                lesson = f"Lesson: {status} ({entry.get('uses', 0)} useful)"
            if stale:
                lesson += " [code changed — re-verify]"
            node["title"] = f"{label}\n{sanitize_label(lesson)}"
        vis_nodes.append(node)

    # Build edges list. Restore original edge direction from _src/_tgt
    # (stashed by build.py for exactly this reason): undirected NetworkX
    # canonicalizes endpoint order, which would otherwise flip the arrow
    # for `calls` and `rationale_for` in the rendered graph (#563).
    vis_edges = []
    for u, v, data in G.edges(data=True):
        confidence = data.get("confidence", "EXTRACTED")
        relation = data.get("relation", "")
        true_src = data.get("_src", u)
        true_tgt = data.get("_tgt", v)
        vis_edges.append({
            "from": true_src,
            "to": true_tgt,
            "label": relation,
            "title": sanitize_label(f"{relation} [{confidence}]"),
            "dashes": confidence != "EXTRACTED",
            "width": 2 if confidence == "EXTRACTED" else 1,
            "color": {"opacity": 0.7 if confidence == "EXTRACTED" else 0.35},
            "confidence": confidence,
        })

    # Build community legend data
    legend_data = []
    for cid in sorted((community_labels or {}).keys()):
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        lbl = _html.escape(sanitize_label((community_labels or {}).get(cid, f"Community {cid}")))
        n = member_counts.get(cid, len(communities.get(cid, []))) if member_counts else len(communities.get(cid, []))
        legend_data.append({"cid": cid, "color": color, "label": lbl, "count": n})

    # Escape </script> sequences so embedded JSON cannot break out of the script tag
    def _js_safe(obj) -> str:
        return json.dumps(obj).replace("</", "<\\/")

    nodes_json = _js_safe(vis_nodes)
    edges_json = _js_safe(vis_edges)
    legend_json = _js_safe(legend_data)
    hyperedges_json = _js_safe(getattr(G, "graph", {}).get("hyperedges", []))
    title = _html.escape(sanitize_label(_html_document_title(output_path)))
    stats = f"{G.number_of_nodes()} nodes &middot; {G.number_of_edges()} edges &middot; {len(communities)} communities"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>graph_fy - {title}</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"
        integrity="sha384-Ux6phic9PEHJ38YtrijhkzyJ8yQlH8i/+buBR8s3mAZOJrP1gwyvAcIYl3GWtpX1"
        crossorigin="anonymous"></script>
{_html_styles()}
</head>
<body>
<div id="graph"></div>
<div id="sidebar">
  <div id="sidebar-tabs">
    <button class="tab-btn active" data-tab="tab-info">Info</button>
    <button class="tab-btn" data-tab="tab-sql">SQL &amp; Query</button>
    <button class="tab-btn" data-tab="tab-blast">Blast Radius</button>
    <button class="tab-btn" data-tab="tab-slice">Slice</button>
    <button class="tab-btn" data-tab="tab-legend">Legend</button>
    <button id="themeToggle" class="tab-btn" onclick="toggleTheme()" title="Toggle dark/light mode" style="flex:0 0 36px; font-size:13px; padding:0 8px;">🌓</button>
  </div>

  <div id="tab-info" class="tab-pane active">
    <div id="search-wrap">
      <input id="search" type="text" placeholder="Search nodes..." autocomplete="off">
      <div id="search-results"></div>
    </div>
    <div id="info-panel">
      <h3>Node Info</h3>
      <div id="info-content"><span class="empty">Click a node to inspect it</span></div>
    </div>
  </div>

  <div id="tab-sql" class="tab-pane">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <h3 style="font-size:12px; text-transform:uppercase; color:var(--muted); font-weight:600;">In-Browser SQL Query Engine</h3>
      <button id="toggle-schema-btn" class="action-btn" style="font-size:10px; padding:2px 8px;">Schema</button>
    </div>
    <div id="db-schema-card" class="schema-box" style="display:none;">
-- Client-Side WebAssembly (WASM) / SQL Explorer Schema --
TABLE nodes (
  id TEXT PRIMARY KEY,
  label TEXT,
  file_type TEXT,
  community INTEGER,
  community_name TEXT,
  source_file TEXT,
  degree INTEGER,
  size REAL
);

TABLE edges (
  id INTEGER PRIMARY KEY,
  from TEXT, -- alias: source
  to TEXT,   -- alias: target
  relation TEXT,
  confidence TEXT,
  width INTEGER
);
    </div>

    <div style="margin-bottom:8px;">
      <select id="sql-presets" style="width:100%; background:#ffffff; border:1px solid var(--border); color:var(--text); padding:6px 8px; font-size:11.5px; border-radius:8px; outline:none;">
        <option value="">-- Preset SQL Queries --</option>
        <option value="SELECT id, label, degree, file_type FROM nodes ORDER BY degree DESC LIMIT 10">Top Degree Hub Nodes</option>
        <option value="SELECT id, label, source_file FROM nodes WHERE label LIKE '%test%' OR source_file LIKE '%test%'">All Test Nodes</option>
        <option value="SELECT id, label, file_type FROM nodes WHERE file_type = 'api' OR label LIKE 'GET%' OR label LIKE 'POST%'">API Endpoints</option>
        <option value="SELECT id, from, to, relation, confidence FROM edges WHERE relation = 'calls'">Calls Execution Edges</option>
        <option value="SELECT id, from, to, relation FROM edges WHERE confidence = 'AGGREGATED'">Cross-Community Aggregated Edges</option>
        <option value="SELECT id, label, community_name, degree FROM nodes WHERE degree > 1">Active Multi-Connected Nodes</option>
      </select>
    </div>

    <textarea id="sql-input" rows="3" placeholder="SELECT * FROM nodes WHERE degree > 2"></textarea>
    <div style="display:flex; gap:6px; margin-bottom:8px;">
      <button id="btn-run-sql" class="action-btn primary" style="flex:1;">Run SQL</button>
      <button id="btn-clear-sql" class="action-btn">Clear</button>
    </div>

    <div style="border-top:1px solid var(--border); padding-top:8px; margin-top:4px;">
      <h4 style="font-size:11px; text-transform:uppercase; color:var(--muted); margin-bottom:6px; font-weight:600;">Ad-Hoc Predicate Filter</h4>
      <input id="filter-label" type="text" placeholder="Filter by label substring..." style="width:100%; background:#ffffff; border:1px solid var(--border); color:var(--text); padding:6px 8px; border-radius:8px; font-size:11.5px; margin-bottom:6px; outline:none;">
      <div style="display:flex; gap:4px; margin-bottom:6px;">
        <input id="filter-type" type="text" placeholder="Type (e.g. py, api)" style="flex:1; background:#ffffff; border:1px solid var(--border); color:var(--text); padding:6px 8px; border-radius:8px; font-size:11.5px; outline:none;">
        <select id="filter-comm-select" style="flex:1; background:#ffffff; border:1px solid var(--border); color:var(--text); padding:6px 8px; border-radius:8px; font-size:11.5px; outline:none;">
          <option value="">All Communities</option>
        </select>
      </div>
      <div style="display:flex; gap:4px;">
        <button id="btn-apply-filter" class="action-btn" style="flex:1;">Apply Filter</button>
        <button id="btn-reset-filter" class="action-btn">Reset</button>
      </div>
    </div>

    <div id="query-results-wrap" style="margin-top:8px; flex:1; display:flex; flex-direction:column; min-height:140px;">
      <div id="query-status" style="font-size:11px; color:var(--muted); margin-bottom:4px;">Ready. Enter query or choose preset.</div>
      <div id="query-results" class="results-table-wrap" style="display:none;"></div>
      <div id="query-actions" style="display:none; margin-top:6px; gap:6px;">
        <button id="btn-highlight-sql" class="action-btn" style="flex:1; font-size:10px;">Highlight Matches</button>
        <button id="btn-isolate-sql" class="action-btn" style="flex:1; font-size:10px;">Isolate in Graph</button>
      </div>
    </div>
  </div>

  <div id="tab-blast" class="tab-pane">
    <h3 style="font-size:12px; text-transform:uppercase; color:var(--muted); margin-bottom:8px; font-weight:600;">Predictive Blast Radius &amp; Impact</h3>
    <div style="margin-bottom:8px;">
      <label style="font-size:11px; color:var(--muted); display:block; margin-bottom:4px;">Target Symbol / Node:</label>
      <select id="blast-target-select" style="width:100%; background:#ffffff; border:1px solid var(--border); color:var(--text); padding:6px 8px; border-radius:8px; font-size:12px; outline:none;">
        <option value="">-- Select or Click a Node --</option>
      </select>
    </div>
    <div style="display:flex; gap:6px; margin-bottom:10px;">
      <button id="btn-calc-blast" class="action-btn primary" style="flex:1;">💥 Calculate Blast Radius</button>
      <button id="btn-clear-blast" class="action-btn">Reset</button>
    </div>

    <div id="blast-results-panel" style="display:none;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span style="font-size:12px; font-weight:600; color:var(--text);" id="blast-target-name">-</span>
        <span id="blast-risk-badge" class="risk-badge risk-LOW">LOW</span>
      </div>
      <div style="background:#e8eaed; border-radius:4px; height:6px; overflow:hidden; margin-bottom:8px;">
        <div id="blast-score-bar" style="background:#137333; height:100%; width:0%; transition:width 0.3s;"></div>
      </div>
      <div style="font-size:11px; color:var(--muted); margin-bottom:8px;">
        Risk Score: <b id="blast-score-val" style="color:var(--text);">0.000</b> / 1.000
      </div>

      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-val" id="blast-downstream-count">0</div>
          <div class="metric-label">Downstream</div>
        </div>
        <div class="metric-card">
          <div class="metric-val" id="blast-comm-count">0</div>
          <div class="metric-label">Communities</div>
        </div>
        <div class="metric-card">
          <div class="metric-val" id="blast-test-count" style="color:#1a73e8;">0</div>
          <div class="metric-label">Tests</div>
        </div>
        <div class="metric-card">
          <div class="metric-val" id="blast-api-count" style="color:#d93025;">0</div>
          <div class="metric-label">APIs</div>
        </div>
      </div>

      <div id="blast-explanation" style="font-size:11px; color:var(--text); line-height:1.4; margin-bottom:10px; padding:8px; background:#f8f9fa; border:1px solid var(--border); border-radius:6px;"></div>

      <div id="blast-breakdown-wrap">
        <div id="blast-apis-section" style="display:none; margin-bottom:8px;">
          <div style="font-size:11px; font-weight:600; color:#d93025; margin-bottom:4px;">Affected APIs (<span id="blast-apis-num">0</span>)</div>
          <div id="blast-apis-list" style="max-height:90px; overflow-y:auto;"></div>
        </div>
        <div id="blast-tests-section" style="display:none; margin-bottom:8px;">
          <div style="font-size:11px; font-weight:600; color:#1a73e8; margin-bottom:4px;">Affected Tests (<span id="blast-tests-num">0</span>)</div>
          <div id="blast-tests-list" style="max-height:90px; overflow-y:auto;"></div>
        </div>
        <div id="blast-closure-section" style="margin-bottom:8px;">
          <div style="font-size:11px; font-weight:600; color:var(--text); margin-bottom:4px;">Downstream Closure (<span id="blast-closure-num">0</span>)</div>
          <div id="blast-closure-list" style="max-height:120px; overflow-y:auto;"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="tab-slice" class="tab-pane">
    <h3 style="font-size:12px; text-transform:uppercase; color:var(--muted); margin-bottom:8px; font-weight:600;">Neighborhood Slicing</h3>
    <div style="font-size:11px; color:var(--muted); margin-bottom:6px;">
      Focused Node: <b id="slice-node-label" style="color:var(--text);">(None selected)</b>
    </div>
    <div style="margin-bottom:12px;">
      <label style="font-size:11px; color:var(--muted); display:block; margin-bottom:4px;">Hop Distance:</label>
      <div style="display:flex; gap:4px;">
        <button class="action-btn slice-hop-btn" data-hops="0" style="flex:1;">All</button>
        <button class="action-btn slice-hop-btn" data-hops="1" style="flex:1;">1-Hop</button>
        <button class="action-btn slice-hop-btn" data-hops="2" style="flex:1;">2-Hop</button>
        <button class="action-btn slice-hop-btn" data-hops="3" style="flex:1;">3-Hop</button>
      </div>
    </div>

    <div style="border-top:1px solid var(--border); padding-top:8px; margin-top:8px;">
      <h3 style="font-size:12px; text-transform:uppercase; color:var(--muted); margin-bottom:8px; font-weight:600;">Community Isolation</h3>
      <select id="slice-comm-select" style="width:100%; background:#ffffff; border:1px solid var(--border); color:var(--text); padding:6px 8px; border-radius:8px; font-size:11.5px; margin-bottom:6px; outline:none;">
        <option value="">-- Choose Community --</option>
      </select>
      <div style="display:flex; gap:6px;">
        <button id="btn-isolate-comm" class="action-btn" style="flex:1;">Isolate Community</button>
        <button id="btn-reset-slice" class="action-btn" style="flex:1;">Reset View</button>
      </div>
    </div>
  </div>

  <div id="tab-legend" class="tab-pane">
    <div id="legend-wrap">
      <h3>Communities</h3>
      <div id="legend-controls">
        <label><input type="checkbox" id="select-all-cb" checked onchange="toggleAllCommunities(!this.checked)">Select All</label>
      </div>
      <div id="legend"></div>
    </div>
  </div>

  <div id="stats">{stats}</div>
</div>
{_html_script(nodes_json, edges_json, legend_json)}
{_hyperedge_script(hyperedges_json)}
</body>
</html>"""  # nosec B608

    write_text_atomic(output_path, html)
    return True
