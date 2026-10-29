"""Tests for issue #4767 / #66: JS/TS nested-function extraction + lexical-scope-aware call resolution."""
from pathlib import Path
import pytest

from graph_fy.extract import extract


def test_js_nested_function_node_and_contains(tmp_path: Path):
    f = tmp_path / "test_nested.js"
    f.write_text(
        "function outer() {\n"
        "    function inner() {}\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    by_label = {n["label"]: n for n in result["nodes"]}

    assert "outer()" in by_label
    assert "inner()" in by_label

    outer_id = by_label["outer()"]["id"]
    inner_id = by_label["inner()"]["id"]

    assert inner_id == f"{outer_id}_inner"
    edges = [(e["source"], e["target"], e["relation"]) for e in result["edges"]]
    assert (outer_id, inner_id, "contains") in edges


def test_js_outer_calls_nested_function(tmp_path: Path):
    f = tmp_path / "test_call.js"
    f.write_text(
        "function outer() {\n"
        "    function inner() {}\n"
        "    inner();\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    by_label = {n["label"]: n for n in result["nodes"]}

    outer_id = by_label["outer()"]["id"]
    inner_id = by_label["inner()"]["id"]

    call_edges = [
        e for e in result["edges"]
        if e["source"] == outer_id and e["target"] == inner_id and e["relation"] == "calls"
    ]
    assert len(call_edges) == 1
    edge = call_edges[0]
    assert edge["confidence"] == "EXTRACTED"
    assert edge["weight"] == 1.0


def test_js_nested_function_does_not_leak_to_module_scope(tmp_path: Path):
    f = tmp_path / "test_shadow.js"
    f.write_text(
        "function walk() {}\n"
        "\n"
        "function trace() {\n"
        "    function walk() {}\n"
        "    walk();\n"
        "}\n"
        "\n"
        "function other() {\n"
        "    walk();\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    trace_nodes = [n for n in result["nodes"] if n["label"] == "trace()"]
    assert len(trace_nodes) == 1
    trace_id = trace_nodes[0]["id"]

    other_nodes = [n for n in result["nodes"] if n["label"] == "other()"]
    assert len(other_nodes) == 1
    other_id = other_nodes[0]["id"]

    walk_nodes = [n for n in result["nodes"] if n["label"] == "walk()"]
    assert len(walk_nodes) == 2

    nested_walk = next(n for n in walk_nodes if n["id"].startswith(trace_id))
    module_walk = next(n for n in walk_nodes if not n["id"].startswith(trace_id))

    calls = [
        (e["source"], e["target"], e["relation"], e["confidence"])
        for e in result["edges"]
        if e["relation"] == "calls"
    ]

    # trace calls nested_walk, other calls module_walk
    assert (trace_id, nested_walk["id"], "calls", "EXTRACTED") in calls
    assert (trace_id, module_walk["id"], "calls", "EXTRACTED") not in calls
    assert (other_id, module_walk["id"], "calls", "EXTRACTED") in calls
    assert (other_id, nested_walk["id"], "calls", "EXTRACTED") not in calls


def test_js_nested_sibling_call(tmp_path: Path):
    f = tmp_path / "test_sibling.js"
    f.write_text(
        "function outer() {\n"
        "    function helper() {}\n"
        "    function worker() {\n"
        "        helper();\n"
        "    }\n"
        "    worker();\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    by_label = {n["label"]: n for n in result["nodes"]}

    outer_id = by_label["outer()"]["id"]
    helper_id = by_label["helper()"]["id"]
    worker_id = by_label["worker()"]["id"]

    calls = [
        (e["source"], e["target"], e["relation"])
        for e in result["edges"]
        if e["relation"] == "calls"
    ]

    assert (worker_id, helper_id, "calls") in calls
    assert (outer_id, worker_id, "calls") in calls


def test_ts_nested_function_scoping(tmp_path: Path):
    f = tmp_path / "test_nested.ts"
    f.write_text(
        "function globalHandler(): void {}\n"
        "function setup(): void {\n"
        "    function globalHandler(): void {}\n"
        "    globalHandler();\n"
        "}\n"
        "function execute(): void {\n"
        "    globalHandler();\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    setup_id = next(n["id"] for n in result["nodes"] if n["label"] == "setup()")
    execute_id = next(n["id"] for n in result["nodes"] if n["label"] == "execute()")

    handler_nodes = [n for n in result["nodes"] if n["label"] == "globalHandler()"]
    assert len(handler_nodes) == 2

    nested_handler = next(n for n in handler_nodes if n["id"].startswith(setup_id))
    module_handler = next(n for n in handler_nodes if not n["id"].startswith(setup_id))

    calls = [(e["source"], e["target"], e["relation"]) for e in result["edges"] if e["relation"] == "calls"]
    assert (setup_id, nested_handler["id"], "calls") in calls
    assert (execute_id, module_handler["id"], "calls") in calls
