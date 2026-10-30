"""Cross-file member call resolvers. Moved from graph_fy/extract.py."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _LANGUAGE_BUILTIN_GLOBALS, _make_id
from graph_fy.extractors.csharp import CsharpNameResolver
from graph_fy.extractors.resolution import _is_type_like_definition
from graph_fy.paths import disambiguate_ambiguous_candidates


UNRESOLVED_CALLS_KEY = "unresolved_calls"
_MAX_PARKED_CALLS_PER_NODE = 64


def _park_unresolved_member_call(
    caller_node: dict | None,
    callee: str,
    receiver_type: str,
    lang: str,
    raw_call: dict,
) -> None:
    """Keep a member call whose receiver type is declared nowhere in this corpus.

    A single-repo build can only bind ``obj.method()`` when the receiver's type is
    declared in the same build, so a call into another repository is dropped with
    the receiver type already in hand and nothing about it reaches ``graph.json``
    — the one artifact ``merge-graphs`` and ``global add`` consume. Parking the
    pair on the caller node lets a merged graph finish the edge (#3152).

    The payload carries names only, never node ids: ids are rewritten by the
    remaps and again by the repo prefixing, and a stale id inside metadata would
    fail silently (#3150 was that bug). Names survive every rewrite.
    """
    if not caller_node or not callee or not receiver_type:
        return
    metadata = caller_node.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return
    parked = metadata.setdefault(UNRESOLVED_CALLS_KEY, [])
    if not isinstance(parked, list) or len(parked) >= _MAX_PARKED_CALLS_PER_NODE:
        return
    callee, receiver_type = str(callee), str(receiver_type)
    for previous in parked:
        if (
            isinstance(previous, dict)
            and previous.get("callee") == callee
            and previous.get("receiver_type") == receiver_type
        ):
            return
    entry = {"callee": callee, "receiver_type": receiver_type, "lang": lang}
    location = raw_call.get("source_location")
    if location:
        entry["line"] = str(location)
    parked.append(entry)


def _resolve_swift_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file Swift member calls (``recv.method()``) to the real
    definition of the receiver's type (#1356).

    The shared cross-file call pass drops every ``is_member_call`` because a bare
    method name (``update``) collides across the corpus and inflates god-nodes
    (#543/#1219). Swift extractors record the receiver of each member call and a
    per-file ``name -> type`` table (``swift_type_table``); this pass uses them to
    type the receiver, then emits an edge ONLY when that type name resolves to
    exactly one definition. A type-qualified call (``Type.staticMethod()``) is
    EXTRACTED (the type is named explicitly in source); an instance call typed via
    local inference (``obj.method()``) is INFERRED. The shared-pass member-call drop
    stays intact: this is purely additive and fires only on receiver-typed Swift calls.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    type_table_by_file: dict[str, dict[str, str]] = {}
    for result in per_file:
        tt = result.get("swift_type_table")
        if tt and tt.get("path"):
            type_table_by_file[tt["path"]] = tt.get("table", {})
    if not type_table_by_file:
        return

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    # A genuine Swift type is the target of a `contains` edge from its file node.
    # Bare type references create a same-label shadow node (via ensure_named_node)
    # that carries a source_file but is NOT contained; excluding non-contained
    # nodes keeps that shadow from making a real type name look ambiguous.
    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    # Type name -> definition node ids (real, source-backed, type-like defs only).
    # len != 1 is the god-node guard: an ambiguous type name bails.
    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    # (type_node_id, method_key) -> method_node_id, from `method` edges.
    method_index: dict[tuple[str, str], str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            method_index[(src, _key(tnode.get("label", "")))] = tgt

    # #2561: pending factory bindings (`let x = Factory.make()`) are label-only —
    # resolve each against the factory method's marked plain return type
    # (`swift_plain_return` on the return_type references edge) and fold the
    # result into the declaring file's table so the raw-call loop below types
    # `x.method()` through the existing INFERRED path. Every step is
    # exactly-one guarded; any failure leaves the receiver untyped (no edge,
    # never a wrong one). setdefault: an explicit annotation wins.
    factory_by_file: dict[str, dict] = {}
    for result in per_file:
        tt = result.get("swift_type_table")
        if tt and tt.get("path") and tt.get("factory"):
            factory_by_file[tt["path"]] = tt["factory"]
    if factory_by_file:
        # method nid -> marked plain-return target nids (must be exactly one).
        return_targets_by_method: dict[str, set[str]] = {}
        for e in all_edges:
            if (e.get("relation") == "references"
                    and e.get("context") == "return_type"
                    and (e.get("metadata") or {}).get("swift_plain_return")):
                return_targets_by_method.setdefault(
                    e.get("source"), set()).add(e.get("target"))
        for path, pending in factory_by_file.items():
            # Copy before folding: the resolved label is corpus-dependent and
            # must not leak back into the per-file result.
            table = dict(type_table_by_file.get(path, {}))
            type_table_by_file[path] = table
            for receiver, bind in pending.items():
                try:
                    factory_type, factory_method = bind
                except (TypeError, ValueError):
                    continue
                if factory_type in _LANGUAGE_BUILTIN_GLOBALS:
                    continue
                factory_defs = type_def_nids.get(_key(factory_type), [])
                if len(factory_defs) != 1:
                    continue
                method_nid = method_index.get((factory_defs[0], _key(factory_method)))
                if method_nid is None:
                    continue
                targets = return_targets_by_method.get(method_nid, set())
                if len(targets) != 1:
                    continue
                tnode = node_by_id.get(next(iter(targets)))
                ret_label = str(tnode.get("label", "")) if tnode else ""
                if not ret_label or ret_label in _LANGUAGE_BUILTIN_GLOBALS:
                    continue
                if len(type_def_nids.get(_key(ret_label), [])) != 1:
                    continue
                table.setdefault(receiver, ret_label)

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        # Determine the receiver's type. An upper-cased receiver is itself a type
        # (Type.staticMethod(), Singleton.shared.x()); otherwise look it up in the
        # declaring file's local type table.
        if receiver[:1].isupper():
            type_name = receiver
            type_qualified = True
        else:
            type_name = type_table_by_file.get(rc.get("source_file", ""), {}).get(receiver)
            type_qualified = False
        if not type_name:
            continue
        # A builtin receiver type (Data, NSLock, DispatchQueue, ...) must not
        # resolve to a same-named user symbol — the cross-file CALL resolver and
        # the TS/Python member-call resolvers already skip these globals (#1726);
        # do the same for Swift (#2147).
        if type_name in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        type_defs = type_def_nids.get(_key(type_name), [])
        if not type_defs:
            # Declared nowhere here — in a multi-repo setup that usually means "in
            # a repo this build does not contain", so park it for the merge
            # (#3152). This resolver shares `all_raw_calls` with every other
            # language and Swift raw_calls carry no `lang` tag, so the parked
            # entry's language comes from the declaring file's suffix.
            if str(rc.get("source_file", "")).lower().endswith(".swift"):
                _park_unresolved_member_call(
                    node_by_id.get(caller), callee, type_name, "swift", rc,
                )
            continue
        if len(type_defs) != 1:  # ambiguous -> bail (god-node guard)
            continue
        type_nid = type_defs[0]
        method_nid = method_index.get((type_nid, _key(callee)))
        target = method_nid or type_nid
        relation = "calls" if method_nid else "references"
        if target == caller or (caller, target) in existing_pairs:
            continue
        existing_pairs.add((caller, target))
        # A type-qualified call (`Type.staticMethod()`) names the receiver type
        # explicitly in source, so it is an exact reference — EXTRACTED, matching
        # the Python qualified-class-method pass (#1533). An instance call whose
        # receiver type came from local inference (`obj.method()`) stays INFERRED.
        all_edges.append({
            "source": caller,
            "target": target,
            "relation": relation,
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _resolve_python_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file Python qualified class-method calls (``ClassName.method()``)
    to the class-qualified method node (#1446).

    The shared cross-file call pass drops every ``is_member_call`` because a bare
    method name (``log``) collides across the corpus and inflates god-nodes
    (#543/#1219). That guard is right for *instance* calls (``obj.method()``) but
    misses *class-qualified* calls (``ClassName.method()``), where the receiver is
    an explicitly-named class — an exact, unambiguous reference. This pass uses the
    receiver captured by the extractor, and when it is a capitalized name resolving
    to exactly one class node that owns the called method, emits an EXTRACTED
    ``calls`` edge. Purely additive (only member calls the shared pass skipped),
    with a single-definition god-node guard.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    def _is_python_file(source_file: object) -> bool:
        if not source_file:
            return True
        sf = str(source_file)
        ext = os.path.splitext(sf)[1]
        if not ext:
            return True
        ext_lower = ext.lower()
        if ext_lower in (".py", ".pyi"):
            return True
        from graph_fy.extract import _DISPATCH
        return ext not in _DISPATCH and ext_lower not in _DISPATCH

    node_by_id: dict[str, dict] = {n.get("id"): n for n in all_nodes}

    # A class owns methods: it is the source of one or more `method` edges. Index
    # class label -> owning class node ids (len != 1 is the god-node guard), and
    # (class_node_id, method_key) -> list of method_node_ids.
    class_def_nids: dict[str, list[str]] = {}
    method_index: dict[tuple[str, str], list[str]] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        cnode = node_by_id.get(src)
        if cnode is not None and _is_python_file(cnode.get("source_file")):
            class_def_nids.setdefault(_key(cnode.get("label", "")), []).append(src)
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            slot = method_index.setdefault((src, _key(tnode.get("label", ""))), [])
            if tgt not in slot:
                slot.append(tgt)

    def _method_of(cls: str, callee: str) -> "str | None":
        hits = method_index.get((cls, _key(callee)), [])
        if len(hits) > 1:
            hits = [
                m for m in hits
                if str(node_by_id.get(m, {}).get("label", "")).strip().lstrip(".").removesuffix("()") == callee
            ]
        return hits[0] if len(hits) == 1 else None
    # A class with N methods produced N entries; collapse to a unique set. (No
    # early return when there are no classes: the module arm below resolves
    # `module.func()` where the callable is a plain function, not a method.)
    for k in list(class_def_nids):
        class_def_nids[k] = sorted(set(class_def_nids[k]))

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    # Module-alias arm index (#1883): `module.func()` where `module` is imported.
    # Key on stable node ids, not source_file strings (source_file is relativized
    # by the CLI id-remap pass but raw_calls keep their original path, so a string
    # join would miss under an explicit cache_root). The `imports` edge's source
    # is the caller's own file node; `contains` maps a file node to its children.
    contains_children: dict[str, dict[str, list[str]]] = {}
    file_of_node: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") == "contains":
            src, tgt = e.get("source"), e.get("target")
            tnode = node_by_id.get(tgt)
            if tnode is not None:
                contains_children.setdefault(src, {}).setdefault(
                    _key(tnode.get("label", "")), []).append(tgt)
                file_of_node[tgt] = src
    imported_by_filenode: dict[str, set[str]] = {}
    # Local alias bound by `as` on a specific import edge (#2082): `from pkg import
    # mod as alias` / `import pkg.mod as alias` bind `alias`, not `mod`'s own stem,
    # to the module in the importing file. Keyed by (importing file, target module)
    # so two files aliasing the same module differently each match their own.
    import_alias_by_filenode: dict[str, dict[str, str]] = {}
    for e in all_edges:
        if e.get("relation") in ("imports", "imports_from"):
            imported_by_filenode.setdefault(e.get("source"), set()).add(e.get("target"))
            alias = e.get("local_alias")
            if alias:
                import_alias_by_filenode.setdefault(e.get("source"), {})[e.get("target")] = _key(alias)

    def _module_stem_key(nid: str) -> str:
        n = node_by_id.get(nid)
        if not n:
            return ""
        sf = n.get("source_file") or ""
        stem = Path(sf).stem if sf else ""
        return _key(stem or n.get("label", ""))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}

    def _emit_call(caller: str, target_nid: "str | None", rc: dict) -> None:
        if not target_nid or target_nid == caller or (caller, target_nid) in existing_pairs:
            return
        target_file = node_by_id.get(target_nid, {}).get("source_file")
        if target_file and not _is_python_file(target_file):
            return
        existing_pairs.add((caller, target_nid))
        # EXTRACTED: a qualified call (`ClassName.method()` or `module.func()`) is
        # an explicit, unambiguous static reference resolved to exactly one
        # definition (each arm applies a single-definition god-node guard).
        all_edges.append({
            "source": caller,
            "target": target_nid,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })

    # Pytest fixtures index (#45, #4332, #4333): path -> {fixture_name: {"class": cls, "nid": nid, "file": sf}}
    fixtures_by_path: dict[str, dict[str, dict[str, Any]]] = {}
    for result in per_file:
        py_fixtures = result.get("_py_fixtures")
        if not py_fixtures:
            for n in result.get("nodes", []):
                py_fix = n.get("_py_fixture")
                if py_fix:
                    lbl = str(n.get("label", "")).rstrip("()")
                    if lbl:
                        py_fixtures = py_fixtures or {}
                        py_fixtures[lbl] = {
                            "class": py_fix,
                            "nid": n.get("id"),
                            "file": n.get("source_file", ""),
                        }
        if py_fixtures:
            sf = result.get("source_file") or (result.get("nodes") and result["nodes"][0].get("source_file")) or ""
            if sf:
                fixtures_by_path[str(Path(sf).resolve())] = py_fixtures
                fixtures_by_path[str(Path(sf))] = py_fixtures
                fixtures_by_path[sf] = py_fixtures
    for n in all_nodes:
        py_fix = n.get("_py_fixture")
        sf = n.get("source_file")
        if py_fix and sf:
            lbl = str(n.get("label", "")).rstrip("()")
            if lbl:
                entry = {
                    "class": py_fix,
                    "nid": n.get("id"),
                    "file": sf,
                }
                for k in (str(Path(sf).resolve()), str(Path(sf)), sf):
                    fixtures_by_path.setdefault(k, {})[lbl] = entry

    def _find_fixture(caller_sf: str, fix_name: str) -> dict[str, Any] | None:
        if not caller_sf:
            return None
        p = Path(caller_sf)
        # 1. Check same test file
        for k in (str(p), str(p.resolve()), caller_sf):
            if k in fixtures_by_path and fix_name in fixtures_by_path[k]:
                return fixtures_by_path[k][fix_name]
        # 2. Check conftest.py in same directory and parent directories
        curr = p.parent
        visited = set()
        while curr and curr not in visited:
            visited.add(curr)
            cand = curr / "conftest.py"
            for k in (str(cand), str(cand.resolve())):
                if k in fixtures_by_path and fix_name in fixtures_by_path[k]:
                    return fixtures_by_path[k][fix_name]
            if curr.parent == curr:
                break
            curr = curr.parent
        return None

    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        if not _is_python_file(rc.get("source_file")):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        if receiver[:1].isupper():
            # Class arm (#1446): a capitalized receiver is a class reference; an
            # instance (`self`, `obj`) never collides with a same-spelled class.
            class_nids = class_def_nids.get(_key(receiver), [])
            if len(class_nids) != 1:  # absent or ambiguous -> bail (god-node guard)
                continue
            _emit_call(caller, _method_of(class_nids[0], callee), rc)
        elif rc.get("_python_fixture_param"):
            # Fixture arm (#45, #4332, #4333): a member call on a pytest fixture parameter
            # is typed from the fixture's returned class according to pytest scoping.
            caller_sf = rc.get("source_file", "")
            if not caller_sf:
                caller_node = node_by_id.get(caller)
                if caller_node:
                    caller_sf = caller_node.get("source_file", "")
            fix_info = _find_fixture(caller_sf, receiver)
            if not fix_info or fix_info.get("class") in (None, "", "?"):
                # Untyped fixture shadows anything higher up; emit nothing
                continue
            cls_name = fix_info["class"]
            class_nids = class_def_nids.get(_key(cls_name), [])
            target_class_nid = None
            if len(class_nids) == 1:
                target_class_nid = class_nids[0]
            elif len(class_nids) > 1:
                # Disambiguate to the class visible from the fixture's file
                fix_file = fix_info.get("file", "")
                fix_filenode = file_of_node.get(fix_info.get("nid", ""))
                candidates = [
                    cn for cn in class_nids
                    if (fix_filenode and file_of_node.get(cn) == fix_filenode)
                    or (fix_filenode and cn in imported_by_filenode.get(fix_filenode, set()))
                    or (fix_file and node_by_id.get(cn, {}).get("source_file") == fix_file)
                ]
                if len(candidates) == 1:
                    target_class_nid = candidates[0]
            if target_class_nid:
                method_nid = _method_of(target_class_nid, callee)
                if method_nid and method_nid != caller and (caller, method_nid) not in existing_pairs:
                    existing_pairs.add((caller, method_nid))
                    all_edges.append({
                        "source": caller,
                        "target": method_nid,
                        "relation": "calls",
                        "context": "call",
                        "confidence": "INFERRED",
                        "confidence_score": 0.85,
                        "source_file": rc.get("source_file", ""),
                        "source_location": rc.get("source_location"),
                        "weight": 1.0,
                    })
        else:
            # Module arm (#1883): a lowercase receiver may be an imported module.
            # Resolve it against the modules imported into the caller's own file
            # (so `self`/`obj`/local instances, which are not imported modules,
            # never match), then to the single callable that module contains. A
            # receiver also matches the local alias bound on that import edge
            # (#2082), so an aliased import resolves the same as the bare name.
            rkey = _key(receiver)
            caller_file = file_of_node.get(caller)
            file_aliases = import_alias_by_filenode.get(caller_file, {})
            mods = [t for t in imported_by_filenode.get(caller_file, ())
                    if t in contains_children
                    and (_module_stem_key(t) == rkey or file_aliases.get(t) == rkey)]
            if len(mods) != 1:  # not an imported module, or ambiguous -> bail
                continue
            children = contains_children[mods[0]].get(_key(callee), [])
            if len(children) != 1:  # absent or ambiguous callable -> bail
                continue
            _emit_call(caller, children[0], rc)

    for e in all_edges:
        if e.get("relation") == "depends_on" and e.get("context") == "pytest_fixture":
            tgt = e.get("target")
            if tgt and node_by_id.get(tgt) is None:
                fix_info = _find_fixture(e.get("source_file", ""), tgt)
                if fix_info and fix_info.get("nid"):
                    e["target"] = fix_info["nid"]


def _resolve_typescript_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file TS/JS member calls via constructor-injection type tables (#1316).

    ``this.repo.findById()`` drops out in the shared cross-file pass because bare
    ``findById`` collides across the corpus (god-node guard).  TS constructors with
    parameter-property modifiers (``private repo: IUserRepository``) produce a
    per-file type table mapping field names to their declared types.  This pass
    looks up the receiver field's type, finds a single-definition class/interface
    owning a method with the callee name, and emits a ``calls`` edge — EXTRACTED
    when the receiver names the type in source (``Type.method()``), INFERRED when
    the type came from the table (the Swift/C#/Java tiering).

    Origin gate (#2553): a name-only match is not evidence the caller can even
    see the matched type. ``import type { Repo } from 'external-pkg'`` plus
    ``this.repo.save()`` must not fabricate an edge to an unrelated local
    ``class Repo`` in another file. The matched type must be origin-verified:
    defined in the caller's own file, a named import of the caller's file, or
    contained in a module the caller's file imports. Otherwise EMIT NOTHING —
    a false call edge is worse than a missing one (the C++ resolver's bar).
    """
    type_table_by_file: dict[str, dict[str, str]] = {}
    classes_by_file: dict[str, dict[str, dict[str, str]]] = {}
    for result in per_file:
        tt = result.get("ts_type_table")
        if tt and tt.get("path"):
            type_table_by_file[tt["path"]] = tt.get("table", {})
            if "classes" in tt:
                classes_by_file[tt["path"]] = tt["classes"]
    if not type_table_by_file:
        return

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    method_index: dict[tuple[str, str], str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            method_index[(src, _key(tnode.get("label", "")))] = tgt

    # Origin maps (#2553), built like the Python resolver's module arm: key on
    # stable NODE ids, not source_file strings (raw_calls keep their original
    # pre-relativization paths, so a string join would miss under an explicit
    # cache_root). ``contains`` maps a node to its file node; a member call's
    # caller is usually a METHOD node, which hangs off its class via a ``method``
    # edge instead, so fold those through to the owning class's file.
    file_of_node: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") == "contains":
            file_of_node[e.get("target")] = e.get("source")
    for e in all_edges:
        if e.get("relation") == "method":
            owner_file = file_of_node.get(e.get("source"))
            if owner_file is not None:
                file_of_node.setdefault(e.get("target"), owner_file)
    # Track owning class of each method/closure for class-scoped field resolution (#4294)
    class_of_method: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") == "method":
            owner = e.get("source")
            target = e.get("target")
            if owner is not None and target is not None:
                class_of_method[target] = owner
    for e in all_edges:
        if e.get("relation") in ("defines", "contains") and e.get("source") in class_of_method:
            target = e.get("target")
            if target is not None:
                class_of_method.setdefault(target, class_of_method[e["source"]])
    # ``imports`` targets are the imported symbol nodes; ``imports_from`` targets
    # are module file nodes. A symbol id never collides with a file id, so one
    # set serves both origin checks below.
    imported_by_filenode: dict[str, set[str]] = {}
    for e in all_edges:
        if e.get("relation") in ("imports", "imports_from"):
            imported_by_filenode.setdefault(e.get("source"), set()).add(e.get("target"))

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        if receiver[:1].isupper():
            type_name = receiver
            type_qualified = True  # the receiver names the type in source
        else:
            type_qualified = False
            src_f = rc.get("source_file", "")
            type_name = None
            caller_cls_nid = class_of_method.get(caller)
            if caller_cls_nid:
                cls_node = node_by_id.get(caller_cls_nid)
                if cls_node and cls_node.get("label"):
                    type_name = classes_by_file.get(src_f, {}).get(cls_node["label"], {}).get(receiver)
            if not type_name:
                type_name = type_table_by_file.get(src_f, {}).get(receiver)
        if not type_name:
            continue
        # A builtin global receiver type (Date, Promise, Map, ...) must not resolve
        # to a user symbol. _key() casefolds, so `x: Date; x.getTime()` would bind
        # the caller to a same-named user `class DATE` in another file, inventing
        # phantom `references[call]` edges and a false god node (#1726). The
        # cross-file CALL resolver already skips these globals; do the same here.
        if type_name in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        type_defs = type_def_nids.get(_key(type_name), [])
        if len(type_defs) != 1:
            continue
        type_nid = type_defs[0]
        # Origin gate (#2553): the caller's file must actually see the matched
        # type — same file, a named import of the type, or a module import of
        # the type's file. Otherwise a third-party type name that happens to
        # collide with a local class fabricates an edge; emit nothing.
        caller_file = file_of_node.get(caller)
        type_file = file_of_node.get(type_nid)
        imported = imported_by_filenode.get(caller_file, set())
        if not (
            (caller_file is not None and caller_file == type_file)
            or type_nid in imported
            or (type_file is not None and type_file in imported)
        ):
            continue
        method_nid = method_index.get((type_nid, _key(callee)))
        if not method_nid:
            # Receiver typed, but the type has no such method. The old fallback
            # (a `references` edge to the type node) was another fabrication
            # vector; skip instead, matching the C# resolver.
            continue
        if method_nid == caller or (caller, method_nid) in existing_pairs:
            continue
        existing_pairs.add((caller, method_nid))
        # `Type.method()` names the receiver type explicitly in source —
        # EXTRACTED; a receiver typed via the constructor-injection/local table
        # is inference — INFERRED (the Swift/C#/Java ternary).
        all_edges.append({
            "source": caller,
            "target": method_nid,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _resolve_cpp_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file C++ member calls (``f.bar()``, ``f->bar()``,
    ``Foo::bar()``, ``this->bar()``) to the real definition of the receiver's type
    (#1547).

    The shared cross-file pass drops every ``is_member_call`` because a bare method
    name (``bar``) collides across the corpus and inflates god-nodes (#543/#1219).
    The C++ extractor records each member call's receiver and a per-file
    ``var -> ClassName`` table (``cpp_type_table``) built from local declarations.
    This pass types the receiver, then emits an edge ONLY when that type resolves
    to exactly ONE definition (the god-node guard).

    Receiver typing, by precision tier:
      * ``Foo::bar()`` — the scope ``Foo`` names the type explicitly -> EXTRACTED.
      * ``this->bar()`` — the receiver is the caller's own enclosing class -> EXTRACTED.
      * ``f.bar()`` / ``f->bar()`` — ``f`` typed via the file's local table -> INFERRED.
    A receiver whose type can't be inferred locally is SKIPPED (no guess): a false
    call edge is worse than a missing one. The ``_merge_decl_def_classes`` pass has
    already folded each header/impl class pair into one node, so a paired class is a
    single definition and clears the single-definition guard.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    type_table_by_file: dict[str, dict[str, str]] = {}
    for result in per_file:
        tt = result.get("cpp_type_table")
        if tt and tt.get("path"):
            type_table_by_file[tt["path"]] = tt.get("table", {})

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    # A genuine C++ type is the target of a `contains` edge from its file node;
    # bare-reference shadow nodes (ensure_named_node stubs) are not contained, so
    # excluding non-contained nodes keeps them from making a real type ambiguous.
    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    # (type_node_id, method_key) -> method_node_id, and caller -> enclosing type
    # (the owning class) for `this->` calls. A C++ class owns its members via
    # `method` edges (out-of-line definitions) AND `defines` edges (in-class
    # declarations, which the extractor models as fields); index both so a header-
    # declared `void bar();` resolves. `method` wins when a key has both.
    method_index: dict[tuple[str, str], str] = {}
    enclosing_type: dict[str, str] = {}
    for rel in ("defines", "method"):
        for e in all_edges:
            if e.get("relation") != rel:
                continue
            src, tgt = e.get("source"), e.get("target")
            tnode = node_by_id.get(tgt)
            if tnode is None:
                continue
            enclosing_type.setdefault(tgt, src)
            method_index[(src, _key(tnode.get("label", "")))] = tgt

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        src_file = rc.get("source_file", "")
        # Only resolve C++ raw_calls (other languages share the raw_calls list;
        # a `.h` may route to either extract_cpp or extract_objc by content, so the
        # extractor-stamped `lang` tag — not the suffix — is the unambiguous gate).
        if rc.get("lang") != "cpp":
            continue
        # Determine the receiver's type and the resulting confidence.
        if receiver == "this":
            # this->bar(): receiver is the caller's own enclosing class.
            type_nid = enclosing_type.get(caller)
            if not type_nid:
                continue
            type_qualified = True
        elif receiver[:1].isupper():
            # Foo::bar(): the type is named explicitly in source.
            type_defs = type_def_nids.get(_key(receiver), [])
            if not type_defs:
                # Declared nowhere here, which in a multi-repo setup usually means
                # "in a repo this build does not contain" (#3152). `Foo::bar()` is
                # also the shape of a namespace-qualified free function, so the
                # merge side's guards do the deciding: it acts only when exactly
                # one other repo declares a `Foo` owning exactly one `bar`.
                _park_unresolved_member_call(
                    node_by_id.get(caller), callee, receiver, "cpp", rc,
                )
                continue
            if len(type_defs) != 1:  # ambiguous -> bail (god-node guard)
                continue
            type_nid = type_defs[0]
            type_qualified = True
        else:
            # f.bar() / f->bar(): type the receiver via the file's local table.
            type_name = type_table_by_file.get(src_file, {}).get(receiver)
            if not type_name:
                continue
            type_defs = type_def_nids.get(_key(type_name), [])
            if not type_defs:
                _park_unresolved_member_call(
                    node_by_id.get(caller), callee, type_name, "cpp", rc,
                )
                continue
            if len(type_defs) != 1:  # ambiguous -> bail (god-node guard)
                continue
            type_nid = type_defs[0]
            type_qualified = False
        method_nid = method_index.get((type_nid, _key(callee)))
        target = method_nid or type_nid
        relation = "calls" if method_nid else "references"
        if target == caller or (caller, target) in existing_pairs:
            continue
        existing_pairs.add((caller, target))
        all_edges.append({
            "source": caller,
            "target": target,
            "relation": relation,
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": src_file,
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _resolve_csharp_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve C# member calls (``recv.Method()``) to the receiver's declared type
    (#1609), namespace-aware (#1620).

    The shared cross-file pass drops every ``is_member_call`` because a bare method
    name collides across the corpus — and for C# an in-file bare match silently
    mis-bound ``_server.Save()`` to an unrelated ``Cache.Save()``. The C# extractor
    records each member call's receiver and stamps ``receiver_type`` on the raw
    call from a METHOD-scoped ``name -> Type`` table of class fields/properties
    plus the declaring method's params/locals (#2299 — per-method like Java, so a
    name rebound in a different method never poisons this one; same-method
    conflicts and untypable rebindings are still POISONED, so a shadowing local of
    a different type produces no edge rather than a wrong one). This pass resolves
    the stamped type name with the same namespace/using/alias scoping machinery the
    type-reference pass uses (``CsharpNameResolver``), so a class name duplicated
    across namespaces still binds to the one in scope; only when scoping knows
    nothing about the name does it fall back to the corpus-wide unique bare-name
    match (the god-node guard). An untypable/ambiguous receiver is skipped — never
    a guess.

    Receiver typing, by precision tier:
      * ``this.M()`` — receiver is the caller's own enclosing class -> EXTRACTED.
      * ``base.M()`` — the caller's single resolvable base class -> EXTRACTED.
      * ``Type.M()`` (capitalized) — the type is named explicitly in source -> EXTRACTED.
      * ``recv.M()`` / ``this.recv.M()`` — ``recv`` typed via the extractor's
        method-scoped field/property/param/local table (``receiver_type`` on the
        raw call) -> INFERRED.

    A method not declared on the receiver's type is looked up through its
    ``inherits`` chain; a chain containing an unresolvable (out-of-corpus) base
    poisons the lookup — the method may live there, so no edge is emitted.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if n.get("source_file") and n.get("id") in contained and _is_type_like_definition(n):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    # Namespace/using/alias-aware simple-name resolution, shared with the C#
    # type-reference pass (which has already arbitrated inherits/implements/
    # references targets by the time the resolver registry runs).
    resolver = CsharpNameResolver(all_nodes, all_edges)

    # (type_node_id, method_key) -> method_node_id, and caller -> enclosing type.
    # C# owns its methods via `method` edges.
    method_index: dict[tuple[str, str], list[tuple[str, int | None]]] = {}
    enclosing_type: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        tnode = node_by_id.get(tgt)
        if tnode is None:
            continue
        enclosing_type.setdefault(tgt, src)
        arity = tnode.get("metadata", {}).get("arity")
        if arity is None and "_a" in tgt:
            m = re.search(r"_a(\d+)", tgt)
            if m:
                arity = int(m.group(1))
        method_index.setdefault((src, _key(tnode.get("label", ""))), []).append((tgt, arity))

    # Base-class chain from `inherits` edges (C# files only). The type-reference
    # pass has already re-pointed each resolvable base to its real definition and
    # left unresolvable ones on dangling sourceless stubs — a stub target marks
    # the derived type's base chain as UNRESOLVED (poison: an inherited-member
    # lookup through it must bail, the member may be declared out of corpus).
    bases_of: dict[str, list[str]] = {}
    unresolved_base: set[str] = set()
    for e in all_edges:
        if e.get("relation") != "inherits":
            continue
        src_file = e.get("source_file")
        if not (isinstance(src_file, str) and src_file.endswith(".cs")):
            continue
        src, tgt = e.get("source"), e.get("target")
        if not (isinstance(src, str) and isinstance(tgt, str)):
            continue
        tnode = node_by_id.get(tgt)
        if tnode is None or not tnode.get("source_file"):
            unresolved_base.add(src)
        else:
            bucket = bases_of.setdefault(src, [])
            if tgt not in bucket:
                bucket.append(tgt)

    def _method_on_type_or_bases(type_nid: str, callee_key: str, arg_count: int | None = None) -> str | None:
        """The method's definition on the type or its resolvable base chain.

        A type that declares the method directly wins (overrides shadow the
        base). Otherwise walk `inherits` upward; an unresolved base anywhere the
        walk actually reaches poisons the lookup (no edge), as does anything
        other than exactly one declaration found.
        """
        hits: list[tuple[str, int | None]] = []
        seen: set[str] = set()
        frontier = [type_nid]
        while frontier:
            nid = frontier.pop()
            if nid in seen:
                continue
            seen.add(nid)
            candidates = method_index.get((nid, callee_key), [])
            if candidates:
                hits.extend(candidates)
                continue  # an override shadows anything above it
            if nid in unresolved_base:
                return None  # the method may live on the out-of-corpus base
            frontier.extend(bases_of.get(nid, []))
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0][0]
        if arg_count is not None:
            matching = [h[0] for h in hits if h[1] == arg_count]
            if len(matching) == 1:
                return matching[0]
        return None

    def _resolve_type_name_nid(type_name: str | None, caller_node: dict | None,
                               src_file: str) -> str | None:
        """Resolve a declared type name to exactly one definition node id.

        Namespace/using/alias scoping first (so `Svc` duplicated across
        namespaces binds to the one in scope); when scoping is decisive but
        ambiguous, bail. Only when scoping knows nothing about the name fall
        back to the corpus-wide unique bare-name match (which also covers
        nested types, absent from the scoped index).
        """
        if not type_name:
            return None
        if caller_node is not None:
            resolved, decisive = resolver.resolve_type_name(
                type_name, caller_node, src_file
            )
            if resolved:
                return resolved
            if decisive:
                return None
        type_defs = type_def_nids.get(_key(type_name), [])
        return type_defs[0] if len(type_defs) == 1 else None

    def _park_if_absent(type_name: str | None, caller_node: dict | None, rc: dict) -> None:
        """Park a call whose receiver type is declared nowhere in this corpus (#3152).

        ``_resolve_type_name_nid`` collapses "absent", "ambiguous" and "scoping was
        decisive" into one ``None``, and only the first is a cross-repo candidate,
        so re-check the bare-name index instead of trusting the ``None``.
        """
        if type_name and not type_def_nids.get(_key(type_name)):
            _park_unresolved_member_call(
                caller_node, rc.get("callee"), type_name, "csharp", rc,
            )

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if rc.get("lang") != "csharp" or not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        src_file = rc.get("source_file", "")
        caller_node = node_by_id.get(caller)
        if receiver == "this":
            type_nid = enclosing_type.get(caller)
            if not type_nid:
                continue
            type_qualified = True
        elif receiver == "base":
            enclosing = enclosing_type.get(caller)
            if not enclosing or enclosing in unresolved_base:
                continue
            bases = bases_of.get(enclosing, [])
            if len(bases) != 1:  # no base, or can't tell which — bail
                continue
            type_nid = bases[0]
            type_qualified = True
        elif receiver[:1].isupper():
            # Type.M() — the type is named explicitly (also covers a Pascal-cased
            # local whose name equals its type, resolved via the table below if the
            # explicit-type lookup misses).
            type_nid = _resolve_type_name_nid(receiver, caller_node, src_file)
            if not type_nid:
                type_name = rc.get("receiver_type")
                type_nid = _resolve_type_name_nid(type_name, caller_node, src_file)
                if not type_nid:
                    _park_if_absent(type_name or receiver, caller_node, rc)
                    continue
            type_qualified = True
        else:
            type_name = rc.get("receiver_type")
            if not type_name:
                continue
            type_nid = _resolve_type_name_nid(type_name, caller_node, src_file)
            if not type_nid:  # ambiguous or absent -> bail (god-node guard)
                _park_if_absent(type_name, caller_node, rc)
                continue
            type_qualified = False
        method_nid = _method_on_type_or_bases(type_nid, _key(callee), rc.get("arg_count"))
        if not method_nid:
            continue  # receiver typed, but the type has no such method — skip
        if method_nid == caller or (caller, method_nid) in existing_pairs:
            continue
        existing_pairs.add((caller, method_nid))
        all_edges.append({
            "source": caller,
            "target": method_nid,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": src_file,
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _bind_member_field_tables(
    per_file: list[dict],
    all_nodes: list[dict],
    *,
    lang: str,
) -> dict[str, dict[str, str]]:
    """Bind the exported per-file field tables (#3151) to class node ids.

    Entries are keyed by class label + source_file so they survive every id
    remap; here they are matched back to the one class node carrying that
    label (disambiguated by source_file basename when several share it).
    An entry that stays ambiguous binds nothing - guessing would attach a
    field table to the wrong class.
    """
    by_label: dict[str, list[dict]] = {}
    for n in all_nodes:
        if n.get("label") and n.get("source_file"):
            by_label.setdefault(str(n["label"]), []).append(n)
    bound: dict[str, dict[str, str]] = {}
    for result in per_file:
        for entry in (result.get("member_field_tables") or []):
            if not isinstance(entry, dict) or entry.get("lang") != lang:
                continue
            label, fields = entry.get("class_label"), entry.get("fields")
            if not label or not isinstance(fields, dict):
                continue
            candidates = by_label.get(str(label), [])
            if len(candidates) > 1:
                sf = str(entry.get("source_file") or "")
                exact = [n for n in candidates if str(n.get("source_file")) == sf]
                if len(exact) == 1:
                    candidates = exact
                else:
                    # the node's source_file may have been relativized since
                    # the entry was written; the basename still identifies it
                    base = os.path.basename(sf)
                    candidates = [
                        n for n in candidates
                        if os.path.basename(str(n.get("source_file"))) == base
                    ]
            if len(candidates) != 1:
                continue
            merged = bound.setdefault(candidates[0]["id"], {})
            for fname, tname in fields.items():
                if merged.get(fname) not in (None, tname):
                    merged.pop(fname, None)  # cross-shard conflict: no guess
                else:
                    merged[fname] = tname
    return bound


def _resolve_java_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve Java member calls against the receiver's declared type.

    Explicit type receivers and ``this`` are exact. Fields declared on the
    caller's class plus method parameters and explicit locals are inferred from
    the extractor's method-scoped type table. A missing or ambiguous receiver
    type is skipped rather than falling back to a bare method-name match.
    """
    def key(label: str) -> str:
        return str(label).strip().removeprefix(".").removesuffix("()")

    contained = {edge.get("target") for edge in all_edges
                 if edge.get("relation") == "contains"}
    node_by_id = {node.get("id"): node for node in all_nodes}

    type_def_nids: dict[str, list[str]] = {}
    for node in all_nodes:
        if (
            node.get("source_file")
            and node.get("id") in contained
            and _is_type_like_definition(node)
        ):
            type_def_nids.setdefault(key(node.get("label", "")), []).append(node["id"])

    method_index: dict[tuple[str, str], set[str]] = {}
    enclosing_type: dict[str, str] = {}
    for edge in all_edges:
        if edge.get("relation") != "method":
            continue
        owner, method = edge.get("source"), edge.get("target")
        method_node = node_by_id.get(method)
        if method_node is None:
            continue
        enclosing_type.setdefault(method, owner)
        method_index.setdefault((owner, key(method_node.get("label", ""))), set()).add(method)

    existing_pairs = {(edge.get("source"), edge.get("target")) for edge in all_edges}
    # Inherited fields (#3151): a field declared on a superclass - possibly in
    # another file - types a `this.<field>` receiver in the subclass. Safe
    # because `this.` names a field by construction; no local can shadow it.
    # The per-file tables ride the results keyed by class label + source_file
    # (never node id, see #3150); bind each to its class node, ambiguity skips.
    inherits_bases: dict[str, list[str]] = {}
    for edge in all_edges:
        if edge.get("relation") == "inherits":
            inherits_bases.setdefault(edge["source"], []).append(edge["target"])
    class_fields = _bind_member_field_tables(per_file, all_nodes, lang="java")

    def _inherited_field_type(class_nid, field: str):
        seen: set = set()
        queue = [class_nid]
        while queue:
            cls = queue.pop(0)
            if not cls or cls in seen:
                continue
            seen.add(cls)
            hit = class_fields.get(cls, {}).get(field)
            if hit:
                return hit
            queue.extend(inherits_bases.get(cls, []))
        return None

    for result in per_file:
        for raw_call in result.get("raw_calls", []):
            if raw_call.get("lang") != "java" or not raw_call.get("is_member_call"):
                continue
            receiver = raw_call.get("receiver")
            callee = raw_call.get("callee")
            caller = raw_call.get("caller_nid")
            if not receiver or not callee or not caller:
                continue

            exact = False
            if receiver == "this":
                type_nid = enclosing_type.get(caller)
                exact = True
                if not type_nid:
                    continue
            else:
                type_name = raw_call.get("receiver_type")
                if not type_name and receiver[:1].isupper():
                    type_name = receiver
                    exact = True
                if not type_name and receiver.startswith("this."):
                    type_name = _inherited_field_type(
                        enclosing_type.get(caller), receiver[len("this."):]
                    )
                if not type_name:
                    continue
                type_defs = type_def_nids.get(key(type_name), [])
                if not type_defs:
                    # The type is declared nowhere in this corpus, which in a
                    # multi-repo setup usually means "in a repo this build does
                    # not contain" rather than "does not exist" — park it for the
                    # merge (#3152). An ambiguous name (>1 declaration) is a
                    # local ambiguity that merging only widens, so it stays
                    # dropped, exactly as the guard below already decided.
                    _park_unresolved_member_call(
                        node_by_id.get(caller), callee, type_name, "java", raw_call,
                    )
                    continue
                if len(type_defs) != 1:
                    continue
                type_nid = type_defs[0]

            method_nids = method_index.get((type_nid, key(callee)), set())
            if len(method_nids) != 1:
                continue
            method_nid = next(iter(method_nids))
            if method_nid == caller or (caller, method_nid) in existing_pairs:
                continue
            existing_pairs.add((caller, method_nid))
            all_edges.append({
                "source": caller,
                "target": method_nid,
                "relation": "calls",
                "context": "call",
                "confidence": "EXTRACTED" if exact else "INFERRED",
                "confidence_score": 1.0 if exact else 0.8,
                "source_file": raw_call.get("source_file", ""),
                "source_location": raw_call.get("source_location"),
                "weight": 1.0,
            })


def _resolve_objc_member_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve cross-file Objective-C message sends (``[recv sel]``) to the real
    definition of the receiver's type (#1556).

    The ObjC extractor keeps its same-file selector matching (alloc/init refs,
    dot-syntax accesses, @selector) and additionally emits ``raw_calls`` for every
    message send, with the receiver and the reconstructed selector as the callee.
    This pass types the receiver and emits a cross-file ``calls`` edge ONLY when the
    type resolves to exactly ONE definition (the god-node guard).

    Receiver typing:
      * ``self`` / ``super`` — the caller's own enclosing class -> EXTRACTED.
      * Capitalized receiver (``[Foo new]``) — the type named explicitly -> EXTRACTED.
      * ``[f doThing]`` — ``f`` typed via the file's ``Foo *f`` local table -> INFERRED.
      * ``[self.bar doIt]`` / ``[_ivarBar doIt]`` — the field typed via the class's
        ``@property``/ivar table (locals shadow fields for the bare-identifier
        form) -> INFERRED. Only the exact ``self.<field>`` receiver shape is
        captured; a dotted receiver like ``Foo.shared`` is never passed through,
        because ``_key`` would strip the dot and collide with a real ``FooShared``.
    An uninferable receiver is SKIPPED (no guess), so an ambiguous selector across
    classes never fans out. ``_merge_decl_def_classes`` folds each @interface/@impl
    pair into one node, so a paired class clears the single-definition guard.
    ``@protocol`` declarations are excluded from the receiver-type index: a protocol
    is a contract, not a message receiver, and ObjC keeps protocol and class names in
    separate namespaces, so a same-named pair used to both mis-bind a message to the
    protocol's declaration and, when a real class existed, trip the god-node guard.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """
    type_table_by_file: dict[str, dict[str, str]] = {}
    for result in per_file:
        tt = result.get("objc_type_table")
        if tt and tt.get("path"):
            type_table_by_file[tt["path"]] = tt.get("table", {})

    # #1556: cross-file `field -> ClassName` tables merged per class nid (the
    # .h/.m pair share one id, preserved by _merge_decl_def_classes, so the header's
    # @property entries and the impl's ivar entries land in one table). A cross-file
    # conflict on the same (class, field) drops the entry — no guess.
    field_types_by_class: dict[str, dict[str, str]] = {}
    field_conflicts: set[tuple[str, str]] = set()
    for result in per_file:
        ft = result.get("objc_field_types")
        if not ft:
            continue
        for cls_nid, tbl in (ft.get("tables") or {}).items():
            merged = field_types_by_class.setdefault(cls_nid, {})
            for field, tname in tbl.items():
                if (cls_nid, field) in field_conflicts:
                    continue
                prev = merged.get(field)
                if prev is None:
                    merged[field] = tname
                elif prev != tname:
                    del merged[field]
                    field_conflicts.add((cls_nid, field))

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}

    def _is_protocol_declaration(n: dict) -> bool:
        """A ``@protocol`` declaration, which the ObjC extractor labels ``<Name>``.

        A protocol is a contract, never a message receiver, so it must not be a
        receiver-typing candidate. It stays a valid target for `implements`; only
        this pass's type index excludes it.
        """
        label = str(n.get("label", "")).strip()
        return label.startswith("<") and label.endswith(">")

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}
    for n in all_nodes:
        node_by_id[n.get("id")] = n
        if (n.get("source_file") and n.get("id") in contained
                and _is_type_like_definition(n) and not _is_protocol_declaration(n)):
            type_def_nids.setdefault(_key(n.get("label", "")), []).append(n["id"])

    method_index: dict[tuple[str, str], str] = {}
    enclosing_type: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        enclosing_type.setdefault(tgt, src)
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            # ObjC method labels carry a +/- sigil (`-doThing`); strip it so the
            # selector `doThing` keys to the method.
            method_index[(src, _key(tnode.get("label", "")))] = tgt

    all_raw_calls: list[dict] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    # A @property declared on a superclass types [self.<field> ...] in the
    # subclass too (#3151): walk the inherits chain, nearest table first.
    _objc_bases: dict[str, list[str]] = {}
    for e in all_edges:
        if e.get("relation") == "inherits":
            _objc_bases.setdefault(e["source"], []).append(e["target"])

    def _field_type_up_chain(cls, receiver):
        seen: set = set()
        queue = [cls]
        while queue:
            c = queue.pop(0)
            if not c or c in seen:
                continue
            seen.add(c)
            hit = field_types_by_class.get(c, {}).get(receiver)
            if hit:
                return hit
            queue.extend(_objc_bases.get(c, []))
        return None

    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        src_file = rc.get("source_file", "")
        if rc.get("lang") != "objc":
            continue
        if rc.get("receiver_kind") == "self_field":
            # `[self.bar doIt]`: the extractor stamped the BARE field name; type it
            # via the caller's own class's @property/ivar table. Checked before the
            # capitalized arm so a capitalized field never reads as a class name.
            cls = enclosing_type.get(caller)
            type_name = _field_type_up_chain(cls, receiver) if cls else None
            if not type_name:
                continue
            type_defs = type_def_nids.get(_key(type_name), [])
            if len(type_defs) != 1:  # ambiguous or absent -> bail (god-node guard)
                continue
            type_nid = type_defs[0]
            type_qualified = False
        elif receiver in ("self", "super"):
            type_nid = enclosing_type.get(caller)
            if not type_nid:
                continue
            type_qualified = True
        elif receiver[:1].isupper():
            type_defs = type_def_nids.get(_key(receiver), [])
            if len(type_defs) != 1:  # ambiguous or absent -> bail (god-node guard)
                continue
            type_nid = type_defs[0]
            type_qualified = True
        else:
            # Locals shadow fields: the file's `Foo *f` local table first, then the
            # enclosing class's @property/ivar table (covers `[_ivarBar doIt]`).
            type_name = type_table_by_file.get(src_file, {}).get(receiver)
            if not type_name:
                cls = enclosing_type.get(caller)
                type_name = _field_type_up_chain(cls, receiver) if cls else None
            if not type_name:
                continue
            type_defs = type_def_nids.get(_key(type_name), [])
            if len(type_defs) != 1:  # ambiguous or absent -> bail (god-node guard)
                continue
            type_nid = type_defs[0]
            type_qualified = False
        method_nid = method_index.get((type_nid, _key(callee)))
        target = method_nid or type_nid
        relation = "calls" if method_nid else "references"
        if target == caller or (caller, target) in existing_pairs:
            continue
        existing_pairs.add((caller, target))
        all_edges.append({
            "source": caller,
            "target": target,
            "relation": relation,
            "context": "call",
            "confidence": "EXTRACTED" if type_qualified else "INFERRED",
            "confidence_score": 1.0 if type_qualified else 0.8,
            "source_file": src_file,
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _kotlin_package_index(per_file: list[dict]) -> dict[str, list[dict]]:
    """Group per-file results by the Kotlin package they declare.

    ``kotlin_package`` is stamped by the generic engine from the file's
    ``package_header`` (see extractors/engine.py); every node in the file
    inherits it. Files with no package header contribute nothing.
    """
    pkg_results: dict[str, list[dict]] = {}
    for result in per_file:
        pkg = result.get("kotlin_package")
        if pkg:
            pkg_results.setdefault(pkg, []).append(result)
    return pkg_results


def _resolve_kotlin_import_targets(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Rewrite Kotlin ``imports`` edge targets from the bare last segment to the
    node the written FQN actually names (#2526).

    ``_import_kotlin`` emits ``file --imports--> _make_id(last_segment)`` with
    the full dotted path stamped as ``metadata.target_fqn``. That target dangles
    (node ids carry a file-stem prefix), so build pruned every Kotlin import and
    the import-evidence promotion in the shared call pass never fired. Here the
    per-file ``kotlin_package`` declarations index each package's importable
    (non-member) symbols by exact label; an edge whose ``target_fqn`` splits
    into a known package P plus a Name defined exactly ONCE in P is rewritten to
    that node id. The FQN is written verbatim in source, so the match is exact —
    confidence stays EXTRACTED. Anything else (external dependency, ambiguous
    name) is left untouched and dangles like other languages' external imports.

    Must run BEFORE the shared call pass builds its import-evidence index (it is
    invoked directly in extract(), not via the tail registry run).
    """
    pkg_results = _kotlin_package_index(per_file)
    if not pkg_results:
        return
    # package fqn -> {importable label -> [node ids]}. Member labels (leading
    # dot) are not importable as `P.Name`, and sourceless reference stubs are
    # not definitions; both are excluded so they can't shadow the real symbol.
    pkg_symbols: dict[str, dict[str, list[str]]] = {}
    for pkg, results in pkg_results.items():
        by_label = pkg_symbols.setdefault(pkg, {})
        for result in results:
            for n in result.get("nodes", []):
                if not n.get("source_file") or n.get("type") == "namespace":
                    continue
                label = str(n.get("label", ""))
                if not label or label.startswith("."):
                    continue
                by_label.setdefault(label.strip("()"), []).append(n["id"])
    for e in all_edges:
        if e.get("relation") != "imports":
            continue
        if not str(e.get("source_file", "")).endswith((".kt", ".kts")):
            continue
        fqn = (e.get("metadata") or {}).get("target_fqn", "")
        pkg, _, name = str(fqn).rpartition(".")
        if not pkg or not name:
            continue
        candidates = pkg_symbols.get(pkg, {}).get(name, [])
        if len(candidates) == 1:  # single-candidate guard: never fabricate
            e["target"] = candidates[0]


def _resolve_csharp_qualified_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve C# constructions that name their namespace (#2997).

    `new Infra.Data.Cache()` reaches the shared pass as the bare name `Cache`,
    so a second `Cache` in another namespace makes it ambiguous and it gets no
    edge, even though the source says which one it means. The reference paths
    (field, property, parameter, return) already honour the qualifier through
    `CsharpNameResolver`; this is the construction twin, built the way
    `_resolve_kotlin_qualified_calls` handles the same shape in Kotlin.

    The prefix must equal a declared namespace exactly. A partially qualified
    `new Data.Cache()` under `using Infra;` stays unresolved rather than
    guessing at the using directives in scope. Exactly one candidate produces an
    edge; zero or several leave the call alone. The pass is additive: an
    ambiguous bare name never had an edge to overwrite.
    """
    raw = [
        rc
        for result in per_file
        for rc in result.get("raw_calls", [])
        if rc.get("lang") == "csharp" and rc.get("qualified_prefix")
        and rc.get("callee") and rc.get("caller_nid")
    ]
    if not raw:
        return

    # (namespace, type name) -> nids, over sourced type declarations only, so a
    # sourceless stub minted for a dangling reference cannot win the match.
    by_namespace: dict[tuple[str, str], list[str]] = {}
    for n in all_nodes:
        if not n.get("_callable_class") or not n.get("source_file"):
            continue
        namespace = str((n.get("metadata") or {}).get("namespace") or "")
        label = str(n.get("label", "")).strip("()")
        if namespace and label:
            by_namespace.setdefault((namespace, label), []).append(n["id"])
    if not by_namespace:
        return

    # Scoped to `calls`: a method that both takes a type as a parameter and
    # constructs it already has a `references` edge to it, and that edge says
    # nothing about whether the construction was resolved.
    existing_pairs = {
        (e.get("source"), e.get("target"))
        for e in all_edges
        if e.get("relation") == "calls"
    }
    for rc in raw:
        candidates = by_namespace.get((rc["qualified_prefix"], rc["callee"]), [])
        if len(candidates) != 1:
            continue
        caller = rc["caller_nid"]
        tgt = candidates[0]
        if tgt == caller or (caller, tgt) in existing_pairs:
            continue
        existing_pairs.add((caller, tgt))
        all_edges.append({
            "source": caller,
            "target": tgt,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED",  # the namespace is written verbatim in source
            "confidence_score": 1.0,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


def _resolve_kotlin_qualified_calls(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve Kotlin fully-qualified call expressions (#2550).

    ``com.example.nav.NavGraph()`` parses to a nested navigation_expression
    chain; the engine flattens it and stamps the raw_call with
    ``qualified_prefix="com.example.nav"`` + ``lang="kotlin"`` when EVERY chain
    segment is a plain identifier. The shared pass skips member calls, so these
    raw_calls produced no edge at all — this pass is strictly additive.

    Resolution, guarded by exactly-one-candidate at every step:
      * prefix == a declared package FQN P -> candidates are P's top-level
        callables (functions/classes the file node `contains`) named callee;
      * prefix == P + "." + TypeName where TypeName is a class/object declared
        in P -> candidates are that type's methods (`method` edges, `.callee()`
        label).
    Zero or 2+ candidates -> no edge. The FQN is written verbatim in source, so
    a unique match is EXTRACTED.
    """
    pkg_results = _kotlin_package_index(per_file)
    if not pkg_results:
        return
    raw = [
        rc
        for result in per_file
        for rc in result.get("raw_calls", [])
        if rc.get("lang") == "kotlin" and rc.get("qualified_prefix")
        and rc.get("callee") and rc.get("caller_nid")
    ]
    if not raw:
        return

    node_by_id: dict[str, dict] = {n.get("id"): n for n in all_nodes}
    contains_by_source: dict[str, list[str]] = {}
    methods_by_type: dict[str, list[str]] = {}
    for e in all_edges:
        rel = e.get("relation")
        if rel == "contains":
            contains_by_source.setdefault(e.get("source"), []).append(e.get("target"))
        elif rel == "method":
            methods_by_type.setdefault(e.get("source"), []).append(e.get("target"))

    # package fqn -> {name -> [top-level callable nids]} and
    # package fqn -> {name -> [top-level type nids]} (classes/objects).
    pkg_callables: dict[str, dict[str, list[str]]] = {}
    pkg_types: dict[str, dict[str, list[str]]] = {}
    for pkg, results in pkg_results.items():
        callables = pkg_callables.setdefault(pkg, {})
        types = pkg_types.setdefault(pkg, {})
        for result in results:
            file_nid = next(
                (n["id"] for n in result.get("nodes", [])
                 if n.get("source_file")
                 and n.get("label") == Path(str(n["source_file"])).name),
                None,
            )
            if file_nid is None:
                continue
            for tgt in contains_by_source.get(file_nid, []):
                n = node_by_id.get(tgt)
                if n is None or not n.get("source_file"):
                    continue
                name = str(n.get("label", "")).strip("()")
                if not name or name.startswith("."):
                    continue
                if n.get("_callable"):
                    callables.setdefault(name, []).append(tgt)
                if n.get("_callable_class"):
                    types.setdefault(name, []).append(tgt)

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in raw:
        prefix = rc["qualified_prefix"]
        callee = rc["callee"]
        caller = rc["caller_nid"]
        candidates: list[str] = []
        if prefix in pkg_callables:
            # `P.callee()` — a top-level function or class constructor in P.
            candidates = pkg_callables[prefix].get(callee, [])
        else:
            # `P.Type.callee()` — a method of a class/object declared in P.
            pkg, _, type_name = prefix.rpartition(".")
            type_nids = pkg_types.get(pkg, {}).get(type_name, []) if pkg else []
            if len(type_nids) == 1:
                wanted = f".{callee}"
                candidates = [
                    m for m in methods_by_type.get(type_nids[0], [])
                    if str(node_by_id.get(m, {}).get("label", "")).strip("()") == wanted
                ]
        if len(candidates) != 1:  # zero or ambiguous -> no edge (god-node guard)
            continue
        tgt = candidates[0]
        if tgt == caller or (caller, tgt) in existing_pairs:
            continue
        existing_pairs.add((caller, tgt))
        all_edges.append({
            "source": caller,
            "target": tgt,
            "relation": "calls",
            "context": "call",
            "confidence": "EXTRACTED",  # the FQN is written verbatim in source
            "confidence_score": 1.0,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })


# Kotlin import-target resolution runs EARLY (directly in extract(), before the
# shared call pass builds its import-evidence index) — registering it in the
# tail registry would rewrite the targets after promotion already read them.
# It still uses the registry's LanguageResolver/driver for the suffix gate and
# failure isolation.
