"""Tests for deterministic lint & drift rules (Phase 11 / Option C).

Tests cover:
- Unpinned GitHub Actions detection (SHA vs tag)
- Environment drift detection across staging/prod
- Missing secret and environment variable definitions
- Dangling references and orphan nodes
- Explicit reverse backlink edge generation
"""

from pathlib import Path
from graph_fy.lint import (
    run_lint,
    check_unpinned_actions,
    check_environment_drift,
    check_missing_secrets_and_envs,
    check_dangling_and_orphans,
    check_dead_code,
    check_cyclic_dependencies,
    check_layer_violations,
    check_context_manager_lifetimes,
    check_ast_signature_drift,
    add_explicit_backlink_edges,
)


def test_unpinned_github_actions():
    nodes = [
        {
            "id": "action_checkout",
            "label": "Action:actions/checkout",
            "type": "action",
            "action_name": "actions/checkout",
            "pinned_version": "v4",
            "is_sha_pinned": False,
            "source_file": ".github/workflows/ci.yml",
        },
        {
            "id": "action_python",
            "label": "Action:actions/setup-python",
            "type": "action",
            "action_name": "actions/setup-python",
            "pinned_version": "0a5c61591373683505ea898e09a3ea4f39ef2b9c",
            "is_sha_pinned": True,
            "source_file": ".github/workflows/ci.yml",
        },
    ]
    edges = []

    findings = check_unpinned_actions(nodes, edges)
    assert len(findings) == 1
    assert findings[0]["rule"] == "unpinned_github_action"
    assert findings[0]["action"] == "actions/checkout"
    assert findings[0]["pin"] == "v4"


def test_environment_drift():
    nodes = [
        # Staging resources
        {
            "id": "tf_staging_db",
            "label": "AWS:aws_db_instance.staging_db",
            "resource_type": "aws_db_instance",
            "resource_name": "staging_db",
            "source_file": "environments/staging/main.tf",
        },
        {
            "id": "tf_staging_redis",
            "label": "AWS:aws_elasticache_cluster.staging_redis",
            "resource_type": "aws_elasticache_cluster",
            "resource_name": "staging_redis",
            "source_file": "environments/staging/main.tf",
        },
        # Prod resources: has redis, but db is missing!
        {
            "id": "tf_prod_redis",
            "label": "AWS:aws_elasticache_cluster.prod_redis",
            "resource_type": "aws_elasticache_cluster",
            "resource_name": "prod_redis",
            "source_file": "environments/prod/main.tf",
        },
    ]

    findings = check_environment_drift(nodes)
    assert len(findings) >= 1
    rules = [f["rule"] for f in findings]
    assert "environment_drift" in rules

    drift_item = next(f for f in findings if "db" in f["resource"])
    assert drift_item["present_in"] == "staging"
    assert drift_item["missing_in"] == "prod"


def test_missing_secrets_and_envs():
    nodes = [
        # Referenced secret and env
        {
            "id": "sec_auth",
            "label": "Secret:auth_token",
            "secret_name": "auth_token",
            "type": "secret_ref",
        },
        {
            "id": "sec_missing",
            "label": "Secret:stripe_api_key",
            "secret_name": "stripe_api_key",
            "type": "secret_ref",
        },
        {
            "id": "env_missing",
            "label": "Env:MISSING_SERVICE_URL",
            "env_name": "MISSING_SERVICE_URL",
            "type": "env_ref",
        },
        # Defined secret (only auth_token is defined!)
        {
            "id": "k8s_sec_auth",
            "label": "K8s:Secret:auth_token",
            "resource_kind": "secret",
            "resource_name": "auth_token",
            "type": "k8s_resource",
        },
    ]
    edges = [
        {"source": "k8s_sec_auth", "target": "sec_auth", "relation": "defines_secret"},
    ]

    findings = check_missing_secrets_and_envs(nodes, edges)
    rules = {f["rule"] for f in findings}

    assert "missing_secret_definition" in rules
    assert "missing_env_definition" in rules

    missing_sec_names = {f["name"] for f in findings if f["rule"] == "missing_secret_definition"}
    assert "stripe_api_key" in missing_sec_names
    assert "auth_token" not in missing_sec_names


