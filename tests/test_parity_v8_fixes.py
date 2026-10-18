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


def test_js_class_member_prefixes_preserve_distinctions(tmp_path):
    """Ensure JS/TS class members with prefixes (_, $, #) have distinct IDs and edges (#34, #4491)."""
    from graph_fy.extract import extract

    code = (
        "class Service {\n"
        "  foo() { return 1; }\n"
        "  _foo() { return 2; }\n"
        "  $foo() { return 3; }\n"
        "  #foo() { return 4; }\n"
        "}\n"
    )
    p = tmp_path / "service.js"
    p.write_text(code, encoding="utf-8")

    res = extract([p], cache_root=tmp_path)
    nodes = {n["id"]: n for n in res["nodes"]}
    method_labels = {n.get("label") for n in res["nodes"] if n.get("_callable") and not n.get("_callable_class")}

    # All 4 methods must survive with distinct labels
    assert ".foo()" in method_labels
    assert "._foo()" in method_labels
    assert ".$foo()" in method_labels
    assert ".#foo()" in method_labels

    # Public method receives unsalted id; prefixed members receive salted ids
    foo_nodes = [n for n in res["nodes"] if n.get("label") in (".foo()", "._foo()", ".$foo()", ".#foo()")]
    assert len(foo_nodes) == 4
    foo_ids = {n["id"] for n in foo_nodes}
    assert len(foo_ids) == 4
    assert any(nid.endswith("_service_foo") for nid in foo_ids)

    # All 4 methods have 'method' edges from Service
    edges = [
        e for e in res["edges"]
        if e.get("relation") == "method" and e.get("target") in foo_ids
    ]
    assert len(edges) == 4


def test_workspace_exports_fallback_to_src_when_unbuilt(tmp_path):
    """Ensure workspace package exports fall back to src/ on unbuilt checkouts (#36, #4489)."""
    import json
    from graph_fy.resolvers.javascript import _resolve_workspace_import

    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n", encoding="utf-8")
    pkg = tmp_path / "packages" / "ui"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(
        json.dumps({
            "name": "@my/ui",
            "exports": {
                ".": "./dist/index.js",
                "./button": "./dist/button.js",
            },
        }),
        encoding="utf-8",
    )
    src = pkg / "src"
    src.mkdir()
    (src / "index.ts").write_text("export const ui = 1;\n", encoding="utf-8")
    (src / "button.tsx").write_text("export const Button = 2;\n", encoding="utf-8")

    app = tmp_path / "packages" / "app"
    app.mkdir(parents=True)

    # Unbuilt: falls back to src/
    res_root = _resolve_workspace_import("@my/ui", app)
    assert res_root is not None
    assert res_root.resolve() == (src / "index.ts").resolve()

    res_sub = _resolve_workspace_import("@my/ui/button", app)
    assert res_sub is not None
    assert res_sub.resolve() == (src / "button.tsx").resolve()

    # Built: dist/index.js exists and is preferred
    dist = pkg / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("export const ui = 1;\n", encoding="utf-8")
    res_built = _resolve_workspace_import("@my/ui", app)
    assert res_built is not None
    assert res_built.resolve() == (dist / "index.js").resolve()


def test_build_from_json_and_to_json_retain_built_at_commit(tmp_path):
    """Ensure built_at_commit provenance is preserved through build_from_json and to_json (#37, #4490)."""
    import json
    from graph_fy.build import build_from_json
    from graph_fy.export import to_json

    data = {
        "nodes": [{"id": "n1", "label": "Node1", "file_type": "code", "source_file": "a.py"}],
        "edges": [],
        "built_at_commit": "abcdef1234567890abcdef1234567890abcdef12",
    }
    G = build_from_json(data)
    assert G.graph.get("built_at_commit") == "abcdef1234567890abcdef1234567890abcdef12"

    out_path = tmp_path / "graph.json"
    to_json(G, {}, str(out_path), force=True)
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert saved.get("built_at_commit") == "abcdef1234567890abcdef1234567890abcdef12"


