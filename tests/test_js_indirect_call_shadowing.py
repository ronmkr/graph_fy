"""JavaScript & TypeScript Lexical Scope Shadowing for Indirect Call Edges.

Consolidated test suite covering all JS/TS scope-shadowing invariants:
- Unparenthesized single arrow function parameters (x => ...)
- Catch clause bindings (catch (e) { ... }) and optional catch (catch { ... })
- External module imports (import { X } from 'pkg')
- Loop bindings (for...of and for...in)
- Function expressions (inline, named, generator, TS/TSX)
- Nested closures and callback parameters

Ensures local identifiers never fabricate false  edges to unrelated
same-named global or module-level callables in minified or multi-file corpora.
"""
import os
from pathlib import Path
import networkx as nx
import pytest

from graph_fy.affected import affected_nodes
from graph_fy.extract import extract

VENDOR = "var Lib=function(){function k(a){return a}return{k:k}}();\n"


def _extract_js_dir(tmp_path, files: dict[str, str]):
    base = tmp_path / "src"
    base.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        target = base / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        r = extract(
            [Path("src") / name for name in files],
            cache_root=Path(".cache"), parallel=False,
        )
    finally:
        os.chdir(old)
    nid = {n["label"].rstrip("()"): n["id"] for n in r["nodes"]}
    return r, nid


def _indirect(r):
    return {(e["source"], e["target"]) for e in r["edges"] if e["relation"] == "indirect_call"}


def _rels(r, relation):
    return {(e["source"], e["target"]) for e in r["edges"] if e["relation"] == relation}


# ── Arrow Single Param Shadow ────────────────────────────────────────────────

def test_single_unparenthesised_arrow_param_emits_no_indirect_call(tmp_path):
    """The reported shape: a minified bundle's private `k` must not become a
    fabricated target because an arrow names its only parameter `k`."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": "var Lib=function(){function k(a){return a}return{k:k}}();\n",
        "a.js": "function sink(f){ return f; }\nexport const run = k => sink(k);\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_parenthesised_arrow_param_still_shadows(tmp_path):
    """Control: the `parameters` path was already correct and must stay correct."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": "var Lib=function(){function k(a){return a}return{k:k}}();\n",
        "a.js": "function sink(f){ return f; }\nexport const run = (k) => sink(k);\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_async_single_param_arrow_shadows(tmp_path):
    """`async x => …` is the same node with the same singular field."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": "var Lib=function(){function k(a){return a}return{k:k}}();\n",
        "a.js": "function sink(f){ return f; }\nexport const run = async k => sink(k);\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_arrow_param_does_not_shadow_a_genuine_reference(tmp_path):
    """The parameter is scoped to its arrow: a same-named module callable
    referenced from a DIFFERENT function must still resolve."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function k(x){ return x; }\n"
        "function sink(f){ return f; }\n"
        "export const shadowed = k => sink(k);\n"
        "export function elsewhere(pool) { pool.submit(k); }\n"
    )})
    assert (nid["elsewhere"], nid["k"]) in _indirect(r)

