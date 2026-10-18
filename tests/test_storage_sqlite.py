import tempfile
from pathlib import Path
import networkx as nx
import pytest

from graph_fy.affected import affected_nodes
from graph_fy.storage_sqlite import SqliteGraphStore


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_graph.db"
    store = SqliteGraphStore(db_file)
    yield store
    store.close()


def test_sqlite_store_initialization(temp_db):
    cur = temp_db.conn.cursor()
    cur.execute("PRAGMA journal_mode;")
    mode = cur.fetchone()[0]
    assert mode.lower() == "wal"

    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {r[0] for r in cur.fetchall()}
    assert "nodes" in tables
    assert "edges" in tables
    assert "hyperedges" in tables
    assert "hyperedge_members" in tables
    assert "graph_metadata" in tables


def test_sqlite_store_save_and_query_nodes(temp_db):
    G = nx.DiGraph()
    G.add_node("auth_py", label="auth.py", file_type="file", source_file="auth.py", source_location="L1-L100", community_id=1, pagerank=0.05)
    G.add_node("login_fn", label="login()", file_type="function", source_file="auth.py", source_location="L10-L25", community_id=1, pagerank=0.08, docstring="Authenticate user")
    G.add_edge("auth_py", "login_fn", relation="contains")

    temp_db.save_graph(G, commit="abcdef123")

    node = temp_db.get_node("login_fn")
    assert node is not None
    assert node["id"] == "login_fn"
    assert node["label"] == "login()"
    assert node["source_file"] == "auth.py"
    assert node["start_line"] == 10
    assert node["end_line"] == 25
    assert node["community_id"] == 1
    assert node["docstring"] == "Authenticate user"

    nodes = temp_db.get_nodes(["auth_py", "login_fn", "nonexistent"])
    assert len(nodes) == 2
    ids = {n["id"] for n in nodes}
    assert ids == {"auth_py", "login_fn"}


def test_sqlite_store_edges_and_callers_callees(temp_db):
    G = nx.DiGraph()
    G.add_node("main_fn", label="main()", file_type="function")
    G.add_node("service_fn", label="process()", file_type="function")
    G.add_node("helper_fn", label="helper()", file_type="function")

    G.add_edge("main_fn", "service_fn", relation="calls", confidence="EXTRACTED", weight=1.0, call_site_line="L5")
    G.add_edge("service_fn", "helper_fn", relation="calls", confidence="INFERRED", weight=0.85, call_site_line="L12")

    temp_db.save_graph(G)

    # get_edges
    out_edges = temp_db.get_edges(source_id="main_fn")
    assert len(out_edges) == 1
    assert out_edges[0]["target_id"] == "service_fn"
    assert out_edges[0]["relation"] == "calls"
    assert out_edges[0]["call_site_line"] == 5

    # callers and callees
    callers, callees = temp_db.get_callers_and_callees("service_fn")
    assert callers == ["main_fn"]
    assert callees == ["helper_fn"]


def test_sqlite_store_reverse_bfs_matches_affected_nodes(temp_db):
    """Verify recursive CTE reverse_bfs produces identical blast-radius hits as in-memory affected_nodes."""
    G = nx.DiGraph()
    # Class with method
    G.add_node("user_class", label="User", file_type="class", source_file="models.py", source_location="L1-L50")
    G.add_node("user_save", label="save()", file_type="function", source_file="models.py", source_location="L10-L20")
    G.add_edge("user_class", "user_save", relation="method")

    # Callers to method
    G.add_node("controller_fn", label="create_user()", file_type="function", source_file="controller.py", source_location="L30-L45")
    G.add_edge("controller_fn", "user_save", relation="calls", call_site_line="L35")

    # Caller to controller
    G.add_node("router_fn", label="post_route()", file_type="function", source_file="router.py", source_location="L50-L60")
    G.add_edge("router_fn", "controller_fn", relation="calls", call_site_line="L55")

    temp_db.save_graph(G)

    # 1. In-memory affected_nodes from user_class
    in_mem_hits = affected_nodes(G, "user_class", relations=("calls", "method"), depth=2)
    in_mem_ids = {h.node_id: h.depth for h in in_mem_hits}
    assert in_mem_ids == {"controller_fn": 1, "router_fn": 2}

    # 2. SQLite recursive CTE reverse_bfs from user_class
    sql_hits = temp_db.reverse_bfs("user_class", relations=("calls",), max_depth=2)
    sql_ids = {h["node_id"]: h["depth"] for h in sql_hits}
    assert sql_ids == {"controller_fn": 1, "router_fn": 2}

    # Location check
    c_hit = [h for h in sql_hits if h["node_id"] == "controller_fn"][0]
    assert c_hit["via_file"] == "controller.py"
    assert c_hit["via_location"] == "L35"


def test_sqlite_store_k_hop_ego_graph(temp_db):
    G = nx.DiGraph()
    G.add_node("n1", label="1")
    G.add_node("n2", label="2")
    G.add_node("n3", label="3")
    G.add_node("n4", label="4")

    G.add_edge("n1", "n2", relation="calls")
    G.add_edge("n2", "n3", relation="calls")
    G.add_edge("n3", "n4", relation="calls")

    temp_db.save_graph(G)

    nodes, edges = temp_db.k_hop_ego_graph("n1", max_depth=1, directed=True)
    assert len(nodes) == 2
    assert {n["id"] for n in nodes} == {"n1", "n2"}
    assert len(edges) == 1
    assert edges[0]["source_id"] == "n1" and edges[0]["target_id"] == "n2"

    nodes_2, edges_2 = temp_db.k_hop_ego_graph("n1", max_depth=2, directed=True)
    assert len(nodes_2) == 3
    assert {n["id"] for n in nodes_2} == {"n1", "n2", "n3"}
    assert len(edges_2) == 2


