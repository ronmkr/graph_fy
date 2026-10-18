"""Tests for upstream v8 parity bug fixes."""
import pytest
from pathlib import Path
from graph_fy.dedup import _collision_rank
from graph_fy.build import _fold_edge_aliases
from graph_fy.extractors.python import _PYTHON_ANNOTATION_NOISE, _PYTHON_DECORATOR_NOISE, _python_collect_type_refs
from graph_fy.validate import validate_extraction


def test_collision_rank_prefers_located_node_over_sourceless_stub():
    """Ensure a located node beats a sourceless stub during ID collision (#32, #4492)."""
    sourceless = {
        "id": "app_config",
        "label": "app_config",
        "file_type": "code",
        "source_file": "",
    }
    located = {
        "id": "app_config",
        "label": "app_config",
        "file_type": "code",
        "source_file": "src/config.py",
    }
    # Smaller rank wins in _collision_rank
    assert _collision_rank(located) < _collision_rank(sourceless)


def test_confidence_folding_normalizes_drifted_strings():
    """Ensure model-drifted string confidences fold to canonical enums (#33, #4495)."""
    edges = [
        {"source": "a", "target": "b", "relation": "calls", "confidence": "high"},
        {"source": "b", "target": "c", "relation": "calls", "confidence": "confirmed"},
        {"source": "c", "target": "d", "relation": "calls", "confidence": "medium"},
        {"source": "d", "target": "e", "relation": "calls", "confidence": "low"},
        {"source": "e", "target": "f", "relation": "calls", "confidence": "ambiguous"},
        {"source": "f", "target": "g", "relation": "calls", "confidence": "inferred"},
    ]
    for e in edges:
        _fold_edge_aliases(e)

    assert edges[0]["confidence"] == "EXTRACTED"
    assert edges[1]["confidence"] == "EXTRACTED"
    assert edges[2]["confidence"] == "INFERRED"
    assert edges[3]["confidence"] == "AMBIGUOUS"
    assert edges[4]["confidence"] == "AMBIGUOUS"
    assert edges[5]["confidence"] == "INFERRED"

    # Verify schema validation passes for folded extraction
    data = {
        "nodes": [
            {"id": x, "label": x, "file_type": "code", "source_file": "test.py"}
            for x in ["a", "b", "c", "d", "e", "f", "g"]
        ],
        "edges": [
            {**e, "source_file": "test.py"} for e in edges
        ],
    }
    errors = validate_extraction(data)
    assert errors == []


def test_python_noise_filters_self_and_property_decorators():
    """Ensure Self and property decorators do not mint sourceless stubs (#38, #4380, #4381)."""
    assert "Self" in _PYTHON_ANNOTATION_NOISE
    assert "setter" in _PYTHON_DECORATOR_NOISE
    assert "getter" in _PYTHON_DECORATOR_NOISE
    assert "deleter" in _PYTHON_DECORATOR_NOISE
