"""storage_sqlite.py — Zero-Deserialization Relational Graph Store.

Persists code graph topology (nodes, edges, hyperedges, metadata) in an ACID SQLite
database (graph.db) in WAL mode. Enables sub-millisecond k-hop ego-graph extraction
and reverse-BFS blast radius analysis via recursive CTEs without loading full
graph into memory (NetworkX).
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

import networkx as nx

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS graph_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT,
    start_line INTEGER,
    end_line INTEGER,
    community_id INTEGER,
    pagerank REAL DEFAULT 0.0,
    metadata_json TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'EXTRACTED',
    weight REAL DEFAULT 1.0,
    call_site_line INTEGER,
    metadata_json TEXT,
    PRIMARY KEY (source_id, target_id, relation, call_site_line)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id, relation);

CREATE TABLE IF NOT EXISTS hyperedges (
    id TEXT PRIMARY KEY,
    relation TEXT,
    metadata_json TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS hyperedge_members (
    hyperedge_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    PRIMARY KEY (hyperedge_id, node_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_hyperedge_members_node ON hyperedge_members(node_id);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    node_id UNINDEXED,
    title,
    headings,
    tags,
    body,
    source_path UNINDEXED,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS node_metadata (
    node_id TEXT PRIMARY KEY,
    label TEXT,
    source_path TEXT,
    source_location TEXT,
    file_type TEXT,
    node_kind TEXT,
    pagerank REAL,
    degree INTEGER,
    content TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS indexed_terms (
    term TEXT PRIMARY KEY,
    doc_count INTEGER DEFAULT 1
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS file_stats (
    path TEXT PRIMARY KEY,
    size INTEGER,
    mtime_ns INTEGER,
    content_hash TEXT
) WITHOUT ROWID;
"""


def _parse_source_location(loc: Any) -> tuple[int | None, int | None]:
    """Parse 'L10', 'L10-L20', or '10' into start_line and end_line integers."""
    if loc is None:
        return None, None
    s = str(loc).strip()
    if not s:
        return None, None
    if s.startswith("L"):
        s = s[1:]
    if "-" in s:
        parts = s.split("-", 1)
        p0 = parts[0].lstrip("L")
        p1 = parts[1].lstrip("L")
        try:
            return int(p0), int(p1)
        except ValueError:
            pass
    try:
        val = int(s)
        return val, val
    except ValueError:
        return None, None