def test_dangling_and_orphans():
    nodes = [
        {"id": "node_a", "label": "Node A"},
        {"id": "node_b", "label": "Node B"},
        {"id": "node_orphan", "label": "Orphan Node"},
    ]
    edges = [
        # Valid edge
        {"source": "node_a", "target": "node_b", "relation": "calls"},
        # Dangling edge (target node_ghost doesn't exist)
        {"source": "node_a", "target": "node_ghost", "relation": "imports"},
    ]

    findings = check_dangling_and_orphans(nodes, edges)
    rules = {f["rule"] for f in findings}

    assert "dangling_reference" in rules
    assert "orphan_node" in rules

    dangling = next(f for f in findings if f["rule"] == "dangling_reference")
    assert dangling["node_id"] == "node_ghost"

    orphan = next(f for f in findings if f["rule"] == "orphan_node")
    assert orphan["node_id"] == "node_orphan"


def test_add_explicit_backlink_edges():
    nodes = [
        {"id": "service_a", "label": "Service A"},
        {"id": "service_b", "label": "Service B"},
    ]
    edges = [
        {"source": "service_a", "target": "service_b", "relation": "depends_on"},
    ]

    augmented_edges = add_explicit_backlink_edges(nodes, edges)

    # Should have forward and reverse backlink edge
    assert len(augmented_edges) == 2
    fwd = augmented_edges[0]
    bwd = augmented_edges[1]

    assert fwd["source"] == "service_a" and fwd["target"] == "service_b"
    assert bwd["source"] == "service_b" and bwd["target"] == "service_a"
    assert bwd["relation"] == "backlink:depends_on"
    assert bwd["is_backlink"] is True


def test_run_lint_end_to_end():
    graph_data = {
        "nodes": [
            {
                "id": "action_unpinned",
                "label": "Action:docker/build-push-action",
                "type": "action",
                "pinned_version": "v3",
                "is_sha_pinned": False,
            },
            {
                "id": "sec_undef",
                "label": "Secret:UNSET_SECRET",
                "type": "secret_ref",
            },
        ],
        "edges": [],
    }

    report = run_lint(graph_data, check_orphans=False)
    assert report["status"] in ("failed", "warning")
    assert report["summary"]["total"] >= 2


def test_check_dead_code_flags_unreferenced_symbols():
    nodes = [
        {
            "id": "fn_active",
            "label": "active_worker",
            "file_type": "code",
            "type": "function",
            "_callable": True,
            "source_file": "src/worker.py",
        },
        {
            "id": "fn_dead",
            "label": "unused_legacy_helper",
            "file_type": "code",
            "type": "function",
            "_callable": True,
            "source_file": "src/worker.py",
        },
        {
            "id": "fn_main",
            "label": "main",
            "file_type": "code",
            "type": "function",
            "_callable": True,
            "source_file": "src/cli.py",
        },
        {
            "id": "fn_test",
            "label": "test_something",
            "file_type": "code",
            "type": "function",
            "_callable": True,
            "source_file": "tests/test_worker.py",
        },
    ]
    edges = [
        {"source": "caller_node", "target": "fn_active", "relation": "calls"},
    ]

    findings = check_dead_code(nodes, edges)
    assert len(findings) == 1
    assert findings[0]["rule"] == "dead_code"
    assert findings[0]["symbol"] == "unused_legacy_helper"
    assert findings[0]["node_id"] == "fn_dead"


def test_check_cyclic_dependencies():
    nodes = [
        {"id": "mod_a", "label": "module_a", "source_file": "pkg/a.py"},
        {"id": "mod_b", "label": "module_b", "source_file": "pkg/b.py"},
        {"id": "mod_c", "label": "module_c", "source_file": "pkg/c.py"},
    ]
    # A -> B -> C -> A (cycle)
    edges = [
        {"source": "mod_a", "target": "mod_b", "relation": "imports"},
        {"source": "mod_b", "target": "mod_c", "relation": "imports"},
        {"source": "mod_c", "target": "mod_a", "relation": "imports_from"},
    ]

    findings = check_cyclic_dependencies(nodes, edges)
    assert len(findings) == 1
    assert findings[0]["rule"] == "cyclic_dependency"
    assert findings[0]["severity"] == "warning"
    assert findings[0]["cycle_length"] == 3
    assert "pkg/a.py" in findings[0]["message"]
    assert "pkg/b.py" in findings[0]["message"]
    assert "pkg/c.py" in findings[0]["message"]


