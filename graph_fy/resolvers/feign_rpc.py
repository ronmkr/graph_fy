"""Spring Cloud Feign & RPC cross-microservice endpoint resolution.

Extracts declarative @FeignClient interfaces and connects their methods to
matching Spring MVC @RestController / @Controller request mapping endpoints
across submodules via `calls_feign_rpc` directed edges.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _make_id


def _normalize_http_path(base_path: str, method_path: str) -> str:
    """Normalize and combine base path and method path with wildcard placeholders."""
    b = (base_path or "").strip("/")
    s = (method_path or "").strip("/")
    if b and s:
        combined = f"/{b}/{s}"
    elif b:
        combined = f"/{b}"
    elif s:
        combined = f"/{s}"
    else:
        combined = "/"
    # Normalize path variables e.g. {id} -> {*}
    return re.sub(r"\{[^}]+\}", "{*}", combined)


def resolve_spring_feign_rpc(
    all_nodes: list[dict[str, Any]],
    all_edges: list[dict[str, Any]],
    root: Path | None = None,
) -> None:
    """Resolve Spring Cloud Feign client methods to target Spring MVC controller endpoints."""
    java_files: dict[str, list[dict[str, Any]]] = {}
    for n in all_nodes:
        sf = n.get("source_file")
        if sf and sf.endswith(".java"):
            java_files.setdefault(sf, []).append(n)

    if not java_files:
        return

    feign_endpoints: list[dict[str, Any]] = []
    controller_endpoints: list[dict[str, Any]] = []

    m_http_anno = re.compile(
        r'''@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\s*(?:\(\s*(?:(?:value|path)\s*=\s*)?["']([^"']*)["'](?:\s*,\s*method\s*=\s*RequestMethod\.([A-Z]+))?[^)]*\)|\(\s*\))?''',
        re.DOTALL,
    )

    for sf, file_nodes in java_files.items():
        try:
            p = Path(sf)
            if not p.exists():
                continue
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        is_feign = "@FeignClient" in src
        is_controller = "@RestController" in src or "@Controller" in src

        if not is_feign and not is_controller:
            continue

        method_nids: dict[str, dict[str, Any]] = {}
        for n in file_nodes:
            lbl = n.get("label", "")
            clean_lbl = lbl.strip(".()")
            method_nids[clean_lbl] = n

        if is_feign:
            m_fc = re.search(r'''@FeignClient\s*\(([^)]+)\)''', src)
            if m_fc:
                fc_args = m_fc.group(1)
                m_srv = re.search(r'''(?:name|value)?\s*=\s*["']([^"']+)["']''', fc_args) or re.search(r'''^["']([^"']+)["']''', fc_args.strip())
                srv_name = m_srv.group(1) if m_srv else ""
                m_bp = re.search(r'''path\s*=\s*["']([^"']+)["']''', fc_args)
                base_path = m_bp.group(1) if m_bp else ""
                if not base_path:
                    m_rm = re.search(r'''@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?["']([^"']+)["']''', src[:m_fc.end() + 200])
                    if m_rm:
                        base_path = m_rm.group(1)

                for m_anno in m_http_anno.finditer(src):
                    if m_anno.start() < m_fc.end():
                        continue
                    anno_name = m_anno.group(1)
                    sub_path = m_anno.group(2) or ""
                    req_method = m_anno.group(3)

                    if anno_name.endswith("Mapping") and anno_name != "RequestMapping":
                        http_verb = anno_name.replace("Mapping", "").upper()
                    else:
                        http_verb = req_method.upper() if req_method else "ANY"

                    norm_path = _normalize_http_path(base_path, sub_path)
                    pos = m_anno.end()
                    after_anno = src[pos:pos + 200]
                    m_fn = re.search(r'''\b[A-Za-z0-9_<>\[\]]+\s+([a-zA-Z0-9_$]+)\s*\(''', after_anno)
                    if m_fn:
                        fn_name = m_fn.group(1)
                        fn_node = method_nids.get(fn_name)
                        fn_nid = fn_node["id"] if fn_node else _make_id(sf, fn_name)
                        line_no = src[:m_anno.start()].count("\n") + 1
                        feign_endpoints.append({
                            "service": srv_name,
                            "http_method": http_verb,
                            "norm_path": norm_path,
                            "method_nid": fn_nid,
                            "source_file": sf,
                            "line": line_no,
                        })

        if is_controller:
            m_crm = re.search(r'''@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?["']([^"']+)["']''', src)
            c_base_path = m_crm.group(1) if m_crm else ""
            c_rm_start = m_crm.start() if m_crm else -1

            for m_anno in m_http_anno.finditer(src):
                if m_anno.start() == c_rm_start:
                    continue
                anno_name = m_anno.group(1)
                sub_path = m_anno.group(2) or ""
                req_method = m_anno.group(3)

                if anno_name.endswith("Mapping") and anno_name != "RequestMapping":
                    http_verb = anno_name.replace("Mapping", "").upper()
                else:
                    http_verb = req_method.upper() if req_method else "ANY"

                norm_path = _normalize_http_path(c_base_path, sub_path)
                pos = m_anno.end()
                after_anno = src[pos:pos + 200]
                m_fn = re.search(r'''\b[A-Za-z0-9_<>\[\]]+\s+([a-zA-Z0-9_$]+)\s*\(''', after_anno)
                if m_fn:
                    fn_name = m_fn.group(1)
                    fn_node = method_nids.get(fn_name)
                    fn_nid = fn_node["id"] if fn_node else _make_id(sf, fn_name)
                    line_no = src[:m_anno.start()].count("\n") + 1
                    controller_endpoints.append({
                        "http_method": http_verb,
                        "norm_path": norm_path,
                        "method_nid": fn_nid,
                        "source_file": sf,
                        "line": line_no,
                    })

    existing_edges = {(e.get("source"), e.get("target"), e.get("relation")) for e in all_edges}

    for fe in feign_endpoints:
        fe_verb = fe["http_method"]
        fe_path = fe["norm_path"]
        fe_nid = fe["method_nid"]
        fe_srv = fe["service"]

        candidates = []
        for ce in controller_endpoints:
            if fe_verb != "ANY" and ce["http_method"] != "ANY" and fe_verb != ce["http_method"]:
                continue
            if fe_path == ce["norm_path"]:
                candidates.append(ce)

        if not candidates:
            continue

        target_ctrl = candidates[0]
        if len(candidates) > 1 and fe_srv:
            clean_srv = fe_srv.lower().replace("-", "").replace("_", "")
            for cand in candidates:
                cand_sf = cand["source_file"].lower().replace("-", "").replace("_", "")
                if clean_srv in cand_sf:
                    target_ctrl = cand
                    break

        ctrl_nid = target_ctrl["method_nid"]
        edge_key = (fe_nid, ctrl_nid, "calls_feign_rpc")
        if edge_key not in existing_edges:
            existing_edges.add(edge_key)
            all_edges.append({
                "source": fe_nid,
                "target": ctrl_nid,
                "relation": "calls_feign_rpc",
                "context": "spring_feign_rpc",
                "confidence": "EXTRACTED",
                "source_file": fe["source_file"],
                "source_location": f"L{fe['line']}",
                "weight": 1.0,
            })