def test_sqlite_store_round_trip_dict_and_networkx(temp_db):
    G = nx.DiGraph()
    G.graph["built_at_commit"] = "commit_12345"
    G.graph["learning"] = {"patterns": ["singleton"]}
    G.add_node("A", label="A", file_type="class", source_file="a.py", source_location="L10-L20")
    G.add_node("B", label="B", file_type="class", source_file="b.py", source_location="L30")
    G.add_edge("A", "B", relation="inherits", confidence="EXTRACTED", weight=1.0)
    G.graph["hyperedges"] = [{"id": "h1", "relation": "cluster", "nodes": ["A", "B"]}]

    temp_db.save_graph(G)

    # 1. to_dict()
    data = temp_db.to_dict()
    assert data["built_at_commit"] == "commit_12345"
    assert data["learning"] == {"patterns": ["singleton"]}
    assert len(data["nodes"]) == 2
    assert len(data["links"]) == 1
    assert len(data["hyperedges"]) == 1
    assert data["hyperedges"][0]["nodes"] == ["A", "B"]

    # 2. to_networkx()
    G2 = temp_db.to_networkx(directed=True)
    assert G2.graph["built_at_commit"] == "commit_12345"
    assert G2.has_edge("A", "B")
    assert G2.edges["A", "B"]["relation"] == "inherits"
    assert "hyperedges" in G2.graph


def test_sqlite_store_stats(temp_db):
    G = nx.DiGraph()
    G.add_node("a", source_file="foo.py")
    G.add_node("b", source_file="foo.py")
    G.add_node("c", source_file="bar.py")
    G.add_edge("a", "b", relation="calls")
    G.add_edge("b", "c", relation="calls")

    temp_db.save_graph(G, commit="git_head_999")

    stats = temp_db.stats()
    assert stats["node_count"] == 3
    assert stats["edge_count"] == 2
    assert stats["file_count"] == 2
    assert stats["built_at_commit"] == "git_head_999"


def test_sqlite_store_scale_and_speed(temp_db):
    """Test performance on 1,000 nodes and 2,000 edges."""
    import time

    G = nx.DiGraph()
    for i in range(1000):
        G.add_node(f"node_{i}", label=f"func_{i}()", source_file=f"file_{i//10}.py", source_location=f"L{i%100}")
        if i > 0:
            G.add_edge(f"node_{i-1}", f"node_{i}", relation="calls", call_site_line=f"L{i}")
            if i % 5 == 0:
                G.add_edge(f"node_0", f"node_{i}", relation="calls", call_site_line="L1")

    # Ingestion benchmark
    t0 = time.perf_counter()
    temp_db.save_graph(G)
    save_time = time.perf_counter() - t0
    assert save_time < 0.5  # <500ms for 1000 nodes and 1200 edges

    # Recursive CTE reverse-BFS benchmark
    t0 = time.perf_counter()
    hits = temp_db.reverse_bfs("node_50", relations=("calls",), max_depth=3)
    bfs_time = time.perf_counter() - t0
    assert bfs_time < 0.05  # <50ms for recursive CTE
    assert len(hits) > 0


def test_export_to_json_mirrors_sqlite(tmp_path):
    from graph_fy.export import to_json
    from graph_fy.storage_sqlite import SqliteGraphStore

    G = nx.DiGraph()
    G.add_node("svc_a", label="ServiceA", file_type="class")
    G.add_node("svc_b", label="ServiceB", file_type="class")
    G.add_edge("svc_a", "svc_b", relation="calls")

    out_json = tmp_path / "graph.json"
    to_json(G, {0: ["svc_a", "svc_b"]}, str(out_json), force=True, built_at_commit="commit_abc")

    assert out_json.exists()
    out_db = tmp_path / "graph.db"
    assert out_db.exists()

    with SqliteGraphStore(out_db) as store:
        stats = store.stats()
        assert stats["node_count"] == 2
        assert stats["edge_count"] == 1
        assert stats["built_at_commit"] == "commit_abc"


def test_query_engine_sqlite_integration(tmp_path):
    from graph_fy.export import to_sqlite
    from graph_fy.query_engine import GraphQueryEngine

    G = nx.DiGraph()
    G.add_node("root_fn", label="root()", file_type="function")
    G.add_node("leaf_fn", label="leaf()", file_type="function")
    G.add_edge("root_fn", "leaf_fn", relation="calls", call_site_line="L22")

    out_db = tmp_path / "graph.db"
    to_sqlite(G, out_db, built_at_commit="test_commit")

    # Test query engine SQLite methods
    hits = GraphQueryEngine.find_affected_sqlite("leaf_fn", db_path=out_db, relations=("calls",), depth=1)
    assert len(hits) == 1
    assert hits[0]["node_id"] == "root_fn"
    assert hits[0]["depth"] == 1

    nodes, edges = GraphQueryEngine.k_hop_ego_sqlite("root_fn", db_path=out_db, max_depth=1, directed=True)
    assert len(nodes) == 2
    assert len(edges) == 1
