"""Tests for comparative evaluation framework: simulated vs empirical modes, token counting, and blast radius recall."""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest

from graph_fy.commands.eval import cmd_eval
from graph_fy.comparative_eval import (
    ComparativeCaseResult,
    ComparativeReport,
    count_tokens,
    run_comparative_evals,
)


def test_count_tokens():
    assert count_tokens("") == 0
    code = "def authenticate_user(username: str, token: str) -> bool:\n    return len(token) > 16"
    toks = count_tokens(code)
    assert toks > 10
    assert toks < len(code)


def test_comparative_eval_simulated_mode():
    G = nx.Graph()
    G.add_node("session", label="session", source_file="flask/sessions.py", source_location="L10")
    G.add_node("save_session", label="save_session", source_file="flask/sessions.py", source_location="L25")
    G.add_edge("session", "save_session", relation="calls")

    scenarios_file = Path("evals/real_world_scenarios.json")
    report = run_comparative_evals(
        graph=G,
        scenarios_path=scenarios_file,
        limit=5,
        mode="simulated",
    )
    assert report.mode == "simulated"
    assert report.total_scenarios == 5
    assert len(report.cases) == 5
    assert report.avg_token_compression_pct > 80.0
    assert report.skill_blast_radius_recall_pct > 0.0

    md = report.to_markdown()
    assert "Evaluation Mode: Simulated (Heuristic Baseline Model)" in md
    assert "## Benchmark Methodology & Limitations" in md
    assert "Syntactic vs Dynamic Dispatch" in md


def test_comparative_eval_empirical_mode():
    G = nx.Graph()
    G.add_node("session", label="session", source_file="flask/sessions.py", source_location="L10")
    G.add_node("save_session", label="save_session", source_file="flask/sessions.py", source_location="L25")
    G.add_edge("session", "save_session", relation="calls")

    traces_file = Path("evals/empirical_traces.json")
    assert traces_file.exists(), "evals/empirical_traces.json should exist"

    scenarios_file = Path("evals/real_world_scenarios.json")
    report = run_comparative_evals(
        graph=G,
        scenarios_path=scenarios_file,
        limit=5,
        mode="empirical",
        traces_path=traces_file,
    )
    assert report.mode == "empirical"
    assert report.total_scenarios == 5
    # First scenario is SEC-001 which is present in empirical traces
    sec_001 = next(c for c in report.cases if c.id == "SEC-001")
    assert "[Empirical]" in sec_001.skill_details or "[Empirical]" in sec_001.raw_details

    md = report.to_markdown()
    assert "Evaluation Mode: Empirical (Live Agent Traces & Ground Truth)" in md
    assert "## Benchmark Methodology & Limitations" in md


def test_cmd_eval_help(capsys):
    rc = cmd_eval(["graph_fy", "eval", "--help"], default_graph_path_fn=lambda: "nonexistent.json")
    assert rc == 0
    captured = capsys.readouterr()
    assert "--simulated" in captured.out
    assert "--empirical" in captured.out
    assert "--traces" in captured.out


def test_cmd_eval_comparative_cli(tmp_path, capsys):
    G = nx.Graph()
    G.add_node("session", label="session", source_file="flask/sessions.py", source_location="L10")
    graph_path = tmp_path / "graph.json"
    data = {
        "nodes": [{"id": "session", "label": "session", "source_file": "flask/sessions.py"}],
        "edges": [],
    }
    graph_path.write_text(json.dumps(data), encoding="utf-8")

    report_out = tmp_path / "comparative_report.md"
    rc = cmd_eval(
        [
            "graph_fy",
            "eval",
            "--comparative",
            "--empirical",
            f"--graph={graph_path}",
            "--limit=2",
            f"--report={report_out}",
            "--json",
        ],
        default_graph_path_fn=lambda: str(graph_path),
    )
    assert rc == 0
    assert report_out.exists()
    md_content = report_out.read_text(encoding="utf-8")
    assert "Evaluation Mode: Empirical" in md_content
    captured = capsys.readouterr()
    json_out = json.loads(captured.out)
    assert json_out["mode"] == "empirical"
    assert json_out["total_scenarios"] == 2
