"""Sub-second unindexed on-demand AST search and single-file skeleton extraction.

Executes lexical search (ripgrep with Python standard-library fallback) joined to
on-demand Tree-sitter AST parsing in <100ms, completely bypassing graph indexing,
database serialization, and community partitioning.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from graph_fy.skeleton import (
    _CONTAINER_NODE_TYPES,
    _FUNC_NODE_TYPES,
    compact_signature,
    get_parser_for_path,
    get_symbol_code_from_file,
    skeletonize_code,
    skeletonize_file,
)

_IGNORED_DIRS = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "env",
    "graph_fy_out",
    "graphify_out",
    "dist",
    "build",
    "target",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
})

_SUPPORTED_EXTS = frozenset({
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".mts",
    ".cts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cxx",
    ".cs",
    ".sol",
})


def _extract_node_name(node: Any) -> str | None:
    """Extract symbol name from AST node."""
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        try:
            return name_node.text.decode("utf-8", errors="replace").strip()
        except Exception:
            pass

    # Handle decorated definitions in Python / Java / etc.
    if node.type in ("decorated_definition", "export_statement"):
        for child in node.children:
            if child.type in _FUNC_NODE_TYPES or child.type in _CONTAINER_NODE_TYPES:
                return _extract_node_name(child)

    # Check for direct identifier children
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            try:
                return child.text.decode("utf-8", errors="replace").strip()
            except Exception:
                pass
    return None


def _classify_node_kind(node: Any) -> str:
    """Classify AST node kind."""
    t = node.type
    if t in ("function_definition", "function_declaration", "function_item", "arrow_function"):
        return "function"
    if t in ("method_definition", "method_declaration"):
        return "method"
    if t in ("class_definition", "class_declaration"):
        return "class"
    if t in ("struct_item",):
        return "struct"
    if t in ("interface_declaration",):
        return "interface"
    if t in ("enum_declaration",):
        return "enum"
    if t in ("type_alias_declaration",):
        return "type_alias"
    if t in ("impl_item",):
        return "impl"
    return "symbol"


def _extract_docstring(source_bytes: bytes, node: Any, lang_name: str) -> str:
    """Extract 1-line docstring from AST node."""
    try:
        body = None
        for child in node.children:
            if child.type in ("block", "statement_block", "compound_statement", "declaration_list"):
                body = child
                break
        if body and body.children:
            first = body.children[0]
            if first.type == "expression_statement" and first.children:
                for sc in first.children:
                    if sc.type == "string":
                        doc_text = sc.text.decode("utf-8", errors="replace").strip()
                        clean = doc_text.strip('"""').strip("'''").strip('"').strip("'").strip()
                        return clean.splitlines()[0].strip() if clean else ""
    except Exception:
        pass
    return ""


def _scan_files_rg(pattern: str, target: Path, max_matches: int = 50) -> list[tuple[Path, int, str]]:
    """Scan directory or file using ripgrep (`rg`)."""
    rg_path = shutil.which("rg")
    if not rg_path:
        return []

    cmd = [
        rg_path,
        "-n",
        "--column",
        "--no-heading",
        "--color=never",
        "-m",
        "5",
        "--max-count",
        str(max_matches),
        pattern,
        str(target),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode not in (0, 1):
            return []
        matches: list[tuple[Path, int, str]] = []
        for line in proc.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) >= 3:
                fpath = Path(parts[0])
                try:
                    lineno = int(parts[1])
                    content = parts[3].strip() if len(parts) > 3 else ""
                    matches.append((fpath, lineno, content))
                except ValueError:
                    continue
            if len(matches) >= max_matches:
                break
        return matches
    except Exception:
        return []


def _scan_files_python(pattern: str, target: Path, max_matches: int = 50) -> list[tuple[Path, int, str]]:
    """Scan directory or file using pure standard library Python."""
    matches: list[tuple[Path, int, str]] = []
    regex = re.compile(re.escape(pattern), re.IGNORECASE)

    if target.is_file():
        try:
            with open(target, "r", encoding="utf-8", errors="ignore") as f:
                for lno, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append((target, lno, line.strip()))
                        if len(matches) >= max_matches:
                            break
        except Exception:
            pass
        return matches

    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS and not d.startswith(".")]
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() not in _SUPPORTED_EXTS:
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    for lno, line in enumerate(f, 1):
                        if regex.search(line):
                            matches.append((p, lno, line.strip()))
                            if len(matches) >= max_matches:
                                return matches
            except Exception:
                continue
    return matches


