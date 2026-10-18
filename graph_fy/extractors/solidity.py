"""Solidity AST extractor and C3 linearization inherited call/modifier resolver."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _make_id
from graph_fy.ids import normalize_id
from graph_fy.skeleton import get_parser_for_path


def _parse_callee(call_node: Any, source: bytes) -> tuple[str, str | None]:
    """Extract (callee_name, receiver) from a Solidity call_expression.

    Receiver is None for bare calls ('f()'), 'this', 'super', or object identifier.
    Unwraps binary expressions such as 'x || super.f(y)' to identify 'super'.
    """
    expr = call_node.child_by_field_name("function")
    if expr is None:
        expr = next(
            (c for c in call_node.children if c.type in ("expression", "member_expression", "identifier")),
            None,
        )
    if expr is None:
        return "", None
    if expr.type == "expression" and expr.child_count == 1:
        expr = expr.children[0]
    if expr.type == "identifier":
        return source[expr.start_byte:expr.end_byte].decode("utf-8", errors="ignore"), None
    if expr.type == "member_expression":
        attr_node = expr.children[-1]
        callee_name = source[attr_node.start_byte:attr_node.end_byte].decode("utf-8", errors="ignore")
        recv_expr = expr.children[0]
        if recv_expr.type == "expression" and recv_expr.child_count == 1:
            recv_expr = recv_expr.children[0]
        while recv_expr.type == "binary_expression":
            right = recv_expr.child_by_field_name("right") or recv_expr.children[-1]
            recv_expr = right
            if recv_expr.type == "expression" and recv_expr.child_count == 1:
                recv_expr = recv_expr.children[0]
        recv_text = source[recv_expr.start_byte:recv_expr.end_byte].decode("utf-8", errors="ignore")
        return callee_name, recv_text
    return "", None


def extract_solidity(path: Path) -> dict[str, Any]:
    """Extract Solidity contracts, interfaces, libraries, functions, modifiers, and calls."""
    try:
        source_bytes = path.read_bytes()
    except Exception as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    parser, lang_name = get_parser_for_path(".sol")
    if not parser or lang_name != "solidity":
        return {"nodes": [], "edges": [], "error": "tree_sitter_solidity parser not available"}

    try:
        tree = parser.parse(source_bytes)
        if not tree or not tree.root_node:
            return {"nodes": [], "edges": []}
    except Exception as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    raw_calls: list[dict[str, Any]] = []

    str_path = str(path)
    file_nid = _make_id(str_path)
    nodes.append({
        "id": file_nid,
        "label": path.name,
        "file_type": "code",
        "source_file": str_path,
    })

    # Track declarations per contract for same-contract call resolution
    contract_fns: dict[str, set[str]] = {}
    contract_mods: dict[str, set[str]] = {}

    for top in tree.root_node.children:
        if top.type not in ("contract_declaration", "interface_declaration", "library_declaration"):
            continue

        c_name_node = top.child_by_field_name("name")
        if not c_name_node:
            for c in top.children:
                if c.type == "identifier":
                    c_name_node = c
                    break
        if not c_name_node:
            continue

        c_name = source_bytes[c_name_node.start_byte:c_name_node.end_byte].decode("utf-8", errors="ignore")
        if not c_name:
            continue

        c_nid = _make_id(file_nid, c_name)
        c_line = top.start_point[0] + 1
        nodes.append({
            "id": c_nid,
            "label": c_name,
            "file_type": "code",
            "source_file": str_path,
            "source_location": f"L{c_line}",
        })
        edges.append({
            "source": file_nid,
            "target": c_nid,
            "relation": "contains",
            "source_file": str_path,
            "source_location": f"L{c_line}",
        })

        contract_fns[c_name] = set()
        contract_mods[c_name] = set()

        # Inheritance specifiers: 'is A, B'
        for child in top.children:
            if child.type == "inheritance_specifier":
                base_ident = None
                for sub in child.children:
                    if sub.type in ("user_defined_type", "identifier"):
                        base_ident = sub if sub.type == "identifier" else (sub.children[0] if sub.children else None)
                        break
                if base_ident:
                    base_name = source_bytes[base_ident.start_byte:base_ident.end_byte].decode("utf-8", errors="ignore")
                    if base_name:
                        edges.append({
                            "source": c_nid,
                            "target": base_name,
                            "relation": "inherits",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{child.start_point[0] + 1}",
                        })

        body = top.child_by_field_name("body")
        if not body:
            body = next((c for c in top.children if c.type == "contract_body"), None)
        if not body:
            continue

        # Pre-scan members of this contract
        fn_counts: dict[str, int] = {}
        for item in body.children:
            if item.type == "function_definition":
                name_n = item.child_by_field_name("name")
                if name_n:
                    fn_name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8", errors="ignore")
                    fn_counts[fn_name] = fn_counts.get(fn_name, 0) + 1
                    contract_fns[c_name].add(fn_name)
            elif item.type == "modifier_definition":
                name_n = item.child_by_field_name("name") or next((c for c in item.children if c.type == "identifier"), None)
                if name_n:
                    mod_name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8", errors="ignore")
                    contract_mods[c_name].add(mod_name)

        # Full extraction of members and calls
        for item in body.children:
            caller_nid = None
            is_func_or_mod = False
            item_modifiers: list[tuple[str, int]] = []

            if item.type == "function_definition":
                name_n = item.child_by_field_name("name")
                fn_name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8", errors="ignore") if name_n else ""
                if not fn_name:
                    continue
                params = [c for c in item.children if c.type == "parameter"]
                arity = len(params)
                line = item.start_point[0] + 1
                if fn_counts.get(fn_name, 0) > 1:
                    caller_nid = _make_id(c_nid, f"{fn_name}_{arity}_L{line}")
                else:
                    caller_nid = _make_id(c_nid, fn_name)
                nodes.append({
                    "id": caller_nid,
                    "label": f"{fn_name}()",
                    "file_type": "code",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                    "metadata": {"arity": arity, "contract": c_name},
                })
                edges.append({
                    "source": c_nid,
                    "target": caller_nid,
                    "relation": "contains",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                })
                is_func_or_mod = True

            elif item.type == "modifier_definition":
                name_n = item.child_by_field_name("name") or next((c for c in item.children if c.type == "identifier"), None)
                mod_name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8", errors="ignore") if name_n else ""
                if not mod_name:
                    continue
                params = [c for c in item.children if c.type == "parameter"]
                arity = len(params)
                line = item.start_point[0] + 1
                caller_nid = _make_id(c_nid, mod_name)
                nodes.append({
                    "id": caller_nid,
                    "label": f"{mod_name}()",
                    "file_type": "code",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                    "metadata": {"arity": arity, "contract": c_name},
                })
                edges.append({
                    "source": c_nid,
                    "target": caller_nid,
                    "relation": "contains",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                })
                is_func_or_mod = True

            elif item.type == "constructor_definition":
                line = item.start_point[0] + 1
                caller_nid = _make_id(c_nid, "constructor")
                nodes.append({
                    "id": caller_nid,
                    "label": "constructor()",
                    "file_type": "code",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                    "metadata": {"contract": c_name},
                })
                edges.append({
                    "source": c_nid,
                    "target": caller_nid,
                    "relation": "contains",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                })
                is_func_or_mod = True

            elif item.type in ("fallback_receive_definition", "receive_function_definition", "fallback_function_definition"):
                is_receive = "receive" in source_bytes[item.start_byte:item.end_byte].decode("utf-8", errors="ignore")
                kind_name = "receive" if is_receive else "fallback"
                line = item.start_point[0] + 1
                caller_nid = _make_id(c_nid, kind_name)
                nodes.append({
                    "id": caller_nid,
                    "label": f"{kind_name}()",
                    "file_type": "code",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                    "metadata": {"contract": c_name},
                })
                edges.append({
                    "source": c_nid,
                    "target": caller_nid,
                    "relation": "contains",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                })
                is_func_or_mod = True

            elif item.type == "event_definition":
                name_n = item.child_by_field_name("name") or next((c for c in item.children if c.type == "identifier"), None)
                if name_n:
                    ev_name = source_bytes[name_n.start_byte:name_n.end_byte].decode("utf-8", errors="ignore")
                    ev_nid = _make_id(c_nid, ev_name)
                    line = item.start_point[0] + 1
                    nodes.append({
                        "id": ev_nid,
                        "label": f"event {ev_name}",
                        "file_type": "code",
                        "source_file": str_path,
                        "source_location": f"L{line}",
                    })
                    edges.append({
                        "source": c_nid,
                        "target": ev_nid,
                        "relation": "contains",
                        "source_file": str_path,
                        "source_location": f"L{line}",
                    })

            if not is_func_or_mod or not caller_nid:
                continue

            # Modifiers on functions, constructors, receive, and fallback
            for child in item.children:
                if child.type == "modifier_invocation":
                    mod_id_node = next((c for c in child.children if c.type == "identifier"), None)
                    if mod_id_node:
                        mod_name = source_bytes[mod_id_node.start_byte:mod_id_node.end_byte].decode("utf-8", errors="ignore")
                        mod_line = child.start_point[0] + 1
                        if mod_name in contract_mods.get(c_name, set()):
                            edges.append({
                                "source": caller_nid,
                                "target": _make_id(c_nid, mod_name),
                                "relation": "uses",
                                "confidence": "EXTRACTED",
                                "confidence_score": 1.0,
                                "source_file": str_path,
                                "source_location": f"L{mod_line}",
                            })
                        else:
                            raw_calls.append({
                                "caller_nid": caller_nid,
                                "callee": mod_name,
                                "is_member_call": False,
                                "solidity_call": "modifier",
                                "contract": c_name,
                                "source_file": str_path,
                                "source_location": f"L{mod_line}",
                            })

            # Walk function body for calls
            def _walk_calls(n: Any) -> None:
                if n.type == "call_expression":
                    callee, recv = _parse_callee(n, source_bytes)
                    if callee and callee not in ("require", "assert", "revert"):
                        args = [c for c in n.children if c.type == "call_argument"]
                        arity = len(args)
                        call_line = n.start_point[0] + 1
                        if recv == "super":
                            raw_calls.append({
                                "caller_nid": caller_nid,
                                "callee": callee,
                                "is_member_call": False,
                                "solidity_call": "super",
                                "contract": c_name,
                                "arity": arity,
                                "source_file": str_path,
                                "source_location": f"L{call_line}",
                            })
                        elif recv in (None, "this"):
                            # If defined in same contract, emit direct call edge
                            if callee in contract_fns.get(c_name, set()):
                                edges.append({
                                    "source": caller_nid,
                                    "target": _make_id(c_nid, callee),
                                    "relation": "calls",
                                    "confidence": "EXTRACTED",
                                    "confidence_score": 1.0,
                                    "source_file": str_path,
                                    "source_location": f"L{call_line}",
                                })
                            else:
                                raw_calls.append({
                                    "caller_nid": caller_nid,
                                    "callee": callee,
                                    "is_member_call": False,
                                    "solidity_call": "inherited",
                                    "contract": c_name,
                                    "arity": arity,
                                    "source_file": str_path,
                                    "source_location": f"L{call_line}",
                                })
                for child in n.children:
                    _walk_calls(child)

            _walk_calls(item)

    return {
        "nodes": nodes,
        "edges": edges,
        "raw_calls": raw_calls,
    }


def c3_linearize(contract_name: str, bases_map: dict[str, list[str]]) -> list[str]:
    """Compute C3 linearization order for a Solidity contract (matching solc).

    In 'contract C is A, B', B is evaluated before A.
    """
    bases = bases_map.get(contract_name, [])
    if not bases:
        return [contract_name]
    reversed_bases = list(reversed(bases))
    seqs = [c3_linearize(b, bases_map) for b in reversed_bases] + [list(reversed_bases)]
    res = [contract_name]
    while True:
        seqs = [s for s in seqs if s]
        if not seqs:
            break
        candidate = None
        for s in seqs:
            head = s[0]
            if not any(head in other[1:] for other in seqs):
                candidate = head
                break
        if not candidate:
            return [contract_name] + list(reversed_bases)
        res.append(candidate)
        for s in seqs:
            if s[0] == candidate:
                s.pop(0)
    return res


def resolve_solidity_inherited_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve inherited calls, super calls, and modifiers via C3 linearization (#39, #4379, #4384)."""
    node_by_id = {n.get("id"): n for n in all_nodes}

    # Contract inheritance graph
    bases_map: dict[str, list[str]] = {}
    contract_nodes: dict[str, str] = {}
    for n in all_nodes:
        # Check if contract node (contains function or event or inherits)
        lbl = n.get("label", "")
        if lbl and not lbl.endswith("()") and not lbl.startswith("."):
            contract_nodes[lbl] = n.get("id")

    for e in all_edges:
        if e.get("relation") == "inherits":
            src = e.get("source")
            tgt = e.get("target")
            src_node = node_by_id.get(src)
            tgt_node = node_by_id.get(tgt)
            src_name = src_node.get("label") if src_node else None
            tgt_name = tgt_node.get("label") if tgt_node else tgt
            if src_name and tgt_name:
                bases_map.setdefault(src_name, []).append(tgt_name)

    # Callables indexed by contract and name:
    # contract_name -> callable_name -> list of (fn_nid, arity)
    contract_functions: dict[str, dict[str, list[tuple[str, int]]]] = {}
    contract_modifiers: dict[str, dict[str, list[str]]] = {}

    for e in all_edges:
        if e.get("relation") == "contains":
            c_nid = e.get("source")
            m_nid = e.get("target")
            c_node = node_by_id.get(c_nid)
            m_node = node_by_id.get(m_nid)
            if not c_node or not m_node:
                continue
            c_name = c_node.get("label", "")
            m_label = m_node.get("label", "")
            meta = m_node.get("metadata", {})
            arity = meta.get("arity", 0)

            clean_name = m_label.rstrip("()")
            if clean_name:
                contract_functions.setdefault(c_name, {}).setdefault(clean_name, []).append((m_nid, arity))
                contract_modifiers.setdefault(c_name, {}).setdefault(clean_name, []).append(m_nid)

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}

    for result in per_file:
        for rc in result.get("raw_calls", []):
            call_type = rc.get("solidity_call")
            if not call_type:
                continue

            c_name = rc.get("contract")
            callee = rc.get("callee")
            caller = rc.get("caller_nid")
            arity = rc.get("arity", 0)
            if not c_name or not callee or not caller:
                continue

            order = c3_linearize(c_name, bases_map)

            if call_type == "inherited":
                for base in order[1:]:
                    funcs = contract_functions.get(base, {}).get(callee, [])
                    if funcs:
                        matches = [nid for nid, a in funcs if a == arity]
                        if len(matches) == 1:
                            target_nid = matches[0]
                            if (caller, target_nid) not in existing_pairs:
                                existing_pairs.add((caller, target_nid))
                                all_edges.append({
                                    "source": caller,
                                    "target": target_nid,
                                    "relation": "calls",
                                    "confidence": "EXTRACTED",
                                    "confidence_score": 1.0,
                                    "source_file": rc.get("source_file", ""),
                                    "source_location": rc.get("source_location"),
                                    "weight": 1.0,
                                })
                            break
                        elif len(matches) > 1:
                            break  # ambiguous overload in winning base contract

            elif call_type == "super":
                # Find caller contract index in C3 linearization
                caller_contract = node_by_id.get(caller, {}).get("metadata", {}).get("contract", c_name)
                start_idx = (order.index(caller_contract) + 1) if caller_contract in order else 1
                for base in order[start_idx:]:
                    funcs = contract_functions.get(base, {}).get(callee, [])
                    if funcs:
                        matches = [nid for nid, a in funcs if a == arity]
                        if len(matches) == 1:
                            target_nid = matches[0]
                            if (caller, target_nid) not in existing_pairs:
                                existing_pairs.add((caller, target_nid))
                                all_edges.append({
                                    "source": caller,
                                    "target": target_nid,
                                    "relation": "calls",
                                    "confidence": "EXTRACTED",
                                    "confidence_score": 1.0,
                                    "source_file": rc.get("source_file", ""),
                                    "source_location": rc.get("source_location"),
                                    "weight": 1.0,
                                })
                            break
                        elif len(matches) > 1:
                            break

            elif call_type == "modifier":
                for base in order[1:]:
                    mods = contract_modifiers.get(base, {}).get(callee, [])
                    if mods:
                        if len(mods) == 1:
                            target_nid = mods[0]
                            if (caller, target_nid) not in existing_pairs:
                                existing_pairs.add((caller, target_nid))
                                all_edges.append({
                                    "source": caller,
                                    "target": target_nid,
                                    "relation": "uses",
                                    "confidence": "EXTRACTED",
                                    "confidence_score": 1.0,
                                    "source_file": rc.get("source_file", ""),
                                    "source_location": rc.get("source_location"),
                                    "weight": 1.0,
                                })
                            break
                        elif len(mods) > 1:
                            break