def test_genuine_reference_inside_the_arrow_still_emits(tmp_path):
    """Widening the shadow set must not blanket-suppress inside arrows: an
    unshadowed callable referenced in the body still emits."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function handler(x){ return x; }\n"
        "export const run = pool => pool.submit(handler);\n"
    )})
    assert (nid["run"], nid["handler"]) in _indirect(r)


# ── Catch Binding Shadow ────────────────────────────────────────────────

def test_catch_binding_emits_no_indirect_call(tmp_path):
    """Reported shape: a minified bundle's private `k` must not become a fabricated
    indirect_call target because an unrelated function names its catch binding `k`
    and passes it on."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": "var Lib=function(){function k(a){return a}return{k:k}}();\n",
        "a.js": (
            "export function run(handlers) {\n"
            "  try { boom(); } catch (k) { handlers.get(k); }\n"
            "}\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["k"] for _s, t in indirect)

def test_catch_binding_destructured_emits_no_indirect_call(tmp_path):
    """`catch ({ cause })` binds through the same field via a pattern — the
    destructured name must shadow too."""
    r, nid = _extract_js_dir(tmp_path, {
        "lib.js": "export function cause(){ return 1; }\n",
        "a.js": (
            "export function run(sink) {\n"
            "  try { boom(); } catch ({ cause }) { sink.push(cause); }\n"
            "}\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["cause"] for _s, t in indirect)

def test_catch_binding_does_not_shadow_outside_its_block(tmp_path):
    """The binding is scoped to the clause: a same-named module callable referenced
    AFTER the try/catch must still resolve. Folding the name into the function-wide
    set instead of this subtree would suppress it."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function k(x){ return x; }\n"
        "export function run(pool) {\n"
        "  try { boom(); } catch (k) { log(k); }\n"
        "  pool.submit(k);\n"
        "}\n"
    )})
    indirect = _rels(r, "indirect_call")
    assert (nid["run"], nid["k"]) in indirect

def test_catch_genuine_reference_still_emits_indirect_call(tmp_path):
    """Widening the shadow set must not blanket-suppress indirect_call inside a catch
    block: a real by-name reference to an unshadowed callable still emits."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function handler(x){ return x; }\n"
        "export function run(pool) {\n"
        "  try { boom(); } catch (e) { pool.submit(handler); }\n"
        "}\n"
    )})
    indirect = _rels(r, "indirect_call")
    assert (nid["run"], nid["handler"]) in indirect

def test_optional_catch_binding_unaffected(tmp_path):
    """ES2019 `catch { }` is a real catch_clause with no `parameter` field — the
    `param is not None` guard keeps it from raising, and nothing is shadowed."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function handler(x){ return x; }\n"
        "export function run(pool) {\n"
        "  try { boom(); } catch { pool.submit(handler); }\n"
        "}\n"
    )})
    indirect = _rels(r, "indirect_call")
    assert (nid["run"], nid["handler"]) in indirect


# ── External Import Shadow ────────────────────────────────────────────────

def test_external_named_import_emits_no_indirect_call(tmp_path):
    """Reported shape: an icon imported from a UI kit must not become a fabricated
    indirect_call target because an unrelated component shares its name."""
    r, nid = _extract_js_dir(tmp_path, {
        "Palette.tsx": "export function Palette() { return null; }\n",
        "Sidebar.tsx": (
            "import { Palette } from 'lucide-react';\n"
            "export function Sidebar() {\n"
            "  return [{ label: 'personalise', icon: Palette }];\n"
            "}\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["Palette"] for _s, t in indirect)

def test_external_default_import_emits_no_indirect_call(tmp_path):
    r, nid = _extract_js_dir(tmp_path, {
        "Chart.tsx": "export function Chart() { return null; }\n",
        "Panel.tsx": (
            "import Chart from 'some-chart-lib';\n"
            "export function Panel(sink) { sink.register(Chart); }\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["Chart"] for _s, t in indirect)

def test_external_namespace_import_emits_no_indirect_call(tmp_path):
    r, nid = _extract_js_dir(tmp_path, {
        "Utils.ts": "export function Utils() { return 1; }\n",
        "run.ts": (
            "import * as Utils from 'vendor-utils';\n"
            "export function run(sink) { sink.push(Utils); }\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["Utils"] for _s, t in indirect)

def test_aliased_import_shadows_the_local_name_only(tmp_path):
    """`import { Search as Find }` binds `Find` in this file, not `Search`. The
    shadow must follow the binding: a same-named local `Find` is not referenced
    here, while an unrelated `Search` definition stays reachable by its own name."""
    r, nid = _extract_js_dir(tmp_path, {
        "Find.ts": "export function Find() { return 1; }\n",
        "app.ts": (
            "import { Search as Find } from 'icon-pack';\n"
            "export function app(sink) { sink.push(Find); }\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["Find"] for _s, t in indirect)

def test_tsconfig_alias_into_node_modules_still_counts_as_external(tmp_path):
    """A `paths` entry pointing a package at its own installed copy resolves to a
    real path — inside `node_modules`, which every scan prunes, so no node is ever
    created for it. Resolution succeeding must not read as 'internal'."""
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions": {"paths": {"icon-kit": ["./node_modules/icon-kit"]}}}\n'
    )
    nm = tmp_path / "node_modules" / "icon-kit"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("export function Palette(){}\n")
    r, nid = _extract_js_dir(tmp_path, {
        "Palette.tsx": "export function Palette() { return null; }\n",
        "Bar.tsx": (
            "import { Palette } from 'icon-kit';\n"
            "export function Bar() { return [{ icon: Palette }]; }\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["Palette"] for _s, t in indirect)

def test_relative_import_still_resolves(tmp_path):
    """The counter-test that bounds the fix: an import of a file INSIDE the corpus
    is exactly the relationship the graph exists to record, so its name must stay
    resolvable. Shadowing every import would delete real edges."""
    r, nid = _extract_js_dir(tmp_path, {
        "widgets.ts": "export function Widget() { return null; }\n",
        "host.ts": (
            "import { Widget } from './widgets';\n"
            "export function host(sink) { sink.push(Widget); }\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert (nid["host"], nid["Widget"]) in indirect

def test_unimported_same_file_callable_still_emits(tmp_path):
    """Widening the shadow set must not blanket-suppress a file that also happens to
    import something external: an unshadowed by-name reference still emits."""
    r, nid = _extract_js_dir(tmp_path, {"a.ts": (
        "import { Icon } from 'icon-pack';\n"
        "function handler(x) { return x; }\n"
        "export function run(pool) { pool.submit(handler); }\n"
    )})
    indirect = _rels(r, "indirect_call")
    assert (nid["run"], nid["handler"]) in indirect

def test_external_import_shadow_does_not_bind_to_a_same_named_local_callable(tmp_path):
    """The precise collision the fix must survive: a name that is BOTH imported
    externally AND defined as a callable in another corpus file. Using the
    external `Filter` value must not fabricate an indirect_call onto the unrelated
    corpus `Filter` — while a genuine by-name use of a local callable still binds."""
    r, nid = _extract_js_dir(tmp_path, {
        "table.ts": "export function Filter() { return null; }\n",   # unrelated corpus callable
        "toolbar.tsx": (
            "import { Filter } from 'lucide-react';\n"                # external, same name
            "function onClick(x) { return x; }\n"
            "export function build(sink, pool) {\n"
            "  sink.push(Filter);\n"                                  # external -> must NOT bind to table.ts Filter
            "  pool.submit(onClick);\n"                               # local -> must still bind
            "}\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert (nid["build"], nid["Filter"]) not in indirect   # no cross-file phantom
    assert (nid["build"], nid["onClick"]) in indirect      # real local reference preserved


# ── For Of Binding Shadow ────────────────────────────────────────────────

def test_for_of_binding_does_not_fabricate_indirect_call(tmp_path):
    """The reported shape: a `for...of` binding `entry` used in an
    object-shorthand argument must not resolve to an unrelated `entry()`."""
    r, nid = _extract_js_dir(tmp_path, {
        "declaration.mjs": (
            "export function entry(signature) {\n"
            "  return { signature };\n"
            "}\n"
            "export const fixture = entry(\"public.entry()\");\n"
        ),
        "consumer.mjs": (
            "export function findNamed(entries, lookup) {\n"
            "  const resolved = [];\n"
            "  for (const entry of entries) {\n"
            "    if (lookup(entry.name)) resolved.push({ entry });\n"
            "  }\n"
            "  return resolved;\n"
            "}\n"
        ),
    })
    assert (nid["findNamed"], nid["entry"]) not in _indirect(r)

def test_for_of_destructuring_binding_shadows(tmp_path):
    """A destructured loop binding (`for (const { entry } of xs)`) must shadow
    the same way — `_js_collect_pattern_idents` walks the pattern."""
    r, nid = _extract_js_dir(tmp_path, {
        "declaration.mjs": "export function entry(x) { return x; }\n",
        "consumer.mjs": (
            "export function findNamed(rows) {\n"
            "  const out = [];\n"
            "  for (const { entry } of rows) {\n"
            "    out.push({ entry });\n"
            "  }\n"
            "  return out;\n"
            "}\n"
        ),
    })
    assert (nid["findNamed"], nid["entry"]) not in _indirect(r)

def test_genuine_reference_outside_the_loop_still_emits(tmp_path):
    """Widening the shadow set must not blanket-suppress: a same-named callable
    referenced from a function that does NOT bind it in a loop still resolves."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function entry(x){ return x; }\n"
        "export function findNamed(rows) {\n"
        "  const out = [];\n"
        "  for (const entry of rows) { out.push({ entry }); }\n"
        "  return out;\n"
        "}\n"
        "export function elsewhere(pool) { pool.submit(entry); }\n"
    )})
    assert (nid["elsewhere"], nid["entry"]) in _indirect(r)


