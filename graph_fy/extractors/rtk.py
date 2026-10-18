"""Redux Toolkit (RTK) and RTK Query (RTKQ) architectural extractor.

Extracts:
1. Redux Toolkit Slices: `createSlice` definitions, state shapes, reducers, and actions.
2. Async Thunks: `createAsyncThunk` action creators and lifecycle states (pending, fulfilled, rejected).
3. Redux Stores: `configureStore` setups and slice attachments.
4. Selectors: `createSelector` definitions and component `useSelector` usages.
5. RTK Query APIs: `createApi` definitions, query/mutation endpoints, HTTP paths, and methods.
6. Cache Invalidation Graphs: `providesTags` and `invalidatesTags` linkages generating `invalidates_cache` edges.
7. Auto-generated React Hooks: `use...Query` and `use...Mutation` hooks and UI component linkages.
8. Component Dispatches: `dispatch(action(...))` calls linking React components to RTK actions/thunks.
9. Cross-Resolution: `resolve_rtk_backend_edges` matching endpoints to backend route nodes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _make_id


def _read_file_content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def is_rtk_file(path: Path) -> bool:
    """Return True if path is a JS/TS file containing Redux Toolkit or RTK Query patterns."""
    ext = path.suffix.lower()
    if ext not in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return False
    try:
        sample = _read_file_content(path)[:4000]
        if "@reduxjs/toolkit" in sample or "react-redux" in sample:
            return True
        if "createSlice" in sample or "createApi" in sample:
            return True
        if "createAsyncThunk" in sample or "configureStore" in sample or "createSelector" in sample:
            return True
        if "useSelector" in sample or "useDispatch" in sample:
            return True
        if re.search(r"\buse[A-Z]\w+(?:Query|Mutation)\b", sample):
            return True
    except Exception:
        pass
    return False


def _clean_str(val: str) -> str:
    return val.strip().strip("'\"`")


def _capitalize(s: str) -> str:
    return s[:1].upper() + s[1:] if s else ""


def _normalize_route_path(p: str) -> str:
    """Normalize route path for cross-layer URL matching (e.g. '/api/users/:id' or '/users/{id}')."""
    if not p:
        return ""
    p = p.strip().strip("'\"`")
    m = re.match(r"^(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\s+(.*)$", p, re.IGNORECASE)
    if m:
        p = m.group(2).strip()
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r":([a-zA-Z0-9_]+)", r"{\1}", p)
    return p.rstrip("/") or "/"


def _extract_balanced_braces(text: str, start_pos: int) -> tuple[str | None, int]:
    """Find the matching closing brace for the first opening brace after start_pos."""
    idx = text.find("{", start_pos)
    if idx == -1:
        return None, -1
    depth = 0
    in_quote: str | None = None
    i = idx
    while i < len(text):
        c = text[i]
        if in_quote:
            if c == in_quote and (i == 0 or text[i - 1] != "\\"):
                in_quote = None
        else:
            if c in ('"', "'", "`"):
                in_quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[idx + 1 : i], i + 1
        i += 1
    return None, -1


def extract_rtk(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Extract Redux Toolkit slices, thunks, stores, and RTK Query APIs from source file."""
    content = _read_file_content(path)
    if not content:
        return {"nodes": [], "edges": []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()

    def _add_node(nid: str, label: str, ntype: str, loc: str = "L1", **attrs: Any) -> None:
        if nid in seen_node_ids:
            return
        seen_node_ids.add(nid)
        node_dict = {
            "id": nid,
            "label": label,
            "type": ntype,
            "source_file": str(path),
            "source_location": loc,
        }
        node_dict.update(attrs)
        nodes.append(node_dict)

    def _add_edge(src: str, tgt: str, relation: str, loc: str = "L1", **attrs: Any) -> None:
        if not src or not tgt or src == tgt:
            return
        edge_dict = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str(path),
            "source_location": loc,
        }
        edge_dict.update(attrs)
        edges.append(edge_dict)

    # -------------------------------------------------------------------------
    # 1. createSlice extraction
    # -------------------------------------------------------------------------
    for m in re.finditer(r"(?:export\s+const\s+(\w+)\s*=\s*)?createSlice\s*\(", content):
        var_name = m.group(1) or ""
        body, _ = _extract_balanced_braces(content, m.end())
        if not body:
            continue
        start_line = content[: m.start()].count("\n") + 1
        loc = f"L{start_line}"

        # Slice name
        name_match = re.search(r"name\s*:\s*['\"`]([^'\"`]+)['\"`]", body)
        slice_name = name_match.group(1) if name_match else var_name or "slice"

        slice_id = _make_id("rtk_slice", slice_name)
        _add_node(
            slice_id,
            f"RTK:Slice:{slice_name}",
            "rtk_slice",
            loc,
            slice_name=slice_name,
            variable_name=var_name,
        )

        # Reducer actions
        red_pos = body.find("reducers")
        if red_pos != -1:
            reducers_body, _ = _extract_balanced_braces(body, red_pos)
            if reducers_body:
                action_keys = re.findall(
                    r"(\w+)\s*(?:\([^)]*\)|:\s*(?:function|\([^)]*\)\s*=>))",
                    reducers_body,
                )
                for action_name in action_keys:
                    if action_name in ("state", "action", "payload"):
                        continue
                    action_id = _make_id("rtk_action", slice_name, action_name)
                    _add_node(
                        action_id,
                        f"RTK:Action:{slice_name}/{action_name}",
                        "rtk_action",
                        loc,
                        slice_name=slice_name,
                        action_name=action_name,
                    )
                    _add_edge(slice_id, action_id, "defines_action", loc)

    # -------------------------------------------------------------------------
    # 2. createAsyncThunk extraction
    # -------------------------------------------------------------------------
    thunk_pattern = re.finditer(
        r"(?:export\s+const\s+(\w+)\s*=\s*)?createAsyncThunk\(\s*['\"`]([^'\"`]+)['\"`]",
        content,
    )
    for m in thunk_pattern:
        var_name = m.group(1) or ""
        type_prefix = m.group(2)
        start_line = content[: m.start()].count("\n") + 1
        loc = f"L{start_line}"

        thunk_id = _make_id("rtk_thunk", type_prefix)
        _add_node(
            thunk_id,
            f"RTK:Thunk:{type_prefix}",
            "rtk_thunk",
            loc,
            type_prefix=type_prefix,
            variable_name=var_name,
        )

        # Add lifecycle sub-action nodes: pending, fulfilled, rejected
        for lifecycle in ("pending", "fulfilled", "rejected"):
            sub_action_id = _make_id("rtk_action", type_prefix, lifecycle)
            _add_node(
                sub_action_id,
                f"RTK:Action:{type_prefix}/{lifecycle}",
                "rtk_action",
                loc,
                type_prefix=type_prefix,
                lifecycle=lifecycle,
            )
            _add_edge(thunk_id, sub_action_id, "emits_action", loc)

        # Link to slice if prefix matches slice/action
        if "/" in type_prefix:
            prefix_slice = type_prefix.split("/", 1)[0]
            slice_id = _make_id("rtk_slice", prefix_slice)
            if slice_id in seen_node_ids:
                _add_edge(slice_id, thunk_id, "handles_thunk", loc)

    # -------------------------------------------------------------------------
    # 3. configureStore extraction
    # -------------------------------------------------------------------------
    for m in re.finditer(r"(?:export\s+const\s+(\w+)\s*=\s*)?configureStore\s*\(", content):
        var_name = m.group(1) or "store"
        body, _ = _extract_balanced_braces(content, m.end())
        if not body:
            continue
        start_line = content[: m.start()].count("\n") + 1
        loc = f"L{start_line}"

        store_id = _make_id("rtk_store", var_name)
        _add_node(
            store_id,
            f"RTK:Store:{var_name}",
            "rtk_store",
            loc,
            variable_name=var_name,
        )

        # Check attached reducers
        red_pos = body.find("reducer")
        if red_pos != -1:
            reducer_body, _ = _extract_balanced_braces(body, red_pos)
            if reducer_body:
                for line in reducer_body.splitlines():
                    kv = re.search(r"(\w+)\s*:\s*(\w+)", line)
                    if kv:
                        slice_key, reducer_val = kv.group(1), kv.group(2)
                        slice_id = _make_id("rtk_slice", slice_key)
                        _add_edge(store_id, slice_id, "manages_slice", loc, reducer=reducer_val)

    # -------------------------------------------------------------------------
    # 4. createSelector extraction
    # -------------------------------------------------------------------------
    selector_pattern = re.finditer(
        r"(?:export\s+const\s+(\w+)\s*=\s*)?createSelector\(\s*\[?([\s\S]*?)\]?\s*,\s*(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>",
        content,
    )
    for m in selector_pattern:
        sel_name = m.group(1) or "selector"
        inputs = m.group(2)
        start_line = content[: m.start()].count("\n") + 1
        loc = f"L{start_line}"

        sel_id = _make_id("rtk_selector", sel_name)
        _add_node(
            sel_id,
            f"RTK:Selector:{sel_name}",
            "rtk_selector",
            loc,
            selector_name=sel_name,
        )
        for in_token in re.findall(r"\b([a-zA-Z0-9_]+)\b", inputs):
            if in_token.startswith("select") and in_token != sel_name:
                dep_sel_id = _make_id("rtk_selector", in_token)
                _add_edge(sel_id, dep_sel_id, "depends_on_selector", loc)

    # -------------------------------------------------------------------------
    # 5. RTK Query (createApi) extraction
    # -------------------------------------------------------------------------
    query_endpoints: dict[str, dict[str, Any]] = {}
    mutation_endpoints: dict[str, dict[str, Any]] = {}

    for m in re.finditer(r"(?:export\s+const\s+(\w+)\s*=\s*)?createApi\s*\(", content):
        var_name = m.group(1) or "api"
        body, _ = _extract_balanced_braces(content, m.end())
        if not body:
            continue
        start_line = content[: m.start()].count("\n") + 1
        loc = f"L{start_line}"

        # reducerPath
        rp_match = re.search(r"reducerPath\s*:\s*['\"`]([^'\"`]+)['\"`]", body)
        reducer_path = rp_match.group(1) if rp_match else var_name

        # tagTypes
        tag_types: list[str] = []
        tags_match = re.search(r"tagTypes\s*:\s*\[([\s\S]*?)\]", body)
        if tags_match:
            tag_types = [
                _clean_str(t)
                for t in re.findall(r"['\"`]([^'\"`]+)['\"`]", tags_match.group(1))
            ]

        api_id = _make_id("rtkq_api", reducer_path)
        _add_node(
            api_id,
            f"RTKQ:API:{reducer_path}",
            "rtkq_api",
            loc,
            reducer_path=reducer_path,
            variable_name=var_name,
            tag_types=tag_types,
        )

        # Parse endpoints block
        ep_pos = body.find("endpoints")
        if ep_pos != -1:
            ep_body, _ = _extract_balanced_braces(body, ep_pos)
            if ep_body:
                # Query endpoints: name: builder.query({ ... })
                for qm in re.finditer(r"(\w+)\s*:\s*builder\.query(?:<[^>]*>)?\s*\(", ep_body):
                    ep_name = qm.group(1)
                    q_body, _ = _extract_balanced_braces(ep_body, qm.end() - 1)
                    if not q_body:
                        continue
                    ep_line = start_line + ep_body[: qm.start()].count("\n")
                    ep_loc = f"L{ep_line}"

                    # URL path
                    url_match = re.search(
                        r"(?:url|query)\s*:\s*(?:(?:\([^)]*\)\s*=>\s*)?['\"`]([^'\"`]+)['\"`]|(?:\([^)]*\)\s*=>\s*)?\{[\s\S]*?url\s*:\s*['\"`]([^'\"`]+)['\"`])",
                        q_body,
                    )
                    url_path = ""
                    if url_match:
                        url_path = url_match.group(1) or url_match.group(2) or ""

                    # Method
                    method_match = re.search(r"method\s*:\s*['\"`]([A-Z]+)['\"`]", q_body)
                    method = method_match.group(1) if method_match else "GET"

                    # providesTags
                    inv_m = re.search(r"providesTags\s*:[\s\S]*?(\[[^\]]*\])", q_body)
                    provides_tags = (
                        re.findall(r"['\"`]([^'\"`]+)['\"`]", inv_m.group(1))
                        if inv_m
                        else []
                    )

                    query_id = _make_id("rtkq_query", reducer_path, ep_name)
                    _add_node(
                        query_id,
                        f"RTKQ:Query:{ep_name}",
                        "rtkq_query",
                        ep_loc,
                        endpoint_name=ep_name,
                        reducer_path=reducer_path,
                        url_path=url_path,
                        normalized_url_path=_normalize_route_path(url_path),
                        http_method=method,
                        provides_tags=provides_tags,
                    )
                    _add_edge(api_id, query_id, "declares_endpoint", ep_loc)

                    # Auto-generated React hook
                    hook_name = f"use{_capitalize(ep_name)}Query"
                    hook_id = _make_id("rtkq_hook", hook_name)
                    _add_node(
                        hook_id,
                        f"Hook:{hook_name}",
                        "rtkq_hook",
                        ep_loc,
                        hook_name=hook_name,
                    )
                    _add_edge(hook_id, query_id, "exposes_endpoint", ep_loc)

                    query_endpoints[query_id] = {
                        "id": query_id,
                        "name": ep_name,
                        "provides_tags": provides_tags,
                        "url_path": url_path,
                    }

                # Mutation endpoints: name: builder.mutation({ ... })
                for mm in re.finditer(r"(\w+)\s*:\s*builder\.mutation(?:<[^>]*>)?\s*\(", ep_body):
                    ep_name = mm.group(1)
                    m_body, _ = _extract_balanced_braces(ep_body, mm.end() - 1)
                    if not m_body:
                        continue
                    ep_line = start_line + ep_body[: mm.start()].count("\n")
                    ep_loc = f"L{ep_line}"

                    # URL path
                    url_match = re.search(
                        r"(?:url|query)\s*:\s*(?:(?:\([^)]*\)\s*=>\s*)?['\"`]([^'\"`]+)['\"`]|(?:\([^)]*\)\s*=>\s*)?\{[\s\S]*?url\s*:\s*['\"`]([^'\"`]+)['\"`])",
                        m_body,
                    )
                    url_path = ""
                    if url_match:
                        url_path = url_match.group(1) or url_match.group(2) or ""

                    # Method
                    method_match = re.search(r"method\s*:\s*['\"`]([A-Z]+)['\"`]", m_body)
                    method = method_match.group(1) if method_match else "POST"

                    # invalidatesTags
                    inv_block = re.search(r"invalidatesTags\s*:[\s\S]*?(\[[^\]]*\])", m_body)
                    invalidates_tags = (
                        re.findall(r"['\"`]([^'\"`]+)['\"`]", inv_block.group(1))
                        if inv_block
                        else []
                    )

                    mutation_id = _make_id("rtkq_mutation", reducer_path, ep_name)
                    _add_node(
                        mutation_id,
                        f"RTKQ:Mutation:{ep_name}",
                        "rtkq_mutation",
                        ep_loc,
                        endpoint_name=ep_name,
                        reducer_path=reducer_path,
                        url_path=url_path,
                        normalized_url_path=_normalize_route_path(url_path),
                        http_method=method,
                        invalidates_tags=invalidates_tags,
                    )
                    _add_edge(api_id, mutation_id, "declares_endpoint", ep_loc)

                    # Auto-generated React hook
                    hook_name = f"use{_capitalize(ep_name)}Mutation"
                    hook_id = _make_id("rtkq_hook", hook_name)
                    _add_node(
                        hook_id,
                        f"Hook:{hook_name}",
                        "rtkq_hook",
                        ep_loc,
                        hook_name=hook_name,
                    )
                    _add_edge(hook_id, mutation_id, "exposes_endpoint", ep_loc)

                    mutation_endpoints[mutation_id] = {
                        "id": mutation_id,
                        "name": ep_name,
                        "invalidates_tags": invalidates_tags,
                        "url_path": url_path,
                    }

    # -------------------------------------------------------------------------
    # 6. Cache Invalidation Graph: invalidatesTags -> providesTags
    # -------------------------------------------------------------------------
    for mut_id, mut_info in mutation_endpoints.items():
        mut_tags = set(mut_info.get("invalidates_tags", []))
        if not mut_tags:
            continue
        for q_id, q_info in query_endpoints.items():
            q_tags = set(q_info.get("provides_tags", []))
            common = mut_tags & q_tags
            if common:
                _add_edge(
                    mut_id,
                    q_id,
                    "invalidates_cache",
                    tags=sorted(common),
                )

    # -------------------------------------------------------------------------
    # 7. React UI Components, Hooks, Selectors & Dispatches
    # -------------------------------------------------------------------------
    comp_match = re.search(
        r"(?:export\s+(?:default\s+)?)?(?:function|const)\s+([A-Z]\w+)\b", content
    )
    current_comp_id = None
    if comp_match and ("return" in content or "use" in content):
        comp_name = comp_match.group(1)
        current_comp_id = _make_id("component", comp_name)
        _add_node(current_comp_id, f"Component:{comp_name}", "react_component")

    # useSelector calls
    selector_matches = re.finditer(
        r"useSelector\(\s*(?:\(?\s*state\s*\)?\s*=>\s*state(?:\.([a-zA-Z0-9_]+))?|([a-zA-Z0-9_]+)\b)",
        content,
    )
    for sm in selector_matches:
        slice_ref = sm.group(1)
        sel_func = sm.group(2)
        line_num = content[:sm.start()].count("\n") + 1
        loc = f"L{line_num}"
        if slice_ref:
            slice_id = _make_id("rtk_slice", slice_ref)
            sel_node_id = _make_id("rtk_selector", slice_ref)
            _add_node(
                sel_node_id,
                f"RTK:Selector:{slice_ref}",
                "rtk_selector",
                loc,
                slice_ref=slice_ref,
            )
            _add_edge(sel_node_id, slice_id, "selects_from", loc)
            if current_comp_id:
                _add_edge(current_comp_id, sel_node_id, "uses_selector", loc)
        elif sel_func and sel_func != "state":
            sel_node_id = _make_id("rtk_selector", sel_func)
            _add_node(
                sel_node_id,
                f"RTK:Selector:{sel_func}",
                "rtk_selector",
                loc,
                selector_name=sel_func,
            )
            if current_comp_id:
                _add_edge(current_comp_id, sel_node_id, "uses_selector", loc)

    # Component hook usage
    used_hooks = set(re.findall(r"\b(use[A-Z]\w+(?:Query|Mutation))\b", content))
    for hook_name in used_hooks:
        hook_id = _make_id("rtkq_hook", hook_name)
        _add_node(hook_id, f"Hook:{hook_name}", "rtkq_hook")
        if current_comp_id:
            _add_edge(current_comp_id, hook_id, "uses_hook")

    # dispatch calls
    dispatch_matches = re.finditer(
        r"dispatch\(\s*([a-zA-Z0-9_]+)\s*\(",
        content,
    )
    for dm in dispatch_matches:
        action_fn = dm.group(1)
        line_num = content[:dm.start()].count("\n") + 1
        loc = f"L{line_num}"
        if current_comp_id:
            candidate_action_id = _make_id("rtk_action_call", action_fn)
            _add_edge(
                current_comp_id,
                candidate_action_id,
                "dispatches_action",
                loc,
                action_name=action_fn,
            )

    return {"nodes": nodes, "edges": edges}