def test_format_graph_stats_and_cmd_stats(tmp_path, monkeypatch, capsys):
    """Ensure format_graph_stats and cmd_stats report build commit and staleness (#37, #4490)."""
    import json
    import networkx as nx
    from graph_fy.commands.analysis import cmd_stats, format_graph_stats

    G = nx.DiGraph()
    G.add_node("n1", label="Node1", community=0)
    G.add_node("n2", label="Node2", community=0)
    G.add_edge("n1", "n2", relation="calls", confidence="EXTRACTED")
    G.graph["built_at_commit"] = "1111222233334444"

    # Mock _git_head to return current HEAD matching built_at_commit
    monkeypatch.setattr("graph_fy.export._git_head", lambda _: "1111222233334444")
    stats_head = format_graph_stats(G, {0: ["n1", "n2"]}, tmp_path / "graph_fy_out" / "graph.json")
    assert "Built at commit: 11112222 (current HEAD)" in stats_head

    # Mock _git_head to return a different HEAD (stale)
    monkeypatch.setattr("graph_fy.export._git_head", lambda _: "9999888877776666")
    stats_stale = format_graph_stats(G, {0: ["n1", "n2"]}, tmp_path / "graph_fy_out" / "graph.json")
    assert "Built at commit: 11112222 (stale - current HEAD is 99998888)" in stats_stale

    # Write graph.json and test cmd_stats text and JSON output
    out_dir = tmp_path / "graph_fy_out"
    out_dir.mkdir()
    graph_file = out_dir / "graph.json"
    graph_file.write_text(
        json.dumps({
            "nodes": [
                {"id": "n1", "label": "Node1", "community": 0, "file_type": "code", "source_file": "a.py"},
                {"id": "n2", "label": "Node2", "community": 0, "file_type": "code", "source_file": "b.py"},
            ],
            "edges": [{"source": "n1", "target": "n2", "relation": "calls", "confidence": "EXTRACTED"}],
            "built_at_commit": "1111222233334444",
        }),
        encoding="utf-8",
    )

    # Test cmd_stats text
    rc = cmd_stats(["graph_fy", "stats", "--graph", str(graph_file)], lambda: str(graph_file), lambda _: graph_file)
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Nodes: 2" in captured
    assert "Edges: 1" in captured
    assert "Built at commit: 11112222" in captured

    # Test cmd_stats --json
    rc_json = cmd_stats(["graph_fy", "stats", "--graph", str(graph_file), "--json"], lambda: str(graph_file), lambda _: graph_file)
    assert rc_json == 0
    captured_json = json.loads(capsys.readouterr().out)
    assert captured_json["nodes"] == 2
    assert captured_json["edges"] == 1
    assert captured_json["built_at_commit"] == "1111222233334444"
    assert captured_json["is_stale"] is True


def test_ts_method_level_receiver_typing_and_class_scoping(tmp_path):
    """Ensure TS this.field receiver typing works across methods, static blocks,
    and accessors, with class-scoped inheritance preventing field collisions (#40, #4473, #4294)."""
    from graph_fy.extract import extract

    svc_code = (
        "export class SvcA {\n"
        "  doA(): number { return 1; }\n"
        "}\n"
        "export class SvcB {\n"
        "  doB(): number { return 2; }\n"
        "}\n"
    )
    (tmp_path / "svc.ts").write_text(svc_code, encoding="utf-8")

    app_code = (
        "import { SvcA, SvcB } from './svc';\n"
        "class BaseClass {\n"
        "  baseA: SvcA;\n"
        "}\n"
        "class ClassOne extends BaseClass {\n"
        "  // field assigned in regular method\n"
        "  init() {\n"
        "    this.fieldB = new SvcB();\n"
        "  }\n"
        "  run() {\n"
        "    this.baseA.doA();\n"
        "    this.fieldB.doB();\n"
        "  }\n"
        "}\n"
        "class ClassTwo {\n"
        "  // same field name fieldB, but typed SvcA in ClassTwo\n"
        "  fieldB: SvcA;\n"
        "  run() {\n"
        "    this.fieldB.doA();\n"
        "  }\n"
        "}\n"
    )
    (tmp_path / "app.ts").write_text(app_code, encoding="utf-8")

    r = extract([tmp_path / "svc.ts", tmp_path / "app.ts"], cache_root=tmp_path / "graph_fy_out", parallel=False)
    lbl = {n["id"]: n["label"] for n in r["nodes"]}
    call_pairs = {
        (lbl.get(e["source"], ""), lbl.get(e["target"], ""))
        for e in r["edges"]
        if e.get("relation") == "calls"
    }

    # ClassOne.run calls BaseClass inherited baseA.doA and method-assigned fieldB.doB
    assert any("run" in s and "doA" in t for s, t in call_pairs), f"expected baseA.doA() call edge: {call_pairs}"
    assert any("run" in s and "doB" in t for s, t in call_pairs), f"expected fieldB.doB() call edge: {call_pairs}"

    # ClassTwo.run calls ClassTwo.fieldB.doA (not SvcB.doB)
    c2_run_edges = [
        e for e in r["edges"]
        if e.get("relation") == "calls" and "classtwo" in e.get("source", "").lower()
    ]
    assert c2_run_edges, "ClassTwo.run should have call edges"
    assert all("doa" in lbl.get(e["target"], "").lower() for e in c2_run_edges)


