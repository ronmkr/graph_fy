"""Unit tests for local PDF ingestion, markdown conversion, and AST extraction."""
from __future__ import annotations

import io
import os
from pathlib import Path
import pytest

pypdf = pytest.importorskip("pypdf")
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from graph_fy.detect import pdf_to_markdown, extract_pdf_text
from graph_fy.extractors.markdown import extract_pdf
from graph_fy.extract import extract


def _create_sample_pdf(file_path: Path, title: str = "Test Spec", author: str = "Architect") -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)

    # In PDF content streams, parentheses inside text strings are escaped with \( and \)
    # We embed a markdown heading and backtick code mentions: `Widget` and `helper()`
    stream_content = (
        b"BT\n"
        b"/F1 12 Tf\n"
        b"50 700 Td\n"
        b"(# Specification) Tj\n"
        b"ET\n"
        b"BT\n"
        b"/F1 12 Tf\n"
        b"50 670 Td\n"
        b"(This component instantiates `Widget`.) Tj\n"
        b"ET\n"
        b"BT\n"
        b"/F1 12 Tf\n"
        b"50 640 Td\n"
        b"(## Details) Tj\n"
        b"ET\n"
        b"BT\n"
        b"/F1 12 Tf\n"
        b"50 610 Td\n"
        b"(It also calls `helper\\(\\)` for setup.) Tj\n"
        b"ET\n"
    )
    stream = DecodedStreamObject()
    stream.set_data(stream_content)
    page[NameObject("/Contents")] = stream

    font_dict = DictionaryObject()
    font_dict[NameObject("/Type")] = NameObject("/Font")
    font_dict[NameObject("/Subtype")] = NameObject("/Type1")
    font_dict[NameObject("/BaseFont")] = NameObject("/Helvetica")

    fonts = DictionaryObject()
    fonts[NameObject("/F1")] = font_dict

    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources

    writer.add_metadata({
        "/Title": title,
        "/Author": author,
    })

    with open(file_path, "wb") as f:
        writer.write(f)


def test_pdf_to_markdown_conversion(tmp_path: Path):
    pdf_file = tmp_path / "manual.pdf"
    _create_sample_pdf(pdf_file, title="Core Architecture", author="Platform Team")

    md = pdf_to_markdown(pdf_file)
    assert "---" in md
    assert "title: Core Architecture" in md
    assert "author: Platform Team" in md
    assert "# Specification" in md
    assert "`Widget`" in md
    assert "## Details" in md
    assert "`helper()`" in md

    # Check extract_pdf_text matches
    assert extract_pdf_text(pdf_file) == md


def test_extract_pdf_ast_structure(tmp_path: Path):
    pdf_file = tmp_path / "manual.pdf"
    _create_sample_pdf(pdf_file, title="Core Architecture", author="Platform Team")

    res = extract_pdf(pdf_file)
    nodes = res["nodes"]
    edges = res["edges"]
    raw_calls = res.get("raw_calls", [])

    # Verify root document node
    page_node = next(n for n in nodes if n["node_kind"] == "page")
    assert page_node["label"] == "manual.pdf"
    assert page_node["frontmatter"] == {"title": "Core Architecture", "author": "Platform Team"}

    # Verify heading nodes
    headings = {n["label"]: n for n in nodes if n["node_kind"] == "heading"}
    assert "Specification" in headings
    assert "Details" in headings

    # Verify contains edges
    contains_edges = [(e["source"], e["target"]) for e in edges if e["relation"] == "contains"]
    assert (page_node["id"], headings["Specification"]["id"]) in contains_edges
    assert (headings["Specification"]["id"], headings["Details"]["id"]) in contains_edges

    # Verify raw code span mentions
    callees = {rc["callee"] for rc in raw_calls}
    assert "Widget" in callees
    assert "helper" in callees


def test_cross_file_pdf_to_code_resolution(tmp_path: Path):
    # PDF references code symbol Widget in service.py
    pdf_file = tmp_path / "docs" / "design.pdf"
    pdf_file.parent.mkdir(parents=True, exist_ok=True)
    _create_sample_pdf(pdf_file, title="Design Spec")

    service_py = tmp_path / "service.py"
    service_py.write_text("""\
class Widget:
    def execute(self):
        pass

def helper():
    return None
""")

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = extract([pdf_file, service_py], cache_root=tmp_path / ".cache", parallel=False)
    finally:
        os.chdir(old_cwd)

    edges = result["edges"]
    nodes = result["nodes"]

    widget_node = next(n for n in nodes if n["label"] == "Widget")
    spec_heading = next(n for n in nodes if n["label"] == "Specification")

    # Verify references edge from Specification heading in PDF to Widget class in service.py
    ref_edges = [
        e for e in edges
        if e["relation"] == "references" and e["source"] == spec_heading["id"] and e["target"] == widget_node["id"]
    ]
    assert len(ref_edges) == 1
    assert ref_edges[0]["confidence"] == "INFERRED"
    assert ref_edges[0]["confidence_score"] == 0.95

