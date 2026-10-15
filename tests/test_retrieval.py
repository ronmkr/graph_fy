"""Tests for fully local SQLite FTS5 graph-augmented retrieval layer."""
import networkx as nx
from pathlib import Path
from graph_fy.retrieval import build_index, query, _clean_fts_query


def test_clean_fts_query():
    # Multi-term query adds NEAR() proximity
    cleaned = _clean_fts_query("hello world")
    assert '"hello"' in cleaned and '"world"' in cleaned
    assert "NEAR(hello world, 10)" in cleaned

    # Punctuation handling
    cleaned_spec = _clean_fts_query("special! characters? --here")
    assert '"special"' in cleaned_spec and '"characters"' in cleaned_spec and '"here"' in cleaned_spec

    # Empty query
    assert _clean_fts_query("") == ""

    # Quoted phrase preserves exact phrase
    assert _clean_fts_query('"decoupled architecture"') == '"decoupled architecture"'

    # Synonym expansion
    synonyms = {"k8s": "kubernetes"}
    cleaned_syn = _clean_fts_query("deploy k8s", synonyms=synonyms)
    assert '"kubernetes"' in cleaned_syn


def test_build_and_query_retrieval(tmp_path: Path):
    G = nx.Graph()
    # Node 1: Architecture guide
    G.add_node("guide.md::arch", label="Architecture Overview", file_type="document", node_kind="heading",
               source_file="guide.md", source_location="L1",
               text="This system uses a decoupled microservices architecture with Kafka event bus.")
    # Node 2: Database setup
    G.add_node("guide.md::db", label="Database Setup", file_type="document", node_kind="heading",
               source_file="guide.md", source_location="L10",
               text="PostgreSQL database configuration and schema migrations.")
    # Node 3: Code symbol
    G.add_node("service.py::AuthService", label="AuthService", file_type="code", node_kind="class",
               source_file="service.py", source_location="L5",
               text="Handles user authentication and token verification.")

    # Edges
    G.add_edge("guide.md::arch", "guide.md::db", relation="contains", weight=1.0)
    G.add_edge("guide.md::arch", "service.py::AuthService", relation="references", weight=1.0)

    db_path = tmp_path / "index.db"
    index_file = build_index(G, db_path)
    assert index_file.exists()

    # Query for "microservices" -> should match Node 1 directly and expand to Node 2 and Node 3
    results = query("microservices", graph=G, db_path=db_path, limit=5, hops=1)
    assert len(results) > 0
    top = results[0]
    assert top["node_id"] == "guide.md::arch"
    assert "microservices" in top["text"].lower()
    assert top["connected_via"] == []

    # Check that connected neighbors were pulled in via 1 hop
    node_ids = [r["node_id"] for r in results]
    assert "guide.md::db" in node_ids or "service.py::AuthService" in node_ids
    for r in results:
        if r["node_id"] != "guide.md::arch":
            assert len(r["connected_via"]) == 1
            assert r["connected_via"][0]["from"] == "guide.md::arch"


def test_weighted_columns_and_phrase_query(tmp_path: Path):
    G = nx.Graph()
    # Doc 1: "Authentication" is in title/label
    G.add_node("auth_doc", label="Authentication Service", file_type="document", node_kind="doc",
               source_file="auth.md", source_location="L1",
               text="Overview of security mechanisms.")
    # Doc 2: "Authentication" is only in body
    G.add_node("misc_doc", label="Release Notes", file_type="document", node_kind="doc",
               source_file="notes.md", source_location="L1",
               text="Version 2.0 adds support for multi-tenant authentication.")

    db_path = tmp_path / "weighted_index.db"
    build_index(G, db_path)

    # Query for authentication: title match should rank higher than body-only match
    results = query("Authentication", graph=G, db_path=db_path, limit=5, hops=0)
    assert len(results) == 2
    assert results[0]["node_id"] == "auth_doc"
    assert results[0]["score"] > results[1]["score"]

    # Phrase query with quotes
    phrase_results = query('"multi-tenant authentication"', graph=G, db_path=db_path, limit=5, hops=0)
    assert len(phrase_results) == 1
    assert phrase_results[0]["node_id"] == "misc_doc"


def test_clean_fts_query_reserved_operators(tmp_path: Path):
    """Verify that reserved FTS5 operators (AND, OR, NOT) inside NEAR() do not cause syntax errors."""
    q = _clean_fts_query("what does NOT work in system")
    assert 'NEAR(what does "NOT" work in system, 10)' in q

    # Ensure executing against FTS5 does not raise sqlite3.OperationalError
    G = nx.Graph()
    G.add_node("n1", label="system", file_type="code", text="how the system operates")
    db = tmp_path / "fts_test.db"
    build_index(G, db)

    # Should execute cleanly without raising sqlite3.OperationalError: fts5: syntax error near "NOT"
    res = query("what does NOT work in system", graph=G, db_path=db, limit=5)
    assert isinstance(res, list)