def test_check_layer_violations():
    nodes = [
        {"id": "core_svc", "label": "CoreService", "source_file": "core/service.py"},
        {"id": "cli_main", "label": "cli_main", "source_file": "cli/main.py"},
    ]
    # Forbidden: core importing cli
    edges = [
        {"source": "core_svc", "target": "cli_main", "relation": "imports"},
    ]

    findings = check_layer_violations(nodes, edges)
    assert len(findings) == 1
    assert findings[0]["rule"] == "architectural_layer_violation"
    assert findings[0]["severity"] == "error"
    assert findings[0]["inner_layer"] == "core"
    assert findings[0]["outer_layer"] == "cli"
    assert "core/service.py" in findings[0]["message"]
    assert "cli/main.py" in findings[0]["message"]


def test_run_lint_supports_networkx_links_key():
    graph_data = {
        "nodes": [
            {"id": "a", "label": "a.py", "source_file": "src/a.py"},
            {"id": "b", "label": "b.py", "source_file": "src/b.py"},
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "imports"},
            {"source": "b", "target": "a", "relation": "imports"},
        ],
    }
    # Run with links key instead of edges
    report = run_lint(graph_data, check_orphans=False, check_dead=False)
    findings = [f for f in report["findings"] if f["rule"] == "cyclic_dependency"]
    assert len(findings) == 1
    assert findings[0]["cycle_length"] == 2


def test_run_lint_selective_flags():
    graph_data = {
        "nodes": [
            {"id": "core", "label": "Core", "source_file": "core/c.py"},
            {"id": "cli", "label": "CLI", "source_file": "cli/c.py"},
        ],
        "edges": [
            {"source": "core", "target": "cli", "relation": "imports"},
        ],
    }
    # Only check cycles (should be 0)
    report_cycles = run_lint(
        graph_data,
        check_actions=False,
        check_drift=False,
        check_secrets=False,
        check_orphans=False,
        check_rtk=False,
        check_dead=False,
        check_cycles=True,
        check_layers=False,
    )
    assert len(report_cycles["findings"]) == 0

    # Only check layers (should find 1 violation)
    report_layers = run_lint(
        graph_data,
        check_actions=False,
        check_drift=False,
        check_secrets=False,
        check_orphans=False,
        check_rtk=False,
        check_dead=False,
        check_cycles=False,
        check_layers=True,
    )
    assert len(report_layers["findings"]) == 1
    assert report_layers["findings"][0]["rule"] == "architectural_layer_violation"


def test_cyclic_dependencies_dense_graph():
    """Verify that a densely connected cyclic component executes instantly without exponential explosion."""
    nodes = [{"id": f"m_{i}", "label": f"m_{i}", "source_file": f"pkg/m_{i}.py"} for i in range(12)]
    edges = []
    # Fully connect all 12 modules in both directions
    for i in range(12):
        for j in range(12):
            if i != j:
                edges.append({"source": f"m_{i}", "target": f"m_{j}", "relation": "imports"})

    import time
    t0 = time.time()
    findings = check_cyclic_dependencies(nodes, edges, max_cycles=10)
    elapsed = time.time() - t0

    assert elapsed < 1.0  # Must finish within 1 second, not hang
    assert len(findings) <= 10
    assert all(f["rule"] == "cyclic_dependency" for f in findings)


