"""Search, scoring, TF-IDF, and seed picking for graph querying in graph_fy serve."""
from __future__ import annotations

import math
import re
import warnings
from array import array
from typing import NamedTuple

import networkx as nx
from graph_fy.export import _strip_diacritics

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba as _jieba  # type: ignore[import-untyped]
except (ImportError, SyntaxError):
    _jieba = None


def _get_serve_attr(name: str, default: Any = None) -> Any:
    import sys
    mod = sys.modules.get("graph_fy.serve")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def _search_tokens(text: str) -> list[str]:
    """Split text into word tokens, stripping punctuation and diacritics."""
    return re.findall(r"[^\W_]+", _strip_diacritics(str(text)).lower())


def _has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _segment_chinese(text: str) -> list[str]:
    """Segment Chinese text and keep the original term for exact matching."""
    jieba_mod = _get_serve_attr("_jieba", _jieba)
    if jieba_mod is not None:
        segments = [w for w in jieba_mod.cut(text) if len(w.strip()) > 0]
    else:
        segments = [text[i:i + 2] for i in range(len(text) - 1)] or [text]
    if len(text) > 1 and text not in segments:
        segments.append(text)
    return segments


def _is_searchable(term: str) -> bool:
    """True if term is Chinese, non-English, or an English word longer than 2 chars."""
    if all("a" <= ch <= "z" for ch in term):
        return len(term) > 2
    return True


_QUERY_STOPWORDS = frozenset({
    # English
    "how", "what", "why", "when", "where", "which", "who", "whom", "whose",
    "does", "did", "is", "are", "was", "were", "be", "been", "being",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "has", "have", "had", "the", "and", "but", "not", "for", "from", "with",
    "without", "into", "onto", "off", "that", "this", "these", "those", "there",
    "here", "all", "any", "both", "each", "few", "more", "most", "other",
    "some", "such", "than", "too", "very", "about", "above", "across",
    "after", "before", "behind", "below", "between", "during", "under",
    "until", "while", "work", "works", "working",
    # German
    "wie", "was", "warum", "wann", "wo", "woher", "wohin", "wer", "wen", "wem",
    "funktioniert", "funktionieren", "macht", "machen", "tut",
    "ist", "sind", "sei", "seien", "war", "waren", "wird", "werden", "wurde", "wurden",
    "kann", "können", "könnte", "könnten", "soll", "sollen", "sollte", "sollten",
    "muss", "müssen", "musste", "darf", "dürfen",
    "hat", "haben", "hatte", "hatten",
    "der", "die", "das", "des", "dem", "den", "ein", "eine", "einer", "eines", "einem", "einen",
    "und", "oder", "aber", "nicht", "für", "von", "vom", "mit", "ohne", "in", "im", "ins",
    "auf", "aus", "bei", "beim", "nach", "zu", "zum", "zur", "über", "unter", "vor", "zwischen",
    "dies", "diese", "dieser", "dieses", "diesem", "diesen",
    "das", "dass", "da", "dort", "hier",
    "alle", "aller", "allen", "alles", "jede", "jeder", "jedes",
    # French
    "pourquoi", "quand", "quel", "quelle", "quels", "quelles", "qui",
    "fonctionne", "fonctionnent", "marche", "font",
    "est", "sont", "ete", "etait", "etaient", "etre",
    "peut", "peuvent", "doit", "doivent",
    "avec", "sans", "dans", "sur", "sous",
    "cette", "cet", "ces",
    # Spanish
    "por", "que", "como", "cuando", "donde", "quien", "quienes",
    "cual", "cuales", "funciona", "funcionan", "hace", "hacen",
    "esta", "estan", "este", "estos", "estas",
    "para", "sobre", "entre", "hasta",
    # Italian
    "perché", "cosa", "quale", "quali", "dove", "funziona", "sono", "che",
    "della",
})


