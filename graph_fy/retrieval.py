"""Fully local lexical + graph-augmented retrieval layer using SQLite FTS5 (BM25) and NetworkX.

No LLM calls, no embedding models, zero external network requests.
Provides:
  - build_index(G, db_path): builds SQLite FTS5 index and precomputes static graph metrics (PageRank, degree)
  - query(query_text, graph, db_path, limit, hops): 4-step GraphRAG retrieval (Seed -> Expand -> Re-rank -> Return)
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import networkx as nx
from rapidfuzz import distance, process

from graph_fy.paths import graph_fy_OUT, out_path


def compute_pagerank(
    G: nx.Graph,
    *,
    personalization: dict[Any, float] | None = None,
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> dict[Any, float]:
    """Compute PageRank or Personalized PageRank (PPR) in pure Python without requiring scipy.

    If personalization is provided, biases random walks toward the seed nodes.
    """
    if len(G) == 0:
        return {}

    nodes = list(G.nodes())
    n_count = len(nodes)
    if personalization is None:
        p = {n: 1.0 / n_count for n in nodes}
    else:
        s = sum(personalization.values())
        if s == 0:
            p = {n: 1.0 / n_count for n in nodes}
        else:
            p = {n: personalization.get(n, 0.0) / s for n in nodes}

    x = dict(p)
    dangling_weights = dict(p)
    out_degree = {n: G.degree(n) if not G.is_directed() else G.out_degree(n) for n in nodes}

    for _ in range(max_iter):
        xlast = x
        x = {n: 0.0 for n in nodes}
        danglesum = alpha * sum(xlast[n] for n in nodes if out_degree[n] == 0)

        for n in nodes:
            deg = out_degree[n]
            if deg > 0:
                share = alpha * xlast[n] / deg
                for nbr in (G.neighbors(n) if not G.is_directed() else G.successors(n)):
                    x[nbr] += share

        for n in nodes:
            x[n] += danglesum * dangling_weights[n] + (1.0 - alpha) * p[n]

        err = sum(abs(x[n] - xlast[n]) for n in nodes)
        if err < tol:
            break

    return x


def get_default_index_path(root: Path | str | None = None) -> Path:
    """Return the default SQLite FTS5 index path."""
    if root is not None:
        return Path(root) / graph_fy_OUT / "index.db"
    return out_path("index.db")


def split_subtokens(text: str) -> list[str]:
    """Split camelCase, PascalCase, and snake_case into sub-word tokens alongside whole identifiers.

    e.g.:
      getUserById -> ['getUserById', 'get', 'user', 'by', 'id']
      AuthManager -> ['AuthManager', 'auth', 'manager']
      parse_query -> ['parse_query', 'parse', 'query']
    """
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z0-9_\-]+", text)
    result = []
    seen = set()

    for tok in tokens:
        clean = tok.strip("-_")
        if not clean:
            continue
        if clean not in seen:
            seen.add(clean)
            result.append(clean)

        parts = [p for p in re.split(r"[_\-]+", clean) if p]
        for p in parts:
            subparts = re.findall(r"[A-Z]+(?=[A-Z][a-z0-9]|\b)|[A-Z]?[a-z0-9]+|[A-Z]+", p)
            for sp in subparts:
                sp_low = sp.lower()
                if sp_low not in seen:
                    seen.add(sp_low)
                    result.append(sp_low)

    return result


def expand_subtokens(text: str) -> str:
    """Expand identifiers with their sub-tokens.
    e.g. 'getUserById' -> 'getUserById get user by id'
    """
    return " ".join(split_subtokens(text))


def max_allowed_distance(term: str) -> int:
    """Return maximum allowed Levenshtein distance based on word length:
    - For word length >= 5: distance <= 2
    - For word length 4: distance <= 1
    - For word length < 4: distance 0 (exact match only)
    """
    n = len(term)
    if n >= 5:
        return 2
    if n == 4:
        return 1
    return 0


def fuzzy_match_term(
    term: str,
    vocabulary: Iterable[str],
    *,
    max_distance: int | None = None,
    limit: int = 3,
) -> list[str]:
    """Find close fuzzy matches for a term in the vocabulary using rapidfuzz Levenshtein distance.

    Rules:
    - For word length >= 5, allow Levenshtein distance <= 2.
    - For word length 4, allow distance <= 1.
    - For word length < 4, only exact matches are returned.
    """
    term_clean = term.strip().lower()
    if not term_clean:
        return []

    cutoff = max_distance if max_distance is not None else max_allowed_distance(term_clean)
    if cutoff <= 0:
        return [term_clean] if term_clean in vocabulary else []

    # If already an exact match in vocabulary, return [term_clean]
    if term_clean in vocabulary:
        return [term_clean]

    raw_matches = process.extract(
        term_clean,
        vocabulary,
        scorer=distance.Levenshtein.distance,
        score_cutoff=cutoff,
        limit=limit * 2,
    )
    if not raw_matches:
        return []

    # Filter by candidate length rules: a candidate of length < 4 should not fuzzy-match (non-exact)
    valid_matches: list[tuple[str, int]] = []
    for cand, dist, _ in raw_matches:
        cand_str = str(cand)
        if dist > 0 and len(cand_str) < 4:
            continue
        valid_matches.append((cand_str, int(dist)))

    if not valid_matches:
        return []

    min_dist = valid_matches[0][1]
    return [cand for cand, dist in valid_matches if dist == min_dist][:limit]


def fuzzy_match_tokens(
    tokens: list[str] | set[str] | Iterable[str],
    vocabulary: Iterable[str],
    *,
    limit: int = 3,
) -> dict[str, list[str]]:
    """Match query tokens against indexed vocabulary and return expansions for tokens needing typo correction."""
    vocab_set = set(vocabulary) if not isinstance(vocabulary, set) else vocabulary
    expansions: dict[str, list[str]] = {}
    for tok in tokens:
        tok_clean = tok.strip("-_").lower()
        if not tok_clean:
            continue
        # If token is already present in vocabulary, no typo correction is needed
        if tok_clean in vocab_set:
            continue
        matches = fuzzy_match_term(tok_clean, vocab_set, limit=limit)
        if matches:
            expansions[tok_clean] = matches
    return expansions


def _extract_terms_from_text(text: str) -> set[str]:
    """Extract individual words, sub-tokens, and identifier parts for vocabulary indexing."""
    if not text:
        return set()
    terms: set[str] = set()
    for tok in split_subtokens(text):
        if len(tok) >= 2:
            terms.add(tok.lower())
    raw_tokens = re.findall(r"[A-Za-z0-9_\-]+", text)
    for raw in raw_tokens:
        clean = raw.strip("-_")
        if not clean:
            continue
        clean_low = clean.lower()
        if len(clean_low) >= 2:
            terms.add(clean_low)
        parts = re.split(r"[_\-]+", clean_low)
        for p in parts:
            if len(p) >= 2:
                terms.add(p)
        camel_parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z][a-z]|\b)", clean)
        for cp in camel_parts:
            cp_low = cp.lower()
            if len(cp_low) >= 2:
                terms.add(cp_low)
    return terms


def get_indexed_terms(conn: sqlite3.Connection) -> set[str]:
    """Retrieve all indexed vocabulary and symbol names from index.db."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='indexed_terms';")
    if cur.fetchone():
        cur.execute("SELECT term FROM indexed_terms;")
        terms = {row[0] for row in cur.fetchall()}
        if terms:
            return terms

    terms = set()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='node_metadata';")
    if cur.fetchone():
        cur.execute("SELECT node_id, label, content FROM node_metadata;")
        for nid, lbl, cnt in cur.fetchall():
            combined = f"{nid} {lbl or ''} {cnt or ''}"
            terms.update(_extract_terms_from_text(combined))

    if terms:
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS indexed_terms (
                    term TEXT PRIMARY KEY,
                    doc_count INTEGER DEFAULT 1
                );
                """
            )
            cur.executemany(
                "INSERT OR IGNORE INTO indexed_terms (term, doc_count) VALUES (?, 1);",
                [(t,) for t in terms],
            )
            conn.commit()
        except sqlite3.Error:
            pass

    return terms


def _clean_fts_query(
    query: str,
    synonyms: dict[str, str] | None = None,
    fuzzy_expansions: dict[str, list[str]] | None = None,
) -> str:
    """Normalize a user query into a safe, phrase, NEAR(), synonym, and fuzzy-expanded FTS5 MATCH expression."""
    if not query or not query.strip():
        return ""

    # 1. Extract explicit quoted phrases
    phrases = re.findall(r'"([^"]+)"', query)
    unquoted = re.sub(r'"[^"]+"', " ", query)

    clean_phrases: list[str] = []
    for p in phrases:
        p_tokens = [t.strip("-") for t in re.findall(r"[A-Za-z0-9_\-]+", p) if t.strip("-")]
        if p_tokens:
            phrase_str = " ".join(p_tokens)
            clean_phrases.append(f'"{phrase_str}"')
            if fuzzy_expansions:
                corrected_p = [
                    fuzzy_expansions.get(t.lower(), [t])[0]
                    for t in p_tokens
                ]
                if corrected_p != p_tokens:
                    clean_phrases.append(f'"{" ".join(corrected_p)}"')

    # 2. Extract unquoted words and sub-tokens
    raw_tokens = [t.strip("-") for t in re.findall(r"[A-Za-z0-9_\-]+", unquoted) if t.strip("-")]
    sub_expanded: list[str] = []
    for t in raw_tokens:
        sub_expanded.extend(split_subtokens(t))
    tokens = list(dict.fromkeys(sub_expanded))

    # 3. Synonym expansion from aliases if provided
    expanded_tokens = list(tokens)
    if synonyms:
        for t in tokens:
            t_lower = t.lower()
            if t_lower in synonyms:
                syn = synonyms[t_lower]
                syn_clean = [s.strip("-") for s in re.findall(r"[A-Za-z0-9_\-]+", syn) if s.strip("-")]
                expanded_tokens.extend(syn_clean)

    # 4. Fuzzy typo expansion
    if fuzzy_expansions:
        for t in tokens:
            t_lower = t.lower()
            if t_lower in fuzzy_expansions:
                expanded_tokens.extend(fuzzy_expansions[t_lower])

    parts: list[str] = list(clean_phrases)
    if expanded_tokens:
        unique_tokens = list(dict.fromkeys(expanded_tokens))
        quoted_tokens = [f'"{t}"' for t in unique_tokens]
        parts.extend(quoted_tokens)

        # 5. Proximity matching with NEAR() for multi-word queries
        if len(raw_tokens) >= 2:
            near_tokens = [
                fuzzy_expansions.get(t.lower(), [t])[0] if fuzzy_expansions else t
                for t in raw_tokens
            ]
            clean_near = [t.replace('"', '').strip("-_") for t in near_tokens if t.replace('"', '').strip("-_")]
            if len(clean_near) >= 2:
                inner_near = " ".join(f'"{t}"' if any(c in t for c in "-_ :/") else t for t in clean_near)
                parts.append(f"NEAR({inner_near}, 10)")

    return " OR ".join(parts)


def _prepare_node_document(
    node_id: Any,
    data: dict[str, Any],
    node_contents: dict[str, str] | None = None,
    pagerank_val: float = 0.0,
    degree_val: int = 0,
) -> tuple[tuple[str, str, str, str, str, str], tuple[str, str, str, str, str, str, float, int, str]]:
    """Prepare FTS5 row and metadata row for a single node, including code-aware sub-tokens."""
    nid_str = str(node_id)
    label = str(data.get("label") or nid_str)
    src_file = str(data.get("source_file") or "")
    src_loc = str(data.get("source_location") or "")
    ftype = str(data.get("file_type") or "concept")
    nkind = str(data.get("node_kind") or "")

    content_pieces = []
    if node_contents and nid_str in node_contents:
        content_pieces.append(node_contents[nid_str])
    if data.get("text"):
        content_pieces.append(str(data["text"]))
    if data.get("docstring"):
        content_pieces.append(str(data["docstring"]))
    if data.get("context"):
        content_pieces.append(str(data["context"]))

    content_str = "\n".join(content_pieces).strip()
    if not content_str:
        content_str = label

    title_raw = str(data.get("label") or data.get("title") or data.get("name") or nid_str)
    name_raw = str(data.get("name") or "")
    expanded_title_parts = [expand_subtokens(title_raw)]
    if name_raw:
        expanded_title_parts.append(expand_subtokens(name_raw))
    title = " ".join(dict.fromkeys(" ".join(expanded_title_parts).split()))

    headings_val = str(data.get("headings") or "")
    if not headings_val and data.get("node_kind") == "heading":
        headings_val = expand_subtokens(str(data.get("text") or label))

    tags_raw = data.get("tags") or data.get("tag") or data.get("category") or ""
    if isinstance(tags_raw, (list, set, tuple)):
        tags_val = " ".join(expand_subtokens(str(t)) for t in tags_raw)
    else:
        tags_val = expand_subtokens(str(tags_raw))

    body_val = content_str
    label_subtokens = expand_subtokens(label)
    if label_subtokens and label_subtokens not in body_val:
        body_val = f"{body_val}\n{label_subtokens}"

    fts_row = (nid_str, title, headings_val, tags_val, body_val, src_file)
    meta_row = (nid_str, label, src_file, src_loc, ftype, nkind, pagerank_val, degree_val, content_str)
    return fts_row, meta_row


def build_index(
    G: nx.Graph,
    db_path: Path | str | None = None,
    *,
    node_contents: dict[str, str] | None = None,
) -> Path:
    """Build a SQLite FTS5 full-text index for all nodes in the graph G.

    Precomputes PageRank and node degrees for static centrality scoring.
    """
    path = Path(db_path) if db_path is not None else get_default_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Compute static centrality metrics
    pagerank: dict[Any, float] = {}
    if len(G) > 0:
        pagerank = compute_pagerank(G)

    degrees = dict(G.degree())

    # Create fresh database schema
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass

    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA synchronous = OFF;")
        cur.execute("PRAGMA journal_mode = MEMORY;")

        # Create FTS5 virtual table with weighted columns and Porter stemmer
        cur.execute(
            """
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                node_id UNINDEXED,
                title,
                headings,
                tags,
                body,
                source_path UNINDEXED,
                tokenize='porter unicode61'
            );
            """
        )

        # Metadata table
        cur.execute(
            """
            CREATE TABLE node_metadata (
                node_id TEXT PRIMARY KEY,
                label TEXT,
                source_path TEXT,
                source_location TEXT,
                file_type TEXT,
                node_kind TEXT,
                pagerank REAL,
                degree INTEGER,
                content TEXT
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS indexed_terms (
                term TEXT PRIMARY KEY,
                doc_count INTEGER DEFAULT 1
            );
            """
        )

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

        term_counter: Counter[str] = Counter()
        for nid_str, title, headings_val, tags_val, body_val, _ in fts_rows:
            combined = f"{nid_str} {title} {headings_val} {tags_val} {body_val}"
            node_terms = _extract_terms_from_text(combined)
            for t in node_terms:
                term_counter[t] += 1

        term_rows = [(term, count) for term, count in term_counter.items()]
        cur.executemany(
            "INSERT OR IGNORE INTO indexed_terms (term, doc_count) VALUES (?, ?);",
            term_rows,
        )

        # Optimize FTS index
        cur.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('optimize');")
        conn.commit()
    finally:
        conn.close()

    return path


def _find_callers_and_callees(G: nx.Graph, nid: Any) -> tuple[list[str], list[str]]:
    """Find immediate callers (inbound calls) and callees (outbound calls) for a node."""
    callers: list[str] = []
    callees: list[str] = []

    def _get_edges(u: Any, v: Any) -> list[dict]:
        edata = G.get_edge_data(u, v) or {}
        if isinstance(edata, dict):
            if any(isinstance(k, int) for k in edata.keys()):
                return [val for val in edata.values() if isinstance(val, dict)]
            return [edata]
        return []

    # 1. Directed graph: predecessors (callers) and successors (callees)
    if hasattr(G, "predecessors"):
        for pred in G.predecessors(nid):
            for ed in _get_edges(pred, nid):
                rel = str(ed.get("relation", "")).lower()
                if rel in ("calls", "call", "invokes"):
                    lbl = str(G.nodes[pred].get("label") or pred)
                    if lbl not in callers:
                        callers.append(lbl)
    if hasattr(G, "successors"):
        for succ in G.successors(nid):
            for ed in _get_edges(nid, succ):
                rel = str(ed.get("relation", "")).lower()
                if rel in ("calls", "call", "invokes"):
                    lbl = str(G.nodes[succ].get("label") or succ)
                    if lbl not in callees:
                        callees.append(lbl)

    # 2. Undirected graph: inspect neighbors and edge source/target
    if hasattr(G, "neighbors") and not G.is_directed():
        for nb in G.neighbors(nid):
            for ed in _get_edges(nid, nb):
                rel = str(ed.get("relation", "")).lower()
                if rel in ("calls", "call", "invokes"):
                    lbl = str(G.nodes[nb].get("label") or nb)
                    src = ed.get("source")
                    tgt = ed.get("target")
                    if src == nb or tgt == nid:
                        if lbl not in callers:
                            callers.append(lbl)
                    elif src == nid or tgt == nb:
                        if lbl not in callees:
                            callees.append(lbl)
                    else:
                        if lbl not in callers:
                            callers.append(lbl)

    return callers, callees


def query(
    query_text: str,
    graph: nx.Graph | None = None,
    db_path: Path | str | None = None,
    *,
    limit: int = 10,
    hops: int = 2,
    graph_path: Path | str | None = None,
    synonyms: dict[str, str] | None = None,
    fuzzy: bool = True,
    fuzzy_min_results: int = 1,
    terse: bool = False,
    caveman: bool = False,
    skeleton: bool = False,
    boundary_pruning: bool = True,
) -> list[dict[str, Any]] | str:
    """Execute 4-step GraphRAG retrieval over the SQLite FTS5 index and graph.

    Steps:
      1. Seed: FTS5 BM25 search (with optional typo-tolerant fuzzy expansion) -> top matching seed nodes
      2. Expand: traverse graph outward up to `hops` from each seed
      3. Re-rank: combined score = BM25 + proximity discount + centrality bonus
      4. Return: ranked list of { node_id, score, text, source_path, connected_via }
    """
    if not query_text or not query_text.strip():
        return []

    # Resolve database path
    idx_path = Path(db_path) if db_path is not None else get_default_index_path()

    if synonyms is None:
        try:
            from graph_fy.labeling import load_aliases_config
            root_cand = idx_path.parent.parent if idx_path.parent.name in ("graph_fy_out", "graph_fy_out") else Path(".")
            synonyms = load_aliases_config(root_cand)
        except Exception:
            synonyms = None

    # Load graph if not provided
    if graph is None:
        gp = Path(graph_path) if graph_path is not None else out_path("graph.json")
        if not gp.exists():
            return []
        try:
            from graph_fy.paths import load_node_link_graph
            graph = load_node_link_graph(gp)
        except Exception:
            return []

    # If FTS index does not exist, build it now
    if not idx_path.exists():
        build_index(graph, idx_path)

    conn = sqlite3.connect(str(idx_path))
    try:
        cur = conn.cursor()
        seed_limit = max(limit * 2, 20)

        vocab: set[str] | None = None
        fuzzy_expansions: dict[str, list[str]] | None = None

        if fuzzy:
            vocab = get_indexed_terms(conn)
            raw_q_tokens = [t.strip("-") for t in re.findall(r"[A-Za-z0-9_\-]+", query_text) if t.strip("-")]
            q_tokens: list[str] = []
            for qt in raw_q_tokens:
                q_tokens.extend(split_subtokens(qt))
            q_tokens = list(dict.fromkeys(q_tokens))
            token_expansions = fuzzy_match_tokens(q_tokens, vocab)
            if token_expansions:
                fuzzy_expansions = token_expansions

        clean_q = _clean_fts_query(query_text, synonyms=synonyms, fuzzy_expansions=fuzzy_expansions)
        if not clean_q:
            return []

        # Step 1: Seed step via BM25 with column weights:
        # title=5.0, headings=3.0, tags=4.0, body=1.0
        # In FTS5, bm25() returns negative values (lower = more relevant);
        # we negate it so higher positive score = more relevant.
        cur.execute(
            """
            SELECT node_id, (-1.0 * bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0)) AS bm25_score
            FROM nodes_fts
            WHERE nodes_fts MATCH ?
            ORDER BY bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0) ASC
            LIMIT ?;
            """,
            (clean_q, seed_limit),
        )
        seed_rows = cur.fetchall()

        # If search term yields 0 or very few BM25 results, and fuzzy matching is enabled,
        # perform broader fuzzy expansion if not already applied
        if fuzzy and len(seed_rows) < fuzzy_min_results and not fuzzy_expansions:
            if vocab is None:
                vocab = get_indexed_terms(conn)
            raw_q_tokens = [t.strip("-") for t in re.findall(r"[A-Za-z0-9_\-]+", query_text) if t.strip("-")]
            q_tokens = []
            for qt in raw_q_tokens:
                q_tokens.extend(split_subtokens(qt))
            q_tokens = list(dict.fromkeys(q_tokens))
            broader_expansions = {}
            for tok in q_tokens:
                tok_clean = tok.lower()
                matches = fuzzy_match_term(tok_clean, vocab)
                if matches and matches != [tok_clean]:
                    broader_expansions[tok_clean] = matches
            if broader_expansions:
                fuzzy_clean_q = _clean_fts_query(query_text, synonyms=synonyms, fuzzy_expansions=broader_expansions)
                if fuzzy_clean_q and fuzzy_clean_q != clean_q:
                    cur.execute(
                        """
                        SELECT node_id, (-1.0 * bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0)) AS bm25_score
                        FROM nodes_fts
                        WHERE nodes_fts MATCH ?
                        ORDER BY bm25(nodes_fts, 5.0, 3.0, 4.0, 1.0) ASC
                        LIMIT ?;
                        """,
                        (fuzzy_clean_q, seed_limit),
                    )
                    fuzzy_rows = cur.fetchall()
                    if fuzzy_rows:
                        seed_rows = fuzzy_rows

        if not seed_rows:
            return []

        # Map seed nodes to their initial BM25 scores (normalized to 0-10 scale)
        max_raw = max((float(score) for _, score in seed_rows), default=1.0)
        if max_raw <= 0:
            max_raw = 1.0
        seed_scores: dict[str, float] = {}
        for nid, score in seed_rows:
            raw_val = max(float(score), 0.0)
            seed_scores[nid] = (raw_val / max_raw) * 10.0

        # Step 2: Expand step (1 to `hops` hops)
        # We find paths from seed nodes to neighbors in the graph
        # visited: nid -> {"dist": int, "seed": str, "path": list[dict]}
        visited: dict[str, dict[str, Any]] = {}

        for seed_id in seed_scores:
            if seed_id not in visited:
                visited[seed_id] = {
                    "dist": 0,
                    "seed": seed_id,
                    "path": [],
                }

        if hops > 0 and len(graph) > 0:
            queue = [(seed_id, 0) for seed_id in list(seed_scores.keys()) if seed_id in graph]
            idx = 0
            while idx < len(queue):
                curr, dist = queue[idx]
                idx += 1
                if dist >= hops:
                    continue

                curr_seed = visited[curr]["seed"]
                curr_path = visited[curr]["path"]

                for neighbor in graph.neighbors(curr):
                    n_str = str(neighbor)
                    new_dist = dist + 1

                    # Dynamic Ego-Graph Boundary Pruning (RepoGraph ICLR 2025)
                    if boundary_pruning and dist >= 1:
                        ed = graph.get_edge_data(curr, neighbor) or {}
                        if isinstance(ed, dict) and 0 in ed:
                            ed = ed[0]
                        weight = float(ed.get("weight", 1.0))
                        if weight < 0.2:
                            continue
                        if len(visited) > max(limit * 2, 20):
                            deg = graph.degree(neighbor)
                            if deg <= 1:
                                continue

                    if n_str not in visited or visited[n_str]["dist"] > new_dist:
                        # Get edge relation if available
                        edge_data = graph.get_edge_data(curr, neighbor) or {}
                        if isinstance(edge_data, dict) and 0 in edge_data:
                            edge_data = edge_data[0]
                        relation = edge_data.get("relation") or "connected"
                        edge_step = {
                            "from": curr,
                            "relation": relation,
                            "to": n_str,
                        }
                        visited[n_str] = {
                            "dist": new_dist,
                            "seed": curr_seed,
                            "path": curr_path + [edge_step],
                        }
                        queue.append((n_str, new_dist))

        # Retrieve metadata & content for all candidate nodes
        all_candidate_ids = list(visited.keys())
        if not all_candidate_ids:
            return []

        # Chunk candidate lookups for SQLite parameter limit safety
        meta_dict: dict[str, tuple] = {}
        chunk_size = 500
        for i in range(0, len(all_candidate_ids), chunk_size):
            chunk = all_candidate_ids[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(
                f"""
                SELECT node_id, label, source_path, source_location, pagerank, degree, content
                FROM node_metadata
                WHERE node_id IN ({placeholders});
                """,  # nosec B608
                chunk,
            )
            for row in cur.fetchall():
                meta_dict[row[0]] = row

        # Step 3: Re-rank step using BM25, proximity discount, and Personalized PageRank (PPR)
        # Compute Personalized PageRank seeded by BM25 match scores over candidate subgraph
        ppr_scores: dict[str, float] = {}
        if graph is not None and len(all_candidate_ids) > 1:
            try:
                sub_nodes = set(all_candidate_ids)
                subG = graph.subgraph([n for n in sub_nodes if n in graph])
                if len(subG) > 0:
                    pers_map = {n: seed_scores.get(n, 0.1) for n in subG.nodes()}
                    ppr_scores = compute_pagerank(subG, personalization=pers_map, alpha=0.85, max_iter=50)
            except Exception:
                ppr_scores = {}

        candidates: list[dict[str, Any]] = []

        for nid, vinfo in visited.items():
            meta = meta_dict.get(nid)
            if not meta:
                continue

            _, label, src_path, src_loc, pagerank, degree, content = meta
            dist = vinfo["dist"]
            seed_id = vinfo["seed"]
            seed_bm25 = seed_scores.get(seed_id, 1.0)

            # Direct BM25 score if this node was an explicit seed hit
            direct_bm25 = seed_scores.get(nid, 0.0)

            # Proximity discount factor:
            # 0 hops: 1.0
            # 1 hop:  0.4 * seed_bm25
            # 2 hops: 0.15 * seed_bm25
            decay = 1.0 if dist == 0 else (0.4 if dist == 1 else 0.15)
            proximity_score = decay * seed_bm25

            # Dynamic Personalized PageRank (PPR) bonus
            ppr_val = float(ppr_scores.get(nid, 0.0))
            ppr_bonus = ppr_val * 10.0

            # Static centrality bonus (scaled PageRank)
            pr = float(pagerank or 0.0)
            centrality_bonus = pr * 5.0

            final_score = direct_bm25 + proximity_score + ppr_bonus + centrality_bonus

            # Content preview (up to 300 characters)
            preview = content.strip()
            if len(preview) > 300:
                preview = preview[:297] + "..."

            candidates.append({
                "node_id": nid,
                "label": label,
                "score": round(final_score, 4),
                "text": preview,
                "source_path": src_path,
                "source_location": src_loc,
                "connected_via": vinfo["path"],
            })

        # Step 4: Sort descending by score and slice
        candidates.sort(key=lambda x: x["score"], reverse=True)
        res = candidates[:limit]

        if skeleton:
            from graph_fy.skeleton import get_symbol_code_from_file
            for item in res:
                spath = item.get("source_path")
                sloc = item.get("source_location")
                nid = item.get("node_id")
                if spath and sloc and Path(spath).is_file():
                    line_m = re.search(r"L?(\d+)", str(sloc))
                    line_num = int(line_m.group(1)) if line_m else 1
                    skel_code = get_symbol_code_from_file(spath, line_num, skeletonize=True, compact_docstrings=True)
                    if skel_code:
                        item["skeleton"] = skel_code

                # Bidirectional execution slicing: callers and callees
                if graph is not None and nid and nid in graph:
                    callers, callees = _find_callers_and_callees(graph, nid)
                    item["callers"] = callers
                    item["callees"] = callees

                # 1-hop type dependency bundling: attach interface/base-class skeletons
                if graph is not None and nid and nid in graph:
                    type_skels: list[dict[str, str]] = []
                    if hasattr(graph, "successors"):
                        succs = list(graph.successors(nid))
                    elif hasattr(graph, "neighbors"):
                        succs = list(graph.neighbors(nid))
                    else:
                        succs = []

                    for succ in succs[:3]:
                        ed = graph.get_edge_data(nid, succ) or {}
                        if isinstance(ed, dict) and 0 in ed:
                            ed = ed[0]
                        rel = str(ed.get("relation", "")).lower()
                        if rel in ("inherits", "implements", "extends", "type_of", "returns"):
                            succ_data = graph.nodes[succ]
                            succ_file = succ_data.get("source_file") or succ_data.get("definition_file")
                            succ_loc = succ_data.get("source_location") or succ_data.get("definition_location")
                            if succ_file and Path(succ_file).is_file():
                                lm = re.search(r"L?(\d+)", str(succ_loc or ""))
                                lnum = int(lm.group(1)) if lm else 1
                                t_skel = get_symbol_code_from_file(succ_file, lnum, skeletonize=True, compact_docstrings=True)
                                if t_skel:
                                    type_skels.append({
                                        "node_id": succ,
                                        "relation": rel,
                                        "skeleton": t_skel,
                                    })
                    if type_skels:
                        item["related_types"] = type_skels

        if terse or caveman:
            return format_query_results(res, terse=True, skeleton=skeleton)
        return res

    finally:
        conn.close()


def trace(
    G: nx.Graph,
    source: str,
    target: str,
    *,
    directed: bool = True,
) -> list[dict[str, Any]]:
    """Shortest-path traversal across graph nodes from source to target.

    Returns a list of edge dicts describing each hop in the path.
    """
    if source not in G:
        # Try matching by label
        matches = [n for n, d in G.nodes(data=True) if d.get("label", "").lower() == source.lower()]
        if matches:
            source = matches[0]
        else:
            return []
    if target not in G:
        matches = [n for n, d in G.nodes(data=True) if d.get("label", "").lower() == target.lower()]
        if matches:
            target = matches[0]
        else:
            return []

    try:
        graph_view = G if directed else G.to_undirected()
        path_nodes = nx.shortest_path(graph_view, source, target)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []

    result: list[dict[str, Any]] = []
    for i in range(len(path_nodes) - 1):
        u = path_nodes[i]
        v = path_nodes[i + 1]
        edata = G.get_edge_data(u, v) or {}
        if isinstance(edata, dict) and 0 in edata and not isinstance(edata.get("relation"), str):
            edata = edata[0]
        result.append({
            "from_node": u,
            "to_node": v,
            "relation": edata.get("relation", "relates_to"),
            "confidence": edata.get("confidence", "EXTRACTED"),
            "weight": edata.get("weight", 1.0),
            "source_file": edata.get("source_file", ""),
            "source_location": edata.get("source_location", ""),
        })
    return result


def explain(
    query_text: str,
    graph: nx.Graph | Path | str | None = None,
    db_path: Path | str | None = None,
    *,
    limit: int = 5,
    hops: int = 2,
    fuzzy: bool = True,
    terse: bool = False,
    caveman: bool = False,
) -> list[dict[str, Any]] | str:
    """Retrieve results with a detailed score breakdown (BM25 score, hop distance, centrality contribution)."""
    raw_results = query(query_text, graph=graph, db_path=db_path, limit=limit, hops=hops, fuzzy=fuzzy)
    if isinstance(raw_results, list):
        # Ensure score breakdown format for explainability
        for r in raw_results:
            hops_count = len(r.get("connected_via", [])) - 1 if r.get("connected_via") else 0
            r["hops"] = max(0, hops_count)
    if terse or caveman:
        return format_query_results(raw_results, terse=True)
    return raw_results


def format_compact_path(path: list[dict[str, Any]]) -> str:
    """Format a sequence of edge steps into compact path: [A] -(calls)-> [B] -(imports)-> [C]."""
    if not path:
        return ""
    start = path[0].get("from", "")
    parts = [f"[{start}]"]
    for step in path:
        rel = step.get("relation", "connected")
        to = step.get("to", "")
        parts.append(f"-({rel})-> [{to}]")
    return " ".join(parts)


def _format_source_location(item: dict[str, Any]) -> str:
    src_path = str(item.get("source_path") or item.get("source_file") or "").strip()
    src_loc = str(item.get("source_location") or "").strip()
    if src_path and src_loc:
        if src_loc.startswith(f"{src_path}:"):
            return src_loc
        return f"{src_path}:{src_loc}"
    return src_path or src_loc


def format_query_results(
    results: list[dict[str, Any]] | dict[str, Any] | str,
    *,
    terse: bool = False,
    caveman: bool = False,
    skeleton: bool = False,
) -> str:
    """Format query/retrieval results into dense fragments or standard JSON."""
    if not (terse or caveman):
        if isinstance(results, str):
            return results
        return json.dumps(results, indent=2)

    if isinstance(results, str):
        lines: list[str] = []
        for line in results.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("NODE "):
                m = re.match(r"^NODE\s+(.*?)\s+\[(.*)\]$", s)
                if m:
                    lbl = m.group(1).strip()
                    attrs_raw = m.group(2)
                    attrs = dict(re.findall(r"(\w+)=([^\s\]]+)", attrs_raw))
                    src = attrs.get("src", "")
                    loc = attrs.get("loc", "")
                    loc_str = f"{src}:{loc}" if src and loc else (src or loc)
                    lines.append(f"[{lbl}] {lbl}{' ' + loc_str if loc_str else ''}".strip())
                else:
                    lines.append(s)
            elif s.startswith("EDGE "):
                m = re.match(r"^EDGE\s+(\S+)\s+--(\w+)\s+\[.*?\]-->\s+(\S+)", s)
                if m:
                    u, rel, v = m.group(1), m.group(2), m.group(3)
                    lines.append(f"  {rel} -> {v}")
                    lines.append(f"  [{u}] -({rel})-> [{v}]")
                else:
                    lines.append(s)
        return "\n".join(lines)

    items: list[dict[str, Any]] = [results] if isinstance(results, dict) else list(results)
    if not items:
        return ""

    lines = []
    for item in items:
        nid = str(item.get("node_id") or item.get("id") or "").strip()
        label = str(item.get("label") or nid).strip()
        loc = _format_source_location(item)
        header = f"[{nid}] {label}{' ' + loc if loc else ''}".strip()
        lines.append(header)

        path = item.get("connected_via") or item.get("path") or []
        if isinstance(path, list) and path:
            for step in path:
                if isinstance(step, dict):
                    rel = step.get("relation", "connected")
                    to = step.get("to") or step.get("target", "")
                    if to:
                        lines.append(f"  {rel} -> {to}")
            compact_path = format_compact_path(path)
            if compact_path:
                lines.append(f"  {compact_path}")

        edges = item.get("edges") or item.get("connections") or []
        if isinstance(edges, list):
            for edge in edges:
                if isinstance(edge, dict):
                    rel = edge.get("relation", "connected")
                    tgt = edge.get("target") or edge.get("to", "")
                    if tgt:
                        lines.append(f"  {rel} -> {tgt}")
                elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
                    lines.append(f"  {edge[0]} -> {edge[1]}")

        if skeleton and item.get("callers"):
            lines.append(f"  [callers: {', '.join(item['callers'])}]")
        if skeleton and item.get("callees"):
            lines.append(f"  [callees: {', '.join(item['callees'])}]")

        if skeleton and item.get("skeleton"):
            lines.append("  ```")
            for sk_line in str(item["skeleton"]).splitlines():
                lines.append(f"  {sk_line}")
            lines.append("  ```")

        if skeleton and item.get("related_types"):
            for rt in item["related_types"]:
                rt_nid = rt.get("node_id", "")
                rt_rel = rt.get("relation", "")
                lines.append(f"  [type: {rt_rel} -> {rt_nid}]")
                lines.append("  ```")
                for rsk_line in str(rt.get("skeleton", "")).splitlines():
                    lines.append(f"  {rsk_line}")
                lines.append("  ```")

    return "\n".join(lines)


def retrieve(
    query_text: str,
    graph: nx.Graph | None = None,
    db_path: Path | str | None = None,
    *,
    limit: int = 10,
    hops: int = 2,
    graph_path: Path | str | None = None,
    synonyms: dict[str, str] | None = None,
    fuzzy: bool = True,
    fuzzy_min_results: int = 1,
    terse: bool = False,
    caveman: bool = False,
    skeleton: bool = False,
) -> list[dict[str, Any]] | str:
    """Execute GraphRAG retrieval with optional terse/caveman token conservation."""
    raw = query(
        query_text,
        graph=graph,
        db_path=db_path,
        limit=limit,
        hops=hops,
        graph_path=graph_path,
        synonyms=synonyms,
        fuzzy=fuzzy,
        fuzzy_min_results=fuzzy_min_results,
        skeleton=skeleton,
    )
    if terse or caveman:
        return format_query_results(raw, terse=True, skeleton=skeleton)
    return raw


def format_rag_prompt(
    results: list[dict[str, Any]],
    question: str,
    *,
    graph: nx.Graph | None = None,
    token_budget: int = 2000,
) -> str:
    """Format retrieval results into a compact, prompt-cache friendly RAG context block.

    Features:
      - Adaptive multi-tier elision ladder (Tier 1 full skeletons for top seeds, Tier 2 compact signatures,
        Tier 3 compact path:line citations when budget is nearly exhausted).
      - Bidirectional execution slicing (caller and callee bundling).
      - Prompt-cache friendly deterministic ordering (ordered by path and line number).
      - Stable prefix structure with isolated task suffix for maximum TTFT prompt caching.
    """
    if not results:
        return f"### Codebase Architecture & Context\n\n### Codebase Context for: {question}\n"

    from graph_fy.skeleton import compact_signature

    # 1. Execution slicing enrichment if graph provided
    if graph is not None:
        for item in results:
            nid = item.get("node_id")
            if nid and nid in graph:
                callers, callees = _find_callers_and_callees(graph, nid)
                if "callers" not in item:
                    item["callers"] = callers
                if "callees" not in item:
                    item["callees"] = callees

    # 2. Identify top seeds vs medium/periphery before sorting
    max_score = max((float(it.get("score", 0.0)) for it in results), default=1.0)
    if max_score <= 0.0:
        max_score = 1.0

    candidate_target_tiers: dict[str, int] = {}
    for idx, it in enumerate(results):
        nid = str(it.get("node_id") or f"cand_{idx}")
        score = float(it.get("score", 0.0))
        dist = len(it.get("connected_via") or [])
        if idx == 0 or score >= max_score * 0.85:
            candidate_target_tiers[nid] = 1
        elif dist > 1 or score < max_score * 0.4:
            candidate_target_tiers[nid] = 3
        else:
            candidate_target_tiers[nid] = 2

    # 3. Deterministically sort candidate blocks by (source_path, line_number, label)
    def _sort_key(it: dict[str, Any]) -> tuple[str, int, str]:
        spath = str(it.get("source_path") or it.get("source_file") or "").lower()
        sloc = str(it.get("source_location") or "")
        m = re.search(r"L?(\d+)", sloc)
        line_num = int(m.group(1)) if m else 0
        lbl = str(it.get("label") or "")
        return (spath, line_num, lbl)

    sorted_results = sorted(results, key=_sort_key)

    def _render_tier1(item: dict[str, Any]) -> str:
        label = str(item.get("label") or item.get("node_id") or "")
        loc = _format_source_location(item)
        score = item.get("score", 0.0)
        lines = [f"#### {label} ({loc}) [relevance: {score}]"]
        if item.get("skeleton"):
            lines.append("```")
            lines.append(str(item["skeleton"]))
            lines.append("```")
        elif item.get("text"):
            lines.append(f"> {item['text']}")

        if item.get("callers"):
            top_c = item["callers"][:6]
            c_extra = len(item["callers"]) - len(top_c)
            lines.append(f"- Callers: {', '.join(top_c)}{f' (+{c_extra} more)' if c_extra > 0 else ''}")
        if item.get("callees"):
            top_cal = item["callees"][:6]
            cal_extra = len(item["callees"]) - len(top_cal)
            lines.append(f"- Callees: {', '.join(top_cal)}{f' (+{cal_extra} more)' if cal_extra > 0 else ''}")

        if item.get("related_types"):
            for rt in item["related_types"]:
                rt_nid = rt.get("node_id", "")
                rt_rel = rt.get("relation", "")
                lines.append(f"- Related {rt_rel} -> `{rt_nid}`:")
                lines.append("```")
                lines.append(str(rt.get("skeleton", "")))
                lines.append("```")
        return "\n".join(lines) + "\n\n"

    def _render_tier2(item: dict[str, Any]) -> str:
        label = str(item.get("label") or item.get("node_id") or "")
        loc = _format_source_location(item)
        score = item.get("score", 0.0)
        lines = [f"#### {label} ({loc}) [relevance: {score}]"]
        raw_code = str(item.get("skeleton") or item.get("text") or label)
        sig = compact_signature(raw_code)
        if sig:
            lines.append("```")
            lines.append(sig)
            lines.append("```")

        if item.get("callers"):
            top_c = item["callers"][:4]
            c_extra = len(item["callers"]) - len(top_c)
            lines.append(f"- Callers: {', '.join(top_c)}{f' (+{c_extra} more)' if c_extra > 0 else ''}")
        if item.get("callees"):
            top_cal = item["callees"][:4]
            cal_extra = len(item["callees"]) - len(top_cal)
            lines.append(f"- Callees: {', '.join(top_cal)}{f' (+{cal_extra} more)' if cal_extra > 0 else ''}")

        if item.get("related_types"):
            for rt in item["related_types"]:
                rt_nid = rt.get("node_id", "")
                rt_rel = rt.get("relation", "")
                rt_sig = compact_signature(str(rt.get("skeleton", "")))
                lines.append(f"- Related {rt_rel} -> `{rt_nid}`: {rt_sig}")
        return "\n".join(lines) + "\n\n"

    def _render_tier3(item: dict[str, Any]) -> str:
        loc_str = _format_source_location(item)
        lbl = str(item.get("label") or item.get("node_id") or "")
        if not lbl.startswith("def ") and not lbl.startswith("class ") and "(" not in lbl:
            lbl = f"def {lbl}()"
        return f"{loc_str} :: {lbl}\n"

    prefix = "### Codebase Architecture & Context\n\n"
    suffix = f"\n### Codebase Context for: {question}\n"

    prefix_tokens = len(prefix) // 4
    suffix_tokens = len(suffix) // 4
    rem_tokens = max(token_budget - prefix_tokens - suffix_tokens, 0)

    blocks: list[str] = []

    for item in sorted_results:
        nid = str(item.get("node_id") or "")
        target_tier = candidate_target_tiers.get(nid, 2)

        # Budget nearly exhausted -> degrade to Tier 3
        if rem_tokens < max(token_budget * 0.15, 60):
            target_tier = 3

        chosen_block = None

        if target_tier == 1:
            t1_block = _render_tier1(item)
            t1_tokens = len(t1_block) // 4
            if t1_tokens <= rem_tokens:
                chosen_block = t1_block
                rem_tokens -= t1_tokens
            else:
                t2_block = _render_tier2(item)
                t2_tokens = len(t2_block) // 4
                if t2_tokens <= rem_tokens:
                    chosen_block = t2_block
                    rem_tokens -= t2_tokens
                else:
                    t3_block = _render_tier3(item)
                    t3_tokens = len(t3_block) // 4
                    if t3_tokens <= rem_tokens:
                        chosen_block = t3_block
                        rem_tokens -= t3_tokens

        elif target_tier == 2:
            t2_block = _render_tier2(item)
            t2_tokens = len(t2_block) // 4
            if t2_tokens <= rem_tokens:
                chosen_block = t2_block
                rem_tokens -= t2_tokens
            else:
                t3_block = _render_tier3(item)
                t3_tokens = len(t3_block) // 4
                if t3_tokens <= rem_tokens:
                    chosen_block = t3_block
                    rem_tokens -= t3_tokens

        else:
            t3_block = _render_tier3(item)
            t3_tokens = len(t3_block) // 4
            if t3_tokens <= rem_tokens:
                chosen_block = t3_block
                rem_tokens -= t3_tokens

        if chosen_block is not None:
            blocks.append(chosen_block)
        else:
            blocks.append("// [remaining context omitted to stay within token budget]\n")
            break

    return prefix + "".join(blocks) + suffix


_KEYWORDS_AND_BUILTINS = frozenset({
    # Python
    "def", "class", "return", "import", "from", "as", "if", "elif", "else",
    "for", "while", "try", "except", "finally", "with", "yield", "lambda",
    "pass", "break", "continue", "raise", "assert", "global", "nonlocal",
    "async", "await", "true", "false", "none", "self", "cls", "int", "str",
    "float", "bool", "list", "dict", "set", "tuple", "print", "len", "range",
    "isinstance", "issubclass", "hasattr", "getattr", "setattr", "delattr",
    # JS/TS
    "const", "let", "var", "function", "export", "default",
    "extends", "implements", "interface", "type", "new", "this",
    "throw", "catch", "switch", "case", "null", "undefined",
    "void", "any", "number", "string", "boolean", "console", "log",
})


def diff_context(
    graph: nx.Graph | None = None,
    base_ref: str = "HEAD",
    root_dir: str | Path = ".",
    token_budget: int = 2000,
    graph_path: Path | str | None = None,
) -> dict[str, Any]:
    """Retrieve git diff-scoped ego-network context for code review / PR analysis.

    1. Executes `git diff -U0 <base_ref>` to identify modified files and line ranges.
    2. Maps changed line ranges to graph nodes via source_file & source_location.
    3. Finds 1-hop callers, affected tests, and api routes.
    4. Packs node skeletons and impact summary into `token_budget`.
    """
    import subprocess
    root_path = Path(root_dir)

    diff_text = ""
    try:
        proc = subprocess.run(
            ["git", "diff", "-U0", base_ref],
            cwd=str(root_path),
            capture_output=True,
            text=True,
            check=False,
        )
        diff_text = proc.stdout or ""
    except Exception:
        diff_text = ""

    if not diff_text.strip() and base_ref == "HEAD":
        try:
            proc2 = subprocess.run(
                ["git", "diff", "-U0"],
                cwd=str(root_path),
                capture_output=True,
                text=True,
                check=False,
            )
            diff_text = proc2.stdout or ""
        except Exception:
            pass

    file_changes: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            if current_file not in file_changes:
                file_changes[current_file] = []
        elif line.startswith("@@ ") and current_file:
            m = re.search(r"@@\s+-[0-9]+(?:,[0-9]+)?\s+\+([0-9]+)(?:,([0-9]+))?\s+@@", line)
            if m:
                start_l = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                end_l = start_l + max(count, 1) - 1
                file_changes[current_file].append((start_l, end_l))

    if graph is None:
        if graph_path is not None:
            gp = Path(graph_path)
        elif (root_path / "graph_fy_out" / "graph.json").exists():
            gp = root_path / "graph_fy_out" / "graph.json"
        elif (root_path / "graph_fy_out" / "graph.json").exists():
            gp = root_path / "graph_fy_out" / "graph.json"
        else:
            gp = out_path("graph.json")
        if not gp.exists():
            return {
                "base_ref": base_ref,
                "modified_files": list(file_changes.keys()),
                "changed_nodes": [],
                "callers": [],
                "affected_tests": [],
                "endpoints": [],
                "prompt_context": "No knowledge graph found.",
            }
        try:
            from graph_fy.paths import load_node_link_graph
            graph = load_node_link_graph(gp)
        except Exception:
            return {
                "base_ref": base_ref,
                "modified_files": list(file_changes.keys()),
                "changed_nodes": [],
                "callers": [],
                "affected_tests": [],
                "endpoints": [],
                "prompt_context": "Error parsing knowledge graph.",
            }

    changed_nodes: list[str] = []
    norm_changes = {Path(p).as_posix(): ranges for p, ranges in file_changes.items()}

    file_to_nodes: dict[str, list[tuple[str, int | None]]] = {}
    for nid, data in graph.nodes(data=True):
        src_file = data.get("source_file") or data.get("source_path") or ""
        if not src_file:
            continue
        norm_src = Path(src_file).as_posix()
        for cf in norm_changes:
            if norm_src == cf or norm_src.endswith("/" + cf) or cf.endswith("/" + norm_src):
                src_loc = str(data.get("source_location") or "")
                lm = re.search(r"L?(\d+)", src_loc)
                line_num = int(lm.group(1)) if lm else None
                file_to_nodes.setdefault(cf, []).append((nid, line_num))
                break

    for cf, matched_ranges in norm_changes.items():
        nodes_in_file = file_to_nodes.get(cf, [])
        if not nodes_in_file:
            continue
        numbered = [(nid, lnum) for nid, lnum in nodes_in_file if lnum is not None]
        numbered.sort(key=lambda x: x[1])

        for idx, (nid, lnum) in enumerate(numbered):
            next_lnum = numbered[idx + 1][1] - 1 if idx + 1 < len(numbered) else 999999
            node_start = lnum
            node_end = next_lnum
            for start_l, end_l in matched_ranges:
                if max(node_start, start_l) <= min(node_end, end_l):
                    changed_nodes.append(nid)
                    break

        for nid, lnum in nodes_in_file:
            if lnum is None and nid not in changed_nodes:
                changed_nodes.append(nid)

    callers: list[str] = []
    affected_tests: list[str] = []
    endpoints: list[str] = []
    seen_callers: set[str] = set()
    seen_tests: set[str] = set()

    for cn in changed_nodes:
        c_list, _ = _find_callers_and_callees(graph, cn)
        for c in c_list:
            if c not in seen_callers and c not in changed_nodes:
                seen_callers.add(c)
                callers.append(c)

        for nb in (graph.neighbors(cn) if cn in graph else []):
            nb_data = graph.nodes[nb]
            nb_src = str(nb_data.get("source_file") or nb_data.get("source_path") or "")
            nb_lbl = str(nb_data.get("label") or nb)

            if ("test" in nb.lower() or "test" in nb_src.lower()) and nb not in seen_tests:
                seen_tests.add(nb)
                affected_tests.append(nb_lbl)

            ed = graph.get_edge_data(cn, nb) or {}
            if isinstance(ed, dict) and 0 in ed:
                ed = ed[0]
            rel = str(ed.get("relation") or "").lower()
            if rel in ("route", "endpoint", "api") or "route" in nb.lower():
                endpoints.append(nb_lbl)

    risk_tier = "LOW"
    risk_score = 0.0
    downstream_impact_count = 0
    try:
        from graph_fy.impact import compute_impact
        for cn in changed_nodes[:5]:
            imp = compute_impact(graph, cn, max_depth=3)
            r_score = float(imp.get("risk_score", 0.0))
            if r_score > risk_score:
                risk_score = r_score
                risk_tier = str(imp.get("risk_tier", "LOW"))
            downstream_impact_count += int(imp.get("downstream_count", 0))
            for at in imp.get("affected_tests", []):
                if at not in affected_tests:
                    affected_tests.append(at)
            for ap in imp.get("affected_apis", []):
                if ap not in endpoints:
                    endpoints.append(ap)
    except Exception:
        pass

    from graph_fy.skeleton import get_symbol_code_from_file, compact_signature

    co_changed_files: list[tuple[str, float]] = []
    if file_changes and root_path.is_dir():
        try:
            import subprocess
            log_proc = subprocess.run(
                ["git", "log", "--pretty=format:---COMMIT---", "--name-only", "-n", "50"],
                cwd=str(root_path),
                capture_output=True,
                text=True,
                check=False,
            )
            commits = log_proc.stdout.split("---COMMIT---")
            target_set = {Path(f).as_posix() for f in file_changes.keys()}
            co_counts: Counter[str] = Counter()
            target_commits = 0
            for c in commits:
                files = {Path(f.strip()).as_posix() for f in c.splitlines() if f.strip()}
                if target_set.intersection(files):
                    target_commits += 1
                    for f in files:
                        if f not in target_set:
                            co_counts[f] += 1
            if target_commits > 0:
                for f, count in co_counts.most_common(4):
                    freq = round(count / target_commits, 2)
                    if freq >= 0.2:
                        co_changed_files.append((f, freq))
        except Exception:
            pass

    lines: list[str] = [
        f"### Diff-Scoped Ego Context (base: {base_ref})",
        f"- Blast Radius Risk: {risk_tier} (Score: {risk_score:.2f}, {downstream_impact_count} downstream dependents)",
        f"- Modified files: {len(file_changes)} ({', '.join(file_changes.keys()) or 'none'})",
        f"- Directly touched symbols: {len(changed_nodes)}",
    ]
    if callers:
        lines.append(f"- Impacted callers (1-hop): {', '.join(callers[:10])}")
    if affected_tests:
        lines.append(f"- Potentially affected tests: {', '.join(affected_tests[:10])}")
    if endpoints:
        lines.append(f"- Exposed endpoints/routes: {', '.join(endpoints[:5])}")
    if co_changed_files:
        co_str = ", ".join(f"{f} ({int(freq*100)}% co-change)" for f, freq in co_changed_files)
        lines.append(f"- Historically co-changed files: {co_str}")

    char_budget = token_budget * 4
    current_chars = sum(len(l) + 1 for l in lines)

    if changed_nodes:
        lines.append("\n#### Changed Symbols:")
        for cn in changed_nodes:
            if current_chars >= char_budget:
                break
            c_data = graph.nodes[cn]
            spath = c_data.get("source_file") or c_data.get("source_path")
            sloc = c_data.get("source_location")
            lbl = c_data.get("label", cn)
            if spath:
                res_path = Path(spath) if Path(spath).is_absolute() else (root_path / spath)
                if res_path.is_file():
                    lm = re.search(r"L?(\d+)", str(sloc or ""))
                    lnum = int(lm.group(1)) if lm else 1
                    skel = get_symbol_code_from_file(res_path, lnum, skeletonize=True, compact_docstrings=True)
                    if skel:
                        block = f"// Symbol: {lbl} ({spath}:{sloc})\n{skel}\n"
                        if current_chars + len(block) <= char_budget:
                            lines.append(block)
                            current_chars += len(block)
                        else:
                            sig = compact_signature(skel)
                            block = f"// Symbol: {lbl} ({spath}:{sloc}) :: {sig}\n"
                            lines.append(block)
                            current_chars += len(block)
                            break

    return {
        "base_ref": base_ref,
        "risk_tier": risk_tier,
        "risk_score": round(risk_score, 2),
        "downstream_impact_count": downstream_impact_count,
        "modified_files": list(file_changes.keys()),
        "changed_nodes": changed_nodes,
        "callers": callers,
        "affected_tests": affected_tests,
        "endpoints": endpoints,
        "historically_co_changed": co_changed_files,
        "prompt_context": "\n".join(lines),
    }


def refine_query(
    code_draft: str,
    graph: nx.Graph | None = None,
    db_path: Path | str | None = None,
    *,
    root_dir: str | Path = ".",
    token_budget: int = 2000,
    graph_path: Path | str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Iterative feedback querying (RepoCoder pattern).

    Extracts referenced identifiers from an agent's incomplete code draft,
    queries FTS5 BM25 index, excludes draft-local symbols, and retrieves
    missing dependency skeletons and type signatures packed within token budget.
    """
    root_path = Path(root_dir)
    if graph_path is None:
        if (root_path / "graph_fy_out" / "graph.json").exists():
            graph_path = root_path / "graph_fy_out" / "graph.json"
        elif (root_path / "graph_fy_out" / "graph.json").exists():
            graph_path = root_path / "graph_fy_out" / "graph.json"
    if db_path is None:
        if (root_path / "graph_fy_out" / "index.db").exists():
            db_path = root_path / "graph_fy_out" / "index.db"
        elif (root_path / "graph_fy_out" / "index.db").exists():
            db_path = root_path / "graph_fy_out" / "index.db"

    if not code_draft or not code_draft.strip():
        return {
            "extracted_identifiers": [],
            "resolved_dependencies": [],
            "prompt_context": "",
        }

    defined_in_draft: set[str] = set()
    for m in re.finditer(r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", code_draft):
        defined_in_draft.add(m.group(1))
    for m in re.finditer(r"\b(?:function|class|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)", code_draft):
        defined_in_draft.add(m.group(1))
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", code_draft):
        defined_in_draft.add(m.group(1))

    all_raw_idents = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", code_draft)
    unresolved_idents: list[str] = []
    seen = set()
    for ident in all_raw_idents:
        if ident in defined_in_draft or ident.lower() in _KEYWORDS_AND_BUILTINS:
            continue
        if ident not in seen:
            seen.add(ident)
            unresolved_idents.append(ident)

    if not unresolved_idents:
        return {
            "extracted_identifiers": [],
            "resolved_dependencies": [],
            "prompt_context": "",
        }

    q_tokens: list[str] = []
    for uid in unresolved_idents[:20]:
        sub = split_subtokens(uid)
        q_tokens.extend(sub)
        q_tokens.append(uid)
    q_tokens = list(dict.fromkeys(q_tokens))
    query_str = " ".join(q_tokens)

    results = query(
        query_str,
        graph=graph,
        db_path=db_path,
        limit=limit,
        graph_path=graph_path,
        skeleton=True,
    )
    if isinstance(results, str):
        candidates = []
    else:
        candidates = results

    resolved: list[dict[str, Any]] = [
        c for c in candidates
        if c.get("label") not in defined_in_draft and c.get("node_id") not in defined_in_draft
    ]

    formatted_context = format_rag_prompt(resolved, token_budget=token_budget, question="Draft dependency resolution")

    return {
        "extracted_identifiers": unresolved_idents,
        "resolved_dependencies": resolved,
        "prompt_context": formatted_context,
    }


def task_context(
    task: str,
    graph: nx.Graph | None = None,
    db_path: Path | str | None = None,
    *,
    root_dir: str | Path = ".",
    token_budget: int = 1500,
    graph_path: Path | str | None = None,
    include_diagram: bool = True,
    limit: int = 6,
) -> dict[str, Any]:
    """Generate a holistic, deterministic pre-agent architectural briefing.

    Combines:
    - Target subsystem & architectural community identification
    - Compact Tier-1 AST skeletons with 1-line docstrings and type annotations
    - Bidirectional execution flow (callers/callees with compaction)
    - Downstream blast radius risk scoring & affected test targets
    - Historical git temporal co-changes
    - Mermaid architectural flow diagram
    - Token economy savings metric
    """
    root_path = Path(root_dir)
    if graph_path is None:
        if (root_path / "graph_fy_out" / "graph.json").exists():
            graph_path = root_path / "graph_fy_out" / "graph.json"
        elif (root_path / "graph_fy_out" / "graph.json").exists():
            graph_path = root_path / "graph_fy_out" / "graph.json"
    if db_path is None:
        if (root_path / "graph_fy_out" / "index.db").exists():
            db_path = root_path / "graph_fy_out" / "index.db"
        elif (root_path / "graph_fy_out" / "index.db").exists():
            db_path = root_path / "graph_fy_out" / "index.db"

    if graph is None and graph_path is not None and Path(graph_path).exists():
        try:
            from graph_fy.paths import load_node_link_graph
            graph = load_node_link_graph(graph_path)
        except Exception:
            graph = None

    if not task or not task.strip():
        return {
            "task": task,
            "subsystems": [],
            "symbols": [],
            "risk_tier": "LOW",
            "risk_score": 0.0,
            "affected_tests": [],
            "historically_co_changed": [],
            "architecture_diagram": "",
            "tokens_saved": 0,
            "prompt_context": "No task provided.",
        }

    # 1. Expand query terms using sub-tokens
    raw_terms = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]+\b", task)
    expanded_terms: list[str] = []
    for t in raw_terms:
        if t.lower() not in _KEYWORDS_AND_BUILTINS and len(t) > 1:
            expanded_terms.extend(split_subtokens(t))
            expanded_terms.append(t)
    query_str = " ".join(list(dict.fromkeys(expanded_terms))) if expanded_terms else task

    # 2. Retrieve candidate symbols with AST skeletons and boundary pruning
    res = query(
        query_str,
        graph=graph,
        db_path=db_path,
        limit=limit,
        graph_path=graph_path,
        skeleton=True,
        boundary_pruning=True,
    )
    candidates: list[dict[str, Any]] = res if isinstance(res, list) else []

    if not candidates:
        return {
            "task": task,
            "subsystems": [],
            "symbols": [],
            "risk_tier": "LOW",
            "risk_score": 0.0,
            "affected_tests": [],
            "historically_co_changed": [],
            "architecture_diagram": "",
            "tokens_saved": 0,
            "prompt_context": f"### graph_fy Pre-Agent Architectural Briefing\n**Task**: {task}\n*No relevant symbols found in knowledge graph.*",
        }

    # 3. Subsystem / Community Detection
    subsystems_map: Counter[str] = Counter()
    candidate_node_ids = [c["node_id"] for c in candidates if "node_id" in c]
    candidate_files: set[str] = set()

    if graph is not None:
        for nid in candidate_node_ids:
            if nid in graph:
                ndata = graph.nodes[nid]
                comm = ndata.get("community_name") or ndata.get("community")
                if comm is not None:
                    comm_str = f"Community {comm}" if isinstance(comm, (int, str)) and not str(comm).startswith("Community") else str(comm)
                    subsystems_map[comm_str] += 1
                f = ndata.get("source_file") or ndata.get("source_path")
                if f:
                    candidate_files.add(Path(f).as_posix())

    subsystems = [s for s, _ in subsystems_map.most_common(3)]
    subsystem_str = ", ".join(subsystems) if subsystems else "General Subsystem"

    # 4. Downstream Impact & Blast Radius
    risk_tier = "LOW"
    risk_score = 0.0
    downstream_impact_count = 0
    affected_tests: list[str] = []
    seen_tests: set[str] = set()

    if graph is not None:
        try:
            from graph_fy.impact import compute_impact
            for nid in candidate_node_ids[:3]:
                if nid in graph:
                    imp = compute_impact(graph, nid, max_depth=3)
                    r_score = float(imp.get("risk_score", 0.0))
                    if r_score > risk_score:
                        risk_score = r_score
                        risk_tier = str(imp.get("risk_tier", "LOW"))
                    downstream_impact_count += int(imp.get("downstream_count", 0))
                    for at in imp.get("affected_tests", []):
                        if at not in seen_tests:
                            seen_tests.add(at)
                            affected_tests.append(at)
        except Exception:
            pass

    # 5. Historical Co-Change Mining (Git)
    co_changed_files: list[tuple[str, float]] = []
    if candidate_files and root_path.is_dir():
        try:
            import subprocess
            log_proc = subprocess.run(
                ["git", "log", "--pretty=format:---COMMIT---", "--name-only", "-n", "50"],
                cwd=str(root_path),
                capture_output=True,
                text=True,
                check=False,
            )
            commits = log_proc.stdout.split("---COMMIT---")
            target_set = {Path(f).as_posix() for f in candidate_files}
            co_counts: Counter[str] = Counter()
            target_commits = 0
            for c in commits:
                files = {Path(f.strip()).as_posix() for f in c.splitlines() if f.strip()}
                if target_set.intersection(files):
                    target_commits += 1
                    for f in files:
                        if f not in target_set:
                            co_counts[f] += 1
            if target_commits > 0:
                for f, count in co_counts.most_common(4):
                    freq = round(count / target_commits, 2)
                    if freq >= 0.2:
                        co_changed_files.append((f, freq))
        except Exception:
            pass

    # 6. Architectural Flow Diagram (Mermaid)
    diagram_lines: list[str] = ["flowchart TD"]
    edge_tuples: list[tuple[str, str, str]] = []
    diagram_nodes: dict[str, str] = {}

    for c in candidates[:6]:
        nid = str(c.get("node_id", ""))
        lbl = str(c.get("label") or nid)
        safe_nid = re.sub(r"[^A-Za-z0-9_]", "_", nid)
        safe_lbl = lbl.replace('"', "'")
        diagram_nodes[safe_nid] = safe_lbl

        callers = c.get("callers", [])
        for clr in callers[:3]:
            safe_c = re.sub(r"[^A-Za-z0-9_]", "_", clr)
            diagram_nodes[safe_c] = clr.replace('"', "'")
            edge_tuples.append((safe_c, safe_nid, "calls"))

        callees = c.get("callees", [])
        for clee in callees[:3]:
            safe_e = re.sub(r"[^A-Za-z0-9_]", "_", clee)
            diagram_nodes[safe_e] = clee.replace('"', "'")
            edge_tuples.append((safe_nid, safe_e, "calls"))

    for snid, slbl in sorted(diagram_nodes.items())[:12]:
        diagram_lines.append(f'    {snid}["{slbl}"]')

    seen_edges: set[tuple[str, str]] = set()
    for u, v, rel in sorted(edge_tuples)[:16]:
        if (u, v) not in seen_edges and u in diagram_nodes and v in diagram_nodes:
            seen_edges.add((u, v))
            diagram_lines.append(f"    {u} --> {v}")

    mermaid_diagram = "\n".join(diagram_lines) if len(edge_tuples) > 0 else ""

    # 7. Token Economy Estimation
    total_raw_chars = 0
    for cf in candidate_files:
        p = Path(cf) if Path(cf).is_absolute() else (root_path / cf)
        if p.is_file():
            try:
                total_raw_chars += len(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    raw_tokens = max(1, total_raw_chars // 4)

    # 8. Assemble Prompt Context
    lines: list[str] = [
        "### graph_fy Pre-Agent Architectural Briefing",
        f"**Task**: {task}",
        f"**Target Subsystem**: {subsystem_str}",
        f"**Blast Radius Risk**: {risk_tier} (Risk Score: {risk_score:.2f}, {downstream_impact_count} downstream dependents)",
    ]
    if affected_tests:
        lines.append(f"**Verification Targets (Tests)**: {', '.join(sorted(affected_tests)[:6])}")
    if co_changed_files:
        co_str = ", ".join(f"`{f}` ({int(freq*100)}% co-change)" for f, freq in co_changed_files)
        lines.append(f"**Historically Co-Changed Files**: {co_str}")

    char_budget = token_budget * 4
    current_chars = sum(len(l) + 1 for l in lines)

    if include_diagram and mermaid_diagram:
        diag_block = f"\n#### Subsystem Architecture Diagram:\n```mermaid\n{mermaid_diagram}\n```\n"
        if current_chars + len(diag_block) <= char_budget:
            lines.append(diag_block)
            current_chars += len(diag_block)

    lines.append("\n#### Targeted Symbol Skeletons (Tier 1 AST):")
    from graph_fy.skeleton import compact_signature
    for c in candidates:
        if current_chars >= char_budget:
            break
        nid = c.get("node_id", "")
        lbl = c.get("label", "")
        spath = c.get("source_path", "")
        sloc = c.get("source_location", "")
        skel = c.get("skeleton", "")
        callers = c.get("callers", [])
        callees = c.get("callees", [])

        meta_parts = [f"// Symbol: {lbl} ({spath}:{sloc})"]
        if nid:
            meta_parts.append(f'// [On-Demand Body: get_symbol_implementation("{nid}")]')
        if callers:
            c_str = ", ".join(callers[:5]) + (f" (+{len(callers)-5} more)" if len(callers) > 5 else "")
            meta_parts.append(f"// Callers: {c_str}")
        if callees:
            meta_parts.append(f"// Callees: {', '.join(callees[:5])}")

        meta_hdr = "\n".join(meta_parts)
        if skel:
            if str(spath).lower().endswith((".md", ".txt", ".rst", ".doc", ".pdf")):
                dlines = [dl for dl in skel.splitlines() if dl.strip()]
                skel_preview = "\n".join(dlines[:3]) + ("\n..." if len(dlines) > 3 else "")
                block = f"{meta_hdr}\n{skel_preview}\n"
            else:
                block = f"{meta_hdr}\n{skel}\n"
            if current_chars + len(block) <= char_budget:
                lines.append(block)
                current_chars += len(block)
            else:
                sig = compact_signature(skel)
                block = f"{meta_hdr}\n{sig}\n"
                lines.append(block)
                current_chars += len(block)
                break
        elif spath and sloc:
            block = f"{meta_hdr}\n"
            lines.append(block)
            current_chars += len(block)

    prompt_context = "\n".join(lines)
    briefing_tokens = max(1, len(prompt_context) // 4)
    compression_ratio = round(raw_tokens / max(1, briefing_tokens), 1)

    return {
        "task": task,
        "subsystems": subsystems,
        "symbols": candidates,
        "risk_tier": risk_tier,
        "risk_score": round(risk_score, 2),
        "downstream_impact_count": downstream_impact_count,
        "affected_tests": affected_tests,
        "historically_co_changed": co_changed_files,
        "architecture_diagram": mermaid_diagram,
        "raw_tokens": raw_tokens,
        "briefing_tokens": briefing_tokens,
        "compression_ratio": compression_ratio,
        "prompt_context": prompt_context,
    }