# ── Function Expression Shadow ────────────────────────────────────────────────

def test_inline_function_expression_param_emits_no_indirect_call(tmp_path):
    """The reported shape: a minified bundle's private `k` must not become a
    fabricated target because an inline callback names its parameter `k`."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": VENDOR,
        "a.js": "export function run(xs, m){ return xs.some(function (k) { return m.indexOf(k); }); }\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_named_function_expression_param_shadows(tmp_path):
    """A named function expression is the same node type and must behave alike."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": VENDOR,
        "a.js": "export function run(xs, m){ return xs.some(function nm(k) { return m.indexOf(k); }); }\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_nested_const_function_expression_shadows(tmp_path):
    """Not only the inline-argument position: a `const` inside a function body
    binds no tracked symbol, so the expression stays untracked."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": VENDOR,
        "a.js": "export function run(m){ const f = function (k) { return m.indexOf(k); }; return f(1); }\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_top_level_const_function_expression_was_already_tracked(tmp_path):
    """Boundary control: a MODULE-level `const f = function (…) {}` gets its own
    node and shadow-set entry through the declarator, so it never went through
    this gate and passes with and without the fix. Documents what is NOT the
    bug, so the scope of the change stays legible."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": VENDOR,
        "a.js": "export const f = function (k) { return [].indexOf(k); };\n",
    })
    assert "f" in nid
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_function_expression_local_shadows(tmp_path):
    """Locals, not just parameters, are scoped to the expression's body."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": VENDOR,
        "a.js": "export function run(m){ return [].map(function () { const k = 1; return m.get(k); }); }\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_arrow_callback_still_shadows(tmp_path):
    """Control: the arrow path was already correct and must stay correct."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": VENDOR,
        "a.js": "export function run(xs, m){ return xs.some(k => m.indexOf(k)); }\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_same_named_reference_after_the_expression_still_resolves(tmp_path):
    """The bindings are scoped to the expression: a same-named module callable
    referenced AFTER it, in the enclosing function, must still resolve."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function k(x){ return x; }\n"
        "export function run(m){ [].map(function (z) { return z; }); return m.get(k); }\n"
    )})
    assert (nid["run"], nid["k"]) in _indirect(r)

def test_sibling_function_expression_param_does_not_leak(tmp_path):
    """Two function expressions in one initializer are SEPARATE scopes: the
    first one's parameter must not shadow the second one's body. This is the
    #2568 shape — several closures registered under one id — reached through
    the function-expression form rather than the arrow form."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function k(x){ return x; }\n"
        "function wrap(a, b){ return a || b; }\n"
        "export const handler = wrap(\n"
        "  function (k) { return k; },\n"
        "  function (pool) { return pool.submit(k); }\n"
        ");\n"
    )})
    assert (nid["handler"], nid["k"]) in _indirect(r)

