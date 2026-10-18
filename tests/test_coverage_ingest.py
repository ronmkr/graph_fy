"""tests/test_coverage_ingest.py — Tests for test-to-code coverage ingestion.

Validates Cobertura XML, LCOV, and SQLite .coverage parsing, AST node line coverage
decoration, and covered_by_test / covers bidirectional edge injection.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest
import networkx as nx

from graph_fy.coverage_ingest import (
    CoverageReport,
    FileCoverage,
    apply_coverage_to_graph,
    decode_numbits,
    find_coverage_file,
    ingest_coverage,
    parse_cobertura_xml,
    parse_coverage_file,
    parse_lcov,
    parse_python_coverage,
)


SAMPLE_COBERTURA_XML = """<?xml version="1.0" ?>
<coverage version="7.0" timestamp="1700000000">
    <packages>
        <package name="pkg">
            <classes>
                <class name="service.py" filename="src/service.py">
                    <lines>
                        <line number="10" hits="1"/>
                        <line number="11" hits="1"/>
                        <line number="12" hits="0"/>
                        <line number="13" hits="1"/>
                        <line number="20" hits="0"/>
                        <line number="21" hits="0"/>
                    </lines>
                </class>
            </classes>
        </package>
    </packages>
</coverage>
"""

SAMPLE_LCOV_INFO = """TN:test_process_data
SF:src/processor.ts
FN:5,processData
FNDA:2,processData
DA:5,2
DA:6,2
DA:7,0
DA:8,2
end_of_record
TN:test_calculate
SF:src/calculator.ts
DA:1,1
DA:2,1
end_of_record
"""


def test_parse_cobertura_xml(tmp_path: Path):
    cov_file = tmp_path / "coverage.xml"
    cov_file.write_text(SAMPLE_COBERTURA_XML, encoding="utf-8")

    report = parse_cobertura_xml(cov_file)
    assert "src/service.py" in report.files
    fc = report.files["src/service.py"]
    assert fc.total_executable_lines == 6
    assert fc.covered_lines == {10, 11, 13}
    assert fc.missing_lines == {12, 20, 21}
    assert fc.coverage_pct == 50.0
    assert report.overall_coverage_pct == 50.0


def test_parse_lcov(tmp_path: Path):
    lcov_file = tmp_path / "lcov.info"
    lcov_file.write_text(SAMPLE_LCOV_INFO, encoding="utf-8")

    report = parse_lcov(lcov_file)
    assert "src/processor.ts" in report.files
    proc = report.files["src/processor.ts"]
    assert proc.total_executable_lines == 4
    assert proc.covered_lines == {5, 6, 8}
    assert proc.missing_lines == {7}
    assert proc.coverage_pct == 75.0

    # Test provenance
    assert "test_process_data" in report.test_to_files
    assert "src/processor.ts" in report.test_to_files["test_process_data"]
    assert report.test_to_lines["test_process_data"]["src/processor.ts"] == {5, 6, 8}


def test_decode_numbits():
    # Byte 0: bit 1 (line 1), bit 3 (line 3) -> 10 (0b00001010)
    # Byte 1: bit 0 (line 8) -> 1 (0b00000001)
    raw = bytes([0b00001010, 0b00000001])
    lines = decode_numbits(raw)
    assert lines == {1, 3, 8}


def test_parse_python_coverage_sqlite(tmp_path: Path):
    cov_db = tmp_path / ".coverage"
    conn = sqlite3.connect(str(cov_db))
    cur = conn.cursor()

    cur.execute("CREATE TABLE file (id INTEGER PRIMARY KEY, path TEXT);")
    cur.execute("CREATE TABLE context (id INTEGER PRIMARY KEY, context TEXT);")
    cur.execute("CREATE TABLE file_data (file_id INTEGER, context_id INTEGER, numbits BLOB);")

    cur.execute("INSERT INTO file (id, path) VALUES (1, 'src/api.py');")
    cur.execute("INSERT INTO context (id, context) VALUES (10, 'tests.test_api.test_auth');")
    # line 5 and line 10
    numbits = bytes([0b00100000, 0b00000100])  # bit 5 of byte 0 (line 5), bit 2 of byte 1 (line 10)
    cur.execute("INSERT INTO file_data (file_id, context_id, numbits) VALUES (1, 10, ?);", (numbits,))
    conn.commit()
    conn.close()

    report = parse_python_coverage(cov_db)
    assert "src/api.py" in report.files
    assert report.files["src/api.py"].covered_lines == {5, 10}
    assert "tests.test_api.test_auth" in report.test_to_files
    assert "src/api.py" in report.test_to_files["tests.test_api.test_auth"]


def test_apply_coverage_to_graph():
    G = nx.DiGraph()

    # Function node
    G.add_node(
        "service_process",
        label="process",
        source_file="src/service.py",
        start_line=10,
        end_line=15,
    )
    # Another function in same file, unhit
    G.add_node(
        "service_cleanup",
        label="cleanup",
        source_file="src/service.py",
        start_line=20,
        end_line=25,
    )
    # Test node
    G.add_node(
        "test_service_process",
        label="test_process",
        source_file="tests/test_service.py",
        start_line=5,
        end_line=12,
    )

    report = CoverageReport()
    fc = FileCoverage(file_path="src/service.py")
    fc.total_executable_lines = 4
    fc.covered_lines = {10, 11, 13}
    fc.missing_lines = {12}
    report.files["src/service.py"] = fc

    report.test_to_lines["test_process"] = {"src/service.py": {10, 11, 13}}

    metrics = apply_coverage_to_graph(G, report, root_dir=".")
    assert metrics["files_covered"] == 1
    assert metrics["nodes_annotated"] >= 1

    # Check node attributes
    proc_data = G.nodes["service_process"]
    assert proc_data["coverage_pct"] == 75.0  # 3 covered out of 4 (10, 11, 12, 13)
    assert proc_data["covered_lines"] == [10, 11, 13]
    assert proc_data["missing_lines"] == [12]

    # Check bidirectional edges
    assert G.has_edge("test_service_process", "service_process")
    assert G.edges["test_service_process", "service_process"]["relation"] == "covers"

    assert G.has_edge("service_process", "test_service_process")
    assert G.edges["service_process", "test_service_process"]["relation"] == "covered_by_test"


def test_find_coverage_file_discovery(tmp_path: Path):
    assert find_coverage_file(tmp_path) is None

    cov = tmp_path / "coverage.xml"
    cov.write_text("<coverage/>", encoding="utf-8")
    assert find_coverage_file(tmp_path) == cov
