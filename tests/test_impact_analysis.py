from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest
from networkx.readwrite import json_graph

from graph_fy.impact import compute_impact, format_impact_report, resolve_targets
import graph_fy.__main__ as mainmod


def _build_test_graph() -> nx.DiGraph:
    """Build a rich synthetic knowledge graph with:
    - Core logic (target)
    - Callers / importers (downstream dependencies)
    - Unit and integration tests
    - API endpoints (REST / RPC)
    - IaC / cloud resources
    - God node (hub)
    - Community partitioning
    """
    G = nx.DiGraph()

    # Target node: Core auth service
    G.add_node(
        "auth_service",
        label="AuthService",
        source_file="src/auth.py",
        source_location="L10",
        file_type="code",
        community=1,
    )

    # Sub-component of AuthService
    G.add_node(
        "token_validator",
        label="TokenValidator",
        source_file="src/auth_token.py",
        source_location="L5",
        file_type="code",
        community=1,
    )
    # TokenValidator calls AuthService
    G.add_edge("token_validator", "auth_service", relation="calls")

    # API Endpoint: GET /login and UserAPI
    G.add_node(
        "endpoint_login",
        label="POST /api/v1/login",
        source_file="src/routes/auth_routes.py",
        source_location="L20",
        type="endpoint",
        community=2,
    )
    G.add_edge("endpoint_login", "auth_service", relation="calls")

    G.add_node(
        "rpc_user_service",
        label="UserRPCService",
        source_file="src/routes/rpc.py",
        source_location="L45",
        type="rpc",
        community=2,
    )
    G.add_edge("rpc_user_service", "auth_service", relation="references")

    # Affected Tests: test_auth.py and integration_test
    G.add_node(
        "test_auth_unit",
        label="test_authenticate_success()",
        source_file="tests/test_auth.py",
        source_location="L15",
        file_type="code",
        community=3,
    )
    G.add_edge("test_auth_unit", "auth_service", relation="calls")

    G.add_node(
        "test_auth_integration",
        label="test_oauth_flow()",
        source_file="tests/integration/test_oauth.py",
        source_location="L30",
        file_type="code",
        community=3,
    )
    G.add_edge("test_auth_integration", "token_validator", relation="calls")

    # IaC & Config: k8s ingress / deployment and terraform
    G.add_node(
        "k8s_auth_ingress",
        label="auth-service-ingress",
        source_file="deploy/k8s/ingress.yaml",
        type="k8s",
        community=4,
    )
    G.add_edge("k8s_auth_ingress", "endpoint_login", relation="depends_on")

    G.add_node(
        "tf_auth_db",
        label="aws_rds_cluster.auth",
        source_file="infra/terraform/rds.tf",
        type="terraform",
        community=4,
    )
    G.add_edge("tf_auth_db", "auth_service", relation="references")

    # God node (Hub)
    G.add_node(
        "app_gateway",
        label="AppGateway",
        source_file="src/gateway.py",
        source_location="L1",
        file_type="code",
        community=2,
    )
    G.add_edge("app_gateway", "auth_service", relation="imports")

    # Add extra edges to app_gateway so it acts as a god node (degree >= 10)
    for i in range(12):
        dummy_id = f"dummy_leaf_{i}"
        G.add_node(dummy_id, label=f"leaf_{i}", source_file=f"src/leaf_{i}.py", community=5)
        G.add_edge("app_gateway", dummy_id, relation="calls")

    return G


def _save_graph_file(graph: nx.DiGraph, path: Path) -> Path:
    data = json_graph.node_link_data(graph, edges="links")
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_resolve_targets():
    G = _build_test_graph()

    # Exact ID match
    assert resolve_targets(G, "auth_service") == ["auth_service"]

    # Exact label match
    assert resolve_targets(G, "AuthService") == ["auth_service"]

    # Source file path match
    assert resolve_targets(G, "src/auth.py") == ["auth_service"]

    # Substring match
    matches = resolve_targets(G, "POST /api/v1/login")
    assert "endpoint_login" in matches

    # Non-existent target
    assert resolve_targets(G, "non_existent_symbol") == []


