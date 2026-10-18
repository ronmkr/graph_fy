"""JavaScript and TypeScript AST extractor."""
from __future__ import annotations

import hashlib
from pathlib import Path

from graph_fy.extractors.base import _LANGUAGE_BUILTIN_GLOBALS, _file_stem, _make_id, _read_text
from graph_fy.ids import normalize_id
from graph_fy.extractors.models import LanguageConfig
from graph_fy.extractors.resolution import _resolve_js_import_target
from graph_fy.extractors.ts_normalize import _normalize_ts_import_types
from graph_fy.extractors.js_web import _extract_js_rationale, _rescue_js_dynamic_imports
from graph_fy.extractors.rtk import is_rtk_file, extract_rtk


def _import_js(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None = None) -> None:
    is_reexport = node.type == "export_statement"
    # Only handle export_statement if it has a `from` clause (re-export).
    # Pure exports like `export const x = 1` or `export { localVar }` have no source module.
    if is_reexport:
        has_from = any(child.type == "from" or (_read_text(child, source) == "from") for child in node.children if child.type in ("from", "identifier"))
        if not has_from:
            # Check for string child (source path) as a more reliable indicator
            has_from = any(child.type == "string" for child in node.children)
            if not has_from:
                return

    # `import type {...} from` / `export type {...} from` are erased by the
    # TypeScript compiler: no runtime emit, no module-graph edge. The
    # dependency is still real for "what references this type", so the edge is
    # stamped `type_only` rather than dropped, and Import Cycles excludes it
    # the way it already excludes deferred `import(...)` (#1241) - on the
    # reporter's corpus all 3 reported cycles closed only through these
    # (#3123). The grammar keeps `import type from './x'` (a default binding
    # NAMED type) distinct: there `type` sits inside the import_clause, not as
    # a bare keyword child of the statement. A mixed `import { type B, C }`
    # stays a runtime edge - C is a runtime import.
    is_type_only = any(
        child.type == "type" and not child.is_named for child in node.children
    )
    resolved_path: "Path | None" = None
    module_string = None
    for child in node.children:
        if child.type == "string":
            module_string = child
            break
        if child.type == "import_require_clause":
            # TS import-equals form: `import x = require("./m")`. The module
            # string sits inside the clause, not on the import_statement
            # itself, so the direct-child scan above never sees it.
            module_string = next(
                (sub for sub in child.children if sub.type == "string"), None
            )
            break
    if module_string is not None:
        raw = _read_text(module_string, source).strip("'\"` ")
        resolved = _resolve_js_import_target(raw, str_path)
        if resolved is not None:
            tgt_nid, resolved_path = resolved
            # `_resolve_js_import_path` returns the attempted path when no
            # local file exists. Static ES imports must treat that as unresolved
            # rather than minting a checkout-specific target ID (#2457).
            if resolved_path is not None and not resolved_path.is_file():
                tgt_nid = _make_id("ref", raw)
                resolved_path = None
            edge = {
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports_from",
                "context": "re-export" if is_reexport else "import",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            }
            # Stamp the resolved target file so a same-basename cross-extension
            # sibling (foo.ts importing/re-exporting ./foo.mjs) keys its target salt
            # by the TARGET's file rather than the importer's. Both files collapse to
            # the base id `foo`; without this the salted lookup mis-points the target
            # back onto the importer's own variant, a phantom self-loop (#1814).
            if resolved_path is not None:
                edge["target_file"] = str(resolved_path)
            if is_type_only:
                edge["type_only"] = True
            edges.append(edge)

    # Emit symbol-level edges for named imports/re-exports from local/aliased files.
    # e.g. `import { Foo, type Bar } from './bar'` → file → Foo, file → Bar (EXTRACTED)
    # e.g. `export { Foo } from './bar'` → file → Foo (re_exports edge)
    # Uses the same _make_id(target_stem, name) key that _extract_generic emits when
    # defining the symbol, so these edges wire importers directly to existing symbol nodes.
    if resolved_path is not None:
        target_stem = _file_stem(resolved_path)
        line = node.start_point[0] + 1

        if is_reexport:
            # Handle: export { foo, bar } from './module'
            #         export { default as baz } from './module'
            for child in node.children:
                if child.type == "export_clause":
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            # The exported name is the local name from the source module
                            name_node = spec.child_by_field_name("name")
                            if name_node:
                                sym = _read_text(name_node, source)
                                if sym == "default":
                                    continue  # skip default re-exports for ID matching
                                edges.append({
                                    **({"type_only": True} if is_type_only else {}),
                                    "source": file_nid,
                                    "target": _make_id(target_stem, sym),
                                    "relation": "re_exports",
                                    "context": "re-export",
                                    "confidence": "EXTRACTED",
                                    "source_file": str_path,
                                    "source_location": f"L{line}",
                                    "weight": 1.0,
                                    "target_file": str(resolved_path),
                                })
        else:
            # Handle: import { Foo, type Bar } from './bar'
            for child in node.children:
                if child.type == "import_clause":
                    for sub in child.children:
                        if sub.type == "named_imports":
                            for spec in sub.children:
                                if spec.type == "import_specifier":
                                    name_node = spec.child_by_field_name("name")
                                    if name_node:
                                        sym = _read_text(name_node, source)
                                        edges.append({
                                            "source": file_nid,
                                            "target": _make_id(target_stem, sym),
                                            "relation": "imports",
                                            "context": "import",
                                            "confidence": "EXTRACTED",
                                            "source_file": str_path,
                                            "source_location": f"L{line}",
                                            "weight": 1.0,
                                            "target_file": str(resolved_path),
                                        })


_JS_SCOPE_BOUNDARY = frozenset({
    "function_declaration", "function_expression", "function", "arrow_function",
    "method_definition", "class_declaration", "class", "generator_function",
    "generator_function_declaration",
})

_JS_FUNCTION_VALUE_TYPES = frozenset({
    "arrow_function", "function_expression", "function", "generator_function",
})


def _js_collect_pattern_idents(node, source: bytes, bound: set) -> None:
    """Collect binding identifier names from a JS/TS pattern (a parameter, or a
    declarator LHS). Recurses through destructuring (object/array patterns, rest)
    but never into the default-value side of `x = default` or a type annotation,
    so only names actually bound by the pattern are collected."""
    t = node.type
    if t in ("identifier", "shorthand_property_identifier_pattern"):
        bound.add(_read_text(node, source))
        return
    if t == "type_annotation":
        return  # `(h: Handler)` — Handler is a type, not a bound name
    if t == "assignment_pattern":  # `x = default` — only x is bound
        left = node.child_by_field_name("left")
        if left is not None:
            _js_collect_pattern_idents(left, source, bound)
        return
    if t == "pair_pattern":  # `{ a: localName }` — localName is bound
        val = node.child_by_field_name("value")
        if val is not None:
            _js_collect_pattern_idents(val, source, bound)
        return
    for c in node.children:
        if c.is_named:
            _js_collect_pattern_idents(c, source, bound)

