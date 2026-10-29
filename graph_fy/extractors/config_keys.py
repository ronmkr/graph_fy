"""Extractor for configuration key reads across application languages.

Extracts reads of environment variables and configuration keys:
- Python: os.environ["X"], os.environ.get("X"), os.getenv("X")
- JS/TS: process.env.X, process.env["X"], import.meta.env.X
- Rust: std::env::var("X"), env::var("X"), env!("X")
- Go: os.Getenv("X"), os.LookupEnv("X"), viper.GetString("X")
- Java/JVM: @Value("${X}"), System.getenv("X"), System.getProperty("X")
as `config_key` reference nodes and `reads_config` edges.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _make_id

# Regex patterns per language family
_PYTHON_CONFIG_RE = re.compile(
    r"""\b(?:os\.)?(?:environ\[["']([A-Za-z0-9_.-]+)["']\]|environ\.get\s*\(\s*["']([A-Za-z0-9_.-]+)["']|getenv\s*\(\s*["']([A-Za-z0-9_.-]+)["'])"""
)

_JS_TS_CONFIG_RE = re.compile(
    r"""\b(?:process\.env|import\.meta\.env)(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*["']([A-Za-z0-9_.-]+)["']\s*\])"""
)

_RUST_CONFIG_RE = re.compile(
    r"""\b(?:(?:std::)?env::(?:var|var_os)|(?:option_)?env!)\s*\(\s*["']([A-Za-z0-9_.-]+)["']\s*\)"""
)

_GO_CONFIG_RE = re.compile(
    r"""\b(?:os\.(?:Getenv|LookupEnv)|viper\.(?:GetString|Get|GetInt|GetBool))\s*\(\s*["']([A-Za-z0-9_.-]+)["']\s*\)"""
)

_JVM_CONFIG_RE = re.compile(
    r"""(?:@Value\s*\(\s*["']\$\{([A-Za-z0-9_.-]+)(?::[^}]*)?\}["']\s*\)|System\.(?:getenv|getProperty)\s*\(\s*["']([A-Za-z0-9_.-]+)["']\s*\))"""
)


def _find_enclosing_node(
    line_num: int,
    nodes: list[dict[str, Any]],
    str_path: str,
    file_nid: str,
) -> str:
    """Find the closest enclosing function/method/class node in str_path above line_num."""
    matched_n: dict[str, Any] | None = None
    for n in nodes:
        if n.get("source_file") != str_path:
            continue
        sloc = str(n.get("source_location") or "")
        m = re.search(r"L?(\d+)", sloc)
        if not m:
            continue
        node_line = int(m.group(1))
        if node_line <= line_num:
            if matched_n is None:
                matched_n = n
            else:
                m_prev = re.search(r"L?(\d+)", str(matched_n.get("source_location") or ""))
                prev_line = int(m_prev.group(1)) if m_prev else 0
                if node_line > prev_line:
                    matched_n = n

    return matched_n["id"] if matched_n else file_nid


def extract_config_keys_from_source(
    path: Path,
    source_text: str,
    result: dict[str, Any],
) -> None:
    """Extract configuration and environment variable reads from source code into result."""
    suffix = path.suffix.lower()
    patterns: list[re.Pattern] = []

    if suffix in (".py", ".pyi"):
        patterns.append(_PYTHON_CONFIG_RE)
    elif suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"):
        patterns.append(_JS_TS_CONFIG_RE)
    elif suffix == ".rs":
        patterns.append(_RUST_CONFIG_RE)
    elif suffix == ".go":
        patterns.append(_GO_CONFIG_RE)
    elif suffix in (".java", ".kt", ".kts", ".groovy", ".scala"):
        patterns.append(_JVM_CONFIG_RE)
    else:
        return

    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    seen_ids = {n["id"] for n in nodes if "id" in n}
    seen_edges = {
        (e.get("source"), e.get("target"), e.get("relation"))
        for e in edges
    }

    str_path = str(path.resolve() if path.exists() else path)
    file_nid = _make_id(str_path)

    for pat in patterns:
        for match in pat.finditer(source_text):
            # Extract first non-None capture group
            key_name = next((g for g in match.groups() if g), None)
            if not key_name:
                continue

            line_num = source_text[:match.start()].count("\n") + 1
            src_nid = _find_enclosing_node(line_num, nodes, str_path, file_nid)

            key_nid = _make_id("config_key", str_path, key_name, f"L{line_num}")
            if key_nid not in seen_ids:
                seen_ids.add(key_nid)
                nodes.append({
                    "id": key_nid,
                    "label": f"ConfigKey:{key_name}",
                    "type": "config_key",
                    "file_type": "code",
                    "config_name": key_name,
                    "source_file": str_path,
                    "source_location": f"L{line_num}",
                    "text": f"Configuration key reference {key_name}",
                })

            edge_key = (src_nid, key_nid, "reads_config")
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append({
                    "source": src_nid,
                    "target": key_nid,
                    "relation": "reads_config",
                    "confidence": "EXTRACTED",
                    "weight": 1.0,
                    "source_file": str_path,
                    "source_location": f"L{line_num}",
                })