def test_drop_dangling_barrel_named_import_edge(tmp_path):
    """Ensure a dangling provisional barrel-named import edge is dropped once the import
    resolves through the barrel to its real definition (#41, #4486)."""
    from graph_fy.extract import extract

    target = tmp_path / "src" / "lib" / "foo.ts"
    target.parent.mkdir(parents=True)
    target.write_text("export class Foo { id = '' }\n", encoding="utf-8")

    barrel = tmp_path / "src" / "lib" / "index.ts"
    barrel.write_text("export * from './foo'\n", encoding="utf-8")

    consumer = tmp_path / "src" / "routes" / "page.ts"
    consumer.parent.mkdir(parents=True)
    consumer.write_text("import type { Foo } from '../lib/index'\nexport type X = Foo\n", encoding="utf-8")

    r = extract([target, barrel, consumer], cache_root=tmp_path / "graph_fy_out", parallel=False)
    page_imports = [
        e for e in r["edges"]
        if "page" in e.get("source", "") and e.get("relation") == "imports"
    ]

    # Exactly 1 imports edge surviving to Foo's real definition (not the dangling barrel target)
    assert len(page_imports) == 1
    assert page_imports[0]["target"].endswith("foo_foo")


def test_python_module_level_class_and_def_collision(tmp_path):
    """Ensure colliding same-named module-level class and function declarations both survive with deterministic IDs (#42, #2810, #4467)."""
    from graph_fy.extract import extract_python

    # Test class first, then def
    p1 = tmp_path / "mod1.py"
    p1.write_text("class User:\n    pass\n\ndef User():\n    pass\n", encoding="utf-8")
    r1 = extract_python(p1)
    nodes1 = [n for n in r1["nodes"] if n.get("file_type") != "rationale"]
    labels1 = {n["label"]: n["id"] for n in nodes1}
    assert "User" in labels1, "class User should exist"
    assert "User()" in labels1, "function User() should exist"
    assert labels1["User"] != labels1["User()"], "class and function should have distinct IDs"

    # Test def first, then class (order-independent canonical ID)
    p2 = tmp_path / "mod2.py"
    p2.write_text("def User():\n    pass\n\nclass User:\n    pass\n", encoding="utf-8")
    r2 = extract_python(p2)
    nodes2 = [n for n in r2["nodes"] if n.get("file_type") != "rationale"]
    labels2 = {n["label"]: n["id"] for n in nodes2}
    assert "User" in labels2, "class User should exist"
    assert "User()" in labels2, "function User() should exist"
    assert labels2["User"] != labels2["User()"], "class and function should have distinct IDs"

    # In both files, class User must have the unsalted plain ID ending in _user
    assert labels1["User"].endswith("_user")
    assert labels2["User"].endswith("_user")
    # Function User() must have the deterministic salted ID
    assert labels1["User()"].endswith("_user_8e7486")
    assert labels2["User()"].endswith("_user_8e7486")