def _js_local_bound_names(func_node, source: bytes) -> set[str]:
    """Names bound locally inside a JS/TS function: parameters plus `const`/`let`/
    `var` declarator targets. Mirrors `_python_local_bound_names`: an argument that
    is a parameter or local binding names a local value, not a same-named module
    function, so it must not manufacture an indirect_call edge. Nested function and
    class scopes are not descended into."""
    bound: set[str] = set()
    params = func_node.child_by_field_name("parameters")
    if params is not None:
        _js_collect_pattern_idents(params, source, bound)
    # An arrow with ONE unparenthesised parameter exposes it as `parameter`
    # (singular) — there is no `parameters` list node — so `x => f(x)` bound
    # nothing at all and `x` read as a by-name reference to any same-named
    # callable in the corpus. Same singular/plural trap as `catch_clause`.
    solo = func_node.child_by_field_name("parameter")
    if solo is not None:
        _js_collect_pattern_idents(solo, source, bound)

    def walk(n) -> None:
        for c in n.children:
            if c.type in _JS_SCOPE_BOUNDARY:
                continue  # inner scope — its bindings are not this function's locals
            if c.type == "variable_declarator":
                name = c.child_by_field_name("name")
                if name is not None:
                    _js_collect_pattern_idents(name, source, bound)
            elif c.type == "for_in_statement":
                # `for (const entry of xs)` / `for (const {k} of xs)`: the loop
                # binding is the `left` pattern, NOT wrapped in a
                # variable_declarator, so the branch above misses it and `entry`
                # read as a by-name reference to any same-named module callable
                # (#2606). C-style `for (let i = 0; ...)` uses a lexical_declaration
                # with real declarators, already covered by the recursion below.
                left = c.child_by_field_name("left")
                if left is not None:
                    _js_collect_pattern_idents(left, source, bound)
            walk(c)

    body = func_node.child_by_field_name("body")
    if body is not None:
        walk(body)
    return bound

def _js_module_bound_names(root, source: bytes) -> set[str]:
    """Module-scope names rebound to NON-function data (`const X = {...}`, `let y = 5`).

    The JS/TS module-scope shadow set. Unlike the per-function set, a declarator
    whose value is itself a function (`const cb = () => {}`) is EXCLUDED: that name
    IS a callable we want dispatch tables to resolve to, not a data shadow.
    """
    bound: set[str] = set()

    def walk(n) -> None:
        for c in n.children:
            if c.type in _JS_SCOPE_BOUNDARY:
                continue
            if c.type == "variable_declarator":
                value = c.child_by_field_name("value")
                if value is None or value.type not in _JS_FUNCTION_VALUE_TYPES:
                    name = c.child_by_field_name("name")
                    if name is not None:
                        _js_collect_pattern_idents(name, source, bound)
            walk(c)

    walk(root)
    return bound

def _js_import_binds_external(raw: str, str_path: str) -> bool:
    """True when a JS/TS import specifier names a module outside the scanned corpus.

    Reuses `_resolve_js_import_target`, so this is graph_fy's own verdict rather
    than a second opinion: a specifier it cannot resolve is an external package
    (the `ref`-namespaced branch). The extra `node_modules` test covers the case
    where resolution *succeeds* but lands in a dependency tree — a `tsconfig`
    `paths` entry mapping a package to its own installed copy
    (`"lucide-react": ["./node_modules/lucide-react"]`) is common, and
    `node_modules` is pruned from every scan, so the target is never a node.
    """
    resolved = _resolve_js_import_target(raw, str_path)
    if resolved is None:
        return False  # empty specifier — binds nothing
    _target_nid, resolved_path = resolved
    if resolved_path is None:
        return True  # unresolved after relative / alias / workspace lookup
    return "node_modules" in resolved_path.parts


def _js_external_import_names(root, source: bytes, str_path: str) -> set[str]:
    """Names an `import` binds to a module OUTSIDE the corpus.

    An imported name is a module-scoped binding: within this file it denotes the
    imported symbol and nothing else. Neither shadow set collects it —
    `_js_local_bound_names` reads parameters and `variable_declarator`s and
    `_js_module_bound_names` only the latter — so the name reaches
    `_emit_indirect_ref` as an unresolved by-name reference, gets resolved against
    the corpus-wide label index, and fabricates an `indirect_call` (INFERRED, 0.8)
    to any unique same-named callable elsewhere in the corpus. That is the symptom
    already fixed for `catch` bindings, single-parameter arrows and untracked
    closures; an import binding is the same class of shadow, and a UI kit makes it
    land constantly because icon names (`Palette`, `Search`, `Filter`) collide with
    ordinary component names.

    Only imports the corpus cannot contain are collected. A relative specifier
    resolves to a real file and that edge is the graph's whole point, so those
    names stay resolvable.
    """
    bound: set[str] = set()

    def _clause_names(clause) -> None:
        for c in clause.children:
            if c.type == "identifier":            # import Default from "pkg"
                bound.add(_read_text(c, source))
            elif c.type == "namespace_import":    # import * as NS from "pkg"
                for ident in c.children:
                    if ident.type == "identifier":
                        bound.add(_read_text(ident, source))
            elif c.type == "named_imports":       # import { A, B as C } from "pkg"
                for spec in c.children:
                    if spec.type != "import_specifier":
                        continue
                    idents = [g for g in spec.children if g.type == "identifier"]
                    # `B as C` exposes both names; only the LAST one is bound here.
                    if idents:
                        bound.add(_read_text(idents[-1], source))

    def walk(n) -> None:
        for c in n.children:
            if c.type == "import_statement":
                src_node = c.child_by_field_name("source")
                if src_node is not None:
                    raw = _read_text(src_node, source).strip("\"'`")
                    if _js_import_binds_external(raw, str_path):
                        for child in c.children:
                            if child.type == "import_clause":
                                _clause_names(child)
                continue
            walk(c)

    walk(root)
    return bound


def _js_dispatch_value_idents(coll_node):
    """Yield identifier value-nodes of a JS/TS object/array literal that are
    function-reference candidates: object property VALUES and shorthand properties
    (`{ handler }`), and array elements. Keys and inline methods are not references."""
    if coll_node.type == "object":
        for c in coll_node.children:
            if c.type == "pair":
                val = c.child_by_field_name("value")
                if val is not None and val.type == "identifier":
                    yield val
            elif c.type == "shorthand_property_identifier":
                yield c
    else:  # array
        for el in coll_node.children:
            if el.type == "identifier":
                yield el

def _find_body(node, config: LanguageConfig):
    """Find the body node using config.body_field, falling back to child types."""
    b = node.child_by_field_name(config.body_field)
    if b:
        return b
    for child in node.children:
        if child.type in config.body_fallback_child_types:
            return child
    return None

def _dynamic_import_js(node, source: bytes, caller_nid: str, str_path: str, edges: list,
                       seen_dyn_pairs: set) -> bool:
    """Detect dynamic import() calls in JS/TS and emit imports_from edges.

    Handles patterns like:
      await import('./foo.js')
      import('./foo.js').then(...)
      const m = await import(`./foo`)

    Returns True if the node was a dynamic import (caller should skip normal call handling).
    """
    # Dynamic import is a call_expression whose function child is the keyword "import".
    # tree-sitter-typescript parses `import('...')` as call_expression with first child
    # being an "import" token (type="import").
    func_node = node.child_by_field_name("function")
    if func_node is None:
        # Fallback: check first child directly (some TS versions)
        if node.children and _read_text(node.children[0], source) == "import":
            func_node = node.children[0]
        else:
            return False
    if _read_text(func_node, source) != "import":
        return False

    # Extract the module path from the arguments
    args = node.child_by_field_name("arguments")
    if args is None:
        return True  # It's an import() but no args — skip
    for arg in args.children:
        if arg.type == "template_string":
            # Skip dynamic template literals — path can't be statically resolved
            if any(c.type == "template_substitution" for c in arg.children):
                break
            raw = _read_text(arg, source).strip("`")
        elif arg.type == "string":
            raw = _read_text(arg, source).strip("'\" ")
        else:
            continue
        if not raw:
            break
        # Resolve path using the same logic as static imports.
        resolved = _resolve_js_import_target(raw, str_path)
        if resolved is None:
            break
        tgt_nid, resolved_path = resolved
        pair = (caller_nid, tgt_nid)
        if pair not in seen_dyn_pairs:
            seen_dyn_pairs.add(pair)
            edge = {
                "source": caller_nid,
                "target": tgt_nid,
                # A deferred `import(...)` is a real dependency, so keep it as an
                # `imports_from` edge (visible in the graph) but mark it `deferred`
                # so find_import_cycles does not treat it as a static import and
                # report a phantom file cycle (#1241).
                "relation": "imports_from",
                "context": "import",
                "deferred": True,
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            }
            # Key the target salt by the resolved target file so a same-basename
            # cross-extension sibling isn't mis-salted onto the importer (#1814).
            if resolved_path is not None:
                edge["target_file"] = str(resolved_path)
            edges.append(edge)
        break
    return True