def _query_terms(question: str) -> list[str]:
    """Split a query into searchable terms, segmenting Chinese text, then drop
    question/filler words (`_QUERY_STOPWORDS`)."""
    terms: list[str] = []
    for raw in question.split():
        if _has_chinese(raw):
            for seg in _segment_chinese(raw.lower().strip()):
                seg = seg.strip()
                if seg and _is_searchable(seg):
                    terms.append(seg)
        else:
            for tok in re.findall(r"\w+", raw.lower()):
                if _is_searchable(tok):
                    terms.append(tok)
    content = [t for t in terms if t not in _QUERY_STOPWORDS]
    return content or terms


_EXACT_MATCH_BONUS = 1000.0
_PREFIX_MATCH_BONUS = 100.0
_SUBSTRING_MATCH_BONUS = 1.0
_SOURCE_MATCH_BONUS = 0.5
_RATIONALE_MATCH_BONUS = 0.75
_ATTRIBUTE_MATCH_BONUS = 0.75


def _flatten_attr_text(val, parts: list[str]) -> None:
    if isinstance(val, dict):
        for k, v in val.items():
            parts.append(str(k))
            _flatten_attr_text(v, parts)
    elif isinstance(val, (list, tuple)):
        for item in val:
            _flatten_attr_text(item, parts)
    elif val is not None:
        parts.append(str(val))


def _node_attributes_text(data: dict) -> tuple[str, str]:
    """The node's `attributes` key-value pairs normalized and tokenized for search."""
    attrs = data.get("attributes")
    if not isinstance(attrs, dict) or not attrs:
        return "", ""
    parts: list[str] = []
    _flatten_attr_text(attrs, parts)
    if not parts:
        return "", ""
    raw_text = " ".join(parts)
    norm = _strip_diacritics(raw_text).lower()
    tokens = " ".join(_search_tokens(norm))
    return norm, tokens


def _compute_idf(G: nx.Graph, terms: list[str]) -> dict[str, float]:
    """IDF weights for query terms, cached in G.graph['_idf_cache']."""
    cache: dict[str, float] = G.graph.setdefault("_idf_cache", {})
    N = G.number_of_nodes() or 1
    uncached = [t for t in terms if t not in cache]
    if uncached:
        df: dict[str, int] = {t: 0 for t in uncached}
        for _, data in G.nodes(data=True):
            norm_label = (
                data.get("norm_label") or _strip_diacritics(data.get("label") or "")
            ).lower()
            for t in uncached:
                if t in norm_label:
                    df[t] += 1
        for t in uncached:
            cache[t] = math.log(1 + N / (1 + df[t]))
    return {t: cache.get(t, math.log(1 + N)) for t in terms}


def _trigrams(text: str) -> set[str]:
    """Character trigrams of `text`; for <3-char text the whole string is the key."""
    if len(text) < 3:
        return {text} if text else set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def _node_rationale_text(data: dict) -> str:
    """The node's `rationale` attribute normalized like a label for substring matching."""
    raw = data.get("rationale")
    if not raw:
        return ""
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(part) for part in raw if part)
    return _strip_diacritics(str(raw)).lower()


def _node_search_text(data: dict, nid: str) -> str:
    """Concatenate every field _score_nodes / _find_node match a query against."""
    norm_label = data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()
    label_tokens = " ".join(_search_tokens(data.get("label") or ""))
    source = (data.get("source_file") or "").lower()
    source_tokens = " ".join(_search_tokens(data.get("source_file") or ""))
    nid_text = str(nid).lower()
    fields = (norm_label, label_tokens, nid_text, source, source_tokens)
    if not nid_text.isascii():
        nid_folded = _strip_diacritics(str(nid)).lower()
        if nid_folded != nid_text:
            fields += (nid_folded,)
    rationale = _node_rationale_text(data)
    if rationale:
        fields += (rationale,)
    attr_norm, attr_tokens = _node_attributes_text(data)
    if attr_norm:
        fields += (attr_norm, attr_tokens)
    return "\x00".join(fields)


