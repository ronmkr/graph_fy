import tempfile
from pathlib import Path
import pytest

from graph_fy.ffi_resolve import resolve_ffi_boundaries


def test_rust_to_c_ffi_resolution(tmp_path):
    """Test linking Rust #[no_mangle] extern "C" function with C extern "C" declaration and callers."""
    rs_file = tmp_path / "crypto.rs"
    rs_file.write_text(
        """
        #[no_mangle]
        pub extern "C" fn calculate_sha256(data: *const u8, len: usize) -> u32 {
            42
        }
        """,
        encoding="utf-8",
    )

    c_file = tmp_path / "main.c"
    c_file.write_text(
        """
        extern "C" {
            unsigned int calculate_sha256(const unsigned char* data, size_t len);
        }

        int run_app() {
            return calculate_sha256("test", 4);
        }
        """,
        encoding="utf-8",
    )

    nodes = [
        {"id": "crypto_rs", "label": "crypto.rs", "file_type": "file", "source_file": str(rs_file)},
        {"id": "rust_sha256", "label": "calculate_sha256()", "file_type": "code", "source_file": str(rs_file), "source_location": "L3"},
        {"id": "main_c", "label": "main.c", "file_type": "file", "source_file": str(c_file)},
        {"id": "c_decl_sha256", "label": "calculate_sha256()", "file_type": "code", "source_file": str(c_file), "source_location": "L3"},
        {"id": "c_run_app", "label": "run_app()", "file_type": "code", "source_file": str(c_file), "source_location": "L6"},
    ]
    edges = []

    res_nodes, res_edges = resolve_ffi_boundaries(nodes, edges, root=tmp_path)

    # Expect calls_ffi from C declaration and C run_app to Rust sha256
    ffi_edges = [e for e in res_edges if e.get("relation") == "calls_ffi"]
    assert len(ffi_edges) >= 1
    edge_pairs = {(e["source"], e["target"]) for e in ffi_edges}
    assert ("c_decl_sha256", "rust_sha256") in edge_pairs or ("c_run_app", "rust_sha256") in edge_pairs
    assert any(e.get("context") == "ffi:rust_c" for e in ffi_edges)


def test_rust_to_python_pyo3_resolution(tmp_path):
    """Test linking PyO3 #[pymodule] and #[pyfunction] to Python imports and callers."""
    rs_file = tmp_path / "native_core.rs"
    rs_file.write_text(
        """
        use pyo3::prelude::*;

        #[pyfunction]
        fn compute_pi() -> f64 {
            3.14159
        }

        #[pymodule]
        fn native_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
            m.add_function(wrap_pyfunction!(compute_pi, m)?)?;
            Ok(())
        }
        """,
        encoding="utf-8",
    )

    py_file = tmp_path / "app.py"
    py_file.write_text(
        """
        import native_core
        from native_core import compute_pi

        def calculate():
            return compute_pi()
        """,
        encoding="utf-8",
    )

    nodes = [
        {"id": "native_core_rs", "label": "native_core.rs", "file_type": "file", "source_file": str(rs_file)},
        {"id": "rs_mod_node", "label": "native_core", "file_type": "code", "source_file": str(rs_file), "source_location": "L10"},
        {"id": "rs_fn_node", "label": "compute_pi()", "file_type": "code", "source_file": str(rs_file), "source_location": "L5"},
        {"id": "app_py", "label": "app.py", "file_type": "file", "source_file": str(py_file)},
        {"id": "py_calculate_fn", "label": "calculate()", "file_type": "code", "source_file": str(py_file), "source_location": "L5"},
    ]
    edges = []

    res_nodes, res_edges = resolve_ffi_boundaries(nodes, edges, root=tmp_path)

    # 1. imports_native from app.py to native_core and compute_pi
    import_edges = [e for e in res_edges if e.get("relation") == "imports_native"]
    assert len(import_edges) >= 1
    assert any(e["source"] == "app_py" and e["target"] == "rs_mod_node" for e in import_edges)

    # 2. calls_ffi from calculate() to compute_pi()
    call_edges = [e for e in res_edges if e.get("relation") == "calls_ffi"]
    assert len(call_edges) >= 1
    assert any(e["source"] == "py_calculate_fn" and e["target"] == "rs_fn_node" for e in call_edges)


def test_cpp_to_python_pybind11_resolution(tmp_path):
    """Test linking pybind11 PYBIND11_MODULE to Python imports and function calls."""
    cpp_file = tmp_path / "bindings.cpp"
    cpp_file.write_text(
        """
        #include <pybind11/pybind11.h>
        namespace py = pybind11;

        int multiply_fast(int a, int b) { return a * b; }

        PYBIND11_MODULE(fast_math, m) {
            m.def("multiply_fast", &multiply_fast, "Multiply two numbers");
        }
        """,
        encoding="utf-8",
    )

    py_file = tmp_path / "solver.py"
    py_file.write_text(
        """
        import fast_math

        def solve(x, y):
            return fast_math.multiply_fast(x, y)
        """,
        encoding="utf-8",
    )

    nodes = [
        {"id": "bindings_cpp", "label": "bindings.cpp", "file_type": "file", "source_file": str(cpp_file)},
        {"id": "cpp_mod_node", "label": "fast_math", "file_type": "code", "source_file": str(cpp_file), "source_location": "L7"},
        {"id": "cpp_mult_fn", "label": "multiply_fast()", "file_type": "code", "source_file": str(cpp_file), "source_location": "L5"},
        {"id": "solver_py", "label": "solver.py", "file_type": "file", "source_file": str(py_file)},
        {"id": "py_solve_fn", "label": "solve()", "file_type": "code", "source_file": str(py_file), "source_location": "L4"},
    ]
    edges = []

    res_nodes, res_edges = resolve_ffi_boundaries(nodes, edges, root=tmp_path)

    import_edges = [e for e in res_edges if e.get("relation") == "imports_native"]
    assert any(e["source"] == "solver_py" and e["target"] == "cpp_mod_node" for e in import_edges)

    call_edges = [e for e in res_edges if e.get("relation") == "calls_ffi"]
    assert any(e["source"] == "py_solve_fn" and e["target"] == "cpp_mult_fn" for e in call_edges)