def _ts_receiver_type_table(
    root,
    source: bytes,
    table: dict[str, str],
    class_tables: dict[str, dict[str, str]] | None = None,
) -> None:
    """Add TS/JS receiver bindings to ``table`` (name -> TypeName) and ``class_tables``,
    for member-call resolution beyond constructor parameter properties (#1630, #4473):

      * class field definitions: ``svc: Svc``, ``private db = new Database()``;
      * method/accessor/static-block assignments: ``this.cache = new CacheService()``;
      * local ``const/let/var x = new Foo()`` -> ``x: Foo`` (Pattern A);
      * type-annotated parameters ``(svc: Svc)`` -> ``svc: Svc`` (Pattern B).

    Class-scoped field types are stored in ``class_tables[class_name]`` with inheritance
    resolution (subclass inherits base class fields), preventing collisions between
    classes sharing field names (#4294)."""
    def _bare_type_ident(type_annotation):
        if type_annotation is None:
            return None
        idents = [c for c in type_annotation.children if c.type in ("type_identifier", "identifier")]
        others = [c for c in type_annotation.children
                  if c.is_named and c.type not in ("type_identifier", "identifier")]
        if len(idents) == 1 and not others:
            return _read_text(idents[0], source)
        return None

    direct_fields: dict[str, dict[str, str]] = {}
    heritage_map: dict[str, list[str]] = {}

    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if t in ("class_declaration", "class"):
            name_n = n.child_by_field_name("name")
            if name_n is not None:
                cls_name = _read_text(name_n, source)
                cls_fields = direct_fields.setdefault(cls_name, {})
                for child in n.children:
                    if child.type == "class_heritage":
                        for hc in child.children:
                            if hc.type == "extends_clause":
                                for sub in hc.children:
                                    if sub.type in ("type_identifier", "identifier"):
                                        heritage_map.setdefault(cls_name, []).append(_read_text(sub, source))
                body = n.child_by_field_name("body")
                if body is not None:
                    for m in body.children:
                        if m.type in ("public_field_definition", "field_definition", "property_definition"):
                            fname_n = m.child_by_field_name("name")
                            ann_n = m.child_by_field_name("type")
                            val_n = m.child_by_field_name("value")
                            if fname_n is not None:
                                fname = _read_text(fname_n, source)
                                ftype = _bare_type_ident(ann_n) if ann_n is not None else None
                                if not ftype and val_n is not None and val_n.type == "new_expression":
                                    ctor = val_n.child_by_field_name("constructor")
                                    if ctor is not None and ctor.type in ("identifier", "type_identifier"):
                                        ftype = _read_text(ctor, source)
                                if fname and ftype:
                                    cls_fields[fname] = ftype
                        elif m.type in ("method_definition", "class_static_block"):
                            param_types: dict[str, str] = {}
                            params = m.child_by_field_name("parameters")
                            if params is not None:
                                for p in params.children:
                                    if p.type in ("required_parameter", "optional_parameter"):
                                        p_name = p.child_by_field_name("pattern")
                                        p_type = p.child_by_field_name("type")
                                        if p_name is not None and p_type is not None:
                                            tname = _bare_type_ident(p_type)
                                            if tname:
                                                p_str = _read_text(p_name, source)
                                                param_types[p_str] = tname
                                                has_mod = any(
                                                    x.type in ("accessibility_modifier", "readonly")
                                                    for x in p.children
                                                )
                                                if has_mod:
                                                    cls_fields[p_str] = tname
                            mbody = m.child_by_field_name("body") or m
                            mstack = [mbody]
                            while mstack:
                                curr = mstack.pop()
                                if curr.type == "assignment_expression":
                                    left = curr.child_by_field_name("left")
                                    right = curr.child_by_field_name("right")
                                    if left is not None and left.type == "member_expression":
                                        obj = left.child_by_field_name("object")
                                        prop = left.child_by_field_name("property")
                                        if obj is not None and obj.type == "this" and prop is not None:
                                            pname = _read_text(prop, source)
                                            ptype = None
                                            if right is not None:
                                                if right.type == "new_expression":
                                                    ctor = right.child_by_field_name("constructor")
                                                    if ctor is not None and ctor.type in ("identifier", "type_identifier"):
                                                        ptype = _read_text(ctor, source)
                                                elif right.type in ("identifier", "property_identifier"):
                                                    rname = _read_text(right, source)
                                                    ptype = param_types.get(rname)
                                            if pname and ptype and pname not in cls_fields:
                                                cls_fields[pname] = ptype
                                for child in curr.children:
                                    mstack.append(child)
        elif t == "variable_declarator":
            name_n = n.child_by_field_name("name")
            value = n.child_by_field_name("value")
            if (name_n is not None and name_n.type == "identifier"
                    and value is not None and value.type == "new_expression"):
                ctor = value.child_by_field_name("constructor")
                if ctor is not None and ctor.type in ("identifier", "type_identifier"):
                    name = _read_text(name_n, source)
                    tname = _read_text(ctor, source)
                    if name and tname and name not in table:
                        table[name] = tname
        elif t == "required_parameter" or t == "optional_parameter":
            pat = n.child_by_field_name("pattern")
            ann = n.child_by_field_name("type")
            if pat is not None and pat.type == "identifier" and ann is not None:
                tname = _bare_type_ident(ann)
                name = _read_text(pat, source)
                if name and tname and name not in table:
                    table[name] = tname
        for c in n.children:
            stack.append(c)

    def _fields_up_chain(cls_name: str, seen: set | None = None) -> dict[str, str]:
        if seen is None:
            seen = set()
        if cls_name in seen:
            return {}
        seen.add(cls_name)
        merged: dict[str, str] = {}
        for base in heritage_map.get(cls_name, []):
            for k, v in _fields_up_chain(base, seen).items():
                merged.setdefault(k, v)
        for k, v in direct_fields.get(cls_name, {}).items():
            merged[k] = v
        return merged

    all_class_tables: dict[str, dict[str, str]] = {}
    for cls in direct_fields:
        all_class_tables[cls] = _fields_up_chain(cls)
        for fld, ftype in all_class_tables[cls].items():
            if fld not in table:
                table[fld] = ftype

    if class_tables is not None:
        class_tables.update(all_class_tables)

def _find_require_call(value_node):
    """Return the call_expression node if `value_node` is a `require(...)` call
    or `require(...).x` member access. Otherwise None."""
    if value_node is None:
        return None
    if value_node.type == "call_expression":
        fn = value_node.child_by_field_name("function")
        if fn is not None and fn.type == "identifier":
            return value_node
    if value_node.type == "member_expression":
        obj = value_node.child_by_field_name("object")
        return _find_require_call(obj)
    return None

