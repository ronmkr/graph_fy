"""Angular component template (.component.html) extractor.

Extracts:
- Event bindings: (click)="onSelect(item)", on-click="save()"
- Property bindings: [disabled]="isPending", bind-prop="field"
- Two-way bindings: [(ngModel)]="username"
- Structural directives: *ngFor="let item of items", *ngIf="isLoaded"
- Angular signals and interpolations: {{ count() }}, {{ getFullName() }}

Connects template elements to component class methods and fields via template_binds_to edges.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _file_stem, _make_id


def is_angular_template(path: Path) -> bool:
    """Return True if path is an Angular component template (.component.html)."""
    return path.name.endswith(".component.html")


def extract_angular_template(path: Path) -> dict[str, Any]:
    """Extract Angular component template HTML and connect bindings to component class."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)
    stem = _file_stem(path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": path.name,
        "type": "angular_template",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
    }]
    edges: list[dict[str, Any]] = []

    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    # Companion TypeScript component file (.component.ts)
    ts_path = path.with_name(path.name[:-5] + ".ts")
    cls_nid: str | None = None
    ts_members: set[str] = set()

    if ts_path.exists():
        try:
            ts_text = ts_path.read_text(encoding="utf-8", errors="replace")
            ts_stem = _file_stem(ts_path)
            ts_file_nid = _make_id(str(ts_path.resolve()))

            # Find component class name
            m_cls = (
                re.search(r"@Component\s*\([^)]*\)\s*(?:export\s+)?class\s+([A-Za-z0-9_]+)", ts_text)
                or re.search(r"export\s+class\s+([A-Za-z0-9_]+Component)", ts_text)
                or re.search(r"export\s+class\s+([A-Za-z0-9_]+)", ts_text)
            )
            if m_cls:
                cls_name = m_cls.group(1)
                cls_nid = _make_id(ts_stem, cls_name)
                # Link template to component class
                edges.append({
                    "source": file_nid,
                    "target": cls_nid,
                    "relation": "template_for",
                    "context": "angular_component",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": "L1",
                    "weight": 1.0,
                })
            else:
                edges.append({
                    "source": file_nid,
                    "target": ts_file_nid,
                    "relation": "template_for",
                    "context": "angular_component",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": "L1",
                    "weight": 1.0,
                })

            # Identify known member methods and fields
            for m_fn in re.finditer(r"^\s*([a-zA-Z0-9_$]+)\s*\([^)]*\)\s*(?::\s*[^;{]+)?\s*\{", ts_text, re.MULTILINE):
                fn_name = m_fn.group(1)
                if fn_name not in ("constructor", "ngOnInit", "ngOnDestroy", "ngAfterViewInit"):
                    ts_members.add(fn_name)
            for m_fld in re.finditer(r"^\s*([a-zA-Z0-9_$]+)\s*(?::|=(?!=))", ts_text, re.MULTILINE):
                ts_members.add(m_fld.group(1))
        except OSError:
            pass

    # Collect local loop variables (*ngFor="let item of items")
    local_vars: set[str] = set()
    for m in re.finditer(r"\*ngFor\s*=\s*[\"']\s*let\s+([a-zA-Z0-9_$]+)", src):
        local_vars.add(m.group(1))

    # Helper to emit binding edge
    seen_bindings: set[tuple[str, int]] = set()

    def _add_binding(symbol: str, line_no: int, binding_type: str) -> None:
        if not symbol or symbol in local_vars:
            return
        if symbol in ("true", "false", "null", "undefined", "this", "$event"):
            return
        key = (symbol, line_no)
        if key in seen_bindings:
            return
        seen_bindings.add(key)

        target_nid = _make_id(cls_nid, symbol) if cls_nid else _make_id(symbol)
        if target_nid not in {n["id"] for n in nodes}:
            nodes.append({
                "id": target_nid,
                "label": symbol,
                "file_type": "code",
                "source_file": str(ts_path.resolve()) if ts_path.exists() else str_path,
                "source_location": f"L{line_no}",
            })

        edges.append({
            "source": file_nid,
            "target": target_nid,
            "relation": "template_binds_to",
            "context": f"angular_{binding_type}",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line_no}",
            "weight": 1.0,
        })

    lines = src.splitlines()
    for idx, line in enumerate(lines, 1):
        # 1. Structural directives (*ngFor, *ngIf)
        for m in re.finditer(r"\*ngFor\s*=\s*[\"']\s*let\s+\w+\s+of\s+([a-zA-Z0-9_$]+)", line):
            _add_binding(m.group(1), idx, "structural_ngFor")
        for m in re.finditer(r"\*ngIf\s*=\s*[\"']\s*([a-zA-Z0-9_$]+)", line):
            _add_binding(m.group(1), idx, "structural_ngIf")

        # 2. Two-way binding [(...)]
        for m in re.finditer(r"\[\(([a-zA-Z0-9_\-]+)\)\]\s*=\s*[\"']\s*([a-zA-Z0-9_$]+)", line):
            _add_binding(m.group(2), idx, "two_way")

        # 3. Event bindings (event)="..." or on-event="..."
        for m in re.finditer(r"(?:\(([a-zA-Z0-9_\-]+)\)|on-([a-zA-Z0-9_\-]+))\s*=\s*[\"']([^\"'\n]+)[\"']", line):
            expr = m.group(3).strip()
            call_m = re.match(r"^([a-zA-Z0-9_$]+)\s*(?:\(|$)", expr)
            if call_m:
                _add_binding(call_m.group(1), idx, "event")

        # 4. Property bindings [prop]="..." or bind-prop="..."
        for m in re.finditer(r"(?:\[([a-zA-Z0-9_\-]+)\]|bind-([a-zA-Z0-9_\-]+))\s*=\s*[\"']([^\"'\n]+)[\"']", line):
            if m.start() > 0 and line[m.start()-1] == "(":
                continue  # skip banana-in-a-box already handled
            expr = m.group(3).strip()
            prop_m = re.match(r"^([a-zA-Z0-9_$]+)(?:\(\))?", expr)
            if prop_m:
                _add_binding(prop_m.group(1), idx, "property")

        # 5. Interpolation {{ ... }}
        for m in re.finditer(r"\{\{\s*([^{}]+)\s*\}\}", line):
            expr = m.group(1).strip()
            # Extract identifiers or signals
            for ident_m in re.finditer(r"\b([a-zA-Z_$][a-zA-Z0-9_$]*)\b", expr):
                _add_binding(ident_m.group(1), idx, "interpolation")

    return {"nodes": nodes, "edges": edges}
