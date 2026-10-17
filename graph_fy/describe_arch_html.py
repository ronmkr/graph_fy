"""graph_fy presentation layer for architecture visualization (SVG and HTML)."""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any

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
        col_height = header_height + len(stage["steps"]) * (step_height + step_gap) + 16

        # Stage Column Registration Ticks
        stage_ticks = _render_corner_ticks(stage_x, stage_y, stage_width, col_height, size=5, color=stage["color"], opacity=0.55)

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
                stroke="{stage['color']}" stroke-width="2.5" opacity="0.9" />
          {stage_ticks}

          <!-- Row 1 Left: Stage Monospace Tag -->
          <text x="{stage_x + 16}" y="{stage_y + 22}" fill="#64748b" font-size="9.5" font-weight="700" font-family="ui-monospace, monospace" letter-spacing="0.08em">
            STAGE 0{s_idx + 1} // {_html.escape(stage['id'].upper())}
          </text>
          <!-- Row 1 Right: Stage Steps Pill Badge (Row 1 - Never Collides With Stage Title) -->
          <rect x="{stage_x + stage_width - 78}" y="{stage_y + 10}" width="62" height="18" rx="4"
                fill="rgba(255, 255, 255, 0.05)" stroke="{stage['color']}" stroke-width="0.8" opacity="0.9" />
          <text x="{stage_x + stage_width - 47}" y="{stage_y + 22.5}" fill="{stage['color']}" font-size="8.5" font-weight="700" text-anchor="middle" font-family="ui-monospace, monospace">
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
          <text x="{stage_x + 16}" y="{stage_y + 62}" fill="{stage['color']}" font-size="9.5" font-weight="600" font-family="ui-monospace, monospace" opacity="0.85">
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
    :root {{
      --md-primary: #0b57d0;
      --md-primary-container: #d3e3fd;
      --md-on-primary-container: #041e49;
      --md-surface: #ffffff;
      --md-surface-dim: #f8f9fa;
      --md-surface-container: #f1f3f4;
      --md-on-surface: #1f1f1f;
      --md-on-surface-variant: #444746;
      --md-outline: #747775;
      --md-border: #dadce0;

      --bg: #f8f9fa;
      --surface: #ffffff;
      --card: #ffffff;
      --card-hover: #fdfdfd;
      --border: #dadce0;
      --border-accent: #1a73e8;
      --text: #202124;
      --text-sub: #3c4043;
      --muted: #5f6368;
      --accent: #0b57d0;
      --accent-subtle: #e8f0fe;
      --success: #137333;
      --success-bg: #e6f4ea;
      --warning: #b06000;
      --warning-bg: #fef7e0;
      --error: #c5221f;
      --error-bg: #fce8e6;
    }}
    [data-theme="dark"] {{
      --md-primary: #8ab4f8;
      --md-surface: #1e1e1e;
      --md-surface-dim: #121212;
      --md-surface-container: #282828;
      --md-on-surface: #e8eaed;
      --md-on-surface-variant: #bdc1c6;
      --md-border: #333333;

      --bg: #121212;
      --surface: #1e1e1e;
      --card: #1e1e1e;
      --card-hover: #252525;
      --border: #333333;
      --border-accent: #8ab4f8;
      --text: #e8eaed;
      --text-sub: #bdc1c6;
      --muted: #9aa0a6;
      --accent: #8ab4f8;
      --accent-subtle: #1f2b3e;
      --success: #81c995;
      --success-bg: #133820;
      --warning: #fdd663;
      --warning-bg: #382d00;
      --error: #f28b82;
      --error-bg: #3e1614;
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) {{
        --md-surface: #1e1e1e;
        --md-surface-dim: #121212;
        --md-surface-container: #282828;
        --md-on-surface: #e8eaed;
        --md-on-surface-variant: #bdc1c6;
        --md-border: #333333;

        --bg: #121212;
        --surface: #1e1e1e;
        --card: #1e1e1e;
        --card-hover: #252525;
        --border: #333333;
        --border-accent: #8ab4f8;
        --text: #e8eaed;
        --text-sub: #bdc1c6;
        --muted: #9aa0a6;
        --accent: #8ab4f8;
        --accent-subtle: #1f2b3e;
        --success: #81c995;
        --success-bg: #133820;
        --warning: #fdd663;
        --warning-bg: #382d00;
        --error: #f28b82;
        --error-bg: #3e1614;
      }}
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "Google Sans", Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.6;
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
    }}
    a {{ color: var(--md-primary); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    /* Google Material Top App Bar */
    .app-header {{
      position: sticky;
      top: 0;
      z-index: 50;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      box-shadow: 0 1px 2px 0 rgba(60,64,67,0.1), 0 2px 6px 2px rgba(60,64,67,0.05);
      padding: 10px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    .gnav-bar {{
      width: 100%;
      max-width: 1360px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-shrink: 0;
    }}
    .brand-badge {{
      background: #0b57d0;
      color: #ffffff;
      font-size: 11px;
      font-weight: 600;
      padding: 4px 8px;
      border-radius: 4px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      font-family: "Roboto Mono", monospace;
    }}
    .brand-title {{
      font-size: 1.2rem;
      font-weight: 500;
      color: var(--text);
      letter-spacing: -0.01em;
    }}
    .search-bar {{
      flex: 1;
      max-width: 480px;
      position: relative;
    }}
    .search-input {{
      width: 100%;
      background: #f1f3f4;
      border: 1px solid transparent;
      border-radius: 24px;
      padding: 9px 16px 9px 40px;
      color: var(--text);
      font-size: 13.5px;
      outline: none;
      transition: all 0.2s;
    }}
    .search-input:focus {{
      background: #ffffff;
      border-color: #dadce0;
      box-shadow: 0 1px 3px rgba(60,64,67,0.25);
    }}
    .search-icon {{
      position: absolute;
      left: 14px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--muted);
      font-size: 13px;
      pointer-events: none;
    }}
    .view-tabs {{
      display: flex;
      gap: 6px;
      flex-shrink: 0;
    }}
    .view-tab-btn {{
      background: transparent;
      border: 1px solid transparent;
      color: var(--md-on-surface-variant);
      padding: 7px 16px;
      font-size: 13.5px;
      font-weight: 500;
      border-radius: 8px;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .view-tab-btn:hover {{
      background: #f1f3f4;
      color: var(--text);
    }}
    .view-tab-btn.active {{
      background: var(--md-primary-container);
      color: var(--md-on-primary-container);
      font-weight: 600;
    }}

    .container {{
      max-width: 1360px;
      margin: 0 auto;
      padding: 28px 24px 60px;
    }}

    /* Material Hero Statement & Stat Strip */
    .hero-studio {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px 28px;
      margin-bottom: 24px;
      box-shadow: 0 1px 2px rgba(60,64,67,0.06);
    }}
    .hero-tag {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 10px;
      border-radius: 4px;
      background: var(--accent-subtle);
      color: var(--accent);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      font-family: "Roboto Mono", monospace;
      margin-bottom: 12px;
    }}
    .pulse-dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent);
      display: inline-block;
    }}
    .hero-title {{
      font-size: 1.75rem;
      font-weight: 500;
      color: var(--text);
      letter-spacing: -0.01em;
      margin-bottom: 8px;
    }}
    .hero-lead {{
      font-size: 14.5px;
      color: var(--text-sub);
      max-width: 880px;
      line-height: 1.6;
      margin-bottom: 20px;
    }}
    .stat-strip {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 16px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
    }}
    @media (max-width: 860px) {{
      .stat-strip {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    .stat-cell {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 18px;
    }}
    .stat-val {{
      font-size: 26px;
      font-weight: 600;
      color: var(--md-primary);
      line-height: 1.1;
      font-family: "Google Sans", Roboto, sans-serif;
    }}
    .stat-lbl {{
      font-size: 11.5px;
      font-weight: 500;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-top: 4px;
    }}

    /* Stage Pill Pipeline Ribbon (Material Filter Chips) */
    .pipeline-ribbon {{
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding-bottom: 10px;
      margin-bottom: 24px;
      scrollbar-width: thin;
      scrollbar-color: var(--border) transparent;
    }}
    .stage-pill {{
      flex: 1 1 0px;
      min-width: 145px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 4px;
      transition: all 0.15s;
      box-shadow: 0 1px 2px rgba(60,64,67,0.04);
    }}
    .stage-pill:hover {{
      background: var(--card-hover);
      border-color: #c4c7c5;
    }}
    .stage-pill.active {{
      background: var(--md-primary-container);
      border-color: var(--md-primary);
      color: var(--md-on-primary-container);
      box-shadow: 0 1px 3px rgba(60,64,67,0.12);
    }}
    .stage-pill-head {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .stage-pill-title {{
      font-size: 13px;
      font-weight: 500;
      color: var(--text);
    }}
    .stage-pill.active .stage-pill-title {{
      color: var(--md-on-primary-container);
      font-weight: 600;
    }}
    .stage-pill-badge {{
      font-size: 11px;
      font-weight: 500;
      color: var(--accent);
      background: var(--accent-subtle);
      padding: 2px 6px;
      border-radius: 4px;
      font-family: "Roboto Mono", monospace;
      flex-shrink: 0;
    }}

    /* Main View Panes */
    .view-pane {{ display: none; }}
    .view-pane.active {{ display: block; }}

    /* Stage Deep-Dive Section (Material Container) */
    .stage-section {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 24px;
      box-shadow: 0 1px 2px rgba(60,64,67,0.06);
    }}
    .stage-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      border-bottom: 1px solid var(--border);
      padding-bottom: 16px;
      margin-bottom: 20px;
      gap: 16px;
    }}
    .stage-title {{
      font-size: 1.35rem;
      font-weight: 500;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .stage-desc {{
      color: var(--muted);
      font-size: 14px;
      margin-top: 4px;
      max-width: 800px;
    }}

    /* Step Sequence Cards (Material Outlined Cards) */
    .stage-section, .step-card, h1, h2, h3, [id] {{
      scroll-margin-top: 70px;
    }}
    .steps-timeline {{
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .step-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px 20px;
      transition: all 0.15s;
      box-shadow: 0 1px 2px rgba(60,64,67,0.04);
    }}
    .step-card:hover {{
      border-color: #b0b4b8;
      box-shadow: 0 2px 6px rgba(60,64,67,0.12);
    }}
    .step-card-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 8px;
      gap: 12px;
    }}
    .step-meta {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .step-num-badge {{
      background: #e8f0fe;
      color: #1967d2;
      font-size: 11px;
      font-weight: 600;
      font-family: "Roboto Mono", monospace;
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .step-role-badge {{
      background: #f1f3f4;
      color: var(--md-on-surface-variant);
      font-size: 11.5px;
      font-weight: 500;
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .step-title {{
      font-size: 1.05rem;
      font-weight: 500;
      color: var(--text);
    }}
    .step-desc {{
      font-size: 13.5px;
      color: var(--muted);
      margin-bottom: 12px;
      line-height: 1.5;
    }}
    .step-contracts {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 14px;
      font-size: 12px;
    }}
    .contract-pill {{
      background: #f8f9fa;
      border: 1px solid var(--border);
      padding: 3px 10px;
      border-radius: 4px;
      color: var(--muted);
      font-size: 12px;
    }}
    .contract-pill strong {{ color: var(--text); }}

    /* Symbol Chips */
    .symbol-chips {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .symbol-chip {{
      background: #f8f9fa;
      border: 1px solid var(--border);
      color: var(--md-primary);
      padding: 4px 12px;
      border-radius: 16px;
      font-family: "Roboto Mono", monospace;
      font-size: 12px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s;
    }}
    .symbol-chip:hover {{
      background: #e8f0fe;
      border-color: var(--md-primary);
    }}

    /* Material Action Buttons */
    .pill-btn {{
      background: var(--card);
      border: 1px solid var(--md-outline);
      color: var(--md-primary);
      padding: 7px 18px;
      border-radius: 20px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 500;
      transition: all 0.15s;
    }}
    .pill-btn:hover {{
      background: var(--card-hover);
    }}
    .pill-btn-primary {{
      background: var(--md-primary);
      border: 1px solid var(--md-primary);
      color: #ffffff;
      padding: 7px 20px;
      border-radius: 20px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 500;
      transition: all 0.15s;
      box-shadow: 0 1px 2px rgba(60,64,67,0.2);
    }}
    .pill-btn-primary:hover {{
      background: #0842a0;
      box-shadow: 0 1px 3px rgba(60,64,67,0.3);
    }}

    /* SVG Canvas Container */
    .svg-viewport-wrap {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      overflow: auto;
      position: relative;
      min-height: 640px;
      box-shadow: 0 1px 2px rgba(60,64,67,0.06);
    }}
    /* Mermaid Diagram Viewport */
    .mermaid-viewport {{
      overflow: auto;
      background: #08080d;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 28px;
      min-height: 480px;
      max-height: 800px;
      position: relative;
      cursor: grab;
      touch-action: none;
    }}
    .mermaid-viewport.is-dragging {{
      cursor: grabbing;
    }}
    .mermaid-viewport svg {{
      max-width: none !important;
      height: auto !important;
      transform-origin: 50% 0;
      transition: transform 120ms ease;
    }}
    .pill-btn-toggle.active {{
      background: var(--md-primary) !important;
      color: #ffffff !important;
      border-color: var(--md-primary) !important;
    }}
    .svg-controls {{
      position: absolute;
      top: 16px;
      right: 16px;
      z-index: 20;
      display: flex;
      align-items: center;
      gap: 4px;
      padding: 4px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 1px 3px rgba(60,64,67,0.15);
    }}
    .svg-btn {{
      background: transparent;
      border: none;
      color: var(--md-on-surface-variant);
      height: 28px;
      padding: 0 10px;
      border-radius: 4px;
      cursor: pointer;
      font-weight: 500;
      font-size: 12px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      transition: all 0.15s;
    }}
    .svg-btn:hover {{
      background: #f1f3f4;
      color: var(--md-primary);
    }}

    /* Flow Animations */
    @keyframes flowDash {{
      to {{
        stroke-dashoffset: -20;
      }}
    }}
    .flow-wire {{
      animation: flowDash 1.4s linear infinite;
    }}
    .flow-wire.intra-wire {{
      animation: flowDash 1.8s linear infinite;
    }}
    .flow-paused .flow-wire {{
      animation-play-state: paused !important;
    }}
    .svg-step-card {{
      transition: transform 0.18s ease;
    }}
    .svg-step-card:hover .step-card-bg {{
      stroke: var(--md-primary) !important;
      fill: #f1f5f9 !important;
    }}

    /* Execution Matrix Table (Material Data Table) */
    .matrix-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13.5px;
    }}
    .matrix-table th {{
      background: #f8f9fa;
      color: var(--md-on-surface-variant);
      text-align: left;
      padding: 12px 16px;
      border-bottom: 2px solid var(--border);
      font-family: "Google Sans", Roboto, sans-serif;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .matrix-table td {{
      padding: 12px 16px;
      border-bottom: 1px solid #e0e0e0;
      vertical-align: top;
      color: var(--text);
    }}
    .matrix-table tr:hover td {{
      background: #f8fafd;
    }}

    /* Slide-over AST Inspector Drawer (Material Side Sheet) */
    .drawer-overlay {{
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.32);
      z-index: 99;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
    }}
    .drawer-overlay.open {{
      opacity: 1;
      pointer-events: auto;
    }}
    .drawer {{
      position: fixed;
      top: 0;
      right: -660px;
      width: 100%;
      max-width: 620px;
      height: 100vh;
      background: var(--surface);
      border-left: 1px solid var(--border);
      box-shadow: -2px 0 16px rgba(60,64,67,0.15);
      z-index: 100;
      display: flex;
      flex-direction: column;
      transition: right 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    }}
    .drawer.open {{
      right: 0;
    }}
    .drawer-header {{
      padding: 18px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: var(--surface);
    }}
    .drawer-body {{
      flex: 1;
      overflow-y: auto;
      padding: 24px;
    }}
    .drawer-close {{
      background: transparent;
      border: none;
      color: var(--muted);
      width: 36px;
      height: 36px;
      border-radius: 50%;
      cursor: pointer;
      font-size: 20px;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.15s;
    }}
    .drawer-close:hover {{
      background: #f1f3f4;
      color: var(--text);
    }}
    .code-block {{
      background: #f8f9fa;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      font-family: "Roboto Mono", monospace;
      font-size: 12.5px;
      color: #202124;
      overflow-x: auto;
      line-height: 1.5;
      margin: 10px 0 18px;
      white-space: pre-wrap;
    }}
    .drawer-sec-title {{
      font-size: 12px;
      font-weight: 600;
      color: var(--md-on-surface-variant);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin: 18px 0 6px;
    }}
    .drawer-call-list {{
      list-style: none;
      font-size: 13px;
    }}
    .drawer-call-list li {{
      padding: 8px 12px;
      margin-bottom: 6px;
      background: #f8f9fa;
      border: 1px solid #e0e0e0;
      border-radius: 6px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
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
  (function() {{
    const ARCH_DATA = {data_json};

    // Flatten symbols index
    const symbolMap = new Map();
    ARCH_DATA.stages.forEach(st => {{
      st.steps.forEach(step => {{
        step.symbols.forEach(sym => {{
          symbolMap.set(sym.id, sym);
        }});
      }});
    }});

    // Tab Switching
    const tabBtns = document.querySelectorAll('.view-tab-btn');
    const viewPanes = document.querySelectorAll('.view-pane');
    let mermaidRendered = false;

    function renderMermaidIfNeeded() {{
      if (mermaidRendered) return;
      const target = document.getElementById('mermaid-target');
      if (!target) return;
      if (typeof mermaid === 'undefined') {{
        setTimeout(renderMermaidIfNeeded, 100);
        return;
      }}
      try {{
        mermaid.run({{ nodes: [target] }});
        mermaidRendered = true;
      }} catch (e) {{
        console.warn('Mermaid render warning:', e);
      }}
    }}

    tabBtns.forEach(btn => {{
      btn.addEventListener('click', () => {{
        if (!btn.dataset.view) return;
        tabBtns.forEach(b => b.classList.remove('active'));
        viewPanes.forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const viewId = 'view-' + btn.dataset.view;
        const target = document.getElementById(viewId);
        if (target) target.classList.add('active');
        if (btn.dataset.view === 'mermaid') {{
          renderMermaidIfNeeded();
        }}
        try {{
          if (history.replaceState) {{
            const url = new URL(window.location);
            url.searchParams.set('view', btn.dataset.view);
            history.replaceState(null, '', url);
          }}
        }} catch(e) {{}}
      }});
    }});

    // Stage Pill Filtering
    const stagePills = document.querySelectorAll('.stage-pill');
    stagePills.forEach(pill => {{
      pill.addEventListener('click', () => {{
        stagePills.forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        const filter = pill.dataset.stageFilter;

        const stageSections = document.querySelectorAll('.stage-section');
        stageSections.forEach(sec => {{
          if (filter === 'all' || sec.dataset.stage === filter) {{
            sec.style.display = 'block';
          }} else {{
            sec.style.display = 'none';
          }}
        }});
      }});
    }});

    // Search Filtering & URL Sync
    const searchInput = document.getElementById('arch-search');
    searchInput.addEventListener('input', (e) => {{
      const query = e.target.value.toLowerCase().trim();
      const stepCards = document.querySelectorAll('.step-card');
      const stageSections = document.querySelectorAll('.stage-section');

      try {{
        if (history.replaceState) {{
          const url = new URL(window.location);
          if (query) url.searchParams.set('search', query);
          else url.searchParams.delete('search');
          history.replaceState(null, '', url);
        }}
      }} catch(e) {{}}

      if (!query) {{
        stepCards.forEach(c => c.style.display = 'block');
        stageSections.forEach(s => s.style.display = 'block');
        return;
      }}

      stageSections.forEach(sec => {{
        let hasMatch = false;
        const cards = sec.querySelectorAll('.step-card');
        cards.forEach(card => {{
          const text = card.textContent.toLowerCase();
          if (text.includes(query)) {{
            card.style.display = 'block';
            hasMatch = true;
          }} else {{
            card.style.display = 'none';
          }}
        }});
        sec.style.display = hasMatch ? 'block' : 'none';
      }});
    }});

    // Initialize from URL deep link
    (function() {{
      try {{
        const params = new URLSearchParams(window.location.search);
        const q = params.get('search') || params.get('q') || (window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '');
        const view = params.get('view');
        if (view) {{
          const btn = document.querySelector(`.view-tab-btn[data-view="${{view}}"]`);
          if (btn) btn.click();
        }}
        if (q && searchInput) {{
          searchInput.value = q;
          searchInput.dispatchEvent(new Event('input'));
        }}
      }} catch(e) {{}}
    }})();

    // Keyboard shortcut to search
    window.addEventListener('keydown', (e) => {{
      if (e.key === '/' && document.activeElement !== searchInput) {{
        e.preventDefault();
        searchInput.focus();
      }}
      if (e.key === 'Escape') {{
        closeDrawer();
      }}
    }});

    // AST Inspector Drawer
    const drawer = document.getElementById('ast-drawer');
    const overlay = document.getElementById('drawer-overlay');
    const closeBtn = document.getElementById('drawer-close-btn');

    function openDrawer(symId) {{
      const sym = symbolMap.get(symId);
      if (!sym) return;

      document.getElementById('drawer-title').textContent = sym.label;
      document.getElementById('drawer-kind').textContent = sym.kind;
      document.getElementById('drawer-file').textContent = sym.source_file + (sym.line_number ? ':' + sym.line_number : '');
      document.getElementById('drawer-skeleton').textContent = sym.skeleton || sym.signature || sym.label;

      // Render Callers
      const callersList = document.getElementById('drawer-callers');
      callersList.innerHTML = '';
      if (sym.callers && sym.callers.length > 0) {{
        sym.callers.forEach(c => {{
          const li = document.createElement('li');
          li.innerHTML = '<span>λ ' + escapeHtml(c.label) + '</span><span style="font-size:11px;color:var(--muted);">' + escapeHtml(c.file) + '</span>';
          callersList.appendChild(li);
        }});
      }} else {{
        callersList.innerHTML = '<li style="color:var(--muted);">No inbound callers recorded</li>';
      }}

      // Render Callees
      const calleesList = document.getElementById('drawer-callees');
      calleesList.innerHTML = '';
      if (sym.callees && sym.callees.length > 0) {{
        sym.callees.forEach(c => {{
          const li = document.createElement('li');
          li.innerHTML = '<span>λ ' + escapeHtml(c.label) + '</span><span style="font-size:11px;color:var(--muted);">' + escapeHtml(c.file) + '</span>';
          calleesList.appendChild(li);
        }});
      }} else {{
        calleesList.innerHTML = '<li style="color:var(--muted);">No outbound calls recorded</li>';
      }}

      // Render Sinks
      const sinksDiv = document.getElementById('drawer-sinks');
      if (sym.sinks && sym.sinks.length > 0) {{
        sinksDiv.innerHTML = sym.sinks.map(s => '<span class="contract-pill" style="margin-right:6px;color:#f472b6;">' + escapeHtml(s) + '</span>').join('');
      }} else {{
        sinksDiv.textContent = 'None';
      }}

      drawer.classList.add('open');
      overlay.classList.add('open');
    }}

    function closeDrawer() {{
      drawer.classList.remove('open');
      overlay.classList.remove('open');
    }}

    closeBtn.addEventListener('click', closeDrawer);
    overlay.addEventListener('click', closeDrawer);

    // Click on symbol chips
    document.querySelectorAll('.symbol-chip, .matrix-sym-link').forEach(el => {{
      el.addEventListener('click', (e) => {{
        e.preventDefault();
        const symId = el.dataset.symId;
        if (symId) openDrawer(symId);
      }});
    }});

    // SVG Node Click Handlers
    document.querySelectorAll('.svg-step-card').forEach(card => {{
      card.addEventListener('click', () => {{
        const symId = card.dataset.symId;
        if (symId) openDrawer(symId);
      }});
    }});

    // SVG Pan & Zoom
    const svgCanvas = document.getElementById('arch-svg-canvas');
    let scale = 1.0;
    const zoomInBtn = document.getElementById('zoom-in');
    const zoomOutBtn = document.getElementById('zoom-out');
    const zoomResetBtn = document.getElementById('zoom-reset');

    if (svgCanvas && zoomInBtn) {{
      zoomInBtn.addEventListener('click', () => {{
        scale = Math.min(2.0, scale + 0.15);
        applySvgScale();
      }});
      zoomOutBtn.addEventListener('click', () => {{
        scale = Math.max(0.4, scale - 0.15);
        applySvgScale();
      }});
      zoomResetBtn.addEventListener('click', () => {{
        scale = 1.0;
        applySvgScale();
      }});
      function applySvgScale() {{
        svgCanvas.style.transform = 'scale(' + scale + ')';
        svgCanvas.style.transformOrigin = '0 0';
      }}
    }}

    // Flow Animation Controls (Pause / Resume & 1x / 2x Speed)
    const flowToggleBtn = document.getElementById('flow-toggle-btn');
    const flowSpeedBtn = document.getElementById('flow-speed-btn');
    let isFlowPaused = false;
    let isDoubleSpeed = false;

    if (svgCanvas && flowToggleBtn) {{
      flowToggleBtn.addEventListener('click', () => {{
        isFlowPaused = !isFlowPaused;
        if (isFlowPaused) {{
          try {{ svgCanvas.pauseAnimations(); }} catch (e) {{}}
          svgCanvas.classList.add('flow-paused');
          flowToggleBtn.textContent = '▶ Flow';
          flowToggleBtn.style.color = 'var(--accent)';
        }} else {{
          try {{ svgCanvas.unpauseAnimations(); }} catch (e) {{}}
          svgCanvas.classList.remove('flow-paused');
          flowToggleBtn.textContent = '⏸ Flow';
          flowToggleBtn.style.color = '';
        }}
      }});
    }}

    if (svgCanvas && flowSpeedBtn) {{
      flowSpeedBtn.addEventListener('click', () => {{
        isDoubleSpeed = !isDoubleSpeed;
        const wires = svgCanvas.querySelectorAll('.flow-wire');
        const anims = svgCanvas.querySelectorAll('animateMotion');
        if (isDoubleSpeed) {{
          wires.forEach(w => {{ w.style.animationDuration = '0.7s'; }});
          anims.forEach(a => {{
            const orig = parseFloat(a.dataset.origDur || a.getAttribute('dur') || '2.4');
            a.dataset.origDur = orig;
            a.setAttribute('dur', (orig / 2) + 's');
          }});
          flowSpeedBtn.textContent = '⚡ 2x';
          flowSpeedBtn.style.color = 'var(--accent)';
        }} else {{
          wires.forEach(w => {{ w.style.animationDuration = ''; }});
          anims.forEach(a => {{
            const orig = a.dataset.origDur || '2.4';
            a.setAttribute('dur', orig + 's');
          }});
          flowSpeedBtn.textContent = '⚡ 1x';
          flowSpeedBtn.style.color = '';
        }}
      }});
    }}

    function escapeHtml(str) {{
      return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }}

    // Mermaid Controls: Orientation Toggle, Zoom, Pan & Copy
    const copyMermaidBtn = document.getElementById('copy-mermaid-btn');
    const toggleMermaidRaw = document.getElementById('toggle-mermaid-raw');
    const mermaidRawWrap = document.getElementById('mermaid-raw-wrap');
    const rawMermaidPre = document.getElementById('raw-mermaid-pre');
    const mermaidDirTdBtn = document.getElementById('mermaid-dir-td');
    const mermaidDirLrBtn = document.getElementById('mermaid-dir-lr');
    const mermaidTarget = document.getElementById('mermaid-target');
    const mermaidPzContainer = document.getElementById('mermaid-pan-zoom-container');
    const mermaidRenderedWrap = document.getElementById('mermaid-rendered-wrap');

    const mermaidCodeTD = {json.dumps(mermaid_code_td)};
    const mermaidCodeLR = {json.dumps(mermaid_code_lr)};
    let currentMermaidDir = 'TD';
    let mermaidScale = 1.0;

    function applyMermaidScale() {{
      if (mermaidPzContainer) {{
        mermaidPzContainer.style.transform = 'scale(' + mermaidScale + ')';
      }}
    }}

    const mZoomIn = document.getElementById('mermaid-zoom-in');
    const mZoomOut = document.getElementById('mermaid-zoom-out');
    const mZoomReset = document.getElementById('mermaid-zoom-reset');

    if (mZoomIn) {{
      mZoomIn.addEventListener('click', () => {{
        mermaidScale = Math.min(2.5, mermaidScale + 0.15);
        applyMermaidScale();
      }});
    }}
    if (mZoomOut) {{
      mZoomOut.addEventListener('click', () => {{
        mermaidScale = Math.max(0.3, mermaidScale - 0.15);
        applyMermaidScale();
      }});
    }}
    if (mZoomReset) {{
      mZoomReset.addEventListener('click', () => {{
        mermaidScale = 1.0;
        applyMermaidScale();
      }});
    }}

    // Direction Toggle
    function setMermaidDirection(dir) {{
      if (currentMermaidDir === dir) return;
      currentMermaidDir = dir;
      if (dir === 'TD') {{
        if (mermaidDirTdBtn) mermaidDirTdBtn.classList.add('active');
        if (mermaidDirLrBtn) mermaidDirLrBtn.classList.remove('active');
        if (mermaidTarget) {{
          mermaidTarget.removeAttribute('data-processed');
          mermaidTarget.innerHTML = escapeHtml(mermaidCodeTD);
        }}
        if (rawMermaidPre) rawMermaidPre.textContent = mermaidCodeTD;
      }} else {{
        if (mermaidDirLrBtn) mermaidDirLrBtn.classList.add('active');
        if (mermaidDirTdBtn) mermaidDirTdBtn.classList.remove('active');
        if (mermaidTarget) {{
          mermaidTarget.removeAttribute('data-processed');
          mermaidTarget.innerHTML = escapeHtml(mermaidCodeLR);
        }}
        if (rawMermaidPre) rawMermaidPre.textContent = mermaidCodeLR;
      }}
      mermaidScale = 1.0;
      applyMermaidScale();
      if (typeof mermaid !== 'undefined' && mermaidTarget) {{
        try {{
          mermaid.run({{ nodes: [mermaidTarget] }});
        }} catch(e) {{
          console.warn('Mermaid re-render error:', e);
        }}
      }}
    }}

    if (mermaidDirTdBtn) {{
      mermaidDirTdBtn.addEventListener('click', () => setMermaidDirection('TD'));
    }}
    if (mermaidDirLrBtn) {{
      mermaidDirLrBtn.addEventListener('click', () => setMermaidDirection('LR'));
    }}

    if (copyMermaidBtn) {{
      copyMermaidBtn.addEventListener('click', () => {{
        const textToCopy = currentMermaidDir === 'TD' ? mermaidCodeTD : mermaidCodeLR;
        navigator.clipboard.writeText(textToCopy).then(() => {{
          copyMermaidBtn.textContent = 'Copied! ✓';
          setTimeout(() => {{ copyMermaidBtn.textContent = 'Copy Mermaid 📋'; }}, 2000);
        }});
      }});
    }}

    if (toggleMermaidRaw && mermaidRawWrap) {{
      toggleMermaidRaw.addEventListener('click', () => {{
        const isHidden = mermaidRawWrap.style.display === 'none';
        mermaidRawWrap.style.display = isHidden ? 'block' : 'none';
        toggleMermaidRaw.textContent = isHidden ? 'Hide Raw Syntax' : 'Toggle Raw Syntax';
      }});
    }}
  }})();
  </script>
  <script>
    if (typeof mermaid !== 'undefined') {{
      try {{
        mermaid.initialize({{
          startOnLoad: false,
          theme: 'dark',
          securityLevel: 'loose',
          flowchart: {{ htmlLabels: true, useMaxWidth: false }}
        }});
      }} catch (e) {{
        console.warn('Mermaid initialization warning:', e);
      }}
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
</html>"""
    return html