def _get_trigram_index(G: nx.Graph) -> dict:
    """Lazily build and cache a trigram -> node-position postings map on the graph."""
    idx = G.graph.get("_trigram_index")
    if idx is not None:
        return idx
    ids = list(G.nodes())
    postings: dict[str, array] = {}
    for i, nid in enumerate(ids):
        for g in _trigrams(_node_search_text(G.nodes[nid], nid)):
            bucket = postings.get(g)
            if bucket is None:
                bucket = array("i")
                postings[g] = bucket
            bucket.append(i)
    idx = {"ids": ids, "postings": postings, "set_cache": {}}
    G.graph["_trigram_index"] = idx
    return idx


def _trigram_candidates(G: nx.Graph, needles: list[str], *, guard_frac: float = 0.10) -> list[str] | None:
    """Node IDs whose text could contain any `needle` as a substring, via the trigram index."""
    idx = _get_trigram_index(G)
    ids, postings, set_cache = idx["ids"], idx["postings"], idx["set_cache"]
    n = len(ids)
    if n == 0:
        return []
    needles = [s for s in needles if s]
    thresh = int(n * guard_frac)
    for s in needles:
        tgs = _trigrams(s)
        if not tgs or any(len(g) < 3 for g in tgs):
            return None
        present = [len(postings[g]) for g in tgs if g in postings]
        if not present:
            continue
        if min(present) > thresh:
            return None
    cand: set[int] = set()
    for s in needles:
        sets: list[set] | None = []
        for g in _trigrams(s):
            bucket = postings.get(g)
            if bucket is None:
                sets = None
                break
            cached = set_cache.get(g)
            if cached is None:
                cached = set(bucket)
                set_cache[g] = cached
            sets.append(cached)
        if not sets:
            continue
        sets.sort(key=len)
        hit = set(sets[0])
        for other in sets[1:]:
            hit &= other
            if not hit:
                break
        cand |= hit
    return [ids[i] for i in sorted(cand)]


class _QueryScores(NamedTuple):
    ranked: list[tuple[float, str]]
    best_seed_by_term: dict[str, str]


def _score_nodes(G: nx.Graph, terms: list[str]) -> list[tuple[float, str]]:
    """Combined query scorer returning the existing ranked `(score, node_id)` list."""
    sq = _get_serve_attr("_score_query", _score_query)
    return sq(G, terms, collect_per_term_seeds=False).ranked


def _score_query(
    G: nx.Graph, terms: list[str], *, collect_per_term_seeds: bool
) -> _QueryScores:
    """Single-pass combined scorer that optionally also records the best seed
    for each normalized query token."""
    scored: list[tuple[float, str]] = []
    norm_terms = list(dict.fromkeys(tok for t in terms for tok in _search_tokens(t)))
    n_terms = len(norm_terms)
    idf = _compute_idf(G, norm_terms)
    joined = " ".join(norm_terms)
    joined_w = max((idf.get(t, 1.0) for t in norm_terms), default=1.0)
    tc = _get_serve_attr("_trigram_candidates", _trigram_candidates)
    candidate_ids = tc(G, norm_terms + ([joined] if joined else []))
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    best_by_term: dict[str, tuple[tuple, str]] | None = (
        {} if collect_per_term_seeds else None
    )
    for nid, data in node_iter:
        norm_label = data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        label_tokens = " ".join(_search_tokens(data.get("label") or ""))
        source = (data.get("source_file") or "").lower()
        rationale = _node_rationale_text(data)
        attr_norm, attr_tokens = _node_attributes_text(data)
        nid_lower = nid.lower() if (joined or collect_per_term_seeds) else ""
        score = 0.0
        if joined:
            if joined in (norm_label, bare_label, label_tokens, nid_lower):
                score += _EXACT_MATCH_BONUS * 10 * joined_w
            elif (
                norm_label.startswith(joined)
                or bare_label.startswith(joined)
                or label_tokens.startswith(joined)
            ):
                score += _PREFIX_MATCH_BONUS * 10 * joined_w
        matched = 0
        tiered = 0.0
        for t in norm_terms:
            w = idf.get(t, 1.0)
            tier_value = 0.0
            substr_value = 0.0
            source_value = 0.0
            if t == norm_label or t == bare_label:
                tier_value = _EXACT_MATCH_BONUS * w
                matched += 1
            elif norm_label.startswith(t) or bare_label.startswith(t):
                tier_value = _PREFIX_MATCH_BONUS * w
                matched += 1
            elif t in norm_label:
                substr_value = _SUBSTRING_MATCH_BONUS * w
                score += substr_value
                matched += 1
            if t in source:
                source_value = _SOURCE_MATCH_BONUS * w
                score += source_value
            rationale_value = 0.0
            if rationale and t in rationale:
                rationale_value = _RATIONALE_MATCH_BONUS * w
                score += rationale_value
            attr_value = 0.0
            if attr_norm and (t in attr_norm or t in attr_tokens):
                attr_value = _ATTRIBUTE_MATCH_BONUS * w
                score += attr_value
            tiered += tier_value
            if collect_per_term_seeds and best_by_term is not None:
                if t in (norm_label, bare_label, label_tokens, nid_lower):
                    singleton = _EXACT_MATCH_BONUS * 10 * w
                elif (
                    norm_label.startswith(t)
                    or bare_label.startswith(t)
                    or label_tokens.startswith(t)
                ):
                    singleton = _PREFIX_MATCH_BONUS * 10 * w
                else:
                    singleton = 0.0
                singleton += tier_value + substr_value + source_value + rationale_value + attr_value
                if singleton > 0:
                    key = (-singleton, -G.degree(nid), len(data.get("label") or nid), nid)
                    cur = best_by_term.get(t)
                    if cur is None or key < cur[0]:
                        best_by_term[t] = (key, nid)
        if tiered:
            score += tiered * (matched / n_terms) ** 2
        if score > 0:
            scored.append((score, nid))
    scored.sort(key=lambda s: (-s[0], len(G.nodes[s[1]].get("label") or s[1]), s[1]))
    best_seed_by_term: dict[str, str] = {}
    if collect_per_term_seeds and best_by_term:
        best_seed_by_term = {t: nid for t, (_key, nid) in best_by_term.items()}
    return _QueryScores(ranked=scored, best_seed_by_term=best_seed_by_term)