def resolve_rtk_backend_edges(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cross-resolve RTK Query endpoint nodes to backend route nodes (OpenAPI/FastAPI/Express).

    Connects:
      rtkq_query / rtkq_mutation -> backend_route / openapi_operation
      via 'fetches_from' relation.
    """
    seen_edges: set[tuple[str, str, str]] = {
        (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        for e in edges
    }
    new_edges: list[dict[str, Any]] = []

    # 1. Collect RTK Query endpoints
    rtk_endpoints = [
        n
        for n in nodes
        if n.get("type") in ("rtkq_query", "rtkq_mutation") and n.get("url_path")
    ]
    if not rtk_endpoints:
        return new_edges

    # 2. Collect backend route nodes
    backend_routes: list[tuple[dict[str, Any], str, str]] = []
    for n in nodes:
        ntype = str(n.get("type", ""))
        lbl = str(n.get("label", ""))

        if ntype in ("openapi_operation", "openapi_path", "api_operation", "api_path") or lbl.startswith(("OpenAPI:", "API:")):
            route_path = n.get("path") or n.get("route") or ""
            route_method = str(n.get("http_method") or n.get("method") or "").upper()
            if not route_path and ":" in lbl:
                parts = lbl.split(":")
                route_path = parts[-1].strip()
            if not route_method and route_path:
                m_meth = re.match(r"^(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\s+(.*)$", route_path, re.IGNORECASE)
                if m_meth:
                    route_method = m_meth.group(1).upper()
                    route_path = m_meth.group(2).strip()
            if route_path:
                backend_routes.append((n, _normalize_route_path(route_path), route_method or "GET"))

        elif "route" in ntype or "endpoint" in ntype or n.get("http_route") or n.get("http_path"):
            route_path = n.get("http_route") or n.get("http_path") or n.get("route") or ""
            route_method = str(n.get("http_method") or n.get("method") or "GET").upper()
            if route_path:
                backend_routes.append((n, _normalize_route_path(route_path), route_method))

    # 3. Match RTK Query endpoints against backend routes
    for ep in rtk_endpoints:
        ep_path = _normalize_route_path(ep.get("url_path", ""))
        ep_method = str(ep.get("http_method", "GET")).upper()
        if not ep_path:
            continue

        for backend_node, b_path, b_method in backend_routes:
            match = False
            if ep_path == b_path or ep_path.endswith(b_path) or b_path.endswith(ep_path):
                if ep_method == b_method or not b_method or ep_method == "ALL":
                    match = True

            if match:
                key = (ep["id"], backend_node["id"], "fetches_from")
                if key not in seen_edges:
                    seen_edges.add(key)
                    edge = {
                        "source": ep["id"],
                        "target": backend_node["id"],
                        "relation": "fetches_from",
                        "confidence": "INFERRED",
                        "weight": 1.0,
                        "source_file": ep.get("source_file", ""),
                        "source_location": ep.get("source_location", "L1"),
                    }
                    edges.append(edge)
                    new_edges.append(edge)

    # 4. Resolve candidate action dispatches
    action_nodes_by_name: dict[str, str] = {}
    for n in nodes:
        if n.get("type") in ("rtk_action", "rtk_thunk"):
            act_name = n.get("action_name") or n.get("variable_name")
            if act_name:
                action_nodes_by_name[act_name] = n["id"]

    for e in list(edges):
        if e.get("relation") == "dispatches_action":
            act_name = e.get("action_name")
            if act_name and act_name in action_nodes_by_name:
                e["target"] = action_nodes_by_name[act_name]

    return new_edges