def test_rust_to_ts_wasm_bindgen_resolution(tmp_path):
    """Test linking #[wasm_bindgen] Rust functions to TypeScript imports and callers."""
    rs_file = tmp_path / "lib.rs"
    rs_file.write_text(
        """
        use wasm_bindgen::prelude::*;

        #[wasm_bindgen]
        pub fn render_canvas(width: u32, height: u32) {
            // wasm logic
        }
        """,
        encoding="utf-8",
    )

    ts_file = tmp_path / "view.ts"
    ts_file.write_text(
        """
        import { render_canvas } from './pkg/wasm_renderer.js';

        export function initCanvas() {
            render_canvas(800, 600);
        }
        """,
        encoding="utf-8",
    )

    nodes = [
        {"id": "lib_rs", "label": "lib.rs", "file_type": "file", "source_file": str(rs_file)},
        {"id": "rs_render_fn", "label": "render_canvas()", "file_type": "code", "source_file": str(rs_file), "source_location": "L5"},
        {"id": "view_ts", "label": "view.ts", "file_type": "file", "source_file": str(ts_file)},
        {"id": "ts_init_fn", "label": "initCanvas()", "file_type": "code", "source_file": str(ts_file), "source_location": "L4"},
    ]
    edges = []

    res_nodes, res_edges = resolve_ffi_boundaries(nodes, edges, root=tmp_path)

    import_edges = [e for e in res_edges if e.get("relation") == "imports_native"]
    assert any(e["source"] == "view_ts" and e["target"] == "rs_render_fn" for e in import_edges)

    call_edges = [e for e in res_edges if e.get("relation") == "calls_ffi"]
    assert any(e["source"] == "ts_init_fn" and e["target"] == "rs_render_fn" for e in call_edges)


def test_ffi_resolution_noop_on_monoglot():
    """Verify resolver short-circuits instantly on projects without native code boundaries."""
    nodes = [
        {"id": "a_py", "label": "a.py", "file_type": "file", "source_file": "a.py"},
        {"id": "fn_a", "label": "fn()", "file_type": "code", "source_file": "a.py"},
    ]
    edges = []
    res_nodes, res_edges = resolve_ffi_boundaries(nodes, edges)
    assert len(res_edges) == 0


def test_ffi_end_to_end_blast_radius(tmp_path):
    """Verify affected_nodes traverses across calls_ffi to identify polyglot callers."""
    import networkx as nx
    from graph_fy.affected import affected_nodes

    rs_file = tmp_path / "core.rs"
    rs_file.write_text(
        """
        #[pyfunction]
        pub fn process_tensor() { }
        """,
        encoding="utf-8",
    )

    py_file = tmp_path / "trainer.py"
    py_file.write_text(
        """
        from core import process_tensor

        def train_step():
            process_tensor()
        """,
        encoding="utf-8",
    )

    nodes = [
        {"id": "core_rs", "label": "core.rs", "file_type": "file", "source_file": str(rs_file)},
        {"id": "rs_tensor_fn", "label": "process_tensor()", "file_type": "code", "source_file": str(rs_file), "source_location": "L3"},
        {"id": "trainer_py", "label": "trainer.py", "file_type": "file", "source_file": str(py_file)},
        {"id": "py_train_fn", "label": "train_step()", "file_type": "code", "source_file": str(py_file), "source_location": "L4"},
    ]
    edges = []

    res_nodes, res_edges = resolve_ffi_boundaries(nodes, edges, root=tmp_path)

    # Build graph
    G = nx.DiGraph()
    for n in res_nodes:
        G.add_node(n["id"], **n)
    for e in res_edges:
        G.add_edge(e["source"], e["target"], **e)

    # Blast radius from Rust tensor function
    hits = affected_nodes(G, "rs_tensor_fn", depth=2)
    hit_ids = {h.node_id: h.via_relation for h in hits}
    assert "py_train_fn" in hit_ids
    assert hit_ids["py_train_fn"] == "calls_ffi"


def test_python_ctypes_resolution(tmp_path):
    """Test linking ctypes.CDLL to Rust exported functions."""
    rs_file = tmp_path / "native.rs"
    rs_file.write_text(
        """
        #[no_mangle]
        pub extern "C" fn fast_add(a: i32, b: i32) -> i32 { a + b }
        """,
        encoding="utf-8",
    )

    py_file = tmp_path / "wrapper.py"
    py_file.write_text(
        """
        import ctypes
        lib = ctypes.CDLL("libnative.so")
        """,
        encoding="utf-8",
    )

    nodes = [
        {"id": "native_rs", "label": "native.rs", "file_type": "file", "source_file": str(rs_file)},
        {"id": "rs_add_fn", "label": "fast_add()", "file_type": "code", "source_file": str(rs_file), "source_location": "L3"},
        {"id": "wrapper_py", "label": "wrapper.py", "file_type": "file", "source_file": str(py_file)},
    ]
    edges = []

    res_nodes, res_edges = resolve_ffi_boundaries(nodes, edges, root=tmp_path)
    ctypes_edges = [e for e in res_edges if e.get("relation") == "imports_native"]
    assert any(e["source"] == "wrapper_py" and e["target"] == "rs_add_fn" for e in ctypes_edges)