def test_compute_impact_closure_and_categories():
    G = _build_test_graph()
    impact = compute_impact(G, "AuthService", max_depth=5)

    assert impact["target"] == "AuthService"
    assert "auth_service" in impact["target_nodes"]
    assert impact["downstream_count"] >= 6

    # Downstream IDs
    downstream_ids = {n["id"] for n in impact["downstream_nodes"]}
    assert "token_validator" in downstream_ids
    assert "endpoint_login" in downstream_ids
    assert "rpc_user_service" in downstream_ids
    assert "test_auth_unit" in downstream_ids
    assert "test_auth_integration" in downstream_ids
    assert "k8s_auth_ingress" in downstream_ids
    assert "tf_auth_db" in downstream_ids
    assert "app_gateway" in downstream_ids

    # Check detected affected tests
    test_ids = {t["id"] for t in impact["affected_tests"]}
    assert "test_auth_unit" in test_ids
    assert "test_auth_integration" in test_ids

    # Check detected affected APIs
    api_ids = {a["id"] for a in impact["affected_apis"]}
    assert "endpoint_login" in api_ids
    assert "rpc_user_service" in api_ids

    # Check detected affected IaC
    iac_ids = {i["id"] for i in impact["affected_iac"]}
    assert "k8s_auth_ingress" in iac_ids
    assert "tf_auth_db" in iac_ids

    # Check god nodes impacted
    god_ids = {g["id"] for g in impact["god_nodes_impacted"]}
    assert "app_gateway" in god_ids

    # Communities crossed
    assert len(impact["communities_crossed"]) >= 3

    # Risk score & tier
    assert impact["risk_score"] > 0.45
    assert impact["risk_tier"] in ("HIGH", "CRITICAL")
    assert "downstream dependent" in impact["explanation"]
    assert "architectural hub/god node" in impact["explanation"]


def test_compute_impact_leaf_low_risk():
    G = _build_test_graph()
    # Query an integration test which has no downstream dependents
    impact = compute_impact(G, "test_oauth_flow()", max_depth=5)
    assert impact["downstream_count"] == 0
    assert impact["risk_tier"] == "LOW"
    assert impact["risk_score"] < 0.20
    assert len(impact["affected_tests"]) == 0
    assert len(impact["affected_apis"]) == 0


def test_compute_impact_unknown_target():
    G = _build_test_graph()
    impact = compute_impact(G, "UnknownFunction", max_depth=5)
    assert "error" in impact
    assert impact["downstream_count"] == 0
    assert impact["risk_tier"] == "LOW"


def test_format_impact_report():
    G = _build_test_graph()
    impact = compute_impact(G, "AuthService", max_depth=5)

    # Full report formatting
    report = format_impact_report(impact, terse=False)
    assert "PREDICTIVE IMPACT ANALYSIS & BLAST RADIUS" in report
    assert "Risk Tier:" in report
    assert "Affected External API Endpoints" in report
    assert "Affected IaC & Cloud Resources" in report
    assert "Affected Tests" in report
    assert "God Nodes Impacted" in report

    # Terse formatting
    terse = format_impact_report(impact, terse=True)
    assert terse.startswith("Impact for AuthService:")
    assert "dependents" in terse
    assert "tests" in terse
    assert "APIs" in terse


def test_cli_impact_and_blast(monkeypatch, tmp_path, capsys):
    G = _build_test_graph()
    graph_path = _save_graph_file(G, tmp_path / "graph.json")

    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)

    # 1. Test "graph_fy impact AuthService"
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graph_fy", "impact", "AuthService", "--graph", str(graph_path)],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "PREDICTIVE IMPACT ANALYSIS & BLAST RADIUS: AuthService" in out
    assert "Risk Tier:" in out

    # 2. Test "graph_fy blast AuthService --terse"
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graph_fy", "blast", "AuthService", "--terse", "--graph", str(graph_path)],
    )
    mainmod.main()
    out_terse = capsys.readouterr().out
    assert out_terse.startswith("Impact for AuthService:")

    # 3. Test "graph_fy impact AuthService --json"
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graph_fy", "impact", "AuthService", "--json", "--graph", str(graph_path)],
    )
    mainmod.main()
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["target"] == "AuthService"
    assert data["downstream_count"] > 0
    assert "risk_tier" in data


def test_cli_impact_missing_target(monkeypatch, capsys):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", ["graph_fy", "impact"])

    with pytest.raises(SystemExit) as exc:
        mainmod.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Usage: graph_fy impact <target>" in err