def fast_ast_grep(
    pattern: str,
    target: Path | str = ".",
    limit: int = 10,
    max_files: int = 20,
    include_code: bool = True,
) -> list[dict[str, Any]]:
    """Fast unindexed lexical search + on-demand Tree-sitter AST extraction (<100ms).

    Args:
        pattern: Symbol name or text pattern to locate.
        target: Root directory or specific file path to search.
        limit: Maximum number of ranked hits to return.
        max_files: Maximum matching files to parse AST on demand.
        include_code: Whether to include full symbol implementation snippet.

    Returns:
        List of ranked dictionary records with AST metadata, skeletons, and signatures.
    """
    target_path = Path(target).resolve()
    if not target_path.exists():
        return []

    # 1. Rapid lexical scan
    matches = _scan_files_rg(pattern, target_path, max_matches=max_files * 3)
    if not matches:
        matches = _scan_files_python(pattern, target_path, max_matches=max_files * 3)

    if not matches:
        return []

    # Group matches by file path
    file_map: dict[Path, list[tuple[int, str]]] = {}
    for fpath, lno, line_text in matches:
        file_map.setdefault(fpath, []).append((lno, line_text))

    clean_pattern = pattern.strip().lower()
    hits: list[dict[str, Any]] = []
    seen_symbols: set[tuple[str, str]] = set()

    for fpath, line_matches in list(file_map.items())[:max_files]:
        parser, lang_name = get_parser_for_path(fpath)
        try:
            source_bytes = fpath.read_bytes()
        except OSError:
            continue

        if parser is None or lang_name is None:
            # Unsupported extension fallback: line window
            for lno, line_text in line_matches[:2]:
                hits.append({
                    "file": str(fpath.relative_to(Path.cwd()) if fpath.is_relative_to(Path.cwd()) else fpath),
                    "line": lno,
                    "end_line": lno,
                    "symbol": pattern,
                    "kind": "text",
                    "signature": line_text[:120],
                    "docstring": "",
                    "skeleton": line_text,
                    "code": line_text,
                    "match_type": "text_match",
                    "rank": 4,
                })
            continue

        try:
            tree = parser.parse(source_bytes)
        except Exception:
            continue

        def find_enclosing(node: Any, target_row: int) -> Any:
            if node.start_point.row <= target_row <= node.end_point.row:
                for child in node.children:
                    sub = find_enclosing(child, target_row)
                    if sub is not None:
                        return sub
                if node.type in _FUNC_NODE_TYPES or node.type in _CONTAINER_NODE_TYPES:
                    return node
            return None

        # Check line matches against AST
        for lno, line_text in line_matches:
            target_row = lno - 1
            node = find_enclosing(tree.root_node, target_row)

            rel_path = str(fpath.relative_to(Path.cwd()) if fpath.is_relative_to(Path.cwd()) else fpath)

            if node is not None:
                sym_name = _extract_node_name(node) or pattern
                sym_key = (rel_path, sym_name)
                if sym_key in seen_symbols:
                    continue
                seen_symbols.add(sym_key)

                kind = _classify_node_kind(node)
                start_l = node.start_point.row + 1
                end_l = node.end_point.row + 1

                node_bytes = source_bytes[node.start_byte : node.end_byte]
                raw_code = node_bytes.decode("utf-8", errors="replace")

                sig = compact_signature(raw_code, lang_name)
                doc = _extract_docstring(source_bytes, node, lang_name)
                skel = skeletonize_code(node_bytes, parser, lang_name, compact_docstrings=True)

                # Determine rank
                sym_lower = sym_name.lower()
                if sym_lower == clean_pattern:
                    match_type = "exact_definition"
                    rank = 1
                elif clean_pattern in sym_lower:
                    match_type = "substring_definition"
                    rank = 2
                else:
                    match_type = "symbol_reference"
                    rank = 3

                hits.append({
                    "file": rel_path,
                    "line": start_l,
                    "end_line": end_l,
                    "symbol": sym_name,
                    "kind": kind,
                    "signature": sig,
                    "docstring": doc,
                    "skeleton": skel,
                    "code": raw_code if include_code else "",
                    "match_type": match_type,
                    "rank": rank,
                })
            else:
                hits.append({
                    "file": rel_path,
                    "line": lno,
                    "end_line": lno,
                    "symbol": pattern,
                    "kind": "text",
                    "signature": line_text[:120],
                    "docstring": "",
                    "skeleton": line_text,
                    "code": line_text if include_code else "",
                    "match_type": "text_match",
                    "rank": 4,
                })

    hits.sort(key=lambda x: (x.get("rank", 5), x.get("file", ""), x.get("line", 0)))
    return hits[:limit]