def test_go_and_rust_case_collisions(tmp_path):
    """Ensure Go and Rust declarations differing only by case survive as separate nodes (#43, #4468)."""
    from graph_fy.extractors.go import extract_go
    from graph_fy.extractors.rust import extract_rust

    # 1. Test Go: type Token struct vs func token()
    go_file = tmp_path / "pkg" / "main.go"
    go_file.parent.mkdir(parents=True)
    go_file.write_text(
        "package pkg\n"
        "type Token struct {}\n"
        "func token() Token { return Token{} }\n",
        encoding="utf-8",
    )
    res_go = extract_go(go_file)
    go_nodes = {n["label"]: n["id"] for n in res_go["nodes"] if n.get("file_type") != "rationale"}
    assert "Token" in go_nodes, "Go Token struct should exist"
    assert "token()" in go_nodes, "Go token() function should exist"
    assert go_nodes["Token"] != go_nodes["token()"], "Go Token and token() should have distinct IDs"
    # Forward reference from token() return type to Token struct
    go_refs = [(e["source"], e["target"]) for e in res_go["edges"] if e.get("relation") == "references"]
    assert (go_nodes["token()"], go_nodes["Token"]) in go_refs

    # 2. Test Rust: struct Token vs fn token()
    rs_file = tmp_path / "src" / "lib.rs"
    rs_file.parent.mkdir(parents=True)
    rs_file.write_text(
        "pub struct Token {}\n"
        "pub fn token() -> Token { Token {} }\n",
        encoding="utf-8",
    )
    res_rs = extract_rust(rs_file)
    rs_nodes = {n["label"]: n["id"] for n in res_rs["nodes"] if n.get("file_type") != "rationale"}
    assert "Token" in rs_nodes, "Rust Token struct should exist"
    assert "token()" in rs_nodes, "Rust token() function should exist"
    assert rs_nodes["Token"] != rs_nodes["token()"], "Rust Token and token() should have distinct IDs"
    # Forward reference from token() return type to Token struct
    rs_refs = [(e["source"], e["target"]) for e in res_rs["edges"] if e.get("relation") == "references"]
    assert (rs_nodes["token()"], rs_nodes["Token"]) in rs_refs

def test_parallel_edges_preservation_round_trip_and_mcp(tmp_path):
    """Ensure parallel edges with distinct relations between the same node pair are preserved
    across graph.json serialization, deserialization, and MCP subgraph queries (#44, #4492)."""
    import json
    import networkx as nx
    from graph_fy.build import build_from_json
    from graph_fy.export import to_json
    from graph_fy.serve_traversal import _subgraph_to_text

    raw_data = {
        "nodes": [
            {"id": "mod_a", "label": "ModuleA", "file_type": "code", "source_file": "a.py"},
            {"id": "mod_b", "label": "ModuleB", "file_type": "code", "source_file": "b.py"},
        ],
        "edges": [
            {"source": "mod_a", "target": "mod_b", "relation": "imports", "confidence": "EXTRACTED", "source_file": "a.py"},
            {"source": "mod_a", "target": "mod_b", "relation": "calls", "confidence": "INFERRED", "source_file": "a.py"},
        ],
    }

    # Deserialization into nx.DiGraph / MultiDiGraph
    G = build_from_json(raw_data)
    assert G.has_edge("mod_a", "mod_b")

    # Verify export to_json preserves both edges
    out_file = tmp_path / "graph.json"
    to_json(G, {}, str(out_file), force=True)
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved.get("multigraph") is True
    edges = saved.get("links", [])
    assert len(edges) == 2
    relations = {e["relation"] for e in edges if e["source"] == "mod_a" and e["target"] == "mod_b"}
    assert relations == {"imports", "calls"}

    # Second round-trip deserialization from exported JSON
    G2 = build_from_json(saved)
    assert G2.has_edge("mod_a", "mod_b")

    # Verify subgraph rendering in MCP query outputs both relations
    text_repr = _subgraph_to_text(G, {"mod_a", "mod_b"}, list(G.edges()))
    assert "imports" in text_repr
    assert "calls" in text_repr


