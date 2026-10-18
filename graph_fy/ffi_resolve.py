"""ffi_resolve.py — Cross-Language Foreign Function Interface (FFI) Resolution.

Discovers and links native language boundaries in polyglot projects:
1. Rust <-> C/C++: #[no_mangle] pub extern "C" fn and #[cxx::bridge] <-> extern "C" declarations.
2. Rust <-> Python (PyO3): #[pymodule], #[pyfunction], #[pymethods] <-> Python import and calls.
3. C/C++ <-> Python (pybind11 / ctypes): PYBIND11_MODULE, PyMODINIT_FUNC, ctypes.CDLL <-> Python callers.
4. Rust/C++ <-> JS/TS (WASM): #[wasm_bindgen] exports <-> TypeScript/JavaScript imports.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Rust FFI patterns
_RUST_NO_MANGLE_RE = re.compile(
    r'#\[no_mangle\]\s*(?:pub\s+)?(?:unsafe\s+)?extern\s+"C"\s+fn\s+([A-Za-z0-9_]+)',
    re.MULTILINE,
)
_RUST_EXTERN_C_FN_RE = re.compile(
    r'(?:pub\s+)?(?:unsafe\s+)?extern\s+"C"\s+fn\s+([A-Za-z0-9_]+)',
    re.MULTILINE,
)
_RUST_CXX_BRIDGE_RE = re.compile(
    r'#\[cxx::bridge(?:\([^)]*\))?\]\s*mod\s+([A-Za-z0-9_]+)',
    re.MULTILINE,
)
_RUST_PYMODULE_RE = re.compile(
    r'#\[pymodule\]\s*(?:pub\s+)?(?:unsafe\s+)?fn\s+([A-Za-z0-9_]+)',
    re.MULTILINE,
)
_RUST_PYFUNCTION_RE = re.compile(
    r'#\[pyfunction(?:\([^)]*\))?\]\s*(?:pub\s+)?(?:unsafe\s+)?fn\s+([A-Za-z0-9_]+)',
    re.MULTILINE,
)
_RUST_PYCLASS_RE = re.compile(
    r'#\[pyclass(?:\([^)]*\))?\]\s*(?:pub\s+)?struct\s+([A-Za-z0-9_]+)',
    re.MULTILINE,
)
_RUST_WASM_FN_RE = re.compile(
    r'#\[wasm_bindgen(?:\([^)]*\))?\]\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z0-9_]+)',
    re.MULTILINE,
)
_RUST_WASM_STRUCT_RE = re.compile(
    r'#\[wasm_bindgen(?:\([^)]*\))?\]\s*(?:pub\s+)?struct\s+([A-Za-z0-9_]+)',
    re.MULTILINE,
)

# C/C++ FFI patterns
_CPP_EXTERN_C_BLOCK_RE = re.compile(
    r'extern\s+"C"\s*\{([^}]+)\}',
    re.MULTILINE | re.DOTALL,
)
_CPP_EXTERN_C_FN_RE = re.compile(
    r'extern\s+"C"\s+(?:[A-Za-z0-9_:*&<>]+\s+)+([A-Za-z0-9_]+)\s*\([^)]*\)\s*;',
    re.MULTILINE,
)
_CPP_PYBIND11_MODULE_RE = re.compile(
    r'PYBIND11_MODULE\s*\(\s*([A-Za-z0-9_]+)\s*,',
    re.MULTILINE,
)
_CPP_PYBIND11_DEF_RE = re.compile(
    r'\.def\s*\(\s*["\']([A-Za-z0-9_]+)["\']',
    re.MULTILINE,
)
_CPP_PYMODINIT_RE = re.compile(
    r'PyMODINIT_FUNC\s+PyInit_([A-Za-z0-9_]+)',
    re.MULTILINE,
)

# Python consumer patterns
_PY_IMPORT_RE = re.compile(
    r'^\s*(?:from\s+([A-Za-z0-9_.]+)\s+import\s+([A-Za-z0-9_,\s*]+)|import\s+([A-Za-z0-9_.]+))',
    re.MULTILINE,
)
_PY_CTYPES_CDLL_RE = re.compile(
    r'ctypes\.(?:CDLL|cdll\.LoadLibrary)\s*\(\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)

# JS/TS WASM consumer patterns
_JS_IMPORT_RE = re.compile(
    r'^\s*import\s+(?:\{([^}]+)\}|\*\s+as\s+([A-Za-z0-9_]+)|([A-Za-z0-9_]+))\s+from\s+["\']([^"\']+)["\']',
    re.MULTILINE,
)


def _read_file_safe(path_str: str, cache: dict[str, str]) -> str:
    """Read file content with caching and error tolerance."""
    if not path_str:
        return ""
    if path_str in cache:
        return cache[path_str]
    p = Path(path_str)
    if not p.is_file():
        cache[path_str] = ""
        return ""
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        cache[path_str] = content
        return content
    except Exception:
        cache[path_str] = ""
        return ""


def _match_symbol_node(file_nodes: list[dict[str, Any]], name: str, fallback_file_nid: str | None = None) -> dict[str, Any] | None:
    for n in file_nodes:
        if n.get("file_type") == "file":
            continue
        lbl = (n.get("label") or "").rstrip("()")
        if lbl == name:
            return n
    for n in file_nodes:
        if n.get("file_type") == "file":
            continue
        lbl = (n.get("label") or "").rstrip("()")
        if lbl.startswith(name):
            return n
    if fallback_file_nid:
        for n in file_nodes:
            if n.get("id") == fallback_file_nid:
                return n
    return file_nodes[0] if file_nodes else None


def resolve_ffi_boundaries(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    root: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Discover cross-language FFI exports and callers, injecting calls_ffi and imports_native edges."""
    if not nodes:
        return nodes, edges

    # Quick language presence check
    source_files = {n.get("source_file") for n in nodes if n.get("source_file")}
    has_rust = any(sf.endswith(".rs") for sf in source_files if sf)
    has_cpp = any(sf.endswith((".c", ".cpp", ".cc", ".cxx", ".h", ".hpp")) for sf in source_files if sf)
    has_py = any(sf.endswith(".py") for sf in source_files if sf)
    has_js = any(sf.endswith((".js", ".jsx", ".ts", ".tsx")) for sf in source_files if sf)

    # Need at least two interacting language families
    native_count = (1 if has_rust else 0) + (1 if has_cpp else 0)
    consumer_count = (1 if has_py else 0) + (1 if has_js else 0)
    if native_count == 0 or (native_count < 2 and consumer_count == 0):
        return nodes, edges

    file_cache: dict[str, str] = {}
    seen_edges: set[tuple[str, str, str]] = {
        (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        for e in edges
    }

    def add_edge(src: str, tgt: str, rel: str, conf: str = "EXTRACTED", ctx: str = "ffi", sf: str = "", loc: str = "L1") -> None:
        if not src or not tgt or src == tgt:
            return
        key = (src, tgt, rel)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({
            "source": src,
            "target": tgt,
            "relation": rel,
            "confidence": conf,
            "context": ctx,
            "weight": 1.0,
            "source_file": sf,
            "source_location": loc,
        })

    # Group nodes by source file and by label / base name
    nodes_by_file: dict[str, list[dict[str, Any]]] = {}
    nodes_by_name: dict[str, list[dict[str, Any]]] = {}
    file_nodes: dict[str, str] = {}  # source_file -> file_node_id

    for n in nodes:
        nid = n.get("id")
        if not nid:
            continue
        sf = n.get("source_file")
        if sf:
            nodes_by_file.setdefault(sf, []).append(n)
            lbl = n.get("label", "")
            if sf.endswith(lbl) or n.get("file_type") == "file":
                file_nodes[sf] = nid

        raw_label = str(n.get("label") or nid)
        clean_name = raw_label[:-2] if raw_label.endswith("()") else raw_label
        nodes_by_name.setdefault(clean_name, []).append(n)

    # -------------------------------------------------------------
    # 1. Rust FFI Exports Indexing
    # -------------------------------------------------------------
    rust_c_exports: dict[str, dict[str, Any]] = {}     # fn_name -> node
    rust_py_modules: dict[str, dict[str, Any]] = {}    # mod_name -> file_or_fn_node
    rust_py_functions: dict[str, dict[str, Any]] = {}  # fn_name -> node
    rust_py_classes: dict[str, dict[str, Any]] = {}    # class_name -> node
    rust_wasm_exports: dict[str, dict[str, Any]] = {}  # export_name -> node

    if has_rust:
        for sf, file_nodes_list in nodes_by_file.items():
            if not sf.endswith(".rs"):
                continue
            code = _read_file_safe(sf, file_cache)
            if not code:
                continue

            fnid = file_nodes.get(sf)

            # a. #[no_mangle] pub extern "C" fn
            for m in _RUST_NO_MANGLE_RE.finditer(code):
                fn_name = m.group(1)
                node = _match_symbol_node(file_nodes_list, fn_name, fnid)
                if node:
                    rust_c_exports[fn_name] = node

            for m in _RUST_EXTERN_C_FN_RE.finditer(code):
                fn_name = m.group(1)
                if fn_name not in rust_c_exports:
                    node = _match_symbol_node(file_nodes_list, fn_name, fnid)
                    if node:
                        rust_c_exports[fn_name] = node

            # b. PyO3 #[pymodule]
            for m in _RUST_PYMODULE_RE.finditer(code):
                mod_name = m.group(1)
                node = _match_symbol_node(file_nodes_list, mod_name, fnid)
                if node:
                    rust_py_modules[mod_name] = node

            # c. PyO3 #[pyfunction]
            for m in _RUST_PYFUNCTION_RE.finditer(code):
                fn_name = m.group(1)
                node = _match_symbol_node(file_nodes_list, fn_name, fnid)
                if node:
                    rust_py_functions[fn_name] = node

            # d. PyO3 #[pyclass]
            for m in _RUST_PYCLASS_RE.finditer(code):
                cls_name = m.group(1)
                node = _match_symbol_node(file_nodes_list, cls_name, fnid)
                if node:
                    rust_py_classes[cls_name] = node

            # e. #[wasm_bindgen]
            for m in _RUST_WASM_FN_RE.finditer(code):
                fn_name = m.group(1)
                node = _match_symbol_node(file_nodes_list, fn_name, fnid)
                if node:
                    rust_wasm_exports[fn_name] = node
            for m in _RUST_WASM_STRUCT_RE.finditer(code):
                st_name = m.group(1)
                node = _match_symbol_node(file_nodes_list, st_name, fnid)
                if node:
                    rust_wasm_exports[st_name] = node

    # -------------------------------------------------------------
    # 2. C/C++ FFI Exports & Declarations Indexing
    # -------------------------------------------------------------
    cpp_extern_c_decls: dict[str, dict[str, Any]] = {} # fn_name -> node
    cpp_py_modules: dict[str, dict[str, Any]] = {}     # mod_name -> file_or_fn_node
    cpp_py_functions: dict[str, dict[str, Any]] = {}   # fn_name -> node

    if has_cpp:
        for sf, file_nodes_list in nodes_by_file.items():
            if not sf.endswith((".c", ".cpp", ".cc", ".cxx", ".h", ".hpp")):
                continue
            code = _read_file_safe(sf, file_cache)
            if not code:
                continue

            fnid = file_nodes.get(sf)

            # a. extern "C" { void fn_name(); }
            for m in _CPP_EXTERN_C_BLOCK_RE.finditer(code):
                block = m.group(1)
                fn_names = re.findall(r'(?:[A-Za-z0-9_:*&<>]+\s+)+([A-Za-z0-9_]+)\s*\([^)]*\)\s*;', block)
                for fn in fn_names:
                    node = _match_symbol_node(file_nodes_list, fn, fnid)
                    if node:
                        cpp_extern_c_decls[fn] = node

            for m in _CPP_EXTERN_C_FN_RE.finditer(code):
                fn = m.group(1)
                node = _match_symbol_node(file_nodes_list, fn, fnid)
                if node:
                    cpp_extern_c_decls[fn] = node

            # b. PYBIND11_MODULE(name, m)
            for m in _CPP_PYBIND11_MODULE_RE.finditer(code):
                mod_name = m.group(1)
                node = _match_symbol_node(file_nodes_list, mod_name, fnid)
                if node:
                    cpp_py_modules[mod_name] = node

            # c. PyMODINIT_FUNC PyInit_name
            for m in _CPP_PYMODINIT_RE.finditer(code):
                mod_name = m.group(1)
                node = _match_symbol_node(file_nodes_list, mod_name, fnid)
                if node:
                    cpp_py_modules[mod_name] = node

            # d. m.def("func", &func)
            for m in _CPP_PYBIND11_DEF_RE.finditer(code):
                py_fn = m.group(1)
                node = _match_symbol_node(file_nodes_list, py_fn, fnid)
                if node:
                    cpp_py_functions[py_fn] = node

            # d. m.def("func", &func)
            for m in _CPP_PYBIND11_DEF_RE.finditer(code):
                py_fn = m.group(1)
                matching = [n for n in file_nodes_list if (n.get("label") or "").startswith(py_fn)]
                if matching:
                    cpp_py_functions[py_fn] = matching[0]

    # -------------------------------------------------------------
    # 3. Resolve Rust <-> C/C++ FFI Link
    # -------------------------------------------------------------
    if has_rust and has_cpp:
        # Match Rust extern "C" exports to C/C++ declarations or callers
        for fn_name, rust_node in rust_c_exports.items():
            # If C/C++ has matching extern "C" declaration
            if fn_name in cpp_extern_c_decls:
                cpp_node = cpp_extern_c_decls[fn_name]
                add_edge(
                    src=cpp_node["id"],
                    tgt=rust_node["id"],
                    rel="calls_ffi",
                    ctx="ffi:rust_c",
                    sf=cpp_node.get("source_file", ""),
                    loc=cpp_node.get("source_location", "L1"),
                )

            # Also find C/C++ caller functions that call fn_name
            for cpp_sf, cpp_nodes in nodes_by_file.items():
                if not cpp_sf.endswith((".c", ".cpp", ".cc", ".cxx")):
                    continue
                code = _read_file_safe(cpp_sf, file_cache)
                if not code or fn_name not in code:
                    continue
                for caller in cpp_nodes:
                    if caller["id"] == rust_node["id"] or caller.get("file_type") == "file":
                        continue
                    add_edge(
                        src=caller["id"],
                        tgt=rust_node["id"],
                        rel="calls_ffi",
                        ctx="ffi:rust_c",
                        sf=caller.get("source_file", ""),
                        loc=caller.get("source_location", "L1"),
                    )

    # -------------------------------------------------------------
    # 4. Resolve Python <-> Rust / C++ (PyO3, pybind11, ctypes)
    # -------------------------------------------------------------
    if has_py and (has_rust or has_cpp):
        all_native_modules = {**rust_py_modules, **cpp_py_modules}
        all_native_functions = {**rust_py_functions, **cpp_py_functions}

        for py_sf, py_nodes in nodes_by_file.items():
            if not py_sf.endswith(".py"):
                continue
            code = _read_file_safe(py_sf, file_cache)
            if not code:
                continue

            py_file_nid = file_nodes.get(py_sf) or (py_nodes[0]["id"] if py_nodes else None)

            # a. Check Python imports
            for m in _PY_IMPORT_RE.finditer(code):
                from_mod, from_items, imp_mod = m.group(1), m.group(2), m.group(3)
                target_mod = from_mod or imp_mod
                if not target_mod:
                    continue

                mod_base = target_mod.split(".")[0]
                if mod_base in all_native_modules:
                    target_nid = all_native_modules[mod_base]["id"]
                    if py_file_nid:
                        add_edge(
                            src=py_file_nid,
                            tgt=target_nid,
                            rel="imports_native",
                            ctx="ffi:import",
                            sf=py_sf,
                            loc=f"L{code[:m.start()].count(chr(10)) + 1}",
                        )

                # Imported functions
                if from_items:
                    items = [it.strip() for it in from_items.split(",")]
                    for it in items:
                        if it in all_native_functions:
                            fn_nid = all_native_functions[it]["id"]
                            if py_file_nid:
                                add_edge(
                                    src=py_file_nid,
                                    tgt=fn_nid,
                                    rel="imports_native",
                                    ctx="ffi:import",
                                    sf=py_sf,
                                    loc=f"L{code[:m.start()].count(chr(10)) + 1}",
                                )

            # b. Check ctypes CDLL bindings
            for m in _PY_CTYPES_CDLL_RE.finditer(code):
                lib_name = m.group(1)
                clean_lib = lib_name.replace("lib", "").split(".")[0]
                # Look for matching native module or c exports
                for exp_name, exp_node in rust_c_exports.items():
                    if clean_lib in exp_node.get("source_file", "").lower():
                        if py_file_nid:
                            add_edge(
                                src=py_file_nid,
                                tgt=exp_node["id"],
                                rel="imports_native",
                                ctx="ffi:ctypes",
                                sf=py_sf,
                                loc=f"L{code[:m.start()].count(chr(10)) + 1}",
                            )

            # c. Check function call sites in Python nodes
            for fn_name, fn_node in all_native_functions.items():
                if fn_name not in code:
                    continue
                for caller in py_nodes:
                    if caller["id"] == fn_node["id"] or caller.get("file_type") == "file":
                        continue
                    # Call pattern: fn_name( or mod.fn_name(
                    call_pat = re.compile(rf'\b{re.escape(fn_name)}\s*\(')
                    m = call_pat.search(code)
                    if m:
                        line_no = code[:m.start()].count("\n") + 1
                        add_edge(
                            src=caller["id"],
                            tgt=fn_node["id"],
                            rel="calls_ffi",
                            ctx="ffi:call",
                            sf=caller.get("source_file", ""),
                            loc=f"L{line_no}",
                        )

    # -------------------------------------------------------------
    # 5. Resolve JS/TS <-> Rust/C++ WASM (wasm-bindgen)
    # -------------------------------------------------------------
    if has_js and (has_rust or rust_wasm_exports):
        for js_sf, js_nodes in nodes_by_file.items():
            if not js_sf.endswith((".js", ".jsx", ".ts", ".tsx")):
                continue
            code = _read_file_safe(js_sf, file_cache)
            if not code:
                continue

            js_file_nid = file_nodes.get(js_sf) or (js_nodes[0]["id"] if js_nodes else None)

            # Check JS/TS imports
            for m in _JS_IMPORT_RE.finditer(code):
                named_imports, star_import, def_import, imp_path = m.group(1), m.group(2), m.group(3), m.group(4)
                is_wasm_import = (
                    "wasm" in imp_path.lower()
                    or "pkg" in imp_path
                    or imp_path.endswith("_bg.js")
                    or imp_path.endswith(".wasm")
                )

                if named_imports:
                    items = [it.strip().split(" as ")[0].strip() for it in named_imports.split(",")]
                    for item in items:
                        if item in rust_wasm_exports:
                            wasm_node = rust_wasm_exports[item]
                            if js_file_nid:
                                add_edge(
                                    src=js_file_nid,
                                    tgt=wasm_node["id"],
                                    rel="imports_native",
                                    ctx="ffi:wasm",
                                    sf=js_sf,
                                    loc=f"L{code[:m.start()].count(chr(10)) + 1}",
                                )
                elif is_wasm_import:
                    # Generic wasm module import -> connect to all wasm exports in crate
                    for exp_name, wasm_node in rust_wasm_exports.items():
                        if js_file_nid:
                            add_edge(
                                src=js_file_nid,
                                tgt=wasm_node["id"],
                                rel="imports_native",
                                ctx="ffi:wasm",
                                sf=js_sf,
                                loc=f"L{code[:m.start()].count(chr(10)) + 1}",
                            )

            # Check calls from JS/TS nodes
            for exp_name, wasm_node in rust_wasm_exports.items():
                if exp_name not in code:
                    continue
                call_pat = re.compile(rf'\b{re.escape(exp_name)}\s*\(')
                m = call_pat.search(code)
                if m:
                    line_no = code[:m.start()].count("\n") + 1
                    for caller in js_nodes:
                        if caller["id"] == wasm_node["id"] or caller.get("file_type") == "file":
                            continue
                        add_edge(
                            src=caller["id"],
                            tgt=wasm_node["id"],
                            rel="calls_ffi",
                            ctx="ffi:wasm_call",
                            sf=caller.get("source_file", ""),
                            loc=f"L{line_no}",
                        )

    return nodes, edges
