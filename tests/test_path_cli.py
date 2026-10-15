"""Regression tests for `graph_fy path` arrow direction (#849) and determinism +
honest edge labels (#2074)."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import networkx as nx
import pytest
from networkx.readwrite import json_graph
import graph_fy.__main__ as mainmod


def _write_graph(tmp_path):
    graph_data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "create_patch", "label": "createPatchHandler()",
             "source_file": "server/create-patch-handler.ts", "community": 0},
            {"id": "validate", "label": "validateSanitySession()",
             "source_file": "server/sanity-validate-session.ts", "community": 0},
        ],
        "links": [
            {"source": "create_patch", "target": "validate",
             "relation": "calls", "confidence": "EXTRACTED"},
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph_data))
    return p


def _run(monkeypatch, graph_path, src, tgt, capsys, *extra):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv",
        ["graph_fy", "path", src, tgt, "--graph", str(graph_path), *extra])
    mainmod.main()
    return capsys.readouterr().out


def test_forward_arrow(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, p, "createPatchHandler", "validateSanitySession", capsys)
    assert "Shortest path (1 hops):" in out
    assert "createPatchHandler() --calls [EXTRACTED]--> validateSanitySession()" in out


def test_reverse_arrow(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    # #2487: path is directed by default, so walking the stored edge backwards
    # needs the --undirected opt-out to exercise the reverse-arrow rendering.
    out = _run(monkeypatch, p, "validateSanitySession", "createPatchHandler", capsys,
               "--undirected")
    assert "Shortest path (1 hops):" in out
    assert "validateSanitySession() <--calls [EXTRACTED]-- createPatchHandler()" in out
    assert "validateSanitySession() --calls [EXTRACTED]--> createPatchHandler()" not in out


def _write_misranking_graph(tmp_path):
    """Graph where IDF scoring ranks a partial-token decoy above the full match.

    Query "Reject-everything judge": the decoy "Rejection Summary" prefix-matches
    the rare token "reject" and out-scores "Degenerate Reject-Everything Judge"
    (whose full-query tier never fires — the query is a token subset of the
    label, not a prefix). The filler nodes make "judge"/"everything" common so
    their IDF stays low. Decoy and target live in different components: resolving
    the source to the decoy yields a false "No path found".
    """
    nodes = [
        {"id": "target", "label": "Degenerate Reject-Everything Judge", "community": 0},
        {"id": "decoy", "label": "Rejection Summary", "community": 0},
    ]
    for i in range(30):
        nodes.append({"id": f"j{i}", "label": f"Judge Helper {i}", "community": 0})
        nodes.append({"id": f"e{i}", "label": f"Everything Widget {i}", "community": 0})
    graph_data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": nodes,
        "links": [
            {"source": "target", "target": "j0",
             "relation": "verified_by", "confidence": "EXTRACTED"},
            {"source": "decoy", "target": "e0",
             "relation": "mentions", "confidence": "EXTRACTED"},
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph_data))
    return p


def test_endpoint_prefers_full_token_match(monkeypatch, tmp_path, capsys):
    """A token-subset query resolves to the full-match node, not the IDF head."""
    p = _write_misranking_graph(tmp_path)
    out = _run(monkeypatch, p, "Reject-everything judge", "Judge Helper 0", capsys)
    assert "Shortest path (1 hops):" in out
    assert "Degenerate Reject-Everything Judge" in out
    assert "No path found" not in out


def test_endpoint_falls_back_to_score_head(monkeypatch, tmp_path, capsys):
    """No full-token candidate -> behavior identical to the old scored[0] pick."""
    p = _write_misranking_graph(tmp_path)
    # "Rejection judge" full-matches nothing ("rejection" only appears in the
    # decoy, "judge" never joins it), so the IDF head (the decoy) still wins,
    # and the disconnected components make that a "No path found" exit(0).
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv",
        ["graph_fy", "path", "Rejection judge", "Judge Helper 0", "--graph", str(p)])
    with pytest.raises(SystemExit) as exc_info:
        mainmod.main()
    assert exc_info.value.code == 0
    # #2487: path is directed by default, so the flagless no-path message is
    # now the directed one (the decoy/target components stay disconnected
    # either way — this test is about endpoint resolution, not direction).
    assert "No directed path found" in capsys.readouterr().out


# ── #2074: deterministic route + honest edge relation ────────────────────────

def _diamond_graph(tmp_path):
    """Two equal-length routes A->P->B and A->Q->B — a tie the traversal must
    resolve deterministically."""
    data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "Alpha", "source_file": "a.py"},
            {"id": "p", "label": "Pmid", "source_file": "p.py"},
            {"id": "q", "label": "Qmid", "source_file": "q.py"},
            {"id": "b", "label": "Beta", "source_file": "b.py"},
        ],
        "links": [
            {"source": "a", "target": "p", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "p", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "a", "target": "q", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "q", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(data))
    return p


def _arrow_line(stdout: str) -> str:
    return next((l.strip() for l in stdout.splitlines() if "-->" in l or "<--" in l), "")


def test_path_deterministic_across_hash_seeds(tmp_path):
    """#2074: the same graph must yield the same route regardless of
    PYTHONHASHSEED. pytest fixes the seed per process, so run out-of-process."""
    gp = _diamond_graph(tmp_path)
    routes = set()
    for seed in ("0", "1", "2", "3", "4", "5", "6", "7"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        r = subprocess.run(
            [sys.executable, "-m", "graph_fy", "path", "Alpha", "Beta", "--graph", str(gp)],
            capture_output=True, text=True, env=env, cwd=str(tmp_path),
        )
        assert r.returncode == 0, r.stderr
        routes.add(_arrow_line(r.stdout))
    assert len(routes) == 1, f"non-deterministic path across hash seeds: {routes}"
    # Canonical tie-break picks the lexicographically-smaller mid node (Pmid).
    assert "Pmid" in next(iter(routes))


def test_path_relation_matches_stored_edge_not_fabricated(monkeypatch, tmp_path, capsys):
    """#2074: the printed relation must be the edge's ACTUAL stored relation,
    never a hardcoded/fabricated `calls`."""
    data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "Alpha", "source_file": "a.py"},
            {"id": "b", "label": "Beta", "source_file": "b.py"},
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "references", "confidence": "INFERRED"},
        ],
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(data))
    out = _run(monkeypatch, gp, "Alpha", "Beta", capsys)
    assert "--references [INFERRED]-->" in out
    assert "calls" not in out


def test_path_relation_fallback_related_when_missing(monkeypatch, tmp_path, capsys):
    """#2074: an edge with no stored relation prints an honest 'related', not an
    empty '---->' arrow and not a fabricated relation."""
    data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "Alpha", "source_file": "a.py"},
            {"id": "b", "label": "Beta", "source_file": "b.py"},
        ],
        "links": [{"source": "a", "target": "b"}],
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(data))
    out = _run(monkeypatch, gp, "Alpha", "Beta", capsys)
    assert "--related-->" in out
    assert "---->" not in out.replace("--related-->", "")


# ── #2309: hop direction must honor _src/_tgt markers, not stored arc order ──

def _flipped_marker_graph(tmp_path):
    """3-node chain where the middle link is PERSISTED in flipped endpoint
    order (source/target swapped) but carries its direction truth in the
    per-link _src/_tgt markers — the shape produced by pre-#563 graphs, raw
    node_link_data dumps, and undirected-storage canonicalization."""
    data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "ingest", "label": "ingest.ts", "source_file": "src/ingest.ts"},
            {"id": "logger", "label": "logger.ts", "source_file": "src/logger.ts"},
            {"id": "draft", "label": "draft-generator.ts",
             "source_file": "src/draft-generator.ts"},
        ],
        "links": [
            # Canonical order + matching markers.
            {"source": "ingest", "target": "logger",
             "_src": "ingest", "_tgt": "logger",
             "relation": "calls", "confidence": "EXTRACTED"},
            # FLIPPED persisted order; truth is draft --imports_from--> logger.
            {"source": "logger", "target": "draft",
             "_src": "draft", "_tgt": "logger",
             "relation": "imports_from", "confidence": "EXTRACTED"},
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(data))
    return p


def test_path_direction_recovered_from_src_tgt_markers(monkeypatch, tmp_path, capsys):
    """#2309: a hop over a link stored in flipped order must render the TRUE
    direction from its _src/_tgt markers, not the persisted arc order."""
    p = _flipped_marker_graph(tmp_path)
    # #2487: the two hops point in opposite TRUE directions (ingest->logger,
    # draft->logger), so this mixed-direction route only exists undirected.
    out = _run(monkeypatch, p, "ingest", "draft-generator", capsys, "--undirected")
    assert "Shortest path (2 hops):" in out
    assert "ingest.ts --calls [EXTRACTED]--> logger.ts" in out
    # True direction is draft -> logger, so the logger->draft hop is reversed.
    assert "logger.ts <--imports_from [EXTRACTED]-- draft-generator.ts" in out
    assert "--imports_from [EXTRACTED]-->" not in out


def test_path_canonical_marker_graph_still_forward(monkeypatch, tmp_path, capsys):
    """#2309 control: a to_json-shaped graph whose markers AGREE with the
    persisted source/target order keeps rendering forward (no regression)."""
    data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "Alpha", "source_file": "a.py"},
            {"id": "b", "label": "Beta", "source_file": "b.py"},
        ],
        "links": [
            {"source": "a", "target": "b", "_src": "a", "_tgt": "b",
             "relation": "calls", "confidence": "EXTRACTED"},
        ],
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(data))
    out = _run(monkeypatch, gp, "Alpha", "Beta", capsys)
    assert "Alpha --calls [EXTRACTED]--> Beta" in out
    # And walking the same edge backwards still reverses the arrow (#2487:
    # backwards traversal now requires the --undirected opt-out).
    out = _run(monkeypatch, gp, "Beta", "Alpha", capsys, "--undirected")
    assert "Beta <--calls [EXTRACTED]-- Alpha" in out


def test_explain_direction_recovered_from_src_tgt_markers(monkeypatch, tmp_path, capsys):
    """#2309: explain's in/out classification must honor _src markers — an
    edge persisted as hub->spoke but truly spoke->hub is an IN edge of hub."""
    data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "hub", "label": "hub.ts", "source_file": "src/hub.ts"},
            {"id": "spoke", "label": "spoke.ts", "source_file": "src/spoke.ts"},
        ],
        "links": [
            # Persisted arc hub->spoke, but the markers say spoke calls hub.
            {"source": "hub", "target": "spoke",
             "_src": "spoke", "_tgt": "hub",
             "relation": "calls", "confidence": "EXTRACTED"},
        ],
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(data))
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv",
        ["graph_fy", "explain", "hub", "--graph", str(gp)])
    mainmod.main()
    out = capsys.readouterr().out
    assert "<-- spoke.ts [calls]" in out
    assert "--> spoke.ts" not in out


def test_semantic_weighting_prefers_calls_over_containment(monkeypatch, tmp_path, capsys):
    """Semantic impedance: a longer call chain (3 hops of calls=3.0) must beat
    a 2-hop AST containment shortcut (2 hops of contains=7.0)."""
    data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "source_fn", "label": "sourceFn()", "source_file": "src/app.ts"},
            {"id": "file_node", "label": "app.ts", "source_file": "src/app.ts"},
            {"id": "mid1", "label": "midOne()", "source_file": "src/mid.ts"},
            {"id": "mid2", "label": "midTwo()", "source_file": "src/mid.ts"},
            {"id": "target_fn", "label": "targetFn()", "source_file": "src/app.ts"},
        ],
        "links": [
            # 2-hop containment shortcut (2 hops, weight 3.5 + 3.5 = 7.0)
            {"source": "file_node", "target": "source_fn", "_src": "file_node", "_tgt": "source_fn",
             "relation": "contains", "confidence": "EXTRACTED"},
            {"source": "file_node", "target": "target_fn", "_src": "file_node", "_tgt": "target_fn",
             "relation": "contains", "confidence": "EXTRACTED"},
            # 3-hop execution call chain (3 hops, weight 1.0 + 1.0 + 1.0 = 3.0)
            {"source": "source_fn", "target": "mid1", "_src": "source_fn", "_tgt": "mid1",
             "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "mid1", "target": "mid2", "_src": "mid1", "_tgt": "mid2",
             "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "mid2", "target": "target_fn", "_src": "mid2", "_tgt": "target_fn",
             "relation": "calls", "confidence": "EXTRACTED"},
        ],
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(data))

    # Default weighted: chooses the 3-hop execution path over the 2-hop containment shortcut
    out_weighted = _run(monkeypatch, gp, "sourceFn", "targetFn", capsys, "--undirected")
    assert "Shortest path (3 hops):" in out_weighted
    assert "midOne" in out_weighted
    assert "midTwo" in out_weighted
    assert "app.ts" not in out_weighted

    # Explicit --unweighted: takes the 2-hop containment shortcut
    out_unweighted = _run(monkeypatch, gp, "sourceFn", "targetFn", capsys, "--undirected", "--unweighted")
    assert "Shortest path (2 hops):" in out_unweighted
    assert "app.ts" in out_unweighted


def test_canonical_search_graph_caching(tmp_path):
    """G.graph['_canonical_search_views'] caches the materialized view across queries."""
    from graph_fy.retrieval import get_canonical_search_graph
    data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "A"},
            {"id": "b", "label": "B"},
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "calls"},
        ],
    }
    G = nx.node_link_graph(data, edges="links")
    assert "_canonical_search_views" not in G.graph

    v1 = get_canonical_search_graph(G, undirected=False, weighted=True)
    assert "_canonical_search_views" in G.graph
    v2 = get_canonical_search_graph(G, undirected=False, weighted=True)
    assert v1 is v2  # Identity check: zero-realloc cache hit


def test_path_flags_before_positional(monkeypatch, tmp_path, capsys):
    """Flags like --undirected or --graph can appear before positional arguments."""
    p = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graph_fy", "path", "--undirected", "--graph", str(p), "validateSanitySession", "createPatchHandler"],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "Shortest path (1 hops):" in out
    assert "validateSanitySession()" in out
    assert "createPatchHandler()" in out


def test_explain_flags_before_positional(monkeypatch, tmp_path, capsys):
    """Flags like --terse or --graph can appear before positional node label in explain."""
    p = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        ["graph_fy", "explain", "--terse", "--graph", str(p), "createPatchHandler"],
    )
    mainmod.main()
    out = capsys.readouterr().out
    assert "createPatchHandler" in out
    assert "calls -> validate" in out

