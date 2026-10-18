"""coverage_ingest.py — Ingest test coverage artifacts and map to AST function nodes.

Supports:
1. Cobertura XML (coverage.xml) — Python/Java/C#/JS
2. LCOV (lcov.info) — C/C++, Rust (cargo-tarpaulin/llvm-cov), JavaScript/TypeScript (Jest, Karma, Istanbul)
3. Python coverage.py SQLite database (.coverage) — Native Python coverage

Maps covered lines to AST functions/methods, calculates per-function line coverage
percentages, and attaches `covered_by_test` and `covers` edges where test-level
provenance is present.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable
import xml.etree.ElementTree as ET

import networkx as nx


@dataclass
class FileCoverage:
    """Line-level coverage data for a single source file."""
    file_path: str
    total_executable_lines: int = 0
    covered_lines: set[int] = field(default_factory=set)
    missing_lines: set[int] = field(default_factory=set)
    line_hits: dict[int, int] = field(default_factory=dict)
    # line -> set of test names that executed this line
    test_covers: dict[int, set[str]] = field(default_factory=dict)

    @property
    def coverage_pct(self) -> float:
        if not self.total_executable_lines:
            return 100.0 if not self.missing_lines else 0.0
        return round((len(self.covered_lines) / self.total_executable_lines) * 100.0, 1)


@dataclass
class CoverageReport:
    """Aggregated coverage report across all files."""
    files: dict[str, FileCoverage] = field(default_factory=dict)
    # test_name -> set of file paths
    test_to_files: dict[str, set[str]] = field(default_factory=dict)
    # test_name -> {file_path: set of covered lines}
    test_to_lines: dict[str, dict[str, set[int]]] = field(default_factory=dict)

    @property
    def total_lines(self) -> int:
        return sum(f.total_executable_lines for f in self.files.values())

    @property
    def total_covered(self) -> int:
        return sum(len(f.covered_lines) for f in self.files.values())

    @property
    def overall_coverage_pct(self) -> float:
        tot = self.total_lines
        if not tot:
            return 100.0
        return round((self.total_covered / tot) * 100.0, 1)


def decode_numbits(numbits: bytes) -> set[int]:
    """Decode coverage.py binary bitmap into a set of 1-indexed line numbers."""
    lines: set[int] = set()
    for byte_idx, byte_val in enumerate(numbits):
        if byte_val:
            for bit_idx in range(8):
                if (byte_val >> bit_idx) & 1:
                    line_num = byte_idx * 8 + bit_idx
                    if line_num > 0:
                        lines.add(line_num)
    return lines


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_cobertura_xml(path: Path | str, content: str | None = None) -> CoverageReport:
    """Parse Cobertura-format XML (coverage.xml)."""
    report = CoverageReport()
    root: ET.Element
    if content is not None:
        root = ET.fromstring(content)
    else:
        tree = ET.parse(str(path))
        root = tree.getroot()

    # Find all class elements representing source files
    for cls in root.iter("class"):
        fname = cls.get("filename") or cls.get("name") or ""
        if not fname:
            continue
        norm_path = Path(fname).as_posix()
        file_cov = report.files.setdefault(norm_path, FileCoverage(file_path=norm_path))

        lines_elem = cls.find("lines")
        if lines_elem is not None:
            for line in lines_elem.iter("line"):
                try:
                    lnum = int(line.get("number", "0"))
                    hits = int(line.get("hits", "0"))
                except ValueError:
                    continue
                if lnum <= 0:
                    continue
                file_cov.total_executable_lines += 1
                file_cov.line_hits[lnum] = hits
                if hits > 0:
                    file_cov.covered_lines.add(lnum)
                else:
                    file_cov.missing_lines.add(lnum)

    return report


def parse_lcov(path: Path | str, content: str | None = None) -> CoverageReport:
    """Parse LCOV-format tracefile (lcov.info)."""
    report = CoverageReport()
    lines_seq: Iterable[str]
    if content is not None:
        lines_seq = content.splitlines()
    else:
        with open(str(path), "r", encoding="utf-8", errors="replace") as f:
            lines_seq = f.readlines()

    current_file: FileCoverage | None = None
    current_test: str | None = None

    for raw_line in lines_seq:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("TN:"):
            # Test name
            tn = line[3:].strip()
            current_test = tn if tn else None
        elif line.startswith("SF:"):
            # Source file path
            sf = line[3:].strip()
            norm_sf = Path(sf).as_posix()
            current_file = report.files.setdefault(norm_sf, FileCoverage(file_path=norm_sf))
        elif line.startswith("DA:") and current_file:
            # DA:<line_number>,<execution_count>[,<checksum>]
            parts = line[3:].split(",")
            if len(parts) >= 2:
                try:
                    lnum = int(parts[0].strip())
                    hits = int(parts[1].strip())
                except ValueError:
                    continue
                if lnum <= 0:
                    continue
                current_file.total_executable_lines += 1
                current_file.line_hits[lnum] = hits
                if hits > 0:
                    current_file.covered_lines.add(lnum)
                    if current_test:
                        current_file.test_covers.setdefault(lnum, set()).add(current_test)
                        report.test_to_files.setdefault(current_test, set()).add(current_file.file_path)
                        t_lines = report.test_to_lines.setdefault(current_test, {})
                        t_lines.setdefault(current_file.file_path, set()).add(lnum)
                else:
                    current_file.missing_lines.add(lnum)
        elif line.startswith("FN:") and current_file:
            # Function declaration: FN:<line_number>,<function_name>
            pass
        elif line == "end_of_record":
            current_file = None

    return report


def parse_python_coverage(path: Path | str) -> CoverageReport:
    """Parse Python coverage.py SQLite database (.coverage)."""
    report = CoverageReport()
    p = Path(path)
    if not p.is_file():
        return report

    # Optional fast path: coverage library if available
    try:
        import coverage
        cov_data = coverage.CoverageData(str(p))
        cov_data.read()
        measured_files = cov_data.measured_files()
        for mf in measured_files:
            norm_path = Path(mf).as_posix()
            file_cov = report.files.setdefault(norm_path, FileCoverage(file_path=norm_path))
            lines = cov_data.lines(mf) or []
            for lnum in lines:
                file_cov.total_executable_lines += 1
                file_cov.covered_lines.add(lnum)
                file_cov.line_hits[lnum] = 1

            # Check contexts/test provenance if available
            try:
                contexts = cov_data.contexts_by_lineno(mf)
                for lnum, ctx_list in contexts.items():
                    for ctx in ctx_list:
                        if ctx:
                            file_cov.test_covers.setdefault(lnum, set()).add(ctx)
                            report.test_to_files.setdefault(ctx, set()).add(norm_path)
                            t_lines = report.test_to_lines.setdefault(ctx, {})
                            t_lines.setdefault(norm_path, set()).add(lnum)
            except Exception:
                pass
        if report.files:
            return report
    except Exception:
        pass

    # Pure Python SQLite fallback
    try:
        conn = sqlite3.connect(str(p), timeout=10.0)
        cur = conn.cursor()

        # Check tables
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}

        file_id_to_path: dict[int, str] = {}
        if "file" in tables:
            cur.execute("SELECT id, path FROM file;")
            for fid, fpath in cur.fetchall():
                file_id_to_path[fid] = Path(fpath).as_posix()

        context_id_to_name: dict[int, str] = {}
        if "context" in tables:
            cur.execute("SELECT id, context FROM context;")
            for cid, cname in cur.fetchall():
                context_id_to_name[cid] = str(cname)

        if "file_data" in tables:
            cur.execute("SELECT file_id, context_id, numbits FROM file_data;")
            for fid, cid, numbits in cur.fetchall():
                fpath = file_id_to_path.get(fid)
                if not fpath:
                    continue
                file_cov = report.files.setdefault(fpath, FileCoverage(file_path=fpath))
                lines = decode_numbits(numbits)
                cname = context_id_to_name.get(cid)
                for lnum in lines:
                    file_cov.covered_lines.add(lnum)
                    file_cov.line_hits[lnum] = file_cov.line_hits.get(lnum, 0) + 1
                    if cname:
                        file_cov.test_covers.setdefault(lnum, set()).add(cname)
                        report.test_to_files.setdefault(cname, set()).add(fpath)
                        t_lines = report.test_to_lines.setdefault(cname, {})
                        t_lines.setdefault(fpath, set()).add(lnum)
                file_cov.total_executable_lines = len(file_cov.covered_lines)

        elif "line_bits" in tables:
            cur.execute("SELECT file_id, numbits FROM line_bits;")
            for fid, numbits in cur.fetchall():
                fpath = file_id_to_path.get(fid)
                if not fpath:
                    continue
                file_cov = report.files.setdefault(fpath, FileCoverage(file_path=fpath))
                lines = decode_numbits(numbits)
                for lnum in lines:
                    file_cov.covered_lines.add(lnum)
                    file_cov.line_hits[lnum] = 1
                file_cov.total_executable_lines = len(file_cov.covered_lines)

        conn.close()
    except Exception:
        pass

    return report


def parse_coverage_file(path: Path | str) -> CoverageReport:
    """Auto-detect coverage format and parse into CoverageReport."""
    p = Path(path)
    if not p.is_file():
        return CoverageReport()

    suffix = p.suffix.lower()
    name = p.name.lower()

    if suffix == ".xml" or "coverage" in name and suffix == ".xml":
        return parse_cobertura_xml(p)
    if suffix == ".info" or "lcov" in name:
        return parse_lcov(p)
    if name == ".coverage" or name.startswith(".coverage."):
        return parse_python_coverage(p)

    # Inspect file signature
    try:
        with open(str(p), "rb") as f:
            head = f.read(256)
        if head.startswith(b"SQLite format 3"):
            return parse_python_coverage(p)
        if b"<coverage" in head or b"<?xml" in head:
            return parse_cobertura_xml(p)
        if b"TN:" in head or b"SF:" in head:
            return parse_lcov(p)
    except Exception:
        pass

    return CoverageReport()


def find_coverage_file(root_dir: Path | str = ".") -> Path | None:
    """Auto-discover coverage files in project directory."""
    root = Path(root_dir)
    candidates = [
        root / "coverage.xml",
        root / "coverage" / "coverage.xml",
        root / "lcov.info",
        root / "coverage" / "lcov.info",
        root / ".coverage",
        root / "target" / "coverage" / "lcov.info",
        root / "target" / "llvm-cov" / "lcov.info",
        root / "build" / "reports" / "jacoco" / "test" / "jacocoTestReport.xml",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------------------
# Graph Mapping & AST Decoration
# ---------------------------------------------------------------------------

def _extract_line_range(data: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extract start and end line from node attributes."""
    start_l = data.get("start_line") or data.get("line_start")
    end_l = data.get("end_line") or data.get("line_end")
    if start_l is not None and end_l is not None:
        try:
            return int(start_l), int(end_l)
        except ValueError:
            pass

    loc = data.get("source_location") or data.get("location")
    if loc is not None:
        s = str(loc).strip().lstrip("L")
        if "-" in s:
            parts = s.split("-", 1)
            try:
                return int(parts[0].lstrip("L")), int(parts[1].lstrip("L"))
            except ValueError:
                pass
        try:
            val = int(s)
            return val, val
        except ValueError:
            pass

    return None, None