def _require_imports_js(node, source: bytes, importer_nid: str, stem: str, edges: list, str_path: str) -> bool:
    """Detect CommonJS require imports inside lexical_declaration / variable_declaration.

    Handles three patterns:
      const { foo, bar } = require('./mod')   → file → mod (imports_from), file → foo, file → bar
      const mod         = require('./mod')   → file → mod (imports_from)
      const x           = require('./mod').y → file → mod (imports_from), file → y

    Returns True if any require import was found.
    """
    if node.type not in ("lexical_declaration", "variable_declaration"):
        return False
    found = False
    for child in node.children:
        if child.type != "variable_declarator":
            continue
        value = child.child_by_field_name("value")
        call = _find_require_call(value)
        if call is None:
            continue
        fn = call.child_by_field_name("function")
        if fn is None or _read_text(fn, source) != "require":
            continue
        args = call.child_by_field_name("arguments")
        if args is None:
            continue
        raw = None
        for arg in args.children:
            if arg.type == "string":
                raw = _read_text(arg, source).strip("'\"` ")
                break
        if not raw:
            continue
        resolved = _resolve_js_import_target(raw, str_path)
        if resolved is None:
            continue
        tgt_nid, resolved_path = resolved
        line = node.start_point[0] + 1
        edge = {
            "source": importer_nid,
            "target": tgt_nid,
            "relation": "imports_from",
            "context": "import",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        # Key the target salt by the resolved target file so a same-basename
        # cross-extension sibling isn't mis-salted onto the importer (#1814).
        if resolved_path is not None:
            edge["target_file"] = str(resolved_path)
        edges.append(edge)
        found = True

        # Symbol-level edges for destructured / accessor binders.
        target_stem = _file_stem(resolved_path) if resolved_path is not None else None
        name_node = child.child_by_field_name("name")
        sym_names: list[str] = []
        if name_node is not None and name_node.type == "object_pattern":
            # `const { a, b: alias } = require('./m')` — emit edges for each property key
            for prop in name_node.children:
                if prop.type == "shorthand_property_identifier_pattern":
                    sym_names.append(_read_text(prop, source))
                elif prop.type == "pair_pattern":
                    key = prop.child_by_field_name("key")
                    if key is not None:
                        sym_names.append(_read_text(key, source))
        elif value is not None and value.type == "member_expression":
            # `const x = require('./m').y` — symbol is the property accessed
            prop = value.child_by_field_name("property")
            if prop is not None:
                sym_names.append(_read_text(prop, source))
        if target_stem is not None:
            for sym in sym_names:
                edges.append({
                    "source": importer_nid,
                    "target": _make_id(target_stem, sym),
                    "relation": "imports",
                    "context": "import",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                    "weight": 1.0,
                })
    return found

_JS_FUNCTION_VALUE_TYPES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})


def _scan_js_nested_function_declarations(
    container_node, parent_nid: str, *, source: bytes, config,
    add_node, add_edge, callable_def_nids: set | None,
    local_bound_names: dict | None, function_bodies: list,
) -> None:
    """Emit a node + `contains` edge for every named `function`/generator
    declaration lexically nested inside *container_node*, scoped under
    *parent_nid*, and track its body so calls made from inside it resolve
    instead of dangling (#2653).

    Recurses through non-function children AND through the bodies of nested
    arrow / function expressions, so a `function` declared inside an arrow
    callback (`useEffect(() => { function h(){} })`) or inside an arrow-defined
    component (`const Panel = () => { function handleClick(){} }`, the React
    idiom that motivated #2653) is captured too. Anonymous closures themselves
    are not noded — they are attributed to the nearest enclosing named scope,
    which is *parent_nid*.
    """
    if container_node is None:
        return
    for child in container_node.children:
        if child.type in ("function_declaration", "generator_function_declaration"):
            name_node = child.child_by_field_name(config.name_field)
            if name_node is None:
                for c in child.children:
                    if c.type in config.name_fallback_child_types:
                        name_node = c
                        break
            func_name = _read_text(name_node, source) if name_node else None
            # A name that normalizes to nothing (e.g. minified `$`) would collapse
            # the nested id onto parent_nid and leak the scan path (#1899); skip it.
            if func_name and normalize_id(func_name):
                line = child.start_point[0] + 1
                nested_nid = _make_id(parent_nid, func_name)
                add_node(nested_nid, f"{func_name}()", line)
                add_edge(parent_nid, nested_nid, "contains", line)
                if callable_def_nids is not None:
                    callable_def_nids.add(nested_nid)
                if local_bound_names is not None:
                    local_bound_names[nested_nid] = _js_local_bound_names(child, source)
                nested_body = _find_body(child, config)
                if nested_body:
                    function_bodies.append((nested_nid, nested_body))
                    _scan_js_nested_function_declarations(
                        nested_body, nested_nid, source=source, config=config,
                        add_node=add_node, add_edge=add_edge,
                        callable_def_nids=callable_def_nids,
                        local_bound_names=local_bound_names,
                        function_bodies=function_bodies,
                    )
        elif child.type in _JS_FUNCTION_VALUE_TYPES:
            # An anonymous arrow/function expression is not itself a node, but a
            # `function` declared inside its body still belongs to the enclosing
            # named scope — descend into the body keeping the SAME parent_nid.
            _scan_js_nested_function_declarations(
                _find_body(child, config), parent_nid, source=source, config=config,
                add_node=add_node, add_edge=add_edge,
                callable_def_nids=callable_def_nids,
                local_bound_names=local_bound_names,
                function_bodies=function_bodies,
            )
        else:
            _scan_js_nested_function_declarations(
                child, parent_nid, source=source, config=config,
                add_node=add_node, add_edge=add_edge,
                callable_def_nids=callable_def_nids,
                local_bound_names=local_bound_names,
                function_bodies=function_bodies,
            )


def _js_topmost_closures(node, out: list) -> None:
    """Collect the TOPMOST closure nodes (arrow / function expressions) under
    ``node``, without descending into a found closure — its nested closures
    belong to it and are reached by the walk_calls closure descend (#1630)."""
    for c in node.children:
        if c.type in _JS_FUNCTION_VALUE_TYPES:
            out.append(c)
        else:
            _js_topmost_closures(c, out)

def _js_member_assignment_target(left, source: bytes):
    """Classify the symbol an `assignment_expression` LHS defines when its RHS
    is a function. Returns (kind, owner_name, member_name) or None.

      this.foo = fn            → ("this",      None,  "foo")
      exports.foo = fn         → ("exports",   None,  "foo")
      module.exports.foo = fn  → ("exports",   None,  "foo")
      Foo.prototype.bar = fn   → ("prototype", "Foo", "bar")

    An arbitrary identifier receiver is returned as ``("object", name, member)``.
    It is only materialized after the caller proves that the identifier is a
    direct object-literal binding in the enclosing function. Keeping that scope
    check at the caller avoids the bare-named / phantom-god-node failure mode
    that the module-level guard (#1077) prevents.
    """
    if left is None or left.type != "member_expression":
        return None
    prop = left.child_by_field_name("property")
    if prop is None:
        return None
    member_name = _read_text(prop, source)
    if not member_name:
        return None
    obj = left.child_by_field_name("object")
    if obj is None:
        return None
    if obj.type == "this":
        return ("this", None, member_name)
    if obj.type == "identifier":
        if _read_text(obj, source) == "exports":
            return ("exports", None, member_name)
        return ("object", _read_text(obj, source), member_name)
    if obj.type == "member_expression":
        # module.exports.X  or  Foo.prototype.X
        inner_obj = obj.child_by_field_name("object")
        inner_prop = obj.child_by_field_name("property")
        if inner_obj is None or inner_prop is None:
            return None
        inner_prop_name = _read_text(inner_prop, source)
        if inner_obj.type == "identifier":
            inner_obj_name = _read_text(inner_obj, source)
            if inner_obj_name == "module" and inner_prop_name == "exports":
                return ("exports", None, member_name)
            if inner_prop_name == "prototype":
                return ("prototype", inner_obj_name, member_name)
    return None

