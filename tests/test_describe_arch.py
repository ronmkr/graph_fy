"""Tests for graph_fy describe arch (rich interactive HTML architecture view)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from graph_fy.describe_arch import (
    build_architecture_model,
    classify_node_role,
    describe_architecture,
    generate_architecture_html,
    generate_architecture_svg,
    generate_architecture_mermaid,
)

PYTHON = sys.executable


def _make_sample_service_graph(tmp_path: Path) -> Path:
    """Create a mock knowledge graph for a backend service application."""
    out = tmp_path / "graph_fy_out"
    out.mkdir(parents=True, exist_ok=True)

    # Mock source files so AST skeleton extraction has files to read
    src_dir = tmp_path / "services"
    src_dir.mkdir(parents=True, exist_ok=True)

    api_file = src_dir / "api.py"
    api_file.write_text(
        '"""API Intake and Request Ingestion."""\n\n'
        'def handle_request(payload: dict) -> dict:\n'
        '    """Handle incoming API request."""\n'
        '    return {"status": "ok"}\n'
    )

    auth_file = src_dir / "auth.py"
    auth_file.write_text(
        '"""Authentication & Security Validation."""\n\n'
        'def verify_auth_token(token: str) -> bool:\n'
        '    """Validate bearer token permissions."""\n'
        '    return True\n'
    )

    engine_file = src_dir / "engine.py"
    engine_file.write_text(
        '"""Core domain execution engine."""\n\n'
        'class ExecutionEngine:\n'
        '    """State machine for processing workflows."""\n'
        '    def process_task(self, task_id: str) -> str:\n'
        '        """Execute workflow logic."""\n'
        '        return "completed"\n'
    )

    storage_file = src_dir / "storage.py"
    storage_file.write_text(
        '"""Persistence store handler."""\n\n'
        'def commit_record(record_id: str, data: dict) -> bool:\n'
        '    """Commit state record to storage."""\n'
        '    return True\n'
    )

    graph = {
        "directed": True,
        "multigraph": True,
        "graph": {"project_name": "WorkflowEngine"},
        "metadata": {"project_name": "WorkflowEngine"},
        "nodes": [
            # 1. Initiation
            {
                "id": "post_request",
                "label": "POST /v1/tasks",
                "node_type": "route",
                "community_id": "0",
                "source_file": "services/api.py",
                "source_location": "L3",
            },
            {
                "id": "handle_request",
                "label": "handle_request()",
                "node_type": "function",
                "community_id": "0",
                "source_file": "services/api.py",
                "source_location": "L3",
            },
            # 2. Validation & Security
            {
                "id": "verify_auth_token",
                "label": "verify_auth_token()",
                "node_type": "middleware",
                "community_id": "1",
                "source_file": "services/auth.py",
                "source_location": "L3",
            },
            # 3. Processing
            {
                "id": "engine",
                "label": "ExecutionEngine",
                "node_type": "service",
                "_callable_class": True,
                "community_id": "2",
                "source_file": "services/engine.py",
                "source_location": "L3",
            },
            # 4. Gateway
            {
                "id": "remote_gateway",
                "label": "RemoteApiClient",
                "node_type": "external_api",
                "source_file": "services/gateway.py",
                "source_location": "L1",
            },
            # 5. Settlement / Persistence
            {
                "id": "commit_record",
                "label": "commit_record()",
                "node_type": "service",
                "community_id": "3",
                "source_file": "services/storage.py",
                "source_location": "L3",
            },
            {
                "id": "data_table",
                "label": "tasks_table",
                "node_type": "sql_table",
                "community_id": "3",
                "source_file": "services/schema.sql",
                "source_location": "L1",
            },
            # 6. Events & Outputs
            {
                "id": "task_events_topic",
                "label": "tasks.completed topic",
                "node_type": "kafka_topic",
                "source_file": "services/events.py",
                "source_location": "L1",
            },
        ],
        "links": [
            {"source": "post_request", "target": "handle_request", "relation": "calls"},
            {"source": "handle_request", "target": "verify_auth_token", "relation": "calls"},
            {"source": "verify_auth_token", "target": "engine", "relation": "calls"},
            {"source": "engine", "target": "remote_gateway", "relation": "calls"},
            {"source": "engine", "target": "commit_record", "relation": "calls"},
            {"source": "commit_record", "target": "data_table", "relation": "writes_to"},
            {"source": "commit_record", "target": "task_events_topic", "relation": "publishes_to"},
        ],
    }

    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    (out / ".graph_fy_labels.json").write_text(
        json.dumps({
            "0": "API Intake Gateway",
            "1": "Auth Guard & Security",
            "2": "Task Orchestration Engine",
            "3": "Persistent Record Store",
        }),
        encoding="utf-8",
    )
    return out


# ── Unit Tests ───────────────────────────────────────────────────────────────


def test_classify_node_role():
    assert classify_node_role({"label": "POST /charge", "node_type": "route"})[0] == "route"
    assert classify_node_role({"label": "AuthGuard", "node_type": "middleware"})[0] == "middleware"
    assert classify_node_role({"label": "PaymentService", "node_type": "service"})[0] == "service"
    assert classify_node_role({"label": "StripeClient", "node_type": "external_api"})[0] == "external_api"
    assert classify_node_role({"label": "transactions_table", "node_type": "table"})[0] == "database"
    assert classify_node_role({"label": "payments_topic", "node_type": "kafka_topic"})[0] == "queue"


def test_custom_repo_dynamic_stages(tmp_path):
    """Verify that architecture synthesis adapts dynamically to repository-specific structure."""
    out = tmp_path / "graph_fy_out"
    out.mkdir(parents=True, exist_ok=True)

    cli_file = tmp_path / "cli" / "main.py"
    cli_file.parent.mkdir(parents=True, exist_ok=True)
    cli_file.write_text('def main():\n    """CLI entry point for compiler tool."""\n    pass\n')

    graph = {
        "directed": True,
        "multigraph": True,
        "metadata": {"project_name": "ast-analyzer"},
        "nodes": [
            {"id": "main", "label": "main()", "node_type": "cli", "community_id": "c0", "source_file": "cli/main.py", "source_location": "L1"},
            {"id": "parse", "label": "parse_ast()", "node_type": "service", "community_id": "c1", "source_file": "parser/ast.py"},
            {"id": "store", "label": "symbol_table", "node_type": "table", "community_id": "c2", "source_file": "db/schema.sql"},
        ],
        "links": [
            {"source": "main", "target": "parse", "relation": "calls"},
            {"source": "parse", "target": "store", "relation": "writes_to"},
        ],
    }

    labels = {
        "c0": "Command Line Dispatch",
        "c1": "AST Parser Engine",
        "c2": "Symbol Metadata Store",
    }

    model = build_architecture_model(graph, labels=labels, project_root=tmp_path)
    assert model["project_name"] == "ast-analyzer"
    assert "domain" not in model

    stage_names = [s["name"] for s in model["stages"]]
    assert any("Command Line Dispatch" in s for s in stage_names)
    assert any("AST Parser Engine" in s for s in stage_names)
    assert any("Symbol Metadata Store" in s for s in stage_names)

    # Verify docstring extraction for step description
    cli_stage = next(s for s in model["stages"] if s["id"] == "initiation")
    assert cli_stage["steps"][0]["description"] == "CLI entry point for compiler tool."


def test_repository_architecture_lifecycle(tmp_path):
    out = _make_sample_service_graph(tmp_path)
    graph_data = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    labels_data = json.loads((out / ".graph_fy_labels.json").read_text(encoding="utf-8"))

    model = build_architecture_model(graph_data, labels=labels_data, project_root=tmp_path)

    assert "domain" not in model
    assert model["project_name"] == "WorkflowEngine"

    stage_ids = [s["id"] for s in model["stages"]]
    assert "initiation" in stage_ids
    assert "processing" in stage_ids
    assert "settlement" in stage_ids

    stage_names = [s["name"] for s in model["stages"]]
    assert any("API Intake Gateway" in name for name in stage_names)
    assert any("Task Orchestration Engine" in name for name in stage_names)
    assert any("Persistent Record Store" in name for name in stage_names)

    # Check steps
    init_stage = next(s for s in model["stages"] if s["id"] == "initiation")
    assert len(init_stage["steps"]) >= 1
    assert init_stage["steps"][0]["step_number"] == "1.1"

    # Check AST symbol extraction
    symbols = init_stage["steps"][0]["symbols"]
    assert len(symbols) >= 1
    init_sym = next(sym for sym in symbols if "handle_request" in sym["label"])
    assert "def handle_request" in init_sym["signature"]
    assert "Handle incoming API request." in init_sym["docstring"]

    # Generate HTML
    html = generate_architecture_html(model)

    # Ensure both Native SVG and Mermaid are supported
    assert "mermaid" in html.lower()
    assert "flowchart LR" in html
    assert 'id="arch-svg-canvas"' in html
    assert 'id="ast-drawer"' in html
    assert "WorkflowEngine" in html
    assert "API Intake Gateway" in html
    assert "POST /v1/tasks" in html
    assert "tasks_table" in html


def test_generate_architecture_mermaid():
    from graph_fy.describe_arch import generate_architecture_mermaid
    stages = [
        {
            "id": "initiation",
            "name": "Stage 1: Initiation",
            "icon": "⚡",
            "color": "#38bdf8",
            "bg_color": "rgba(56, 189, 248, 0.12)",
            "steps": [
                {
                    "id": "step-1-1",
                    "step_number": "1.1",
                    "name": "Intake Payment",
                    "symbols": [{"id": "s1", "label": "post_payment()"}],
                }
            ],
        },
        {
            "id": "settlement",
            "name": "Stage 5: Settlement",
            "icon": "💾",
            "color": "#f472b6",
            "bg_color": "rgba(244, 114, 182, 0.12)",
            "steps": [
                {
                    "id": "step-5-1",
                    "step_number": "5.1",
                    "name": "Commit Ledger",
                    "symbols": [{"id": "s2", "label": "ledger_table"}],
                }
            ],
        },
    ]
    mm = generate_architecture_mermaid(stages)
    assert "flowchart LR" in mm
    assert "subgraph stage_initiation" in mm
    assert "subgraph stage_settlement" in mm
    assert "Intake Payment" in mm
    assert "==>" in mm


def test_generate_architecture_svg():
    stages = [
        {
            "id": "initiation",
            "name": "Stage 1: Initiation",
            "icon": "⚡",
            "color": "#38bdf8",
            "bg_color": "rgba(56, 189, 248, 0.12)",
            "border_color": "rgba(56, 189, 248, 0.35)",
            "steps": [
                {
                    "id": "step-1-1",
                    "step_number": "1.1",
                    "name": "Intake Payment",
                    "category_tag": "Route",
                    "symbols": [{"id": "s1", "label": "post_payment()"}],
                }
            ],
        },
        {
            "id": "settlement",
            "name": "Stage 5: Settlement",
            "icon": "💾",
            "color": "#f472b6",
            "bg_color": "rgba(244, 114, 182, 0.12)",
            "border_color": "rgba(244, 114, 182, 0.35)",
            "steps": [
                {
                    "id": "step-5-1",
                    "step_number": "5.1",
                    "name": "Commit Ledger",
                    "category_tag": "DB",
                    "symbols": [{"id": "s2", "label": "ledger_table"}],
                }
            ],
        },
    ]

    svg = generate_architecture_svg(stages)
    assert "<svg" in svg
    assert "Stage 1: Initiation" in svg
    assert "Stage 5: Settlement" in svg
    assert "post_payment()" in svg
    assert "marker-end=\"url(#arrow)\"" in svg
    assert "mermaid" not in svg.lower()

    # Jev.ai Design Language Assertions:
    # 1. Blueprint dot matrix pattern
    assert 'id="blueprint-dots"' in svg
    # 2. Glowing packet filter
    assert 'id="packet-glow"' in svg
    # 3. Native hardware-accelerated animated traveling pulses
    assert "<animateMotion" in svg
    assert "<mpath" in svg
    # 4. Inter-stage bezier flow wires
    assert "flow-wire inter-wire" in svg
    # 5. Live flow monitor telemetry banner
    assert "PIPELINE MONITOR // LIVE FLOW ACTIVE" in svg
    # 6. Blueprint CAD corner registration marks
    assert 'stroke="#38bdf8"' in svg
    # 7. Machine decision badges
    assert "INGRESS: 200 OK" in svg or "SINK: COMMIT" in svg


def test_jev_animated_svg_intra_stage_vertical_bus():
    """Verify intra-stage vertical flow bus and packets when a stage has multiple steps."""
    stages = [
        {
            "id": "processing",
            "name": "Processing",
            "icon": "⚙️",
            "color": "#f43f5e",
            "bg_color": "rgba(244, 63, 94, 0.12)",
            "border_color": "rgba(244, 63, 94, 0.35)",
            "symbols_count": 2,
            "steps": [
                {
                    "id": "step-1",
                    "step_number": "1.1",
                    "name": "Validate Request",
                    "category_tag": "Eval",
                    "symbols": [{"id": "s1", "label": "validate()", "kind": "func"}],
                },
                {
                    "id": "step-2",
                    "step_number": "1.2",
                    "name": "Process State",
                    "category_tag": "Core",
                    "symbols": [{"id": "s2", "label": "process()", "kind": "method"}],
                },
            ],
        }
    ]
    svg = generate_architecture_svg(stages)
    assert 'wire-intra-0-0' in svg
    assert 'flow-wire intra-wire' in svg
    assert '<animateMotion' in svg
    assert 'EVAL: VERIFIED' in svg


# ── CLI Integration Tests ─────────────────────────────────────────────────────


def test_describe_arch_cli_generates_html(tmp_path):
    out = _make_sample_service_graph(tmp_path)
    output_html = tmp_path / "architecture_test.html"

    res = subprocess.run(
        [
            PYTHON,
            "-m",
            "graph_fy",
            "describe",
            "arch",
            str(out / "graph.json"),
            "--output",
            str(output_html),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, res.stderr
    assert output_html.is_file()
    content = output_html.read_text(encoding="utf-8")
    assert "WorkflowEngine" in content
    assert "mermaid" in content.lower()
    assert "Architecture HTML view written" in res.stdout


def test_describe_arch_cli_json_mode(tmp_path):
    out = _make_sample_service_graph(tmp_path)

    res = subprocess.run(
        [
            PYTHON,
            "-m",
            "graph_fy",
            "describe",
            "arch",
            str(out / "graph.json"),
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["project_name"] == "WorkflowEngine"
    assert "stages" in data
    assert len(data["stages"]) >= 4


def test_describe_arch_cli_focus_flag(tmp_path):
    out = _make_sample_service_graph(tmp_path)

    res = subprocess.run(
        [
            PYTHON,
            "-m",
            "graph_fy",
            "describe",
            "arch",
            str(out / "graph.json"),
            "--focus",
            "storage",
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["focus"] == "storage"


def test_describe_help_commands():
    res1 = subprocess.run(
        [PYTHON, "-m", "graph_fy", "describe", "--help"],
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0
    assert "Usage: graph_fy describe arch" in res1.stdout

    res2 = subprocess.run(
        [PYTHON, "-m", "graph_fy", "describe", "arch", "--help"],
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0
    assert "Options:" in res2.stdout


def test_describe_missing_graph_exits_1(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    res = subprocess.run(
        [PYTHON, "-m", "graph_fy", "describe", "arch", str(empty_dir)],
        cwd=empty_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert "error: graph.json not found" in res.stderr


def test_generate_architecture_mermaid_orientations():
    stages = [
        {
            "id": "initiation",
            "name": "Stage 1: Intake",
            "icon": "⚡",
            "steps": [{"id": "s1", "step_number": "1.1", "name": "Context", "symbols": []}],
        }
    ]
    mm_lr = generate_architecture_mermaid(stages, direction="LR")
    assert mm_lr.startswith("flowchart LR")

    mm_td = generate_architecture_mermaid(stages, direction="TD")
    assert mm_td.startswith("flowchart TD")


def test_architecture_svg_stage_header_subsystem_layout():
    stages = [
        {
            "id": "gateway",
            "name": "Stage 4: Integrations & Remote Clients — werkzeug/http.py",
            "icon": "🌐",
            "color": "#c084fc",
            "bg_color": "rgba(192, 132, 252, 0.12)",
            "border_color": "rgba(192, 132, 252, 0.35)",
            "symbols_count": 10,
            "subsystem": "werkzeug/http.py",
            "steps": [
                {
                    "id": "step-4-1",
                    "step_number": "4.1",
                    "name": "Dispatch Remote",
                    "category_tag": "GATEWAY",
                    "symbols": [{"id": "s41", "label": "remote_call()", "kind": "func"}],
                }
            ],
        }
    ]
    svg = generate_architecture_svg(stages)
    # 1. Monospace stage tag and steps pill on Row 1
    assert "STAGE 01 // GATEWAY" in svg
    assert "1 STEPS" in svg
    # 2. Cleaned stage title on Row 2
    assert "Stage 4: Integrations" in svg
    # 3. Subsystem on Row 3
    assert "λ werkzeug/http.py" in svg
    # 4. Step card with 10-char category tag
    assert "GATEWAY" in svg