def apply_coverage_to_graph(
    graph: nx.Graph,
    report: CoverageReport,
    root_dir: Path | str = ".",
) -> dict[str, Any]:
    """Decorate graph AST function nodes with coverage metrics and inject covered_by_test edges.

    Returns summary metrics dict:
      {
        "files_covered": int,
        "nodes_annotated": int,
        "edges_added": int,
        "overall_coverage_pct": float,
      }
    """
    root_path = Path(root_dir).resolve()

    # Index report files by relative path and basename
    norm_report_files: dict[str, FileCoverage] = {}
    for fpath, cov in report.files.items():
        try:
            p = Path(fpath)
            if p.is_absolute():
                try:
                    rel = p.resolve().relative_to(root_path).as_posix()
                    norm_report_files[rel] = cov
                except ValueError:
                    norm_report_files[p.as_posix()] = cov
            else:
                norm_report_files[p.as_posix()] = cov
        except Exception:
            norm_report_files[fpath] = cov

    def _find_coverage_for_node(src_file: str) -> FileCoverage | None:
        if not src_file:
            return None
        norm_src = Path(src_file).as_posix()
        if norm_src in norm_report_files:
            return norm_report_files[norm_src]
        for rf, fcov in norm_report_files.items():
            if norm_src == rf or norm_src.endswith("/" + rf) or rf.endswith("/" + norm_src):
                return fcov
        return None

    # Step 1: Decorate nodes with coverage metrics
    nodes_annotated = 0
    matched_files: set[str] = set()

    for nid, data in graph.nodes(data=True):
        src_file = data.get("source_file") or data.get("source_path") or ""
        fcov = _find_coverage_for_node(src_file)
        if not fcov:
            continue
        matched_files.add(fcov.file_path)

        start_l, end_l = _extract_line_range(data)
        if start_l is None:
            continue
        end_l = end_l or start_l

        # Lines belonging to this node
        node_lines = set(range(start_l, end_l + 1))
        # Covered lines in this range
        covered_in_node = node_lines & fcov.covered_lines
        missing_in_node = node_lines & fcov.missing_lines

        total_exec = len(covered_in_node) + len(missing_in_node)
        if total_exec > 0:
            cov_pct = round((len(covered_in_node) / total_exec) * 100.0, 1)
        elif covered_in_node:
            cov_pct = 100.0
        else:
            cov_pct = 0.0

        data["coverage_pct"] = cov_pct
        data["covered_lines"] = sorted(covered_in_node)
        data["missing_lines"] = sorted(missing_in_node)
        nodes_annotated += 1

    # Step 2: Inject covered_by_test and covers edges where test provenance exists
    edges_added = 0
    if report.test_to_lines:
        # Pre-index test nodes in graph
        test_nodes: dict[str, str] = {}  # test_identifier -> node_id
        for nid, data in graph.nodes(data=True):
            lbl = str(data.get("label") or nid)
            src_f = str(data.get("source_file") or "")
            if "test" in lbl.lower() or "test" in src_f.lower():
                test_nodes[lbl] = nid
                test_nodes[nid] = nid

        for test_name, file_map in report.test_to_lines.items():
            # Resolve test_name to graph node ID
            matched_test_nid: str | None = None
            for cand in (test_name, test_name.split("::")[-1], test_name.split(".")[-1]):
                if cand in test_nodes:
                    matched_test_nid = test_nodes[cand]
                    break

            if not matched_test_nid:
                continue

            for fpath, lines in file_map.items():
                for nid, data in graph.nodes(data=True):
                    if nid == matched_test_nid:
                        continue
                    src_f = data.get("source_file") or data.get("source_path") or ""
                    if not src_f or not (src_f == fpath or src_f.endswith("/" + fpath) or fpath.endswith("/" + src_f)):
                        continue

                    start_l, end_l = _extract_line_range(data)
                    if start_l is None:
                        continue
                    end_l = end_l or start_l

                    if any(start_l <= l <= end_l for l in lines):
                        # Add bidirectional edges
                        graph.add_edge(
                            matched_test_nid,
                            nid,
                            relation="covers",
                            confidence="EXTRACTED",
                            weight=1.0,
                        )
                        graph.add_edge(
                            nid,
                            matched_test_nid,
                            relation="covered_by_test",
                            confidence="EXTRACTED",
                            weight=1.0,
                        )
                        edges_added += 2

    return {
        "files_covered": len(matched_files),
        "nodes_annotated": nodes_annotated,
        "edges_added": edges_added,
        "overall_coverage_pct": report.overall_coverage_pct,
    }


def ingest_coverage(
    graph: nx.Graph,
    coverage_path: Path | str | None = None,
    root_dir: Path | str = ".",
) -> dict[str, Any]:
    """High-level function: parse coverage artifact and apply to graph."""
    if coverage_path is None:
        found = find_coverage_file(root_dir)
        if not found:
            return {
                "error": "No coverage artifact found (checked coverage.xml, lcov.info, .coverage)",
                "files_covered": 0,
                "nodes_annotated": 0,
                "edges_added": 0,
                "overall_coverage_pct": 0.0,
            }
        coverage_path = found

    p = Path(coverage_path)
    if not p.is_file():
        return {
            "error": f"Coverage file not found: {p}",
            "files_covered": 0,
            "nodes_annotated": 0,
            "edges_added": 0,
            "overall_coverage_pct": 0.0,
        }

    report = parse_coverage_file(p)
    res = apply_coverage_to_graph(graph, report, root_dir=root_dir)
    res["coverage_file"] = str(p)
    return res