def _js_scan_member_assignments(
    body, this_owner_nid: str, source: bytes, *,
    this_as_method: bool, add_node_fn, add_edge_fn, function_bodies: list,
) -> None:
    """Materialize callable members assigned in ``body`` (#1322/#3408).

    ``this.X = fn`` members attach to ``this_owner_nid`` — as ``method`` edges
    with a ``.X()`` label when the owner is a named function or class
    (``this_as_method``), and as ``contains`` edges with an ``X()`` label when
    the owner is the file node (the enclosing function is anonymous: an IIFE
    or a callback argument, e.g. an AngularJS ``.service(...)`` body).
    ``api.X = fn`` members attach to the object-literal binding declared in
    the same body, exactly as before — the scope proof that keeps the #1077
    phantom-god-node guard intact.
    """
    object_bindings: dict[str, object] = {}
    for stmt in body.children:
        if stmt.type not in ("lexical_declaration", "variable_declaration"):
            continue
        for declarator in stmt.children:
            if declarator.type != "variable_declarator":
                continue
            name = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name is not None and name.type == "identifier" \
                    and value is not None and value.type == "object":
                object_bindings[_read_text(name, source)] = declarator
    # A factory object gets one owner node and one `contains` edge no
    # matter how many methods hang off it. add_node dedups on id, but
    # add_edge does not, so without this guard N assigned methods would
    # emit N identical `contains` edges (the flood #1077 warns against).
    contained_owners: set[str] = set()
    for stmt in body.children:
        if stmt.type != "expression_statement":
            continue
        assign = next((c for c in stmt.children
                       if c.type == "assignment_expression"), None)
        if assign is None:
            continue
        val = assign.child_by_field_name("right")
        if val is None or val.type not in _JS_FUNCTION_VALUE_TYPES:
            continue
        tgt = _js_member_assignment_target(
            assign.child_by_field_name("left"), source)
        if tgt is None:
            continue
        as_method = True
        if tgt[0] == "this":
            owner_nid = this_owner_nid
            as_method = this_as_method
        elif tgt[0] == "object" and tgt[1] in object_bindings:
            object_name = tgt[1]
            owner_nid = _make_id(this_owner_nid, object_name)
            owner_line = object_bindings[object_name].start_point[0] + 1
            add_node_fn(owner_nid, object_name, owner_line)
            if owner_nid not in contained_owners:
                contained_owners.add(owner_nid)
                add_edge_fn(this_owner_nid, owner_nid, "contains", owner_line)
        else:
            continue
        m_name = tgt[2]
        m_line = stmt.start_point[0] + 1
        m_nid = _make_id(owner_nid, m_name)
        if as_method:
            add_node_fn(m_nid, f".{m_name}()", m_line)
            add_edge_fn(owner_nid, m_nid, "method", m_line)
        else:
            add_node_fn(m_nid, f"{m_name}()", m_line)
            add_edge_fn(owner_nid, m_nid, "contains", m_line)
        m_body = val.child_by_field_name("body")
        if m_body:
            function_bodies.append((m_nid, m_body))


