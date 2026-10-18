from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
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
    dangling_nodes = [n for n in nodes if out_degree[n] == 0]
    is_directed = G.is_directed()
    adj = {n: list(G.successors(n) if is_directed else G.neighbors(n)) for n in nodes}
    deg_items = [(n, out_degree[n], adj[n]) for n in nodes if out_degree[n] > 0]

    for _ in range(max_iter):
        xlast = x
        x = {n: 0.0 for n in nodes}
        danglesum = alpha * sum(xlast[n] for n in dangling_nodes)

        for n, deg, nbrs in deg_items:
            share = alpha * xlast[n] / deg
            for nbr in nbrs:
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
                inner_near = " ".join(
                    f'"{t}"' if t.upper() in {"AND", "OR", "NOT"} or " " in t else t
                    for t in clean_near
                )
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

    conn = sqlite3.connect(str(path), timeout=30.0)
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