def _pick_scored_endpoint(G: nx.Graph, scored: list[tuple[float, str]], query: str) -> str:
    """Pick a path endpoint from a _score_nodes result, preferring full-token matches."""
    qtokens = set(_search_tokens(query))
    if not qtokens:
        return scored[0][1]
    for _score, nid in scored:
        if qtokens <= set(_search_tokens(G.nodes[nid].get("label") or nid)):
            return nid
    return scored[0][1]


def _pick_seeds(
    scored: list[tuple[float, str]],
    max_k: int = 3,
    gap_ratio: float = 0.2,
    *,
    G: nx.Graph | None = None,
    best_seed_by_term: dict[str, str] | None = None,
) -> list[str]:
    """Select BFS seed nodes, stopping when score drops too far below the top."""
    if not scored:
        return []

    def _seed_label_key(nid: str) -> str:
        if G is None:
            return nid
        data = G.nodes[nid]
        return (data.get("norm_label")
                or _strip_diacritics(data.get("label") or "").lower()) or nid

    top_score = scored[0][0]
    seeds: list[str] = []
    seen_labels: set[str] = set()
    for score, nid in scored:
        if len(seeds) >= max_k:
            break
        if seeds and score < top_score * gap_ratio:
            break
        key = _seed_label_key(nid)
        if key in seen_labels:
            continue
        seen_labels.add(key)
        seeds.append(nid)

    if G is not None and best_seed_by_term:
        for term in sorted(best_seed_by_term):
            best_nid = best_seed_by_term[term]
            key = _seed_label_key(best_nid)
            if best_nid not in seeds and key not in seen_labels:
                seen_labels.add(key)
                seeds.append(best_nid)
    return seeds


_RELATIONAL_INTENT_TERMS: frozenset[str] = frozenset({
    "call", "calls", "called", "caller", "callers",
    "invoke", "invokes", "invoked",
    "use", "uses", "used", "using",
    "import", "imports", "imported",
    "export", "exports", "exported",
    "extend", "extends", "extended",
    "implement", "implements", "implemented",
    "depend", "depends",
    "reference", "references", "referenced",
})
