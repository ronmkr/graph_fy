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
