"""Tests for adversarial, graded, and distractor-aware evaluation framework.

Based on the core principle: "If Your AI Scores 100%, Your Eval Is Wrong".
Tests continuous grounding fidelity scores, noise ratio computation,
hard plausible distractor suppression, and counterfactual absent-symbol handling.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import pytest

from graph_fy.comparative_eval import (
    ComparativeCaseResult,
    ComparativeReport,
    run_comparative_evals,
)


def _make_sample_graph():
    G = nx.Graph()
    G.add_node("session", label="session", source_file="flask/sessions.py", source_location="L10")
    G.add_node("save_session", label="save_session", source_file="flask/sessions.py", source_location="L25")
    G.add_node("SessionInterface", label="SessionInterface", source_file="flask/sessions.py", source_location="L50")
    G.add_node("SecureCookieSession", label="SecureCookieSession", source_file="flask/sessions.py", source_location="L70")
    G.add_edge("session", "save_session", relation="calls")
    G.add_edge("save_session", "SessionInterface", relation="references")
    G.add_edge("SessionInterface", "SecureCookieSession", relation="inherits_from")
    return G


def test_adversarial_scenarios_loaded_and_categorized():
    scenarios_file = Path("evals/real_world_scenarios.json")
    assert scenarios_file.exists()
    data = json.loads(scenarios_file.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", [])

    adv_scenarios = [s for s in scenarios if s.get("category") == "Adversarial & Counterfactual Stress Tests"]
    assert len(adv_scenarios) >= 5, f"Expected at least 5 adversarial scenarios, found {len(adv_scenarios)}"

    ids = {s["id"] for s in adv_scenarios}
    assert "ADV-001" in ids  # Homonym disambiguation
    assert "ADV-002" in ids  # Removed before_first_request counterfactual
    assert "ADV-003" in ids  # Confabulated parameter
    assert "ADV-004" in ids  # Polymorphic dispatch
    assert "ADV-005" in ids  # God-node flood resistance


def test_graded_fidelity_and_distractor_suppression(tmp_path):
    G = _make_sample_graph()

    # Create synthetic test scenarios: one clean, one with distractor leak, one phantom
    custom_scenarios = [
        {
            "id": "TEST-CLEAN",
            "title": "Clean Retrieval",
            "category": "Test",
            "severity": "LOW",
            "user_prompt": "Audit Flask session handling in save_session",
            "target_symbols": ["session", "save_session"],
            "expected_files": ["flask/sessions.py"],
            "forbidden_hallucinations": ["NonExistentCrypto"],
            "plausible_distractors": ["sqlalchemy.orm.Session"],
            "verification": {
                "must_contain": ["session", "save_session"],
                "must_not_contain": ["NonExistentCrypto"],
                "is_phantom": False,
                "max_tokens": 1500,
            },
        },
        {
            "id": "TEST-DISTRACTOR",
            "title": "Distractor Leaked",
            "category": "Test",
            "severity": "HIGH",
            "user_prompt": "Inspect SQLAlchemy session coupling with Flask session",
            "target_symbols": ["session", "SessionInterface"],
            "expected_files": ["flask/sessions.py"],
            "forbidden_hallucinations": [],
            "plausible_distractors": ["sqlalchemy.orm.session"],
            "verification": {
                "must_contain": ["session"],
                "must_not_contain": ["sqlalchemy.orm.session"],
                "is_phantom": False,
                "max_tokens": 1500,
            },
        },
        {
            "id": "TEST-PHANTOM",
            "title": "Counterfactual Absent API",
            "category": "Test",
            "severity": "CRITICAL",
            "user_prompt": "Audit completely_fake_and_nonexistent_hook_api in request lifecycle",
            "target_symbols": ["completely_fake_and_nonexistent_hook_api"],
            "expected_files": ["flask/app.py"],
            "forbidden_hallucinations": ["def completely_fake_and_nonexistent_hook_api"],
            "plausible_distractors": ["before_request"],
            "verification": {
                "must_contain": ["Target Symbol Not Found"],
                "must_not_contain": ["def completely_fake_and_nonexistent_hook_api"],
                "is_phantom": True,
                "max_tokens": 1500,
            },
        },
    ]

    sc_path = tmp_path / "custom_scenarios.json"
    sc_path.write_text(json.dumps(custom_scenarios), encoding="utf-8")

    report = run_comparative_evals(
        graph=G,
        scenarios_path=sc_path,
        mode="simulated",
    )

    assert report.total_scenarios == 3
    assert len(report.cases) == 3

    clean_case = next(c for c in report.cases if c.id == "TEST-CLEAN")
    assert clean_case.distractor_suppressed is True
    assert clean_case.result_grade in ("PASS", "PARTIAL")
    assert clean_case.grounding_fidelity_score > 50.0
    assert 0.0 <= clean_case.context_noise_ratio <= 1.0

    phantom_case = next(c for c in report.cases if c.id == "TEST-PHANTOM")
    assert phantom_case.is_phantom is True
    # The nonexistent API should trigger "Target Symbol Not Found" or unmapped guard
    assert phantom_case.skill_anti_hallucination is True
    assert phantom_case.result_grade == "PASS"

    # Report metrics
    assert 0.0 <= report.avg_grounding_fidelity_score <= 100.0
    assert 0.0 <= report.avg_context_noise_ratio <= 1.0
    assert "PASS" in report.grade_distribution
    assert report.distractor_suppression_rate_pct >= 0.0

    md = report.to_markdown()
    assert "Grounding Fidelity (Graded)" in md
    assert "Distractor Suppression Rate" in md
    assert "Context Noise Ratio" in md
    assert "Quality Grade Distribution" in md
    assert "Lokanathan Invariant" in md


def test_markdown_report_reflects_graded_pareto_curves():
    case_pass = ComparativeCaseResult(
        id="C-1",
        title="High Precision Case",
        category="Security",
        severity="HIGH",
        user_prompt="Audit auth token validation",
        is_phantom=False,
        skill_passed=True,
        skill_tokens=210,
        skill_turns=1,
        skill_latency_ms=120.0,
        skill_anti_hallucination=True,
        skill_blast_radius_recall=100.0,
        skill_details="Full match",
        raw_passed=True,
        raw_tokens=15000,
        raw_turns=5,
        raw_latency_ms=16000.0,
        raw_anti_hallucination=True,
        raw_blast_radius_recall=40.0,
        raw_details="5 turns",
        token_compression_pct=98.6,
        turn_reduction_pct=80.0,
        latency_speedup_x=133.0,
        cost_savings_usd=0.044,
        precision_at_3=100.0,
        precision_at_5=80.0,
        context_noise_ratio=0.18,
        grounding_fidelity_score=92.5,
        distractor_suppressed=True,
        result_grade="PASS",
    )

    case_partial = ComparativeCaseResult(
        id="C-2",
        title="Partial Match with Minor Noise",
        category="Security",
        severity="MEDIUM",
        user_prompt="Trace cookie serializer",
        is_phantom=False,
        skill_passed=True,
        skill_tokens=350,
        skill_turns=1,
        skill_latency_ms=140.0,
        skill_anti_hallucination=True,
        skill_blast_radius_recall=75.0,
        skill_details="Partial match",
        raw_passed=True,
        raw_tokens=15000,
        raw_turns=5,
        raw_latency_ms=16000.0,
        raw_anti_hallucination=True,
        raw_blast_radius_recall=30.0,
        raw_details="5 turns",
        token_compression_pct=97.6,
        turn_reduction_pct=80.0,
        latency_speedup_x=114.0,
        cost_savings_usd=0.044,
        precision_at_3=66.7,
        precision_at_5=60.0,
        context_noise_ratio=0.32,
        grounding_fidelity_score=68.4,
        distractor_suppressed=True,
        result_grade="PARTIAL",
    )

    report = ComparativeReport(
        total_scenarios=2,
        skill_passed_count=2,
        raw_passed_count=2,
        skill_pass_rate_pct=100.0,
        raw_pass_rate_pct=100.0,
        total_tokens_with_skill=560,
        total_tokens_without_skill=30000,
        total_tokens_saved=29440,
        avg_token_compression_pct=98.1,
        total_cost_saved_usd=0.088,
        avg_skill_latency_ms=130.0,
        avg_raw_latency_ms=16000.0,
        avg_latency_speedup_x=123.5,
        avg_skill_turns=1.0,
        avg_raw_turns=5.0,
        avg_turn_reduction_pct=80.0,
        skill_anti_hallucination_pct=100.0,
        raw_anti_hallucination_pct=100.0,
        skill_blast_radius_recall_pct=87.5,
        raw_blast_radius_recall_pct=35.0,
        category_breakdown={
            "Security": {
                "count": 2.0,
                "skill_pass_rate": 100.0,
                "raw_pass_rate": 100.0,
                "token_savings_pct": 98.1,
                "speedup_x": 123.5,
            }
        },
        cases=[case_pass, case_partial],
        mode="simulated",
        avg_grounding_fidelity_score=80.45,
        avg_context_noise_ratio=0.25,
        avg_precision_at_3=83.35,
        distractor_suppression_rate_pct=100.0,
        grade_distribution={"PASS": 1, "PARTIAL": 1, "FAIL": 0},
    )

    md = report.to_markdown()
    assert "**80.5%**" in md  # avg grounding fidelity formatted
    assert "**25.0%**" in md  # noise ratio formatted
    assert "**Strict PASS (Fidelity >= 75%)**: 1" in md
    assert "**PARTIAL (50% <= Fidelity < 75%)**: 1" in md
    assert "**PARTIAL**" in md
    assert "**PASS**" in md