def _js_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                   nodes: list, edges: list, seen_ids: set, function_bodies: list,
                   parent_class_nid: str | None, add_node_fn, add_edge_fn,
                   callable_def_nids: set | None = None,
                   local_bound_names: dict | None = None,
                   closure_locals_by_body: dict | None = None,
                   config=None) -> bool:
    """Handle lexical_declaration (arrow functions, CJS requires, module-level const literals) for JS/TS. Returns True if handled."""
    # CommonJS / prototype member assignments whose value is a function:
    #   exports.X = () => {}     → file-contained function  X()
    #   module.exports.X = fn    → file-contained function  X()
    #   Foo.prototype.bar = fn   → method bar() owned by Foo
    # (`this.X = fn` lives inside a function body, which is not recursed here;
    #  it is captured at the enclosing function — see the function branch.)
    if node.type == "expression_statement":
        # #3408: a module-level call statement whose argument (or IIFE callee)
        # is a closure — `callIt(function(){ this.X = fn })`, the AngularJS
        # `.service('name', function(){ this.X = fn })` registration shape,
        # `(function(){ this.X = fn })()` — never reaches the function branch,
        # so its `this.X` members vanished. Scan each TOPMOST closure body;
        # the members attach to the file node (the enclosing function is
        # anonymous, so there is no named owner), keeping ids file-qualified
        # (the #1077 guard's requirement).
        call_stmt = next((c for c in node.children
                          if c.type in ("call_expression", "new_expression")), None)
        if call_stmt is not None:
            _stmt_closures: list = []
            _js_topmost_closures(call_stmt, _stmt_closures)
            for closure in _stmt_closures:
                closure_body = closure.child_by_field_name("body")
                if closure_body is not None:
                    _js_scan_member_assignments(
                        closure_body, file_nid, source, this_as_method=False,
                        add_node_fn=add_node_fn, add_edge_fn=add_edge_fn,
                        function_bodies=function_bodies,
                    )
                    # #3124: calls made directly inside this closure (not just
                    # `this.X = fn` member assignments) were dropped entirely,
                    # since walk_calls only ever runs on function_bodies
                    # entries and this closure has no named owner to seed one.
                    # `it("...", async () => { register(...) })` is exactly
                    # this shape: the nearest enclosing scope is anonymous, so
                    # #1740's "attribute to the enclosing function" fix never
                    # applies and nothing is emitted, not even to the file
                    # node #1740 itself named as the fallback. Attribute calls
                    # here to the file node instead, mirroring the member
                    # assignment attribution just above -- a nested member
                    # function's own body is appended with its own real id by
                    # `_js_scan_member_assignments`, so it lands in
                    # `_tracked_body_ids` and walk_calls' own boundary check
                    # skips re-descending into it from here.
                    function_bodies.append((file_nid, closure_body))
        assign = next((c for c in node.children
                       if c.type == "assignment_expression"), None)
        if assign is not None:
            value = assign.child_by_field_name("right")
            if value is not None:
                target = _js_member_assignment_target(
                    assign.child_by_field_name("left"), source)
                if target is not None:
                    kind, owner_name, member_name = target
                    line = node.start_point[0] + 1
                    if value.type in _JS_FUNCTION_VALUE_TYPES:
                        handled = False
                        if kind == "exports":
                            nid = _make_id(stem, member_name)
                            add_node_fn(nid, f"{member_name}()", line)
                            add_edge_fn(file_nid, nid, "contains", line)
                            handled = True
                        elif kind == "prototype":
                            owner_nid = _make_id(stem, owner_name)
                            nid = _make_id(owner_nid, member_name)
                            add_node_fn(nid, f".{member_name}()", line)
                            add_edge_fn(owner_nid, nid, "method", line)
                            handled = True
                        if handled:
                            if callable_def_nids is not None:
                                callable_def_nids.add(nid)  # CJS/prototype fn is callable
                            if local_bound_names is not None:
                                local_bound_names[nid] = _js_local_bound_names(value, source)
                            body = value.child_by_field_name("body")
                            if body:
                                function_bodies.append((nid, body))
                            return True
                    elif kind == "exports":
                        # #3035: `exports.handler = wrapper(async (req) => …)` or `module.exports.handler = wrapper(…)`
                        inner = value
                        while inner is not None and inner.type in (
                                "as_expression", "satisfies_expression"):
                            inner = (inner.named_children[0]
                                     if inner.named_children else None)
                        if inner is not None and inner.type in (
                                "call_expression", "new_expression"):
                            closures: list = []
                            _js_topmost_closures(inner, closures)
                            if closures:
                                nid = _make_id(stem, member_name)
                                add_node_fn(nid, f"{member_name}()", line)
                                add_edge_fn(file_nid, nid, "contains", line)
                                if callable_def_nids is not None:
                                    callable_def_nids.add(nid)  # exported HOF is callable
                                for closure in closures:
                                    body = closure.child_by_field_name("body")
                                    if body:
                                        if closure_locals_by_body is not None:
                                            closure_locals_by_body[id(body)] = (
                                                _js_local_bound_names(closure, source))
                                        function_bodies.append((nid, body))
                                return True

    # Class fields whose value is a function:
    #   class C { handler = () => {} }   → method handler() owned by C
    # Reaches here with parent_class_nid set because class bodies are recursed
    # with the class nid as parent.
    if parent_class_nid and node.type in ("field_definition", "public_field_definition"):
        prop = node.child_by_field_name("property") or node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if (prop is not None and value is not None
                and value.type in _JS_FUNCTION_VALUE_TYPES):
            field_name = _read_text(prop, source)
            if field_name:
                line = node.start_point[0] + 1
                nid = _make_id(parent_class_nid, field_name)
                add_node_fn(nid, f".{field_name}()", line)
                add_edge_fn(parent_class_nid, nid, "method", line)
                if callable_def_nids is not None:
                    callable_def_nids.add(nid)  # arrow class-field is callable
                if local_bound_names is not None:
                    local_bound_names[nid] = _js_local_bound_names(value, source)
                body = value.child_by_field_name("body")
                if body:
                    function_bodies.append((nid, body))
                return True

    if node.type in ("lexical_declaration", "variable_declaration"):
        # CJS require imports — emit edges, do not block other lexical_declaration handling
        require_found = _require_imports_js(node, source, file_nid, stem, edges, str_path)

        # Scope guard (#1077): only emit nodes for module-level declarations.
        # Without this, `const x = ...` inside an arrow callback (e.g. inside
        # `describe(() => { const set = new Set(...) })`) emits a bare-named
        # node, and the same name collides across unrelated files producing
        # phantom god-nodes. Bodies of arrow functions are walked separately
        # via function_bodies, so we never need to emit nodes for locals here.
        parent = node.parent
        is_exported = parent is not None and parent.type == "export_statement"
        is_module_level = parent is not None and (
            parent.type == "program"
            or (is_exported
                and parent.parent is not None
                and parent.parent.type == "program")
        )

        # Arrow function declarations and module-level const literals (lexical_declaration only)
        arrow_found = False
        const_found = False
        if node.type == "lexical_declaration" and is_module_level:
            for child in node.children:
                if child.type == "variable_declarator":
                    value = child.child_by_field_name("value")
                    name_node = child.child_by_field_name("name")
                    is_exported_scalar_binding = (
                        is_exported
                        and name_node is not None
                        and name_node.type == "identifier"
                        and bool(normalize_id(_read_text(name_node, source)))
                    )
                    if value and value.type in _JS_FUNCTION_VALUE_TYPES:
                        # `const f = () => {}` and `const f = function(){}`
                        if name_node:
                            func_name = _read_text(name_node, source)
                            line = child.start_point[0] + 1
                            # A name that normalizes to nothing (e.g. minified `$`)
                            # would collapse the id to the absolute file-stem and
                            # leak the scan path (#1899); skip it (no graph signal).
                            if not normalize_id(func_name):
                                continue
                            func_nid = _make_id(stem, func_name)
                            add_node_fn(func_nid, f"{func_name}()", line)
                            add_edge_fn(file_nid, func_nid, "contains", line)
                            if callable_def_nids is not None:
                                callable_def_nids.add(func_nid)  # `const f = () =>` is callable
                            if local_bound_names is not None:
                                local_bound_names[func_nid] = _js_local_bound_names(value, source)
                            body = value.child_by_field_name("body")
                            if body:
                                function_bodies.append((func_nid, body))
                                # #2653: a `function` declared inside an arrow-defined
                                # component (`const Panel = () => { function h(){} }`)
                                # is otherwise never seen — the main walk does not
                                # recurse into arrow bodies. Scan it here so the
                                # nested declaration is noded and its calls resolve.
                                _scan_js_nested_function_declarations(
                                    body, func_nid, source=source, config=config,
                                    add_node=add_node_fn, add_edge=add_edge_fn,
                                    callable_def_nids=callable_def_nids,
                                    local_bound_names=local_bound_names,
                                    function_bodies=function_bodies,
                                )
                                # #3408: `this.X = fn` members were captured only
                                # when the enclosing function was a DECLARATION;
                                # `const F = function(){ this.X = fn }` (and the
                                # arrow form) silently dropped them.
                                _js_scan_member_assignments(
                                    body, func_nid, source, this_as_method=True,
                                    add_node_fn=add_node_fn, add_edge_fn=add_edge_fn,
                                    function_bodies=function_bodies,
                                )
                            arrow_found = True
                    elif value and (
                        is_exported_scalar_binding
                        or value.type in (
                            "object", "array", "as_expression",
                            "satisfies_expression", "call_expression",
                            "new_expression",
                        )
                    ):
                        # Simple exported identifiers are part of the module API
                        # regardless of initializer shape. Keep other scalar noise suppressed.
                        if name_node is not None and name_node.type == "object_pattern" and is_exported:
                            # #2604 Class 2: `export const { auth, handlers } =
                            # NextAuth(config)` (NextAuth v5's own documented
                            # boilerplate; next-intl's createNavigation() is the
                            # same shape) used to fall through to the generic
                            # branch below, which reads the WHOLE pattern as one
                            # combined node -- id `..._auth_handlers`, label
                            # "{ auth, handlers }". A single-name import
                            # (`import { auth } from './auth'`) looks for a node
                            # named `auth` alone, which never exists, so the
                            # import edge dangles. Emit one node per exported
                            # name instead, all sharing this statement's line, so
                            # each import resolves to its own node the way it
                            # would if the export had been written as N separate
                            # `export const auth = ...` statements.
                            #
                            # Gated on `is_exported`: an UN-exported destructure
                            # of the same shape (`const { doWork } = require(...)`)
                            # is a LOCAL import binding, not a module export. The
                            # old combined-node label ("{ doWork }") never matched
                            # the bare-name index cross-file resolution builds, so
                            # it was silently inert; giving it a bare-name node
                            # here (id `caller_dowork`, label "doWork") would make
                            # it collide with the real `doWork` definition it is
                            # ONLY importing, turning a clean single-candidate
                            # resolution into a false "ambiguous name" and losing
                            # the real calls edge (caught by
                            # test_cross_file_call_promoted_to_extracted_with_import_evidence).
                            line = child.start_point[0] + 1
                            for prop in name_node.named_children:
                                if prop.type == "shorthand_property_identifier_pattern":
                                    export_name = _read_text(prop, source)
                                elif prop.type == "pair_pattern":
                                    # `handlers: h` exports as `handlers` (the
                                    # key) -- `import { handlers }` binds by the
                                    # property name, never by the local alias.
                                    key_node = prop.child_by_field_name("key")
                                    export_name = _read_text(key_node, source) if key_node else None
                                else:
                                    # `...rest` and default-valued patterns
                                    # (`{ auth = fallback }`) don't correspond to
                                    # one discrete exported name; skip rather
                                    # than guess.
                                    export_name = None
                                if not export_name or not normalize_id(export_name):
                                    continue
                                prop_nid = _make_id(stem, export_name)
                                add_node_fn(prop_nid, export_name, line)
                                add_edge_fn(file_nid, prop_nid, "contains", line)
                            const_found = True
                        elif name_node:
                            const_name = _read_text(name_node, source)
                            line = child.start_point[0] + 1
                            const_nid = _make_id(stem, const_name)
                            add_node_fn(const_nid, const_name, line)
                            add_edge_fn(file_nid, const_nid, "contains", line)
                            const_found = True
                            # #2552: `const handler = wrapper(async (req) => …)`
                            # created the const node above but, unlike the arrow
                            # branch, never tracked the callback's body — so
                            # walk_calls never descended into it and its calls
                            # were dropped. Track each TOPMOST closure in the
                            # initializer under the const's nid; nested closures
                            # are reached by the #1630 closure descend with the
                            # same caller, so appending them too would
                            # double-walk. `_tracked_body_ids` picks these up,
                            # so the descend skips them (no double-count).
                            inner = value
                            while inner is not None and inner.type in (
                                    "as_expression", "satisfies_expression"):
                                inner = (inner.named_children[0]
                                         if inner.named_children else None)
                            if inner is not None and inner.type in (
                                    "call_expression", "new_expression"):
                                closures: list = []
                                _js_topmost_closures(inner, closures)
                                for closure in closures:
                                    # #2568: keep each sibling closure's
                                    # params/locals scoped to its OWN body
                                    # (keyed by id(body), fed to walk_calls as
                                    # extra_locals) instead of unioning them
                                    # under const_nid — the union let closure
                                    # A's param suppress a real indirect_call
                                    # to the same name in sibling closure B.
                                    body = closure.child_by_field_name("body")
                                    if body:
                                        if closure_locals_by_body is not None:
                                            closure_locals_by_body[id(body)] = (
                                                _js_local_bound_names(closure, source))
                                        function_bodies.append((const_nid, body))
        if arrow_found:
            return True
        if const_found:
            return True
        if require_found:
            return True
    return False