def test_genuine_reference_inside_the_function_expression_still_emits(tmp_path):
    """Widening the shadow set must not blanket-suppress inside the body: an
    unshadowed callable referenced there still emits."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function handler(x){ return x; }\n"
        "export function run(xs){ return xs.map(function (pool) { return pool.submit(handler); }); }\n"
    )})
    assert (nid["run"], nid["handler"]) in _indirect(r)

def test_call_attribution_inside_the_expression_is_unchanged(tmp_path):
    """Adding a type to `function_boundary_types` must not change WHERE calls are
    attributed. Before, a function expression was not a boundary at all, so
    walk_calls recursed in normally; now it takes the boundary branch, which
    descends explicitly with the enclosing `caller_nid` (#1630/#1740). Both
    routes must land the call on the enclosing function, not drop it or
    re-parent it."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function helper(x){ return x; }\n"
        "export function run(xs){ return xs.map(function (row) { return helper(row); }); }\n"
    )})
    calls = {(e["source"], e["target"]) for e in r["edges"] if e["relation"] == "calls"}
    assert (nid["run"], nid["helper"]) in calls

def test_typescript_function_expression_shadows(tmp_path):
    """The TS config carries its own copy of the set; both were missing the type."""
    base = tmp_path / "src"
    base.mkdir()
    (base / "vendor.min.ts").write_text(VENDOR)
    (base / "a.ts").write_text(
        "export function run(xs: number[], m: number[]): boolean {\n"
        "  return xs.some(function (k: number) { return m.indexOf(k) >= 0; });\n"
        "}\n"
    )
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        r = extract([Path("src") / "vendor.min.ts", Path("src") / "a.ts"],
                    cache_root=Path(".cache"), parallel=False)
    finally:
        os.chdir(old)
    nid = {n["label"].rstrip("()"): n["id"] for n in r["nodes"]}
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_generator_function_expression_param_shadows(tmp_path):
    """A GENERATOR function expression (`function*(k){}`) must shadow its param
    the same way a plain function expression does — the type was in
    generator_function_declaration form only, so the expression form still
    fabricated the edge until generator_function joined the boundary set."""
    r, nid = _extract_js_dir(tmp_path, {
        "vendor.min.js": VENDOR,
        "a.js": "export function run(xs, m){ const g = function*(k){ yield m.indexOf(k); }; return g; }\n",
    })
    assert all(t != nid["k"] for _s, t in _indirect(r))

