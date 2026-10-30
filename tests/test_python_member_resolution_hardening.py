"""Tests for Python member resolution language gating, exact-name matching, and type aliases (Issue #79)."""
from __future__ import annotations

import warnings
from pathlib import Path
import pytest

from graph_fy.extract import extract
from graph_fy.member_resolution import _resolve_python_member_calls


def _extract(tmp_path: Path, files: dict[str, str]) -> dict:
    for name, src in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return extract(
            [tmp_path / n for n in files],
            root=tmp_path,
            cache_root=tmp_path / "graph_fy_out",
            parallel=False,
        )


def _calls(r: dict) -> set[tuple[str, str]]:
    label = {n["id"]: n["label"] for n in r["nodes"]}
    return {
        (label.get(e["source"]), label.get(e["target"]))
        for e in r["edges"]
        if e["relation"] == "calls"
    }


_WORKER = (
    "class Worker:\n"
    "    def run(self):\n"
    "        return 1\n"
    "\n"
    "    def _run(self):\n"
    "        return 2\n"
)


def test_class_qualified_call_hits_the_spelled_method(tmp_path: Path):
    r = _extract(tmp_path, {
        "worker.py": _WORKER,
        "main.py": "from worker import Worker\n\ndef via_run():\n    return Worker.run(None)\n\ndef via_under_run():\n    return Worker._run(None)\n",
    })
    calls = _calls(r)
    assert ("via_run()", ".run()") in calls
    assert ("via_run()", "._run()") not in calls
    assert ("via_under_run()", "._run()") in calls
    assert ("via_under_run()", ".run()") not in calls


def test_python_caller_does_not_bind_swift_class(tmp_path: Path):
    r = _extract(tmp_path, {
        "inner.swift": "class Inner {\n    static func go() {}\n}\n",
        "use.py": "def run():\n    Inner.go()\n",
    })
    calls = _calls(r)
    # The Python call should not bind to the Swift class
    assert not any(src == "run()" and "go" in str(tgt) for src, tgt in calls)


def test_python_explicit_type_aliases_extracted_as_type_nodes(tmp_path: Path):
    source = (
        "from typing import NewType, TypeAlias\n"
        "type Vector = list[float]\n"
        "UserId = NewType('UserId', int)\n"
        "Matrix: TypeAlias = list[list[float]]\n"
        "\n"
        "def scale(v: Vector, u: UserId, m: Matrix) -> Vector:\n"
        "    return v\n"
    )
    r = _extract(tmp_path, {"types_.py": source})
    labels = {n["label"] for n in r["nodes"]}
    assert "Vector" in labels
    assert "UserId" in labels
    assert "Matrix" in labels

    # Check annotations reference the type aliases
    refs = {
        (n[e["source"]]["label"], n[e["target"]]["label"])
        for n in [{node["id"]: node for node in r["nodes"]}]
        for e in r["edges"]
        if e["relation"] == "references" and e["source"] in n and e["target"] in n
    }
    assert ("scale()", "Vector") in refs
    assert ("scale()", "UserId") in refs
    assert ("scale()", "Matrix") in refs