def test_graph_fy_mcp_module_impact(tmp_path):
    from graph_fy.mcp import get_impact_analysis, get_blast_radius, IMPACT_TOOL_SPEC, BLAST_TOOL_SPEC

    assert IMPACT_TOOL_SPEC["name"] == "get_impact_analysis"
    assert BLAST_TOOL_SPEC["name"] == "get_blast_radius"

    G = _build_test_graph()
    graph_path = _save_graph_file(G, tmp_path / "graph.json")

    # Test via dict args
    res_dict = get_impact_analysis({"target": "AuthService", "as_dict": True, "graph_path": str(graph_path)})
    assert isinstance(res_dict, dict)
    assert res_dict["downstream_count"] > 0
    assert "token_validator" in [n["id"] for n in res_dict["downstream_nodes"]]

    # Test via graph object and keyword args
    res_text = get_impact_analysis(target_or_args="AuthService", graph=G, terse=True)
    assert res_text.startswith("Impact for AuthService:")

    # Test get_blast_radius alias with json
    res_json = get_blast_radius(target_or_args="AuthService", graph=G, as_json=True)
    loaded = json.loads(res_json)
    assert loaded["target"] == "AuthService"


def test_mcp_serve_tools(tmp_path):
    pytest.importorskip("mcp")
    from graph_fy.serve import _build_server

    G = _build_test_graph()
    graph_path = _save_graph_file(G, tmp_path / "graph.json")

    server = _build_server(str(graph_path))
    assert server is not None


def test_supernode_damping_prunes_utility_hub():
    """Verify Issue #54: Supernodes with high in-degree are damped during reverse BFS."""
    G = nx.DiGraph()
    # Target node
    G.add_node("business_fn", label="process_payment()", source_file="src/payment.py", file_type="code")
    # Utility supernode: logger
    G.add_node("logger", label="log_event()", source_file="src/logger.py", file_type="code")
    G.add_edge("business_fn", "logger", relation="calls")

    # Connect 120 generic callers to logger
    for i in range(120):
        cid = f"caller_{i}"
        G.add_node(cid, label=f"caller_{i}()", source_file=f"src/caller_{i}.py", file_type="code")
        G.add_edge(cid, "logger", relation="references")

    # Connect 3 tests to logger
    for j in range(3):
        tid = f"test_{j}"
        G.add_node(tid, label=f"test_{j}()", source_file=f"tests/test_{j}.py", file_type="code")
        G.add_edge(tid, "logger", relation="calls")

    # Case 1: Damping enabled (default) with threshold 100 and max fanout 20
    damped_res = compute_impact(
        G,
        "logger",  # note logger as target won't be damped because it's a seed target
        damp_supernodes=True,
        supernode_threshold=100,
    )
    # When logger is the seed target, it is not damped
    assert "logger" not in damped_res["damped_supernodes"]

    # Now make target 'business_fn'. If business_fn is target, reverse BFS looks for callers of business_fn.
    # To test intermediate damping: business_fn -> caller_A -> logger -> 120 callers.
    # Reverse BFS from business_fn looks for callers of business_fn:
    # Say client -> business_fn -> (business_fn doesn't lead to logger in reverse BFS, callers of business_fn do).
    # In reverse BFS: if A calls B, B changes -> A is impacted.
    # Suppose 'db_layer' is changed. 'logger' calls 'db_layer'. 120 callers call 'logger'.
    G.add_node("db_pool", label="DatabasePool", source_file="src/db.py", file_type="code")
    # logger calls db_pool
    G.add_edge("logger", "db_pool", relation="calls")

    # When db_pool changes, logger is impacted (caller of db_pool).
    # Then logger's incoming edges (120 callers + 3 tests) are explored.
    impact = compute_impact(
        G,
        "DatabasePool",
        max_depth=5,
        damp_supernodes=True,
        supernode_threshold=100,
        max_supernode_fanout=20,
    )
    assert "logger" in impact["damped_supernodes"]
    assert impact["supernodes_pruned_count"] == 1
    # Downstream count should be pruned to 1 (logger) + 3 (tests) + remaining up to fanout <= 25
    assert impact["downstream_count"] <= 25
    assert len(impact["affected_tests"]) == 3  # Tests are prioritized!

    # Case 2: Damping disabled
    undamped = compute_impact(
        G,
        "DatabasePool",
        max_depth=5,
        damp_supernodes=False,
    )
    assert undamped["supernodes_pruned_count"] == 0
    assert undamped["downstream_count"] > 100

    # Format report check
    report = format_impact_report(impact)
    assert "Supernodes Damped: 1 utility hub(s) pruned" in report