def test_find_graph_path_for_target_resolution(tmp_path):
    from graph_fy.cli import _find_graph_path_for_target

    # 1. Direct json file
    jf = tmp_path / "custom.json"
    jf.write_text("{}", encoding="utf-8")
    assert _find_graph_path_for_target(jf) == jf

    # 2. Directory with graph_fy_out/graph.json
    sub = tmp_path / "subproject"
    sub_out = sub / "graph_fy_out"
    sub_out.mkdir(parents=True)
    gj = sub_out / "graph.json"
    gj.write_text("{}", encoding="utf-8")
    assert _find_graph_path_for_target(sub) == gj


def test_check_context_manager_lifetimes():
    nodes = [
        {"id": "cls_sync_bad", "label": "LeakySession", "type": "class", "source_file": "db.py"},
        {"id": "cls_sync_good", "label": "SafeSession", "type": "class", "source_file": "db.py"},
        {"id": "cls_async_bad", "label": "AsyncLeakyConn", "type": "class", "source_file": "conn.py"},
        {"id": "cls_async_good", "label": "AsyncSafeConn", "type": "class", "source_file": "conn.py"},
    ]
    edges = [
        # LeakySession: __enter__ only
        {"source": "cls_sync_bad", "target": "m1", "relation": "defines", "target_label": "__enter__"},
        # SafeSession: __enter__ and __exit__
        {"source": "cls_sync_good", "target": "m2", "relation": "defines", "target_label": "__enter__"},
        {"source": "cls_sync_good", "target": "m3", "relation": "defines", "target_label": "__exit__"},
        # AsyncLeakyConn: __aenter__ only
        {"source": "cls_async_bad", "target": "m4", "relation": "defines", "target_label": "__aenter__"},
        # AsyncSafeConn: __aenter__ and __aexit__
        {"source": "cls_async_good", "target": "m5", "relation": "defines", "target_label": "__aenter__"},
        {"source": "cls_async_good", "target": "m6", "relation": "defines", "target_label": "__aexit__"},
    ]

    findings = check_context_manager_lifetimes(nodes, edges)
    rules = [f["rule"] for f in findings]
    assert "missing_context_manager_exit" in rules
    assert "missing_async_context_manager_exit" in rules
    assert len(findings) == 2

    # Test via run_lint
    graph_data = {"nodes": nodes, "edges": edges}
    report = run_lint(graph_data, check_actions=False, check_drift=False, check_secrets=False,
                      check_orphans=False, check_rtk=False, check_dead=False, check_cycles=False,
                      check_layers=False, check_context_managers=True)
    assert report["status"] == "failed"
    assert report["summary"]["errors"] == 2


def test_check_ast_signature_drift():
    base_nodes = [
        {"id": "f_fetch", "label": "fetch(url, timeout=30)", "type": "function", "source_file": "client.py"},
        {"id": "f_legacy", "label": "legacy_handler(request)", "type": "function", "source_file": "handler.py"},
        {"id": "c_server", "label": "Server.start(port=8080)", "type": "method", "source_file": "server.py"},
    ]

    # Current nodes:
    # 1. f_fetch added a required param 'auth_token' without default
    # 2. f_legacy removed completely
    # 3. c_server unchanged
    curr_nodes = [
        {"id": "f_fetch", "label": "fetch(url, auth_token, timeout=30)", "type": "function", "source_file": "client.py"},
        {"id": "c_server", "label": "Server.start(port=8080)", "type": "method", "source_file": "server.py"},
    ]

    findings = check_ast_signature_drift(curr_nodes, base_nodes)
    rules = [f["rule"] for f in findings]
    assert "breaking_api_removal" in rules
    assert "breaking_signature_change" in rules

    removal = next(f for f in findings if f["rule"] == "breaking_api_removal")
    assert removal["symbol"] == "legacy_handler"

    sig_change = next(f for f in findings if f["rule"] == "breaking_signature_change")
    assert sig_change["symbol"] == "fetch"

    # Test integrated into run_lint
    report = run_lint({"nodes": curr_nodes, "edges": []},
                      check_actions=False, check_drift=False, check_secrets=False,
                      check_orphans=False, check_rtk=False, check_dead=False, check_cycles=False,
                      check_layers=False, check_context_managers=False,
                      base_graph={"nodes": base_nodes, "edges": []})
    assert len(report["findings"]) == 2