def test_tsx_function_expression_shadows(tmp_path):
    """`.tsx` derives its boundary set from the TS config by reference; assert the
    coupling so a future refactor that gives TSX its own literal can't silently
    regress the shadow."""
    base = tmp_path / "src"
    base.mkdir()
    (base / "vendor.min.tsx").write_text(VENDOR)
    (base / "a.tsx").write_text(
        "export function run(xs: number[], m: number[]) {\n"
        "  return xs.some(function (k: number) { return m.indexOf(k) >= 0; });\n"
        "}\n"
    )
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        r = extract([Path("src") / "vendor.min.tsx", Path("src") / "a.tsx"],
                    cache_root=Path(".cache"), parallel=False)
    finally:
        os.chdir(old)
    nid = {n["label"].rstrip("()"): n["id"] for n in r["nodes"]}
    assert all(t != nid["k"] for _s, t in _indirect(r))


# ── Nested Closure Shadow ────────────────────────────────────────────────

def test_untracked_arrow_param_shadow_emits_no_indirect_call(tmp_path):
    """Reported shape (#2241): a one-letter test helper `r` must not become a
    fabricated indirect_call target when an unrelated function's inline `.map`
    callback has its own parameter named `r`, later passed on as a plain call
    argument deeper in the arrow body."""
    r, nid = _extract_js_dir(tmp_path, {
        "round.test.ts": "const r = (v) => Math.round(v);\n",
        "report.ts": (
            "export function buildSheet(rows, cols) {\n"
            "  return rows.map((r) => {\n"
            "    const o = {};\n"
            "    for (const c of cols) o[c.label] = c.get(r);\n"
            "    return o;\n"
            "  });\n"
            "}\n"
        ),
    })
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["r"] for _s, t in indirect)