def test_pytest_fixture_parameter_member_call_typing(tmp_path):
    """Ensure member calls on pytest fixture parameters are typed from the fixture's
    returned class across conftest scoping and unwrap Iterator/Generator (#45, #4332, #4333)."""
    from graph_fy.extract import extract

    # 1. Class definition in lib.py
    lib_py = tmp_path / "lib.py"
    lib_py.write_text(
        "class TestClient:\n"
        "    def get(self, url: str) -> None:\n"
        "        pass\n"
        "    def post(self, url: str) -> None:\n"
        "        pass\n"
        "class Database:\n"
        "    def query(self, sql: str) -> None:\n"
        "        pass\n",
        encoding="utf-8",
    )

    # 2. conftest.py defining fixtures
    conftest_py = tmp_path / "conftest.py"
    conftest_py.write_text(
        "import pytest\n"
        "from typing import Iterator\n"
        "from lib import TestClient, Database\n"
        "\n"
        "@pytest.fixture\n"
        "def client() -> Iterator[TestClient]:\n"
        "    c = TestClient()\n"
        "    yield c\n"
        "\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    d = Database()\n"
        "    yield d\n",
        encoding="utf-8",
    )

    # 3. test_app.py using fixtures
    test_py = tmp_path / "tests" / "test_app.py"
    test_py.parent.mkdir(parents=True)
    test_py.write_text(
        "def test_endpoints(client, db):\n"
        "    client.get('/health')\n"
        "    client.post('/submit')\n"
        "    db.query('SELECT 1')\n"
        "\n"
        "def test_rebound(client):\n"
        "    client = None\n"
        "    client.get('/health')\n"
        "\n"
        "def test_annotated(client: int):\n"
        "    client.get('/health')\n",
        encoding="utf-8",
    )

    # 4. Extract
    res = extract([lib_py, conftest_py, test_py], cache_root=tmp_path / "graph_fy_out", parallel=False)
    edges = res["edges"]

    # Verify calls from test_endpoints to TestClient.get, TestClient.post, Database.query
    call_pairs = [(e["source"], e["target"]) for e in edges if e.get("relation") == "calls"]
    nodes_by_label = {n["label"]: n["id"] for n in res["nodes"]}

    test_endpoints_id = nodes_by_label.get("test_endpoints()")
    assert test_endpoints_id is not None, "test_endpoints node should exist"

    get_id = nodes_by_label.get(".get()") or nodes_by_label.get("TestClient.get()") or nodes_by_label.get("get()")
    post_id = nodes_by_label.get(".post()") or nodes_by_label.get("TestClient.post()") or nodes_by_label.get("post()")
    query_id = nodes_by_label.get(".query()") or nodes_by_label.get("Database.query()") or nodes_by_label.get("query()")

    assert (test_endpoints_id, get_id) in call_pairs, "client.get should resolve to TestClient.get"
    assert (test_endpoints_id, post_id) in call_pairs, "client.post should resolve to TestClient.post"
    assert (test_endpoints_id, query_id) in call_pairs, "db.query should resolve to Database.query"

    # Verify rebound and annotated parameter calls are NOT resolved
    test_rebound_id = nodes_by_label.get("test_rebound()")
    test_annotated_id = nodes_by_label.get("test_annotated()")
    assert (test_rebound_id, get_id) not in call_pairs, "rebound fixture param should not resolve"
    assert (test_annotated_id, get_id) not in call_pairs, "annotated param should not resolve"


def test_untyped_nearer_fixture_shadows_typed_fixture(tmp_path):
    """Ensure an untyped fixture in the same test file shadows a typed fixture in conftest.py,
    preventing spurious call resolution (#45, #4332, #4333)."""
    from graph_fy.extract import extract

    # 1. lib.py
    lib_py = tmp_path / "lib.py"
    lib_py.write_text("class Client:\n    def ping(self): pass\n", encoding="utf-8")

    # 2. conftest.py with typed client fixture
    conftest_py = tmp_path / "conftest.py"
    conftest_py.write_text(
        "import pytest\n"
        "from lib import Client\n"
        "@pytest.fixture\n"
        "def client() -> Client:\n"
        "    return Client()\n",
        encoding="utf-8",
    )

    # 3. test_shadow.py with nearer UNTYPED client fixture
    test_py = tmp_path / "tests" / "test_shadow.py"
    test_py.parent.mkdir(parents=True)
    test_py.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def client():\n"
        "    return None\n"
        "\n"
        "def test_ping(client):\n"
        "    client.ping()\n",
        encoding="utf-8",
    )

    res = extract([lib_py, conftest_py, test_py], cache_root=tmp_path / "graph_fy_out", parallel=False)
    call_pairs = [(e["source"], e["target"]) for e in res["edges"] if e.get("relation") == "calls"]
    nodes_by_label = {n["label"]: n["id"] for n in res["nodes"]}

    test_ping_id = nodes_by_label.get("test_ping()")
    ping_id = nodes_by_label.get(".ping()") or nodes_by_label.get("ping()")

    assert (test_ping_id, ping_id) not in call_pairs, "untyped nearer fixture must shadow typed conftest fixture"


