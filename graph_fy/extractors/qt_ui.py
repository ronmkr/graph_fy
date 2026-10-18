"""Qt Designer UI (.ui) XML extractor.

Extracts:
- UI Class definitions (<class>MainWindow</class>)
- UI Widget hierarchies (<widget class="QPushButton" name="myButton">)
- Signal-to-slot event bindings (<connections><connection><sender>...</sender><signal>...</signal><receiver>...</receiver><slot>...</slot></connection></connections>)
  as connects_signal_slot edges.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _file_stem, _make_id
from graph_fy.extractors.xml_util import _PROJECT_XML_MAX_BYTES, _project_xml_is_safe


def is_qt_ui_file(path: Path) -> bool:
    """Return True if path is a Qt Designer .ui XML file."""
    if path.suffix.lower() != ".ui":
        return False
    try:
        head = path.read_bytes()[:1024].lower()
        return b"<ui" in head or (b"<?xml" in head and b"ui" in head)
    except OSError:
        return False


def _clean_member_signature(sig: str) -> str:
    """Normalize C++/Qt signal or slot signature e.g. clicked() -> clicked()."""
    return sig.strip()


def extract_qt_ui(path: Path) -> dict[str, Any]:
    """Extract Qt Designer .ui XML file into widget nodes and signal-slot connection edges."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)
    stem = _file_stem(path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": f"QtUI:{path.name}",
        "type": "qt_ui_file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Qt Designer UI file {path.name}",
    }]
    edges: list[dict[str, Any]] = []

    try:
        data = path.read_bytes()
    except OSError as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    if len(data) > _PROJECT_XML_MAX_BYTES:
        return {"nodes": nodes, "edges": edges, "error": "file size exceeds cap"}

    if not _project_xml_is_safe(data):
        return {"nodes": nodes, "edges": edges, "error": "unsafe XML (DOCTYPE/ENTITY declared)"}

    try:
        root = ET.fromstring(data)
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": f"malformed XML: {exc}"}

    # Verify root is <ui>
    root_tag = root.tag.rsplit("}", 1)[-1] if root.tag.startswith("{") else root.tag
    if root_tag.lower() != "ui":
        return {"nodes": nodes, "edges": edges}

    # 1. UI Class
    class_elem = root.find("class")
    class_name = class_elem.text.strip() if class_elem is not None and class_elem.text else ""
    class_nid = _make_id(stem, "class", class_name) if class_name else file_nid

    if class_name:
        nodes.append({
            "id": class_nid,
            "label": f"QtUI:Class:{class_name}",
            "type": "ui_class",
            "file_type": "code",
            "class_name": class_name,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Qt UI Class {class_name}",
        })
        edges.append({
            "source": file_nid,
            "target": class_nid,
            "relation": "declares",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    # 2. Widgets
    widgets_by_name: dict[str, str] = {}  # widget_name -> widget_nid

    def _walk_widgets(elem: ET.Element, parent_nid: str) -> None:
        tag = elem.tag.rsplit("}", 1)[-1] if elem.tag.startswith("{") else elem.tag
        curr_parent = parent_nid
        if tag.lower() == "widget":
            w_class = elem.attrib.get("class", "QWidget")
            w_name = elem.attrib.get("name", "")
            if w_name:
                w_nid = _make_id(stem, "widget", w_name)
                widgets_by_name[w_name] = w_nid
                nodes.append({
                    "id": w_nid,
                    "label": f"Widget:{w_name} ({w_class})",
                    "type": "ui_widget",
                    "file_type": "code",
                    "widget_name": w_name,
                    "widget_class": w_class,
                    "source_file": str_path,
                    "source_location": "L1",
                    "text": f"Qt UI Widget {w_name} of type {w_class}",
                })
                edges.append({
                    "source": curr_parent,
                    "target": w_nid,
                    "relation": "contains",
                    "confidence": "EXTRACTED",
                    "weight": 1.0,
                    "source_file": str_path,
                    "source_location": "L1",
                })
                curr_parent = w_nid

        for child in elem:
            # Don't recurse into <connections> block as widgets
            child_tag = child.tag.rsplit("}", 1)[-1] if child.tag.startswith("{") else child.tag
            if child_tag.lower() != "connections":
                _walk_widgets(child, curr_parent)

    _walk_widgets(root, class_nid)

    # 3. Connections (<connections><connection>...)
    for conn in root.findall(".//connection"):
        sender_el = conn.find("sender")
        signal_el = conn.find("signal")
        receiver_el = conn.find("receiver")
        slot_el = conn.find("slot")

        sender = sender_el.text.strip() if sender_el is not None and sender_el.text else ""
        signal = signal_el.text.strip() if signal_el is not None and signal_el.text else ""
        receiver = receiver_el.text.strip() if receiver_el is not None and receiver_el.text else ""
        slot = slot_el.text.strip() if slot_el is not None and slot_el.text else ""

        if not sender or not signal or not slot:
            continue

        # Resolve sender node
        if sender in widgets_by_name:
            source_nid = widgets_by_name[sender]
        elif class_name and sender == class_name:
            source_nid = class_nid
        else:
            source_nid = _make_id(stem, "widget", sender)

        # Resolve receiver node
        if class_name and receiver == class_name:
            target_nid = class_nid
        elif receiver in widgets_by_name:
            target_nid = widgets_by_name[receiver]
        else:
            target_nid = _make_id(stem, "widget", receiver) if receiver else class_nid

        edges.append({
            "source": source_nid,
            "target": target_nid,
            "relation": "connects_signal_slot",
            "signal": _clean_member_signature(signal),
            "slot": _clean_member_signature(slot),
            "sender": sender,
            "receiver": receiver,
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    return {"nodes": nodes, "edges": edges}