def _ts_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                   nodes: list, edges: list, seen_ids: set, function_bodies: list,
                   parent_class_nid: str | None, add_node_fn, add_edge_fn,
                   walk_fn) -> bool:
    """Emit enum member nodes, and a container node for a TS `namespace`/`module`.

    `namespace Foo {}` parses as `internal_module` (with `name`/`body` fields);
    `module Bar {}` and ambient `declare module "pkg" {}` parse as a named
    `module` node that exposes no fields, so its name and body are found
    positionally. Without this the container was never a node — its members were
    still reached by the default recurse but lost their namespace context. The
    members stay file-contained (parity with C#'s `_csharp_extra_walk`); the
    namespace becomes a sibling marker node so it is queryable. Returns True if
    handled.

    The guard requires `is_named` because the anonymous `module` keyword token
    shares the `module` type string and would otherwise match here.
    """
    if (parent_class_nid
            and node.parent is not None
            and node.parent.type == "enum_body"
            and node.type in ("property_identifier", "enum_assignment")):
        # `enum_declaration` is in TS's class_types "parity with Java/C#", so the
        # enum type is a node while its members were not, leaving the type a leaf.
        # Java emits a node per `enum_constant` with a `case_of` edge (#1719),
        # Kotlin per `enum_entry` (#1738), Swift the same; this is that shape.
        #
        # Two member spellings: a bare `Red` is a `property_identifier`, while
        # `Green = 5` is an `enum_assignment` whose `name` is either a
        # `property_identifier` or, for a quoted member, a `string`. The parent
        # check is what keeps this off the `property_identifier` nodes that
        # appear all over a TS file.
        name_node = node if node.type == "property_identifier" else node.child_by_field_name("name")
        member_name = ""
        if name_node is not None:
            member_name = _read_text(name_node, source)
            if name_node.type == "string":
                # `"Odd Name" = 7`: the label is the member name, not the quoted
                # literal. Unquote the whole text the way the namespace handler
                # below does rather than reading a `string_fragment`, because an
                # escape splits the string into several fragments and the first
                # one alone truncates the name (`"A\tB"` would become `A`).
                member_name = member_name.strip("'\"`")
        if member_name:
            line = node.start_point[0] + 1
            member_nid = _make_id(parent_class_nid, member_name)
            # TS is case-sensitive while the id recipe casefolds, so `enum E {
            # Value, value }` puts two legal members on one id. The first
            # declaration keeps the node rather than a second edge on it.
            if member_nid not in seen_ids:
                add_node_fn(member_nid, member_name, line)
                add_edge_fn(parent_class_nid, member_nid, "case_of", line)
        if node.type == "enum_assignment":
            # Claiming the member must not swallow its initializer. An enum value
            # can hold a whole expression, and `A = class Inner { m() {} }.name`
            # loses Inner's method node if the walk stops here. Descend into the
            # `value` only: the `name` is already read above, and walking it
            # again would put the member through the default recurse as well.
            value_node = node.child_by_field_name("value")
            if value_node is not None:
                walk_fn(value_node, parent_class_nid)
        return True
    if node.is_named and node.type in ("internal_module", "module"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for child in node.children:
                if child.is_named and child.type in (
                        "identifier", "nested_identifier", "string"):
                    name_node = child
                    break
        body = node.child_by_field_name("body")
        if body is None:
            for child in node.children:
                if child.type == "statement_block":
                    body = child
                    break
        if name_node is not None:
            ns_name = _read_text(name_node, source)
            if name_node.type == "string":
                ns_name = ns_name.strip("'\"`")
            if ns_name:
                ns_nid = _make_id(stem, ns_name)
                line = node.start_point[0] + 1
                add_node_fn(ns_nid, ns_name, line)
                add_edge_fn(file_nid, ns_nid, "contains", line)
        if body is not None:
            for child in body.children:
                walk_fn(child, parent_class_nid)
        return True
    return False


def _ts_decorator_name(deco_node, source: bytes) -> str | None:
    """Return the head symbol of a TS `decorator` node.

    `@Injectable` -> the identifier; `@Component({...})` / `@Input()` -> the
    `function` of the call_expression; `@ng.Component()` / `@core.Injectable` ->
    the `property` of the member_expression (the imported symbol, not the
    namespace alias).
    """
    for child in deco_node.children:
        if not child.is_named:
            continue
        target = child
        if target.type == "call_expression":
            target = target.child_by_field_name("function") or target
        if target.type == "member_expression":
            prop = target.child_by_field_name("property")
            return _read_text(prop, source) if prop else None
        if target.type == "identifier":
            return _read_text(target, source)
        return None
    return None

def _ts_method_name(method_node, source: bytes) -> str | None:
    """Name of a `method_definition`, matching the id the function-types branch
    builds (`_make_id(class_nid, name)`)."""
    name_node = method_node.child_by_field_name("name")
    return _read_text(name_node, source) if name_node else None

def _ts_descendant_decorators(node) -> list:
    """Collect `decorator` nodes under `node` (e.g. parameter decorators inside a
    method's formal_parameters, or a field's own decorator), without crossing into
    a nested class or a nested method, which own their own decorators."""
    out: list = []

    def rec(n, top: bool) -> None:
        for child in n.children:
            ct = child.type
            if ct == "decorator":
                out.append(child)
            elif ct in ("class_declaration", "abstract_class_declaration"):
                continue
            elif ct == "method_definition" and not top:
                continue
            else:
                rec(child, False)

    rec(node, True)
    return out


def _js_member_salted_nid(plain_nid: str, name: str, groups: dict[str, set[str]]) -> str:
    """Resolve a JS/TS class member's real node id against the collision map."""
    names = groups.get(plain_nid)
    if not names or len(names) < 2:
        return plain_nid
    public = [n for n in names if not n.startswith(("_", "$", "#"))]
    if len(public) == 1 and name == public[0]:
        return plain_nid
    salt = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
    return _make_id(plain_nid, salt)


def _js_pre_scan_member_collisions(root_node, source: bytes, stem: str) -> dict[str, set[str]]:
    """Pre-scan a JS/TS module for symbol collisions caused by prefix stripping (#4475)."""
    groups: dict[str, set[str]] = {}

    def _record(plain_nid: str, name: str) -> None:
        groups.setdefault(plain_nid, set()).add(name)

    classes: list[tuple[str, object]] = []

    def _collect(node):
        for child in node.children:
            ctype = child.type
            if ctype == "export_statement":
                _collect(child)
                continue
            if ctype in ("function_declaration", "generator_function_declaration"):
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = _read_text(name_node, source)
                    if name:
                        _record(_make_id(stem, name), name)
            elif ctype in ("class_declaration", "abstract_class_declaration", "interface_declaration", "class"):
                name_node = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if name_node is not None and body is not None:
                    class_name = _read_text(name_node, source)
                    if class_name:
                        _record(_make_id(stem, class_name), class_name)
                        classes.append((class_name, body))
            elif ctype in ("internal_module", "module", "statement_block", "ambient_declaration"):
                _collect(child)

    _collect(root_node)
    module_groups = {nid: names for nid, names in groups.items() if len(names) >= 2}

    for class_name, body in classes:
        class_nid = _js_member_salted_nid(_make_id(stem, class_name), class_name, module_groups)
        for member in body.children:
            if member.type in ("method_definition", "method_signature", "abstract_method_signature"):
                name_node = member.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _read_text(name_node, source)
                if name:
                    _record(_make_id(class_nid, name), name)

    return {nid: names for nid, names in groups.items() if len(names) >= 2}


def _ts_emit_decorator_edges(class_node, class_nid: str, stem: str, source: bytes,
                             ensure_named_node, add_edge,
                             member_groups: dict[str, set[str]] | None = None) -> None:
    """Emit `references` edges (context="decorator") from a class and its members
    to the symbols of the TS decorators applied to them.

    Decorators only occur on classes, class members, and parameters, so a single
    pass over the class declaration covers them. Members that are graph nodes
    (methods, incl. the constructor) own their decorators and their parameter
    decorators; members that are not nodes (fields, parameters) attribute to the
    enclosing class. Targets go through `ensure_named_node`, so a decorator
    imported from another module (the common case — `@Component` from
    `@angular/core`) becomes a sourceless stub the corpus rewire collapses onto
    the real definition.
    """
    def emit(deco_node, owner_nid: str) -> None:
        name = _ts_decorator_name(deco_node, source)
        if not name:
            return
        line = deco_node.start_point[0] + 1
        target = ensure_named_node(name, line)
        if target != owner_nid:
            add_edge(owner_nid, target, "references", line, context="decorator")

    # Class-level decorators: direct children of the class node (`@Deco class C`),
    # plus — when exported (`@Deco export class C`) — the decorators that sit on
    # the wrapping export_statement, before the class.
    for child in class_node.children:
        if child.type == "decorator":
            emit(child, class_nid)
    parent = class_node.parent
    if parent is not None and parent.type == "export_statement":
        for child in parent.children:
            if child.type == "decorator":
                emit(child, class_nid)
            elif child.type in ("class_declaration", "abstract_class_declaration"):
                break

    # Member decorators inside the class body.
    body = next((c for c in class_node.children if c.type == "class_body"), None)
    if body is None:
        return
    for member in body.children:
        mt = member.type
        if mt == "decorator":
            # A method decorator is a sibling preceding the method; skip past any
            # stacked decorators to find it.
            owner = class_nid
            sib = member.next_named_sibling
            while sib is not None and sib.type == "decorator":
                sib = sib.next_named_sibling
            if sib is not None and sib.type == "method_definition":
                mname = _ts_method_name(sib, source)
                if mname:
                    plain = _make_id(class_nid, mname)
                    owner = _js_member_salted_nid(plain, mname, member_groups or {})
            emit(member, owner)
        elif mt == "method_definition":
            mname = _ts_method_name(member, source)
            plain = _make_id(class_nid, mname) if mname else class_nid
            m_nid = _js_member_salted_nid(plain, mname, member_groups or {}) if mname else class_nid
            for deco in _ts_descendant_decorators(member):
                emit(deco, m_nid)
        else:
            # Fields / accessors: the member is not a node, so attribute its
            # decorators (e.g. `@Input()`, `@Column()`) to the class.
            for deco in _ts_descendant_decorators(member):
                emit(deco, class_nid)


_JS_CONFIG = LanguageConfig(
    ts_module="tree_sitter_javascript",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_declaration", "generator_function_declaration", "method_definition"}),
    import_types=frozenset({"import_statement", "export_statement"}),
    call_types=frozenset({"call_expression", "new_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_expression"}),
    call_accessor_field="property",
    call_accessor_object_field="object",
    function_boundary_types=frozenset({"function_declaration", "generator_function_declaration", "arrow_function", "method_definition", "function_expression", "generator_function"}),
    import_handler=_import_js,
)

_TS_CONFIG = LanguageConfig(
    ts_module="tree_sitter_typescript",
    ts_language_fn="language_typescript",
    class_types=frozenset({
        "class_declaration",
        "abstract_class_declaration",  # TS abstract class
        "interface_declaration",   # parity with Java/C#
        "enum_declaration",        # named enums
        "type_alias_declaration",  # named type aliases
    }),
    function_types=frozenset({"function_declaration", "generator_function_declaration", "method_definition", "method_signature"}),
    import_types=frozenset({"import_statement", "export_statement"}),
    call_types=frozenset({"call_expression", "new_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_expression"}),
    call_accessor_field="property",
    call_accessor_object_field="object",
    function_boundary_types=frozenset({"function_declaration", "generator_function_declaration", "arrow_function", "method_definition", "function_expression", "generator_function"}),
    import_handler=_import_js,
)

_TSX_CONFIG = LanguageConfig(
    ts_module="tree_sitter_typescript",
    ts_language_fn="language_tsx",
    class_types=_TS_CONFIG.class_types,
    function_types=_TS_CONFIG.function_types,
    import_types=_TS_CONFIG.import_types,
    call_types=_TS_CONFIG.call_types,
    call_function_field=_TS_CONFIG.call_function_field,
    call_accessor_node_types=_TS_CONFIG.call_accessor_node_types,
    call_accessor_field=_TS_CONFIG.call_accessor_field,
    call_accessor_object_field=_TS_CONFIG.call_accessor_object_field,
    function_boundary_types=_TS_CONFIG.function_boundary_types,
    import_handler=_TS_CONFIG.import_handler,
)


def extract_js(path: Path) -> dict:
    """Extract classes, functions, arrow functions, and imports from a .js/.ts/.tsx/.mts/.cts file."""
    from graph_fy.extractors.engine import _extract_generic
    suffix = path.suffix.lower()
    is_ts = suffix in (".ts", ".tsx", ".mts", ".cts")
    if suffix == ".tsx":
        config = _TSX_CONFIG
    elif suffix in (".ts", ".mts", ".cts"):
        config = _TS_CONFIG
    else:
        config = _JS_CONFIG
    source_override = None
    if is_ts:
        try:
            source = path.read_bytes()
            source_override = _normalize_ts_import_types(source, tsx=suffix == ".tsx")
        except OSError:
            pass
    result = _extract_generic(path, config, source_override=source_override)
    if "error" not in result:
        _extract_js_rationale(path, result)
        _rescue_js_dynamic_imports(path, result)
        try:
            from graph_fy.extractors.frameworks import extract_js_frameworks
            source_text = path.read_text(encoding="utf-8", errors="ignore")
            extract_js_frameworks(path, source_text, result)
        except Exception:
            pass
        if is_rtk_file(path):
            try:
                rtk_res = extract_rtk(path)
                result.setdefault("nodes", []).extend(rtk_res.get("nodes", []))
                result.setdefault("edges", []).extend(rtk_res.get("edges", []))
            except Exception:
                pass
    return result