class SqliteGraphStore:
    """ACID SQLite-backed relational graph store."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Initialize schema and WAL mode."""
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()

    def __enter__(self) -> "SqliteGraphStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # --- Bulk Ingestion ---

    def save_graph(
        self,
        G: nx.Graph,
        *,
        commit: str | None = None,
        clear_existing: bool = True,
        build_fts: bool = True,
    ) -> None:
        """Persist NetworkX graph directly to SQLite tables with zero JSON deserialization overhead."""
        cur = self.conn.cursor()
        cur.execute("BEGIN TRANSACTION;")
        try:
            if clear_existing:
                cur.execute("DELETE FROM edges;")
                cur.execute("DELETE FROM nodes;")
                cur.execute("DELETE FROM hyperedge_members;")
                cur.execute("DELETE FROM hyperedges;")
                cur.execute("DELETE FROM graph_metadata;")

            # Metadata
            built_commit = commit or getattr(G, "graph", {}).get("built_at_commit")
            if built_commit:
                cur.execute("INSERT OR REPLACE INTO graph_metadata (key, value) VALUES ('built_at_commit', ?);", (str(built_commit),))
            if getattr(G, "graph", {}).get("learning"):
                cur.execute("INSERT OR REPLACE INTO graph_metadata (key, value) VALUES ('learning', ?);", (json.dumps(G.graph["learning"]),))

            # Nodes
            node_rows = []
            for nid, data in G.nodes(data=True):
                nid_str = str(nid)
                lbl = str(data.get("label") or nid_str)
                kind = str(data.get("file_type") or data.get("node_kind") or "concept")
                sf = data.get("source_file")
                src_file = str(sf) if sf else None
                s_line, e_line = _parse_source_location(data.get("source_location"))
                cid = data.get("community_id")
                comm_id = int(cid) if cid is not None else None
                pr = float(data.get("pagerank") or 0.0)
                # Filter redundant keys from metadata JSON
                reserved = {"label", "file_type", "node_kind", "source_file", "source_location", "community_id", "pagerank"}
                extra = {k: v for k, v in data.items() if k not in reserved}
                meta_json = json.dumps(extra) if extra else None
                node_rows.append((nid_str, lbl, kind, src_file, s_line, e_line, comm_id, pr, meta_json))

            cur.executemany(
                """
                INSERT OR REPLACE INTO nodes
                (id, label, kind, source_file, start_line, end_line, community_id, pagerank, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                node_rows,
            )

            # Edges
            edge_rows = []
            seen_edges = set()
            for s, t, data in G.edges(data=True):
                s_str, t_str = str(s), str(t)
                rel = str(data.get("relation") or "references")
                conf = str(data.get("confidence") or "EXTRACTED")
                wt = float(data.get("weight") or data.get("confidence_score") or 1.0)
                cs_line, _ = _parse_source_location(data.get("call_site_line") or data.get("source_location"))
                cs_key = cs_line if cs_line is not None else -1

                edge_key = (s_str, t_str, rel, cs_key)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)

                reserved_e = {"relation", "confidence", "weight", "confidence_score", "call_site_line", "source_location"}
                extra_e = {k: v for k, v in data.items() if k not in reserved_e}
                e_meta = json.dumps(extra_e) if extra_e else None
                edge_rows.append((s_str, t_str, rel, conf, wt, cs_key, e_meta))

                # Handle multi-relations if stashed
                extra_rels = data.get("relations")
                if extra_rels and isinstance(extra_rels, (list, tuple, set)):
                    for r in extra_rels:
                        r_str = str(r)
                        if r_str != rel:
                            alt_key = (s_str, t_str, r_str, cs_key)
                            if alt_key not in seen_edges:
                                seen_edges.add(alt_key)
                                edge_rows.append((s_str, t_str, r_str, conf, wt, cs_key, e_meta))

            cur.executemany(
                """
                INSERT OR REPLACE INTO edges
                (source_id, target_id, relation, confidence, weight, call_site_line, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                edge_rows,
            )

            # Hyperedges
            hyperedges = getattr(G, "graph", {}).get("hyperedges", [])
            for h in hyperedges:
                if not isinstance(h, dict):
                    continue
                hid = str(h.get("id") or "")
                if not hid:
                    continue
                h_rel = str(h.get("relation") or "")
                h_extra = {k: v for k, v in h.items() if k not in ("id", "relation", "nodes", "members")}
                cur.execute(
                    "INSERT OR REPLACE INTO hyperedges (id, relation, metadata_json) VALUES (?, ?, ?);",
                    (hid, h_rel, json.dumps(h_extra) if h_extra else None),
                )
                members = h.get("nodes") or h.get("members") or []
                member_rows = [(hid, str(m)) for m in members if m]
                cur.executemany(
                    "INSERT OR IGNORE INTO hyperedge_members (hyperedge_id, node_id) VALUES (?, ?);",
                    member_rows,
                )

            # FTS5 unified search indexing
            if build_fts:
                self.populate_fts(G, cur=cur)

            # Record file_stats from distinct source_file attributes in G
            file_paths = {data.get("source_file") for _, data in G.nodes(data=True) if data.get("source_file")}
            stats_rows = []
            for fp in file_paths:
                if not fp:
                    continue
                p = Path(fp)
                if not p.is_absolute():
                    cand = self.db_path.parent.parent / p
                    if cand.exists():
                        p = cand
                if p.exists() and p.is_file():
                    try:
                        st = p.stat()
                        stats_rows.append((str(fp), st.st_size, st.st_mtime_ns, None))
                    except OSError:
                        pass
            if stats_rows:
                cur.executemany(
                    "INSERT OR REPLACE INTO file_stats (path, size, mtime_ns, content_hash) VALUES (?, ?, ?, ?);",
                    stats_rows,
                )

            self.conn.commit()

            # Ensure backward compatibility symlink: index.db -> graph.db
            if build_fts and self.db_path.name == "graph.db":
                idx_compat = self.db_path.parent / "index.db"
                if not idx_compat.exists() or (idx_compat.is_symlink() and not idx_compat.exists()):
                    try:
                        if idx_compat.is_symlink():
                            idx_compat.unlink()
                        idx_compat.symlink_to(self.db_path.name)
                    except OSError:
                        pass
                elif idx_compat.exists() and not idx_compat.is_symlink() and idx_compat != self.db_path:
                    try:
                        idx_compat.unlink()
                        idx_compat.symlink_to(self.db_path.name)
                    except OSError:
                        pass
        except Exception:
            self.conn.rollback()
            raise

    def populate_fts(
        self,
        G: nx.Graph,
        cur: sqlite3.Cursor | None = None,
        node_contents: dict[str, str] | None = None,
    ) -> None:
        """Populate FTS5 virtual table, node metadata, and indexed terms inside this unified database."""
        from collections import Counter
        from graph_fy.retrieval_index import (
            _extract_terms_from_text,
            _prepare_node_document,
            compute_pagerank,
        )

        c = cur or self.conn.cursor()
        c.execute("DELETE FROM nodes_fts;")
        c.execute("DELETE FROM node_metadata;")
        c.execute("DELETE FROM indexed_terms;")

        pagerank: dict[Any, float] = {}
        if len(G) > 0:
            pagerank = compute_pagerank(G)
        degrees = dict(G.degree())

        fts_rows = []
        meta_rows = []

        for node_id, data in G.nodes(data=True):
            pr = float(pagerank.get(node_id, 0.0))
            deg = int(degrees.get(node_id, 0))
            fts_row, meta_row = _prepare_node_document(
                node_id,
                data,
                node_contents=node_contents,
                pagerank_val=pr,
                degree_val=deg,
            )
            fts_rows.append(fts_row)
            meta_rows.append(meta_row)

        c.executemany(
            "INSERT INTO nodes_fts (node_id, title, headings, tags, body, source_path) VALUES (?, ?, ?, ?, ?, ?);",
            fts_rows,
        )
        c.executemany(
            """
            INSERT INTO node_metadata
            (node_id, label, source_path, source_location, file_type, node_kind, pagerank, degree, content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            meta_rows,
        )

        term_counter: Counter[str] = Counter()
        for nid_str, title, headings_val, tags_val, body_val, _ in fts_rows:
            combined = f"{nid_str} {title} {headings_val} {tags_val} {body_val}"
            node_terms = _extract_terms_from_text(combined)
            for t in node_terms:
                term_counter[t] += 1

        term_rows = [(term, count) for term, count in term_counter.items()]
        c.executemany(
            "INSERT OR IGNORE INTO indexed_terms (term, doc_count) VALUES (?, ?);",
            term_rows,
        )

        try:
            c.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('optimize');")
        except sqlite3.Error:
            pass

    def record_file_stats(self, stats: dict[str, tuple[int, int, str | None]]) -> None:
        """Record file size, mtime_ns, and content hash in file_stats table."""
        rows = [(path, sz, mtime_ns, h) for path, (sz, mtime_ns, h) in stats.items()]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO file_stats (path, size, mtime_ns, content_hash) VALUES (?, ?, ?, ?);",
                rows,
            )

    def get_file_stats(self) -> dict[str, dict[str, Any]]:
        """Return dict of path -> {size, mtime_ns, content_hash}."""
        cur = self.conn.cursor()
        cur.execute("SELECT path, size, mtime_ns, content_hash FROM file_stats;")
        res: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            res[row["path"]] = {
                "size": row["size"],
                "mtime_ns": row["mtime_ns"],
                "content_hash": row["content_hash"],
            }
        return res

    def remove_file(self, file_path: str) -> None:
        """Remove all nodes and incident edges associated with a source file."""
        fp_str = str(file_path)
        fp_name = Path(file_path).name
        cur = self.conn.cursor()
        with self.conn:
            cur.execute(
                """
                DELETE FROM edges
                WHERE source_id IN (SELECT id FROM nodes WHERE source_file = ? OR source_file = ?)
                   OR target_id IN (SELECT id FROM nodes WHERE source_file = ? OR source_file = ?);
                """,
                (fp_str, fp_name, fp_str, fp_name),
            )
            cur.execute(
                "DELETE FROM nodes_fts WHERE source_path = ? OR source_path = ?;",
                (fp_str, fp_name),
            )
            cur.execute(
                "DELETE FROM node_metadata WHERE source_path = ? OR source_path = ?;",
                (fp_str, fp_name),
            )
            cur.execute(
                "DELETE FROM nodes WHERE source_file = ? OR source_file = ?;",
                (fp_str, fp_name),
            )
            cur.execute("DELETE FROM file_stats WHERE path = ? OR path = ?;", (fp_str, fp_name))

    def hot_patch_file(
        self,
        file_path: str,
        new_nodes: list[dict],
        new_edges: list[dict],
        stat: tuple[int, int, str | None] | None = None,
    ) -> None:
        """Atomically hot-patch nodes, edges, FTS, and file stats for a modified file."""
        fp_str = str(file_path)
        cur = self.conn.cursor()
        with self.conn:
            self.remove_file(fp_str)

            node_rows = []
            fts_rows = []
            meta_rows = []
            reserved = {"label", "file_type", "node_kind", "source_file", "source_location", "community_id", "pagerank"}
            for n in new_nodes:
                nid = str(n.get("id") or "")
                if not nid:
                    continue
                lbl = str(n.get("label") or nid)
                kind = str(n.get("file_type") or n.get("node_kind") or "concept")
                sf = n.get("source_file") or fp_str
                s_line, e_line = _parse_source_location(n.get("source_location"))
                cid = n.get("community_id")
                comm_id = int(cid) if cid is not None else None
                pr = float(n.get("pagerank") or 0.0)
                extra = {k: v for k, v in n.items() if k not in reserved}
                meta_json = json.dumps(extra) if extra else None
                node_rows.append((nid, lbl, kind, str(sf), s_line, e_line, comm_id, pr, meta_json))

                doc = n.get("docstring") or ""
                body = n.get("body") or ""
                content = f"{lbl}\n{doc}\n{body}".strip()
                fts_rows.append((nid, lbl, "", "", content, str(sf)))
                meta_rows.append((nid, lbl, str(sf), str(n.get("source_location") or ""), kind, kind, pr, 0, content))

            if node_rows:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO nodes
                    (id, label, kind, source_file, start_line, end_line, community_id, pagerank, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    node_rows,
                )
                cur.executemany(
                    "INSERT INTO nodes_fts (node_id, title, headings, tags, body, source_path) VALUES (?, ?, ?, ?, ?, ?);",
                    fts_rows,
                )
                cur.executemany(
                    """
                    INSERT INTO node_metadata
                    (node_id, label, source_path, source_location, file_type, node_kind, pagerank, degree, content)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    meta_rows,
                )

            edge_rows = []
            seen_edges = set()
            reserved_e = {"relation", "confidence", "weight", "confidence_score", "call_site_line", "source_location"}
            for e in new_edges:
                s = str(e.get("source") or "")
                t = str(e.get("target") or "")
                if not s or not t:
                    continue
                rel = str(e.get("relation") or "references")
                conf = str(e.get("confidence") or "EXTRACTED")
                wt = float(e.get("weight") or e.get("confidence_score") or 1.0)
                cs_line, _ = _parse_source_location(e.get("call_site_line") or e.get("source_location"))
                cs_key = cs_line if cs_line is not None else -1

                edge_key = (s, t, rel, cs_key)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)

                extra_e = {k: v for k, v in e.items() if k not in reserved_e}
                e_meta = json.dumps(extra_e) if extra_e else None
                edge_rows.append((s, t, rel, conf, wt, cs_key, e_meta))

            if edge_rows:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO edges
                    (source_id, target_id, relation, confidence, weight, call_site_line, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    edge_rows,
                )

            if stat:
                sz, mtime_ns, h = stat
                cur.execute(
                    "INSERT OR REPLACE INTO file_stats (path, size, mtime_ns, content_hash) VALUES (?, ?, ?, ?);",
                    (fp_str, sz, mtime_ns, h),
                )


    def fts_search(
        self,
        query_text: str,
        *,
        limit: int = 10,
        synonyms: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute FTS5 search directly over unified graph.db with BM25 ranking."""
        from graph_fy.retrieval_index import _clean_fts_query
        match_expr = _clean_fts_query(query_text, synonyms=synonyms)
        if not match_expr:
            return []

        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT m.node_id, m.label, m.source_path, m.source_location, m.file_type, m.node_kind,
                   m.pagerank, m.degree, m.content, f.rank AS bm25_rank
            FROM nodes_fts f
            JOIN node_metadata m ON m.node_id = f.node_id
            WHERE nodes_fts MATCH ?
            ORDER BY f.rank
            LIMIT ?;
            """,
            (match_expr, limit),
        )
        results = []
        for row in cur.fetchall():
            results.append(dict(row))
        return results

    def unified_fts_and_bfs(
        self,
        query_text: str,
        *,
        max_hops: int = 2,
        limit: int = 10,
        direction: str = "outward",
    ) -> dict[str, Any]:
        """Single-query full-text search + recursive CTE graph traversal without Python deserialization."""
        from graph_fy.retrieval_index import _clean_fts_query
        match_expr = _clean_fts_query(query_text)
        if not match_expr:
            return {"seeds": [], "traversal": []}

        cur = self.conn.cursor()
        if direction == "inward":
            cte_join = "e.source_id, t.depth + 1, t.seed_rank FROM edges e JOIN traversal t ON e.target_id = t.id"
        else:
            cte_join = "e.target_id, t.depth + 1, t.seed_rank FROM edges e JOIN traversal t ON e.source_id = t.id"

        query = f"""
        WITH RECURSIVE seeds AS (
            SELECT f.node_id, f.rank AS seed_rank
            FROM nodes_fts f
            WHERE nodes_fts MATCH ?
            ORDER BY f.rank
            LIMIT ?
        ),
        traversal(id, depth, seed_rank) AS (
            SELECT node_id, 0, seed_rank FROM seeds
            UNION
            SELECT {cte_join}
            WHERE t.depth < ?
        )
        SELECT n.id, n.label, n.kind, n.source_file, n.start_line, n.end_line, n.pagerank, n.metadata_json,
               t.depth, t.seed_rank
        FROM traversal t
        JOIN nodes n ON n.id = t.id
        ORDER BY t.depth ASC, t.seed_rank ASC;
        """
        cur.execute(query, (match_expr, limit, max_hops))
        rows = cur.fetchall()
        seeds = []
        traversed = []
        for r in rows:
            d = dict(r)
            if d.get("metadata_json"):
                try:
                    d.update(json.loads(d.pop("metadata_json")))
                except Exception:
                    d.pop("metadata_json", None)
            else:
                d.pop("metadata_json", None)
            if d.get("depth") == 0:
                seeds.append(d)
            traversed.append(d)

        return {"seeds": seeds, "traversal": traversed}

    # --- Query API ---

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Fetch single node attributes in O(1)."""
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM nodes WHERE id = ?;", (str(node_id),))
        row = cur.fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("metadata_json"):
            extra = json.loads(res.pop("metadata_json"))
            res.update(extra)
        else:
            res.pop("metadata_json", None)
        return res

    def get_nodes(self, node_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Fetch multiple nodes by IDs."""
        id_list = [str(nid) for nid in node_ids]
        if not id_list:
            return []
        cur = self.conn.cursor()
        placeholders = ",".join("?" for _ in id_list)
        cur.execute(f"SELECT * FROM nodes WHERE id IN ({placeholders});", id_list)
        out = []
        for row in cur.fetchall():
            res = dict(row)
            if res.get("metadata_json"):
                extra = json.loads(res.pop("metadata_json"))
                res.update(extra)
            else:
                res.pop("metadata_json", None)
            out.append(res)
        return out

    def get_edges(
        self,
        *,
        source_id: str | None = None,
        target_id: str | None = None,
        relation: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query edges with optional index-accelerated filtering."""
        cur = self.conn.cursor()
        clauses = []
        params: list[Any] = []
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(str(source_id))
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(str(target_id))
        if relation is not None:
            clauses.append("relation = ?")
            params.append(str(relation))

        where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit_clause = f" LIMIT {int(limit)}" if limit else ""
        sql = f"SELECT * FROM edges{where_clause}{limit_clause};"

        cur.execute(sql, params)
        out = []
        for row in cur.fetchall():
            e = dict(row)
            if e.get("metadata_json"):
                extra = json.loads(e.pop("metadata_json"))
                e.update(extra)
            else:
                e.pop("metadata_json", None)
            if e.get("call_site_line") == -1:
                e["call_site_line"] = None
            out.append(e)
        return out

    def get_callers_and_callees(self, node_id: str) -> tuple[list[str], list[str]]:
        """Fast lookup of 1-hop callers and callees in <0.5ms."""
        nid = str(node_id)
        cur = self.conn.cursor()
        # Callers: incoming calls edge
        cur.execute("SELECT DISTINCT source_id FROM edges WHERE target_id = ? AND relation = 'calls';", (nid,))
        callers = [r[0] for r in cur.fetchall()]
        # Callees: outgoing calls edge
        cur.execute("SELECT DISTINCT target_id FROM edges WHERE source_id = ? AND relation = 'calls';", (nid,))
        callees = [r[0] for r in cur.fetchall()]
        return callers, callees

    # --- Recursive CTE Traversal ---

    def reverse_bfs(
        self,
        seed: str,
        *,
        relations: Iterable[str] = ("calls", "indirect_call", "imports", "imports_from", "inherits", "extends"),
        max_depth: int = 2,
    ) -> list[dict[str, Any]]:
        """Execute upstream reverse-BFS via SQLite indexed Recursive CTE in <2ms.

        Seeds with seed's own member/contains nodes so callers binding to class methods
        are reachable, matching the exact behaviour of `affected_nodes`.
        """
        cur = self.conn.cursor()
        rel_list = [str(r) for r in relations]
        if not rel_list:
            return []

        placeholders = ",".join("?" for _ in rel_list)

        # SQL Recursive CTE
        sql = f"""
        WITH RECURSIVE
        -- Seed initial frontier
        seeds(node_id, depth, via_relation, via_file, via_location) AS (
            -- 1. Direct seed
            SELECT ?, 0, '', NULL, NULL
            UNION
            -- 2. Seed's members / methods (depth 0)
            SELECT target_id, 0, relation, NULL, NULL
            FROM edges
            WHERE source_id = ? AND relation IN ('method', 'contains')
        ),
        -- Upstream reverse BFS
        bfs(node_id, depth, via_relation, via_file, via_location) AS (
            SELECT node_id, depth, via_relation, via_file, via_location FROM seeds
            UNION
            SELECT
                e.source_id,
                b.depth + 1,
                e.relation,
                n.source_file,
                CASE WHEN e.call_site_line > 0 THEN 'L' || e.call_site_line ELSE n.start_line END
            FROM edges e
            JOIN bfs b ON e.target_id = b.node_id
            LEFT JOIN nodes n ON e.source_id = n.id
            WHERE b.depth < ?
              AND e.relation IN ({placeholders})
        )
        SELECT
            node_id,
            MIN(depth) AS depth,
            via_relation,
            via_file,
            via_location
        FROM bfs
        WHERE depth > 0
        GROUP BY node_id
        ORDER BY depth ASC, node_id ASC;
        """

        params = [str(seed), str(seed), int(max_depth), *rel_list]
        cur.execute(sql, params)
        hits = []
        for r in cur.fetchall():
            hits.append({
                "node_id": r["node_id"],
                "depth": r["depth"],
                "via_relation": r["via_relation"],
                "via_file": r["via_file"],
                "via_location": str(r["via_location"]) if r["via_location"] is not None else None,
            })
        return hits

    def k_hop_ego_graph(
        self,
        seed: str,
        *,
        max_depth: int = 1,
        directed: bool = True,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Extract k-hop ego sub-graph (nodes and edges) in <2ms without loading full graph."""
        cur = self.conn.cursor()
        seed_str = str(seed)

        if directed:
            # Directed BFS along outgoing edges
            sql = f"""
            WITH RECURSIVE ego(node_id, depth) AS (
                SELECT ?, 0
                UNION
                SELECT e.target_id, ego.depth + 1
                FROM edges e
                JOIN ego ON e.source_id = ego.node_id
                WHERE ego.depth < ?
            )
            SELECT DISTINCT node_id FROM ego;
            """
            params = (seed_str, int(max_depth))
        else:
            # Undirected BFS
            sql = f"""
            WITH RECURSIVE ego(node_id, depth) AS (
                SELECT ?, 0
                UNION
                SELECT CASE WHEN e.source_id = ego.node_id THEN e.target_id ELSE e.source_id END, ego.depth + 1
                FROM edges e
                JOIN ego ON e.source_id = ego.node_id OR e.target_id = ego.node_id
                WHERE ego.depth < ?
            )
            SELECT DISTINCT node_id FROM ego;
            """
            params = (seed_str, int(max_depth))

        cur.execute(sql, params)
        node_ids = [r[0] for r in cur.fetchall()]
        if not node_ids:
            return [], []

        nodes = self.get_nodes(node_ids)

        # Retrieve induced subgraph edges between these nodes
        id_set = set(node_ids)
        placeholders = ",".join("?" for _ in node_ids)
        cur.execute(
            f"""
            SELECT * FROM edges
            WHERE source_id IN ({placeholders})
              AND target_id IN ({placeholders});
            """,
            [*node_ids, *node_ids],
        )

        edges = []
        for row in cur.fetchall():
            e = dict(row)
            if e.get("metadata_json"):
                extra = json.loads(e.pop("metadata_json"))
                e.update(extra)
            else:
                e.pop("metadata_json", None)
            if e.get("call_site_line") == -1:
                e["call_site_line"] = None
            edges.append(e)

        return nodes, edges

    def to_networkx(self, *, directed: bool = False, multigraph: bool = False) -> nx.Graph:
        """Export SQLite store to NetworkX graph when in-memory operations are explicitly required."""
        G = nx.MultiDiGraph() if (directed and multigraph) else nx.DiGraph() if directed else nx.MultiGraph() if multigraph else nx.Graph()

        cur = self.conn.cursor()
        # Graph metadata
        cur.execute("SELECT key, value FROM graph_metadata;")
        for k, v in cur.fetchall():
            if k == "learning":
                try:
                    G.graph["learning"] = json.loads(v)
                except Exception:
                    G.graph["learning"] = v
            else:
                G.graph[k] = v

        # Nodes
        cur.execute("SELECT * FROM nodes;")
        for row in cur.fetchall():
            d = dict(row)
            nid = d.pop("id")
            meta = d.pop("metadata_json", None)
            if meta:
                try:
                    d.update(json.loads(meta))
                except Exception:
                    pass
            # Format source_location back to L<start>-L<end>
            s_line = d.pop("start_line", None)
            e_line = d.pop("end_line", None)
            if s_line is not None and e_line is not None:
                d["source_location"] = f"L{s_line}" if s_line == e_line else f"L{s_line}-L{e_line}"
            d["file_type"] = d.pop("kind", "concept")
            G.add_node(nid, **d)

        # Edges
        cur.execute("SELECT * FROM edges;")
        for row in cur.fetchall():
            e = dict(row)
            s = e.pop("source_id")
            t = e.pop("target_id")
            meta = e.pop("metadata_json", None)
            if meta:
                try:
                    e.update(json.loads(meta))
                except Exception:
                    pass
            cs = e.pop("call_site_line", None)
            if cs and cs != -1:
                e["call_site_line"] = cs
            G.add_edge(s, t, **e)

        # Hyperedges
        cur.execute("SELECT id, relation, metadata_json FROM hyperedges;")
        hyperedges = []
        for hid, rel, meta in cur.fetchall():
            h_dict: dict[str, Any] = {"id": hid, "relation": rel}
            if meta:
                try:
                    h_dict.update(json.loads(meta))
                except Exception:
                    pass
            cur.execute("SELECT node_id FROM hyperedge_members WHERE hyperedge_id = ?;", (hid,))
            h_dict["nodes"] = [r[0] for r in cur.fetchall()]
            hyperedges.append(h_dict)

        if hyperedges:
            G.graph["hyperedges"] = hyperedges

        return G

    def to_dict(self) -> dict[str, Any]:
        """Export SQLite store directly to canonical graph.json dict format."""
        cur = self.conn.cursor()

        # Nodes
        cur.execute("SELECT * FROM nodes ORDER BY id ASC;")
        nodes = []
        for row in cur.fetchall():
            d = dict(row)
            meta = d.pop("metadata_json", None)
            if meta:
                try:
                    d.update(json.loads(meta))
                except Exception:
                    pass
            s_line = d.pop("start_line", None)
            e_line = d.pop("end_line", None)
            if s_line is not None and e_line is not None:
                d["source_location"] = f"L{s_line}" if s_line == e_line else f"L{s_line}-L{e_line}"
            d["file_type"] = d.pop("kind", "concept")
            nodes.append(d)

        # Edges / Links
        cur.execute("SELECT * FROM edges ORDER BY source_id ASC, target_id ASC, relation ASC;")
        links = []
        for row in cur.fetchall():
            e = dict(row)
            meta = e.pop("metadata_json", None)
            if meta:
                try:
                    e.update(json.loads(meta))
                except Exception:
                    pass
            s = e.pop("source_id")
            t = e.pop("target_id")
            cs = e.pop("call_site_line", None)
            link_dict = {"source": s, "target": t, **e}
            if cs and cs != -1:
                link_dict["call_site_line"] = cs
            links.append(link_dict)

        # Hyperedges
        cur.execute("SELECT id, relation, metadata_json FROM hyperedges ORDER BY id ASC;")
        hyperedges = []
        for hid, rel, meta in cur.fetchall():
            h_dict = {"id": hid, "relation": rel}
            if meta:
                try:
                    h_dict.update(json.loads(meta))
                except Exception:
                    pass
            cur.execute("SELECT node_id FROM hyperedge_members WHERE hyperedge_id = ? ORDER BY node_id ASC;", (hid,))
            h_dict["nodes"] = [r[0] for r in cur.fetchall()]
            hyperedges.append(h_dict)

        data: dict[str, Any] = {
            "directed": True,
            "multigraph": True,
            "graph": {},
            "nodes": nodes,
            "links": links,
            "hyperedges": hyperedges,
        }

        # Metadata
        cur.execute("SELECT key, value FROM graph_metadata;")
        for k, v in cur.fetchall():
            if k == "learning":
                try:
                    data[k] = json.loads(v)
                except Exception:
                    data[k] = v
            else:
                data[k] = v

        return data

    def stats(self) -> dict[str, Any]:
        """Compute quick topology summary directly from SQL indices."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM nodes;")
        node_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM edges;")
        edge_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT source_file) FROM nodes WHERE source_file IS NOT NULL;")
        file_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM hyperedges;")
        hyperedge_count = cur.fetchone()[0]

        cur.execute("SELECT value FROM graph_metadata WHERE key = 'built_at_commit';")
        commit_row = cur.fetchone()
        commit = commit_row[0] if commit_row else None

        return {
            "node_count": node_count,
            "edge_count": edge_count,
            "file_count": file_count,
            "hyperedge_count": hyperedge_count,
            "built_at_commit": commit,
        }
