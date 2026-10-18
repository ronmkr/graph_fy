"""Framework-specific heuristic dependency extractors (FastAPI, Pytest, Express, React)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _file_stem, _make_id

# Regex for FastAPI / Starlette dependency injection: Depends(callable_name)
_FASTAPI_DEPENDS_RE = re.compile(
    r"""(?:\bDepends\s*\(\s*([A-Za-z_][A-Za-z0-9_]*))"""
)

# Route decorators: @app.get(...), @router.post(...), etc.
_FASTAPI_ROUTE_RE = re.compile(
    r"""@(?:app|router|api_router)\.(?:get|post|put|delete|patch|options|head)\s*\("""
)

# Pytest fixture definition: @pytest.fixture or @fixture
_PYTEST_FIXTURE_DEF_RE = re.compile(
    r"""@(?:pytest\.)?fixture\b"""
)

# Pytest test function: def test_foo(param1, param2):
_PYTEST_TEST_FUNC_RE = re.compile(
    r"""def\s+(test_[A-Za-z0-9_]*)\s*\(([^)]*)\)"""
)

# Express middleware: app.use(middleware) or router.use(middleware)
_EXPRESS_USE_RE = re.compile(
    r"""(?:app|router)\.use\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)"""
)

# React custom hook calls: useAuth(), useSession(), etc. (excluding React core built-ins)
_REACT_HOOK_CALL_RE = re.compile(
    r"""\b(use[A-Z][A-Za-z0-9_]*)\s*\("""
)

_REACT_CORE_BUILTINS: frozenset[str] = frozenset({
    "useState",
    "useEffect",
    "useContext",
    "useReducer",
    "useCallback",
    "useMemo",
    "useRef",
    "useImperativeHandle",
    "useLayoutEffect",
    "useDebugValue",
    "useDeferredValue",
    "useTransition",
    "useId",
    "useSyncExternalStore",
    "useInsertionEffect",
})

# Standard noise parameters to ignore in test functions
_TEST_PARAM_IGNORE: frozenset[str] = frozenset({"self", "cls"})


def extract_python_frameworks(path: Path, source_text: str, result: dict[str, Any]) -> None:
    """Extract framework dependency relationships for FastAPI and Pytest."""
    if not source_text:
        return

    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    file_nid = _make_id(str(path))
    str_path = str(path)

    func_nodes: dict[str, dict[str, Any]] = {
        str(n.get("label", "")): n
        for n in nodes
        if str(n.get("file_type", "")).lower() in ("function", "method", "def")
    }

    # 1. FastAPI / Starlette Dependency Injection
    for match in _FASTAPI_DEPENDS_RE.finditer(source_text):
        dep_name = match.group(1)
        if not dep_name:
            continue
        pos = match.start()
        line_num = source_text[:pos].count("\n") + 1

        matched_fn: dict[str, Any] | None = None
        for n in nodes:
            sloc = str(n.get("source_location") or "")
            m = re.search(r"L?(\d+)", sloc)
            if m:
                fn_line = int(m.group(1))
                if fn_line <= line_num:
                    if matched_fn is None:
                        matched_fn = n
                    else:
                        m_prev = re.search(r"L?(\d+)", str(matched_fn.get("source_location") or ""))
                        prev_line = int(m_prev.group(1)) if m_prev else 0
                        if fn_line > prev_line:
                            matched_fn = n

        src_id = matched_fn["id"] if matched_fn else file_nid
        if matched_fn:
            matched_fn["has_dependency_injection"] = True

        edges.append({
            "source": src_id,
            "target": dep_name,
            "relation": "depends_on",
            "context": "fastapi_di",
            "confidence": "INFERRED",
            "source_file": str_path,
            "source_location": f"L{line_num}",
            "weight": 1.0,
        })

    # 2. Pytest Test Fixtures
    for match in _PYTEST_TEST_FUNC_RE.finditer(source_text):
        fn_name = match.group(1)
        params_str = match.group(2)
        pos = match.start()
        line_num = source_text[:pos].count("\n") + 1

        fn_node = func_nodes.get(fn_name)
        src_id = fn_node["id"] if fn_node else file_nid

        for raw_param in params_str.split(","):
            clean_param = raw_param.strip().split(":")[0].split("=")[0].strip()
            if not clean_param or clean_param in _TEST_PARAM_IGNORE:
                continue
            edges.append({
                "source": src_id,
                "target": clean_param,
                "relation": "depends_on",
                "context": "pytest_fixture",
                "confidence": "INFERRED",
                "source_file": str_path,
                "source_location": f"L{line_num}",
                "weight": 1.0,
            })


def extract_js_frameworks(path: Path, source_text: str, result: dict[str, Any]) -> None:
    """Extract framework dependency relationships for Express and React."""
    if not source_text:
        return

    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    file_nid = _make_id(str(path))
    str_path = str(path)

    # 1. Express Middleware
    for match in _EXPRESS_USE_RE.finditer(source_text):
        middleware_name = match.group(1)
        if not middleware_name:
            continue
        line_num = source_text[:match.start()].count("\n") + 1
        edges.append({
            "source": file_nid,
            "target": middleware_name,
            "relation": "uses",
            "context": "express_middleware",
            "confidence": "INFERRED",
            "source_file": str_path,
            "source_location": f"L{line_num}",
            "weight": 1.0,
        })

    # 2. React Custom Hooks
    for match in _REACT_HOOK_CALL_RE.finditer(source_text):
        hook_name = match.group(1)
        if not hook_name or hook_name in _REACT_CORE_BUILTINS:
            continue
        line_num = source_text[:match.start()].count("\n") + 1

        matched_fn: dict[str, Any] | None = None
        for n in nodes:
            sloc = str(n.get("source_location") or "")
            m = re.search(r"L?(\d+)", sloc)
            if m:
                fn_line = int(m.group(1))
                if fn_line <= line_num:
                    if matched_fn is None:
                        matched_fn = n
                    else:
                        m_prev = re.search(r"L?(\d+)", str(matched_fn.get("source_location") or ""))
                        prev_line = int(m_prev.group(1)) if m_prev else 0
                        if fn_line > prev_line:
                            matched_fn = n

        src_id = matched_fn["id"] if matched_fn else file_nid
        edges.append({
            "source": src_id,
            "target": hook_name,
            "relation": "calls",
            "context": "react_hook",
            "confidence": "INFERRED",
            "source_file": str_path,
            "source_location": f"L{line_num}",
            "weight": 1.0,
        })
