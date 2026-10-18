"""Test automatic ingestion of SCIP compiler index during build."""
from __future__ import annotations

import json
from pathlib import Path
from graph_fy.commands.build import cmd_build


def test_build_auto_ingests_scip_index(tmp_path: Path) -> None:
    # Create sample source file
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "service.py"
    py_file.write_text("class PaymentService:\n    def process(self):\n        pass\n")

    # Create SCIP index (compiler ground truth)
    scip_file = tmp_path / "index.scip.json"
    scip_doc = {
        "documents": [
            {
                "relative_path": "src/service.py",
                "language": "python",
                "symbols": [
                    {
                        "symbol": "python/src/service.py:PaymentService#",
                        "kind": "class",
                        "display_name": "PaymentService",
                        "documentation": ["Enterprise payment processing service"],
                        "relationships": [
                            {
                                "symbol": "python/src/service.py:PaymentGateway#",
                                "is_reference": True,
                            }
                        ],
                        "occurrences": [
                            {"range": [1, 0, 1, 14], "symbol": "python/src/service.py:PaymentService#"}
                        ],
                    }
                ],
            }
        ]
    }
    scip_file.write_text(json.dumps(scip_doc), encoding="utf-8")

    try:
        cmd_build(["graph_fy", "build", str(tmp_path), "--no-cluster"])
    except SystemExit as exc:
        assert exc.code == 0

    graph_file = tmp_path / "graph_fy_out" / "graph.json"
    assert graph_file.exists()

    data = json.loads(graph_file.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    # Verify that SCIP symbols were ingested
    scip_nodes = [n for n in nodes if n.get("metadata", {}).get("scip_symbol")]
    assert len(scip_nodes) >= 1
    assert any("PaymentService" in n.get("metadata", {}).get("scip_symbol", "") for n in scip_nodes)
