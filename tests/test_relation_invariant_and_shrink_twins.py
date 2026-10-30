"""Tests for edge relation invariant enforcement and label-only AST file twins in shrink guard (Issue #75)."""
from __future__ import annotations

import json
from pathlib import Path

from graph_fy.build import build_from_json, _lost_saved_semantic_nodes
from graph_fy.export import to_json


def test_build_drops_edge_still_missing_relation(capsys):
    """#4809: after aliases are folded, an edge with no relation must not enter the graph."""
    ext = {
        "nodes": [
            {"id": "n1", "label": "A", "file_type": "document", "source_file": "a.md"},
            {"id": "n2", "label": "B", "file_type": "document", "source_file": "b.md"},
        ],
        "edges": [
            {"source": "n1", "target": "n2", "confidence": "INFERRED", "source_file": "a.md"},
        ],
        "input_tokens": 0,
        "output_tokens": 0,
    }

    G = build_from_json(ext)
    assert not G.has_edge("n1", "n2")
    err = capsys.readouterr().err
    assert "dropped 1 schema-invalid edge(s)" in err
    assert "missing required field 'relation'" in err


def _mention(node_id: str, label: str, doc: str) -> dict:
    return {"id": node_id, "label": label, "file_type": "code", "source_file": doc, "_origin": "semantic"}


def _ast_file(node_id: str, sf: str) -> dict:
    return {
        "id": node_id,
        "label": sf.rsplit("/", 1)[-1],
        "file_type": "code",
        "source_file": sf,
        "source_location": "L1",
        "_origin": "ast",
    }


def test_file_mention_folded_into_ast_file_node_is_not_lost():
    saved = [_mention("readme_config", "src/config.php", "README.md")]
    new = [_ast_file("src_config", "src/config.php")]
    folded: dict[str, str] = {}
    assert _lost_saved_semantic_nodes(saved, new, None, folded) == []
    assert folded == {"readme_config": "src_config"}


def test_file_mention_matching_two_ast_files_is_still_lost():
    saved = [_mention("readme_index", "index.ts", "README.md")]
    new = [_ast_file("a_index", "a/index.ts"), _ast_file("b_index", "b/index.ts")]
    lost = _lost_saved_semantic_nodes(saved, new)
    assert [n["id"] for n in lost] == ["readme_index"]


def test_to_json_writes_recluster_that_folds_file_mention(tmp_path: Path):
    out = tmp_path / "graph.json"
    out.write_text(json.dumps({
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            _ast_file("src_config", "src/config.php"),
            _mention("readme_config", "src/config.php", "README.md"),
            {"id": "readme", "label": "README.md", "file_type": "document",
             "source_file": "README.md", "_origin": "semantic"},
        ],
        "links": [{"source": "readme", "target": "readme_config", "relation": "describes",
                   "confidence": "EXTRACTED", "source_file": "README.md"}],
    }), encoding="utf-8")

    G = build_from_json(json.loads(out.read_text(encoding="utf-8")))
    assert sorted(G.nodes) == ["readme", "src_config"]
    assert to_json(G, {}, str(out)) is True