def skeletonize_single_file(
    file_path: Path | str,
    symbol_name: str | None = None,
    token_budget: int | None = None,
) -> dict[str, Any]:
    """On-demand AST skeleton extraction for a single file without graph build.

    Args:
        file_path: Target source file.
        symbol_name: Optional symbol name to filter down to a specific class or function.
        token_budget: Optional token budget cap.

    Returns:
        Dictionary with skeleton representation, symbol metadata, and line range.
    """
    p = Path(file_path).resolve()
    if not p.is_file():
        return {"error": f"File not found: {file_path}", "skeleton": "", "length": 0}

    rel_path = str(p.relative_to(Path.cwd()) if p.is_relative_to(Path.cwd()) else p)

    if not symbol_name:
        skel = skeletonize_file(p, token_budget=token_budget)
        return {
            "file": rel_path,
            "symbol": None,
            "kind": "file",
            "start_line": 1,
            "end_line": len(skel.splitlines()),
            "skeleton": skel,
            "length": len(skel),
        }

    parser, lang_name = get_parser_for_path(p)
    if parser is None or lang_name is None:
        skel = skeletonize_file(p, token_budget=token_budget)
        return {
            "file": rel_path,
            "symbol": symbol_name,
            "kind": "file",
            "start_line": 1,
            "end_line": len(skel.splitlines()),
            "skeleton": skel,
            "length": len(skel),
        }

    try:
        source_bytes = p.read_bytes()
        tree = parser.parse(source_bytes)
    except Exception as exc:
        return {"error": f"Parse error: {exc}", "skeleton": "", "length": 0}

    target_sym = symbol_name.strip().lower()
    matching_node = None

    def find_symbol(node: Any) -> None:
        nonlocal matching_node
        if matching_node is not None:
            return
        if node.type in _FUNC_NODE_TYPES or node.type in _CONTAINER_NODE_TYPES:
            sname = _extract_node_name(node)
            if sname and sname.lower() == target_sym:
                matching_node = node
                return
        for child in node.children:
            find_symbol(child)

    find_symbol(tree.root_node)

    if matching_node is None:
        return {
            "error": f"Symbol '{symbol_name}' not found in {rel_path}",
            "file": rel_path,
            "symbol": symbol_name,
            "skeleton": "",
            "length": 0,
        }

    node_bytes = source_bytes[matching_node.start_byte : matching_node.end_byte]
    skel = skeletonize_code(node_bytes, parser, lang_name, compact_docstrings=True, token_budget=token_budget)
    kind = _classify_node_kind(matching_node)
    start_l = matching_node.start_point.row + 1
    end_l = matching_node.end_point.row + 1

    return {
        "file": rel_path,
        "symbol": symbol_name,
        "kind": kind,
        "start_line": start_l,
        "end_line": end_l,
        "skeleton": skel,
        "length": len(skel),
    }


def format_fast_hits(query: str, hits: list[dict[str, Any]], elapsed_s: float) -> str:
    """Format fast AST hits into dense, readable text output."""
    if not hits:
        return f"=== Fast AST Search: \"{query}\" ({elapsed_s:.3f}s) ===\n(no matching AST symbols found)\n"

    lines = [f"=== Fast AST Search: \"{query}\" ({elapsed_s:.3f}s, {len(hits)} hits) ==="]
    for h in hits:
        sym = h.get("symbol", "")
        kind = h.get("kind", "")
        f = h.get("file", "")
        line = h.get("line", "")
        sig = h.get("signature", "")
        doc = h.get("docstring", "")

        lines.append(f"\n• {sym} ({kind}) -> {f}:{line}")
        if sig:
            lines.append(f"  {sig}")
        if doc:
            lines.append(f"  \"{doc}\"")

    return "\n".join(lines) + "\n"
