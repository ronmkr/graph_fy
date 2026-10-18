"""graph_fy presentation layer for architecture visualization (SVG and HTML)."""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any

from graph_fy.templates import get_template

def _render_corner_ticks(x: float, y: float, w: float, h: float, size: float = 6.0, color: str = "#38bdf8", opacity: float = 0.5) -> str:
    """Render blueprint CAD crosshairs at container corners for jev.ai machine aesthetic."""
    s = size
    return "".join(
        f'<path d="M {px - s} {py} L {px + s} {py} M {px} {py - s} L {px} {py + s}" stroke="{color}" stroke-width="1.2" opacity="{opacity}" />'
        for px, py in [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    )


def _infer_decision_badge(step: dict[str, Any]) -> tuple[str, str]:
    """Infer deterministic machine decision status tag and accent color for jev.ai primitives."""
    cat = step.get("category_tag", "").lower()
    sinks = step.get("sinks", [])
    if any(k in cat for k in ("auth", "guard", "eval", "check", "valid")):
        return "EVAL: VERIFIED", "#10b981"
    if any(k in cat for k in ("db", "table", "sql", "persist", "sink", "store")) or sinks:
        sink_name = sinks[0][:8].upper() if sinks else "COMMIT"
        return f"SINK: {sink_name}", "#34d399"
    if any(k in cat for k in ("route", "api", "http", "ingress", "webhook")):
        return "INGRESS: 200 OK", "#38bdf8"
    if any(k in cat for k in ("gateway", "external", "remote", "client", "network", "rpc")):
        return "DISPATCH: SYNC", "#c084fc"
    if any(k in cat for k in ("queue", "topic", "event", "pubsub")):
        return "EVENT: EMIT", "#fbbf24"
    return "EXEC: DETERMINISTIC", "#f43f5e"


def generate_architecture_svg(stages: list[dict[str, Any]]) -> str:
    """Render an interactive, hardware-animated SVG architecture flow following the jev.ai design language."""
    stage_width = 340
    stage_gap = 56
    margin_x = 44
    margin_y = 70
    step_height = 104
    step_gap = 22
    header_height = 80

    max_steps = max((len(s["steps"]) for s in stages), default=1)
    svg_height = margin_y * 2 + header_height + max_steps * (step_height + step_gap) + 60
    svg_width = margin_x * 2 + len(stages) * stage_width + (len(stages) - 1) * stage_gap
    total_symbols = sum(s.get("symbols_count", 0) for s in stages)

    svg_elements = []

    # SVG Defs: Blueprint grid pattern, glow filters, arrow markers and linear gradients
    svg_elements.append("""
    <defs>
      <!-- Blueprint dot matrix pattern -->
      <pattern id="blueprint-dots" width="20" height="20" patternUnits="userSpaceOnUse">
        <circle cx="2" cy="2" r="0.9" fill="#38bdf8" opacity="0.14" />
      </pattern>

      <!-- Glowing packet filters -->
      <filter id="packet-glow" x="-100%" y="-100%" width="300%" height="300%">
        <feGaussianBlur stdDeviation="3.5" result="blur" />
        <feColorMatrix type="matrix" values="0 0 0 0 0.22  0 0 0 0 0.74  0 0 0 0 0.97  0 0 0 1 0"/>
        <feMerge>
          <feMergeNode />
          <feMergeNode in="SourceGraphic" />
        </feMerge>
      </filter>

      <!-- Arrow Markers -->
      <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="#64748b" />
      </marker>
      <marker id="arrow-active" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="#38bdf8" />
      </marker>

      <!-- Jev Gradient Cards -->
      <linearGradient id="card-grad" x1="0%" y1="0%" x2="0%" y2="100%">
        <stop offset="0%" stop-color="#141d2e" stop-opacity="0.95" />
        <stop offset="100%" stop-color="#0a1120" stop-opacity="0.98" />
      </linearGradient>
    </defs>
    """)

    # Background canvas and blueprint dot grid
    svg_elements.append(f"""
    <!-- Deep Obsidian Backdrop -->
    <rect width="100%" height="100%" fill="#070b14" />
    <rect width="100%" height="100%" fill="url(#blueprint-dots)" />
    """)

    # Canvas Perimeter Corner Ticks
    svg_elements.append(_render_corner_ticks(margin_x - 14, margin_y - 40, svg_width - (margin_x - 14) * 2, svg_height - margin_y + 10, size=8, color="#38bdf8", opacity=0.7))

    # Top Live Flow Status & Telemetry Header
    svg_elements.append(f"""
    <g class="svg-telemetry-header">
      <!-- Live pulse indicator -->
      <circle cx="{margin_x + 8}" cy="{margin_y - 20}" r="4" fill="#10b981" />
      <circle cx="{margin_x + 8}" cy="{margin_y - 20}" r="7" fill="none" stroke="#10b981" stroke-width="1.2" opacity="0.6">
        <animate attributeName="r" values="4;9;4" dur="2s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.8;0;0.8" dur="2s" repeatCount="indefinite" />
      </circle>
      <text x="{margin_x + 24}" y="{margin_y - 16}" fill="#10b981" font-size="11" font-weight="700" font-family="ui-monospace, monospace" letter-spacing="0.06em">
        PIPELINE MONITOR // LIVE FLOW ACTIVE
      </text>

      <!-- Right Telemetry Readouts -->
      <text x="{svg_width - margin_x}" y="{margin_y - 16}" fill="#64748b" font-size="11" font-weight="600" text-anchor="end" font-family="ui-monospace, monospace">
        [STAGES: {len(stages)}]  [AST NODES: {total_symbols}]  [MOTION: CUBIC-BEZIER]  [SYNC: DETERMINISTIC]
      </text>
    </g>
    """)

    step_centers: dict[str, tuple[float, float]] = {}
    step_ports: dict[str, dict[str, tuple[float, float]]] = {}

    # Render Stage Columns and Step Nodes
    for s_idx, stage in enumerate(stages):
        stage_x = margin_x + s_idx * (stage_width + stage_gap)
        stage_y = margin_y
        stage_color = stage.get("color", "#38bdf8")
        col_height = header_height + len(stage["steps"]) * (step_height + step_gap) + 16

        # Stage Column Registration Ticks
        stage_ticks = _render_corner_ticks(stage_x, stage_y, stage_width, col_height, size=5, color=stage_color, opacity=0.55)

        # Deconstruct stage title and subsystem for clean, collision-free multi-line layout
        full_name = str(stage.get("name", "")).strip()
        subsystem = str(stage.get("subsystem", "")).strip()

        if " — " in full_name:
            main_title, sub_part = full_name.split(" — ", 1)
            main_title = main_title.strip()
            if not subsystem:
                subsystem = sub_part.strip()
        elif " (" in full_name and full_name.endswith(")"):
            main_title = full_name[:full_name.rfind(" (")].strip()
            if not subsystem:
                subsystem = full_name[full_name.rfind(" (") + 2:-1].strip()
        else:
            main_title = full_name

        display_title = main_title if len(main_title) <= 28 else main_title[:27] + "…"
        sub_display = subsystem if len(subsystem) <= 32 else subsystem[:31] + "…"

        # Stage background container
        svg_elements.append(f"""
        <g class="svg-stage-col" data-stage="{stage['id']}">
          <!-- Stage Background Frame -->
          <rect x="{stage_x}" y="{stage_y}" width="{stage_width}" height="{col_height}"
                rx="14" fill="rgba(12, 19, 34, 0.82)" stroke="#1e293b" stroke-width="1.4" />
          <!-- Top Accent Stripe -->
          <line x1="{stage_x + 14}" y1="{stage_y}" x2="{stage_x + stage_width - 14}" y2="{stage_y}"
                stroke="{stage_color}" stroke-width="2.5" opacity="0.9" />
          {stage_ticks}

          <!-- Row 1 Left: Stage Monospace Tag -->
          <text x="{stage_x + 16}" y="{stage_y + 22}" fill="#64748b" font-size="9.5" font-weight="700" font-family="ui-monospace, monospace" letter-spacing="0.08em">
            STAGE 0{s_idx + 1} // {_html.escape(stage['id'].upper())}
          </text>
          <!-- Row 1 Right: Stage Steps Pill Badge (Row 1 - Never Collides With Stage Title) -->
          <rect x="{stage_x + stage_width - 78}" y="{stage_y + 10}" width="62" height="18" rx="4"
                fill="rgba(255, 255, 255, 0.05)" stroke="{stage_color}" stroke-width="0.8" opacity="0.9" />
          <text x="{stage_x + stage_width - 47}" y="{stage_y + 22.5}" fill="{stage_color}" font-size="8.5" font-weight="700" text-anchor="middle" font-family="ui-monospace, monospace">
            {len(stage['steps'])} STEPS
          </text>

          <!-- Row 2: Full-Width Stage Title -->
          <text x="{stage_x + 16}" y="{stage_y + 44}" fill="#f8fafc" font-size="13" font-weight="700" font-family="system-ui, sans-serif">
            {stage['icon']} {_html.escape(display_title)}
          </text>
        """)

        if sub_display:
            svg_elements.append(f"""
          <!-- Row 3: Subsystem / Dominant Hub -->
          <text x="{stage_x + 16}" y="{stage_y + 62}" fill="{stage_color}" font-size="9.5" font-weight="600" font-family="ui-monospace, monospace" opacity="0.85">
            λ {_html.escape(sub_display)}
          </text>
            """)

        # Render Steps inside this stage
        for st_idx, step in enumerate(stage["steps"]):
            step_x = stage_x + 16
            step_y = stage_y + header_height + st_idx * (step_height + step_gap)
            node_w = stage_width - 32

            center_x = step_x + node_w / 2
            center_y = step_y + step_height / 2
            step_centers[step["id"]] = (center_x, center_y)

            # Ports
            step_ports[step["id"]] = {
                "in_left": (step_x, center_y),
                "out_right": (step_x + node_w, center_y),
                "in_top": (center_x, step_y),
                "out_bottom": (center_x, step_y + step_height),
            }

            first_sym = step["symbols"][0] if step.get("symbols") else None
            sym_id = first_sym.get("id", "") if first_sym else ""
            sym_label = first_sym.get("label", step.get("name", "")) if first_sym else step.get("name", "")
            sym_kind = first_sym.get("kind", "step") if first_sym else "step"

            badge_text, badge_color = _infer_decision_badge(step)
            cat_tag = step.get("category_tag", "STEP")
            step_num = step.get("step_number", f"{s_idx + 1}.{st_idx + 1}")
            step_name = step.get("name", "Step")
            in_type = step.get("input_type", "Any")
            out_type = step.get("output_type", "Any")

            clean_step_name = step_name if len(step_name) <= 28 else step_name[:27] + "…"
            clean_sym_label = sym_label if len(sym_label) <= 24 else sym_label[:23] + "…"

            in_words = in_type.strip().split()
            out_words = out_type.strip().split()
            clean_in = in_words[0] if in_words else "Any"
            clean_out = out_words[0] if out_words else "Any"

            # Card ticks
            card_ticks = _render_corner_ticks(step_x, step_y, node_w, step_height, size=3.5, color="#38bdf8", opacity=0.35)

            svg_elements.append(f"""
            <g class="svg-step-card" data-step-id="{step['id']}" data-sym-id="{_html.escape(sym_id)}" cursor="pointer">
              <!-- Card Surface -->
              <rect x="{step_x}" y="{step_y}" width="{node_w}" height="{step_height}"
                    rx="10" fill="url(#card-grad)" stroke="#223048" stroke-width="1.2" class="step-card-bg" />
              {card_ticks}

              <!-- Top Row: Category Pill & Step Sequence -->
              <rect x="{step_x + 10}" y="{step_y + 10}" width="68" height="17" rx="4" fill="rgba(56, 189, 248, 0.12)" stroke="rgba(56, 189, 248, 0.25)" stroke-width="1" />
              <text x="{step_x + 44}" y="{step_y + 22}" fill="#38bdf8" font-size="9" font-weight="700" text-anchor="middle" font-family="ui-monospace, monospace">
                {_html.escape(cat_tag.upper()[:10])}
              </text>
              <text x="{step_x + 84}" y="{step_y + 22}" fill="#64748b" font-size="10.5" font-family="ui-monospace, monospace">
                #{step_num}
              </text>

              <!-- Jev Machine Decision Badge -->
              <rect x="{step_x + node_w - 94}" y="{step_y + 10}" width="84" height="17" rx="4" fill="rgba(255, 255, 255, 0.04)" stroke="{badge_color}" stroke-width="0.8" opacity="0.85" />
              <text x="{step_x + node_w - 52}" y="{step_y + 22}" fill="{badge_color}" font-size="8.5" font-weight="700" text-anchor="middle" font-family="ui-monospace, monospace">
                {badge_text}
              </text>

              <!-- Step Action Title -->
              <text x="{step_x + 12}" y="{step_y + 45}" fill="#f1f5f9" font-size="12.5" font-weight="700" font-family="system-ui, sans-serif">
                {_html.escape(clean_step_name)}
              </text>

              <!-- AST Symbol Identifier Preview -->
              <text x="{step_x + 12}" y="{step_y + 65}" fill="#38bdf8" font-size="11" font-family="ui-monospace, monospace">
                λ {_html.escape(clean_sym_label)} <tspan fill="#64748b" font-size="9">({sym_kind})</tspan>
              </text>

              <!-- Contract Flow Footprint -->
              <text x="{step_x + 12}" y="{step_y + 85}" fill="#64748b" font-size="9.5" font-family="ui-monospace, monospace">
                in: <tspan fill="#94a3b8">{_html.escape(clean_in[:12])}</tspan> ➔ out: <tspan fill="#94a3b8">{_html.escape(clean_out[:12])}</tspan>
              </text>

              <!-- Hardware Ports (Ingress, Egress, Bus) -->
              <circle cx="{step_x}" cy="{center_y}" r="3.5" fill="#070b14" stroke="#38bdf8" stroke-width="1.5" />
              <circle cx="{step_x + node_w}" cy="{center_y}" r="3.5" fill="#38bdf8" stroke="#070b14" stroke-width="1.5" />
              <circle cx="{center_x}" cy="{step_y}" r="3" fill="#070b14" stroke="#475569" stroke-width="1.2" />
              <circle cx="{center_x}" cy="{step_y + step_height}" r="3" fill="#475569" stroke="#070b14" stroke-width="1.2" />
            </g>
            """)

        svg_elements.append("</g>")

    # Render Intra-Stage Vertical Connections with animated pulses
    for s_idx, stage in enumerate(stages):
        steps = stage["steps"]
        for st_idx in range(len(steps) - 1):
            src_id = steps[st_idx]["id"]
            tgt_id = steps[st_idx + 1]["id"]
            if src_id in step_ports and tgt_id in step_ports:
                _, y1 = step_ports[src_id]["out_bottom"]
                cx, _ = step_centers[src_id]
                _, y2 = step_ports[tgt_id]["in_top"]
                wire_id = f"wire-intra-{s_idx}-{st_idx}"

                svg_elements.append(f"""
                <!-- Intra-stage vertical bus wire -->
                <path id="{wire_id}" d="M {cx} {y1} L {cx} {y2}"
                      class="flow-wire intra-wire" stroke="#334155" stroke-width="1.8" stroke-dasharray="4 3" marker-end="url(#arrow)" />
                <!-- Traveling pulse -->
                <g class="flow-packet" pointer-events="none">
                  <circle r="4.5" fill="#38bdf8" opacity="0.35" filter="url(#packet-glow)">
                    <animateMotion dur="1.8s" repeatCount="indefinite" begin="{st_idx * 0.4}s">
                      <mpath href="#{wire_id}" xlink:href="#{wire_id}" />
                    </animateMotion>
                  </circle>
                  <circle r="2" fill="#ffffff">
                    <animateMotion dur="1.8s" repeatCount="indefinite" begin="{st_idx * 0.4}s">
                      <mpath href="#{wire_id}" xlink:href="#{wire_id}" />
                    </animateMotion>
                  </circle>
                </g>
                """)

    # Render Inter-Stage Smooth Cubic Bezier Connections with Animated Data Packets
    for s_idx in range(len(stages) - 1):
        curr_stage = stages[s_idx]
        next_stage = stages[s_idx + 1]

        if not curr_stage["steps"] or not next_stage["steps"]:
            continue

        src_id = curr_stage["steps"][-1]["id"]
        tgt_id = next_stage["steps"][0]["id"]

        if src_id in step_ports and tgt_id in step_ports:
            x1, y1 = step_ports[src_id]["out_right"]
            x2, y2 = step_ports[tgt_id]["in_left"]

            cx1 = x1 + (x2 - x1) * 0.5
            cx2 = x2 - (x2 - x1) * 0.5
            wire_id = f"wire-stage-{s_idx}"
            wire_color = curr_stage["color"]

            svg_elements.append(f"""
            <!-- Inter-stage S-curve bezier connection -->
            <path id="{wire_id}" d="M {x1} {y1} C {cx1} {y1}, {cx2} {y2}, {x2} {y2}"
                  class="flow-wire inter-wire" stroke="{wire_color}" stroke-width="2.2" stroke-dasharray="6 4" marker-end="url(#arrow)" />

            <!-- Traveling Glowing Data Packet -->
            <g class="flow-packet" pointer-events="none">
              <circle r="6" fill="{wire_color}" opacity="0.45" filter="url(#packet-glow)">
                <animateMotion dur="2.4s" repeatCount="indefinite" begin="{s_idx * 0.6}s">
                  <mpath href="#{wire_id}" xlink:href="#{wire_id}" />
                </animateMotion>
              </circle>
              <circle r="2.8" fill="#ffffff">
                <animateMotion dur="2.4s" repeatCount="indefinite" begin="{s_idx * 0.6}s">
                  <mpath href="#{wire_id}" xlink:href="#{wire_id}" />
                </animateMotion>
              </circle>
            </g>
            """)

    # Bottom Precision Legend Bar
    legend_y = svg_height - 24
    svg_elements.append(f"""
    <g class="svg-legend" transform="translate({margin_x}, {legend_y})">
      <circle cx="6" cy="0" r="3.5" fill="#38bdf8" />
      <text x="16" y="4" fill="#94a3b8" font-size="10.5" font-family="ui-monospace, monospace">● DATA PACKET</text>

      <line x1="120" y1="0" x2="146" y2="0" stroke="#334155" stroke-width="2" stroke-dasharray="4 3" />
      <text x="154" y="4" fill="#94a3b8" font-size="10.5" font-family="ui-monospace, monospace">INTRA-STAGE BUS</text>

      <line x1="280" y1="0" x2="310" y2="0" stroke="#38bdf8" stroke-width="2" stroke-dasharray="6 4" />
      <text x="318" y="4" fill="#94a3b8" font-size="10.5" font-family="ui-monospace, monospace">INTER-STAGE DISPATCH</text>

      <text x="490" y="4" fill="#38bdf8" font-size="10.5" font-family="ui-monospace, monospace">λ AST VERIFIED</text>
      <text x="610" y="4" fill="#64748b" font-size="10.5" font-family="ui-monospace, monospace">+ CALIBRATION GRID</text>
    </g>
    """)

    return f"""<svg id="arch-svg-canvas" viewBox="0 0 {svg_width} {svg_height}" width="{svg_width}" height="{svg_height}" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
      {''.join(svg_elements)}
    </svg>"""


def _safe_mermaid_color(color_str: str, fallback: str = "#f8f9fa") -> str:
    """Ensure color is a safe hex color without commas for Mermaid style directives."""
    c = str(color_str or "").strip()
    return c if c.startswith("#") else fallback


def _slug(s: str) -> str:
    return re.sub(r"\W+", "_", str(s or ""))


def _sanitize_mermaid_label(text: str) -> str:
    """Sanitize arbitrary text so it is safe inside Mermaid ["..."] quotes."""
    text = re.sub(r'["`#|]', "", str(text or "")).translate(str.maketrans("{}[]—–", "()()--"))
    text = re.sub(r"==>|-->|->", " to ", text).replace("&", "and")
    return _html.escape(" ".join(text.split()), quote=False)


def generate_architecture_mermaid(stages: list[dict[str, Any]], direction: str = "LR") -> str:
    """Generate Mermaid flowchart syntax representing the lifecycle stages and steps."""
    dir_mode = "TD" if direction.upper() == "TD" else "LR"
    lines = [f"flowchart {dir_mode}"]

    for s_idx, stage in enumerate(stages):
        safe_stage_id = f"stage_{_slug(stage['id'])}"
        st_name = _sanitize_mermaid_label(stage["name"])
        lines.append(f'    subgraph {safe_stage_id} ["{stage["icon"]} {st_name}"]')

        for st_idx, step in enumerate(stage["steps"]):
            step_id = _slug(step["id"])
            first_sym = step["symbols"][0] if step["symbols"] else None
            sym_label = first_sym["label"] if first_sym else ""
            clean_name = _sanitize_mermaid_label(step["name"])
            clean_sym = _sanitize_mermaid_label(sym_label)

            node_label = f"<b>{step['step_number']} {clean_name}</b>"
            if clean_sym:
                node_label += f"<br/><i>λ {clean_sym}</i>"
            lines.append(f'        {step_id}["{node_label}"]')

            if st_idx > 0:
                prev_id = _slug(stage["steps"][st_idx - 1]["id"])
                lines.append(f"        {prev_id} --> {step_id}")

        lines.append("    end")
        fill_color = _safe_mermaid_color(stage.get("bg_color"), "#0f172a")
        stroke_color = _safe_mermaid_color(stage.get("color"), "#38bdf8")
        lines.append(f"    style {safe_stage_id} fill:{fill_color},stroke:{stroke_color},stroke-width:1.5px")

    for s_idx in range(len(stages) - 1):
        curr_stage = stages[s_idx]
        next_stage = stages[s_idx + 1]
        if curr_stage["steps"] and next_stage["steps"]:
            src_step = _slug(curr_stage["steps"][-1]["id"])
            tgt_step = _slug(next_stage["steps"][0]["id"])
            lines.append(f"    {src_step} ==> {tgt_step}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 5. HTML Presentation Generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_architecture_html(arch_data: dict[str, Any], title: str | None = None) -> str:
    """Generate modern, interactive, self-contained HTML architecture dashboard."""
    project_name = title or arch_data["project_name"]
    stats = arch_data["stats"]
    stages = arch_data["stages"]
    svg_diagram = generate_architecture_svg(stages)
    mermaid_code_lr = generate_architecture_mermaid(stages, direction="LR")
    mermaid_code_td = generate_architecture_mermaid(stages, direction="TD")
    mermaid_code = mermaid_code_td  # Default rendered in HTML is TD for clean document flow

    # Encode JSON data for client-side search and inspector drawer
    data_json = json.dumps(arch_data, ensure_ascii=False)

    arch_css = get_template("architecture.css")
    arch_js = (
        get_template("architecture.js")
        .replace("__DATA_JSON__", data_json)
        .replace("__MERMAID_CODE_TD__", json.dumps(mermaid_code_td))
        .replace("__MERMAID_CODE_LR__", json.dumps(mermaid_code_lr))
    )

    # Precompute HTML snippets (Python 3.10+ compatible f-strings)
    stage_pills = [
        f'<div class="stage-pill" data-stage-filter="{s["id"]}">'
        f'<div class="stage-pill-head">'
        f'<span class="stage-pill-title">{s["icon"]} {_html.escape(s["name"])}</span>'
        f'<span class="stage-pill-badge">{len(s["steps"])} steps</span>'
        f'</div>'
        f'<div style="font-size: 11px; color: var(--muted);">{s["files_count"]} files · {s["symbols_count"]} symbols</div>'
        f'</div>'
        for s in stages
    ]
    stage_pills_html = "\n".join(stage_pills)

    pipeline_sections = []
    for s in stages:
        s_id = s["id"]
        s_icon = s["icon"]
        s_name = _html.escape(s["name"])
        s_desc = _html.escape(s["description"])
        n_steps = len(s["steps"])
        f_count = s["files_count"]

        step_cards = []
        for step in s["steps"]:
            step_id = step["id"]
            step_num = step["step_number"]
            cat_tag = step["category_tag"]
            step_name = _html.escape(step["name"])
            step_desc = _html.escape(step["description"])
            inp_type = _html.escape(step["input_type"])
            out_type = _html.escape(step["output_type"])
            n_syms = len(step["symbols"])

            sinks_html = ""
            if step["sinks"]:
                sinks_str = _html.escape(", ".join(step["sinks"]))
                sinks_html = f"<span class='contract-pill' style='color:#f472b6;'>Sinks: <strong>{sinks_str}</strong></span>"

            sym_chips = [
                f'<span class="symbol-chip" data-sym-id="{_html.escape(sym["id"])}">'
                f'λ {_html.escape(sym["label"])} '
                f'<span style="font-size: 10px; color: var(--muted);">({sym["kind"]})</span>'
                f'</span>'
                for sym in step["symbols"]
            ]
            sym_chips_html = "\n".join(sym_chips)

            step_cards.append(
                f'<div class="step-card" data-step-id="{step_id}">'
                f'<div class="step-card-header">'
                f'<div class="step-meta">'
                f'<span class="step-num-badge">STEP {step_num}</span>'
                f'<span class="step-role-badge">{cat_tag}</span>'
                f'<span class="step-title">{step_name}</span>'
                f'</div>'
                f'<div style="font-size: 12px; color: var(--muted);">{n_syms} symbols</div>'
                f'</div>'
                f'<p class="step-desc">{step_desc}</p>'
                f'<div class="step-contracts">'
                f'<span class="contract-pill">Input: <strong>{inp_type}</strong></span>'
                f'<span class="contract-pill">Output: <strong>{out_type}</strong></span>'
                f'{sinks_html}'
                f'</div>'
                f'<div class="symbol-chips">{sym_chips_html}</div>'
                f'</div>'
            )
        step_cards_html = "\n".join(step_cards)

        pipeline_sections.append(
            f'<section class="stage-section" id="section-{s_id}" data-stage="{s_id}">'
            f'<div class="stage-header">'
            f'<div><h2 class="stage-title">{s_icon} {s_name}</h2><p class="stage-desc">{s_desc}</p></div>'
            f'<div style="text-align: right; font-size: 12px; color: var(--muted);">'
            f'<div><strong>{n_steps}</strong> steps</div><div><strong>{f_count}</strong> source files</div>'
            f'</div>'
            f'</div>'
            f'<div class="steps-timeline">{step_cards_html}</div>'
            f'</section>'
        )
    pipeline_sections_html = "\n".join(pipeline_sections)

    matrix_rows = []
    for s in stages:
        s_icon = s["icon"]
        s_name = _html.escape(s["name"])
        for step in s["steps"]:
            step_num = step["step_number"]
            step_name = _html.escape(step["name"])
            cat_tag = step["category_tag"]
            inp_type = _html.escape(step["input_type"])
            out_type = _html.escape(step["output_type"])
            sinks_str = _html.escape(", ".join(step["sinks"]) if step["sinks"] else "None")

            sym_links = "".join(
                f"<a href='#' class='matrix-sym-link' data-sym-id='{_html.escape(sym['id'])}'>{_html.escape(sym['label'])}</a><br>"
                for sym in step["symbols"]
            )
            matrix_rows.append(
                f'<tr>'
                f'<td><strong>{s_icon} {s_name}</strong></td>'
                f'<td><code>{step_num}</code></td>'
                f'<td>{step_name}</td>'
                f'<td><span style="background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.25); color: #38bdf8; padding: 2px 8px; border-radius: 9999px; font-size: 11px;">{cat_tag}</span></td>'
                f'<td><code>{inp_type}</code></td>'
                f'<td><code>{out_type}</code></td>'
                f'<td>{sym_links}</td>'
                f'<td>{sinks_str}</td>'
                f'</tr>'
            )
    matrix_rows_html = "\n".join(matrix_rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;700&family=Roboto:wght@400;500;700&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <style>
{arch_css}
  </style>
</head>
<body>

  <!-- Top Navigation Header (Google Material App Bar) -->
  <header class="app-header">
    <div class="gnav-bar">
      <div class="brand">
        <span class="brand-badge">graph_fy arch</span>
        <h1 class="brand-title">{_html.escape(project_name)}</h1>
      </div>

      <div class="search-bar">
        <span class="search-icon">🔍</span>
        <input type="text" id="arch-search" class="search-input" placeholder="Search stages, steps, symbols, files (Press '/' to focus)...">
      </div>

      <div class="view-tabs">
        <button class="view-tab-btn active" data-view="pipeline">Pipeline Flow</button>
        <button class="view-tab-btn" data-view="diagram">Architecture Map</button>
        <button class="view-tab-btn" data-view="mermaid">Mermaid Diagram</button>
        <button class="view-tab-btn" data-view="matrix">Execution Matrix</button>
        <button id="themeToggle" class="view-tab-btn" onclick="toggleTheme()" title="Toggle dark/light mode">🌓</button>
      </div>
    </div>
  </header>

  <main class="container">
    <!-- Deduxer Editorial Hero Statement & Live Metric Strip -->
    <section class="hero-studio banner">
      <div class="hero-tag"><span class="pulse-dot"></span> ARCHITECTURAL SPECIFICATION &amp; AST FLOW</div>
      <h2 class="hero-title">{_html.escape(project_name)} Architecture</h2>
      <p class="hero-lead banner-summary">{arch_data['summary']}</p>
      <div class="stat-strip stats-row">
        <div class="stat-cell stat-card">
          <div class="stat-val">{stats['stages_count']}</div>
          <div class="stat-lbl">Lifecycle Stages</div>
        </div>
        <div class="stat-cell stat-card">
          <div class="stat-val">{stats['steps_count']}</div>
          <div class="stat-lbl">Execution Steps</div>
        </div>
        <div class="stat-cell stat-card">
          <div class="stat-val">{stats['symbols_count']}</div>
          <div class="stat-lbl">AST Symbols Indexed</div>
        </div>
        <div class="stat-cell stat-card">
          <div class="stat-val">{stats['sinks_count']}</div>
          <div class="stat-lbl">Data Sinks Reached</div>
        </div>
      </div>
    </section>

    <!-- Stage Chevron / Pipeline Ribbon -->
    <nav class="pipeline-ribbon" id="stage-ribbon">
      <div class="stage-pill active" data-stage-filter="all">
        <div class="stage-pill-head">
          <span class="stage-pill-title">All Stages</span>
          <span class="stage-pill-badge">{stats['stages_count']}</span>
        </div>
        <div style="font-size: 11px; color: var(--muted);">Full lifecycle pipeline</div>
      </div>
      {stage_pills_html}
    </nav>

    <!-- VIEW 1: Pipeline & Stages Flow -->
    <div id="view-pipeline" class="view-pane active">
      {pipeline_sections_html}
    </div>

    <!-- VIEW 2: Pure Vector SVG Architecture Diagram -->
    <div id="view-diagram" class="view-pane">
      <div class="svg-viewport-wrap">
        <div class="svg-controls">
          <button class="svg-btn" id="flow-toggle-btn" title="Pause / Play Flow Animation">⏸ Flow</button>
          <button class="svg-btn" id="flow-speed-btn" title="Toggle Speed (1x / 2x)">⚡ 1x</button>
          <button class="svg-btn" id="zoom-in" title="Zoom In">+</button>
          <button class="svg-btn" id="zoom-out" title="Zoom Out">−</button>
          <button class="svg-btn" id="zoom-reset" title="Reset View">↺</button>
        </div>
        <div id="svg-container">
          {svg_diagram}
        </div>
      </div>
    </div>

    <!-- VIEW 3: Execution Matrix Table -->
    <div id="view-matrix" class="view-pane">
      <div style="background: var(--surface); border: 1px solid var(--border); border-radius: 20px; overflow-x: auto; box-shadow: 0 16px 36px rgba(0,0,0,0.35);">
        <table class="matrix-table">
          <thead>
            <tr>
              <th>Stage</th>
              <th>Step #</th>
              <th>Action / Name</th>
              <th>Role</th>
              <th>Input Contract</th>
              <th>Output Contract</th>
              <th>Primary AST Symbols</th>
              <th>Sinks Reached</th>
            </tr>
          </thead>
          <tbody>
            {matrix_rows_html}
          </tbody>
        </table>
      </div>
    </div>

    <!-- VIEW 4: Mermaid Flowchart -->
    <div id="view-mermaid" class="view-pane">
      <div style="background: var(--surface); border: 1px solid var(--border); border-radius: 24px; padding: 28px; box-shadow: 0 16px 36px rgba(0,0,0,0.35);">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 14px;">
          <div>
            <h3 style="font-size: 1.25rem; font-weight: 700; color: var(--text); letter-spacing: -0.02em;">Mermaid Architectural Flowchart</h3>
            <p style="font-size: 0.9rem; color: var(--muted); margin-top: 4px;">Directly pasteable into GitHub markdown, PR reviews, Obsidian, and documentation.</p>
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <div style="display: inline-flex; background: var(--card); border: 1px solid var(--border); border-radius: 20px; padding: 2px;">
              <button id="mermaid-dir-td" class="pill-btn pill-btn-toggle active" style="border: none; border-radius: 16px; font-size: 12px; padding: 5px 12px;">↕ Vertical (TD)</button>
              <button id="mermaid-dir-lr" class="pill-btn pill-btn-toggle" style="border: none; border-radius: 16px; font-size: 12px; padding: 5px 12px;">↔ Horizontal (LR)</button>
            </div>
            <div style="display: inline-flex; gap: 4px;">
              <button id="mermaid-zoom-in" class="pill-btn" title="Zoom In" style="padding: 5px 10px;">＋</button>
              <button id="mermaid-zoom-out" class="pill-btn" title="Zoom Out" style="padding: 5px 10px;">－</button>
              <button id="mermaid-zoom-reset" class="pill-btn" title="Reset Zoom" style="padding: 5px 10px;">1:1</button>
            </div>
            <button id="toggle-mermaid-raw" class="pill-btn">Toggle Raw Syntax</button>
            <button id="copy-mermaid-btn" class="pill-btn-primary">Copy Mermaid 📋</button>
          </div>
        </div>

        <div id="mermaid-rendered-wrap" class="mermaid-viewport">
          <div id="mermaid-pan-zoom-container" style="display: flex; justify-content: center; transform-origin: 50% 0; transition: transform 120ms ease;">
            <pre class="mermaid" id="mermaid-target" style="background: transparent; border: none; font-size: 14px; margin: 0;">
{mermaid_code_td}
            </pre>
          </div>
        </div>

        <div id="mermaid-raw-wrap" style="display: none; margin-top: 16px;">
          <pre class="code-block" id="raw-mermaid-pre" style="max-height: 500px; overflow-y: auto;">{_html.escape(mermaid_code_td)}</pre>
        </div>
      </div>
    </div>
  </main>

  <!-- Slide-Over AST Code Inspector Drawer -->
  <div class="drawer-overlay" id="drawer-overlay"></div>
  <aside class="drawer" id="ast-drawer">
    <div class="drawer-header">
      <div>
        <div style="font-size: 11px; font-weight: 700; color: var(--accent); text-transform: uppercase;" id="drawer-kind">Function</div>
        <h3 style="font-size: 1.25rem; font-weight: 700;" id="drawer-title">Symbol</h3>
      </div>
      <button class="drawer-close" id="drawer-close-btn">&times;</button>
    </div>
    <div class="drawer-body">
      <div style="font-size: 12px; color: var(--muted); margin-bottom: 12px;">
        File: <code id="drawer-file" style="color: #cbd5e1;">src/file.py:L1</code>
      </div>

      <div class="drawer-sec-title">AST Code Skeleton</div>
      <pre class="code-block" id="drawer-skeleton">// Code skeleton will appear here</pre>

      <div class="drawer-sec-title">Inbound Callers (Called By)</div>
      <ul class="drawer-call-list" id="drawer-callers">
        <li style="color: var(--muted);">No inbound callers detected</li>
      </ul>

      <div class="drawer-sec-title">Outbound Callees (Calls)</div>
      <ul class="drawer-call-list" id="drawer-callees">
        <li style="color: var(--muted);">No outbound calls detected</li>
      </ul>

      <div class="drawer-sec-title">Sinks Reached</div>
      <div id="drawer-sinks" style="font-size: 13px; color: var(--muted);">None</div>
    </div>
  </aside>

  <!-- Client-side Interactive Application Logic -->
  <script>
{arch_js}
  </script>
</body>
</html>"""
    return html


