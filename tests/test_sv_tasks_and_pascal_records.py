"""Tests for SystemVerilog class task methods and Pascal record methods/operators (Issue #72)."""
from __future__ import annotations

from pathlib import Path
import pytest

from graph_fy.extractors.verilog import extract_verilog
from graph_fy.extractors.pascal import _extract_pascal_regex


def test_systemverilog_class_task_methods():
    from graph_fy.extractors.verilog import _augment_systemverilog_semantics
    sv_code = (
        "class PacketDriver;\n"
        "    rand Config cfg;\n"
        "    task reset(Config cfg);\n"
        "        LocalHelper helper;\n"
        "    endtask\n"
        "endclass\n"
    )
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    _augment_systemverilog_semantics(sv_code, "driver", "driver.sv", "driver_file", nodes, edges, seen)

    labels = {n["label"] for n in nodes}
    assert "reset" in labels
    assert "PacketDriver" in labels

    # Check method edge
    method_edges = [
        e for e in edges
        if e.get("relation") == "method" and "reset" in e["target"]
    ]
    assert len(method_edges) == 1

    # Check parameter type reference
    param_refs = [
        e for e in edges
        if e.get("relation") == "references" and "config" in e["target"]
    ]
    assert len(param_refs) >= 1

    # Check task local variable helper did NOT leak as class field
    class_fields = [
        e for e in edges
        if e.get("relation") == "references" and e.get("context") == "field" and "helper" in e["target"]
    ]
    assert len(class_fields) == 0


def test_pascal_record_methods_and_operators(tmp_path: Path):
    pas_code = (
        "unit RecordMethodsUnit;\n"
        "interface\n"
        "type\n"
        "  TVec = record\n"
        "    X, Y: Integer;\n"
        "    function Len: Integer;\n"
        "    class operator Add(a, b: TVec): TVec;\n"
        "  end;\n"
        "implementation\n"
        "function TVec.Len: Integer;\n"
        "begin\n"
        "  Result := 0;\n"
        "end;\n"
        "class operator TVec.Add(a, b: TVec): TVec;\n"
        "begin\n"
        "  Result.X := a.X + b.X;\n"
        "  Result.Y := a.Y + b.Y;\n"
        "end;\n"
        "end.\n"
    )
    f = tmp_path / "geometry.pas"
    f.write_text(pas_code, encoding="utf-8")
    res = _extract_pascal_regex(f)

    labels = {n["label"] for n in res["nodes"]}
    assert "TVec" in labels
    assert "Len()" in labels
    assert "Add()" in labels

    method_edges = [
        e for e in res["edges"]
        if e.get("relation") == "method"
    ]
    targets = {e["target"] for e in method_edges}
    assert any("len" in t for t in targets)
    assert any("add" in t for t in targets)