def test_untracked_arrow_genuine_reference_still_emits_indirect_call(tmp_path):
    """The same nested-arrow shape must still capture a REAL by-name reference
    that is not shadowed by any enclosing local — widening the shadow set for
    untracked closures must not blanket-suppress indirect_call inside them."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function handler(x){ return x; }\n"
        "function via(rows, pool){\n"
        "  return rows.map((row) => {\n"
        "    pool.submit(handler);\n"
        "    return row;\n"
        "  });\n"
        "}\n"
    )})
    indirect = _rels(r, "indirect_call")
    assert (nid["via"], nid["handler"]) in indirect

def test_double_nested_untracked_closures_shadow_compounds(tmp_path):
    """Shadowing must compound through two levels of untracked inline
    closures: an outer arrow's own param and an independently-named inner
    arrow's own param must BOTH be recognized as local, however deep the
    call that references them sits."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function outer(){}\n"
        "function inner(){}\n"
        "function via(rows){\n"
        "  return rows.map((outer) => {\n"
        "    return [1].map((inner) => {\n"
        "      pool.submit(outer);\n"
        "      pool.submit(inner);\n"
        "      return 0;\n"
        "    });\n"
        "  });\n"
        "}\n"
    )})
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["outer"] for _s, t in indirect)
    assert all(t != nid["inner"] for _s, t in indirect)

def test_tracked_const_arrow_param_shadow_still_emits_no_indirect_call(tmp_path):
    """A const-assigned arrow IS separately tracked (its own caller_nid, own
    local_bound_names) — this pre-existing path must be unaffected by the new
    extra_locals threading: a same-named param inside it still shadows."""
    r, nid = _extract_js_dir(tmp_path, {"a.js": (
        "function handler(){}\n"
        "const via = (pool, handler) => { pool.submit(handler); };\n"
    )})
    indirect = _rels(r, "indirect_call")
    assert all(t != nid["handler"] for _s, t in indirect)

def test_affected_excludes_shadowed_untracked_closure_caller(tmp_path):
    """Blast-radius traversal must not include a caller that only reached the
    target through the fabricated edge this fix removes."""
    r, nid = _extract_js_dir(tmp_path, {
        "round.test.ts": "const r = (v) => Math.round(v);\n",
        "report.ts": (
            "export function buildSheet(rows, cols) {\n"
            "  return rows.map((r) => {\n"
            "    const o = {};\n"
            "    for (const c of cols) o[c.label] = c.get(r);\n"
            "    return o;\n"
            "  });\n"
            "}\n"
        ),
    })
    g = nx.DiGraph()
    for n in r["nodes"]:
        g.add_node(n["id"], **n)
    for e in r["edges"]:
        g.add_edge(e["source"], e["target"], **e)
    affected = {h.node_id for h in affected_nodes(g, nid["r"])}
    assert nid["buildSheet"] not in affected

def test_untracked_arrow_param_shadow_stable_on_warm_cache(tmp_path):
    """The fix must hold on a warm-cache re-extraction, not just a cold run —
    the shadow set is recomputed from the AST each time, never itself cached,
    but the extracted edge set it feeds into is."""
    files = {
        "round.test.ts": "const r = (v) => Math.round(v);\n",
        "report.ts": (
            "export function buildSheet(rows, cols) {\n"
            "  return rows.map((r) => {\n"
            "    const o = {};\n"
            "    for (const c of cols) o[c.label] = c.get(r);\n"
            "    return o;\n"
            "  });\n"
            "}\n"
        ),
    }
    r_cold, nid_cold = _extract_js_dir(tmp_path, files)
    assert all(t != nid_cold["r"] for _s, t in _rels(r_cold, "indirect_call"))

    # Re-extract against the same tmp_path / cache_root: the second run reads
    # the warm AST cache for both unchanged files.
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        r_warm = extract(
            [Path("src") / name for name in files],
            cache_root=Path(".cache"), parallel=False,
        )
    finally:
        os.chdir(old)
    nid_warm = {n["label"].rstrip("()"): n["id"] for n in r_warm["nodes"]}
    assert all(t != nid_warm["r"] for _s, t in _rels(r_warm, "indirect_call"))

