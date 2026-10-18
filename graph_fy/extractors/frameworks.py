"""Framework-specific heuristic dependency extractors (FastAPI, Pytest, Express, React)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _file_stem, _make_id
from graph_fy.skeleton import get_parser_for_path

# Regex fallback for FastAPI / Starlette dependency injection: Depends(callable_name)
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


def _extract_python_frameworks_ast(path: Path, source_bytes: bytes, result: dict[str, Any]) -> bool:
    """Extract FastAPI and Pytest dependencies via Tree-sitter AST queries."""
    parser, lang_name = get_parser_for_path(".py")
    if not parser or lang_name != "python":
        return False

    try:
        tree = parser.parse(source_bytes)
        if not tree or not tree.root_node:
            return False
    except Exception:
        return False

    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    file_nid = _make_id(str(path))
    str_path = str(path)

    func_nodes: dict[str, dict[str, Any]] = {
        str(n.get("label", "")): n
        for n in nodes
        if str(n.get("file_type", "")).lower() in ("function", "method", "def")
    }

    def _find_enclosing_func_node(ast_node: Any) -> dict[str, Any] | None:
        curr = ast_node.parent
        while curr:
            if curr.type in ("function_definition", "async_function_definition"):
                name_node = curr.child_by_field_name("name")
                if name_node:
                    fn_name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")
                    if fn_name in func_nodes:
                        return func_nodes[fn_name]
            curr = curr.parent
        # Fallback to line proximity
        line_num = ast_node.start_point[0] + 1
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
        return matched_fn

    def _walk(ast_node: Any) -> None:
        if ast_node.type == "call":
            fn = ast_node.child_by_field_name("function")
            if fn:
                fn_name = source_bytes[fn.start_byte:fn.end_byte].decode("utf-8", errors="ignore")
                if fn_name == "Depends" or fn_name.endswith(".Depends"):
                    args = ast_node.child_by_field_name("arguments")
                    if args:
                        for arg in args.children:
                            dep_name = ""
                            if arg.type == "identifier":
                                dep_name = source_bytes[arg.start_byte:arg.end_byte].decode("utf-8", errors="ignore")
                            elif arg.type == "attribute":
                                attr_node = arg.child_by_field_name("attribute")
                                if attr_node:
                                    dep_name = source_bytes[attr_node.start_byte:attr_node.end_byte].decode("utf-8", errors="ignore")
                            if dep_name:
                                matched_fn = _find_enclosing_func_node(ast_node)
                                src_id = matched_fn["id"] if matched_fn else file_nid
                                if matched_fn:
                                    matched_fn["has_dependency_injection"] = True
                                line_num = ast_node.start_point[0] + 1
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
        elif ast_node.type in ("function_definition", "async_function_definition"):
            name_node = ast_node.child_by_field_name("name")
            if name_node:
                fn_name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")
                if fn_name.startswith("test_"):
                    fn_node = func_nodes.get(fn_name)
                    src_id = fn_node["id"] if fn_node else file_nid
                    params = ast_node.child_by_field_name("parameters")
                    if params:
                        for p in params.children:
                            clean_param = ""
                            if p.type == "identifier":
                                clean_param = source_bytes[p.start_byte:p.end_byte].decode("utf-8", errors="ignore")
                            elif p.type in ("default_parameter", "typed_parameter", "typed_default_parameter"):
                                p_name = p.child_by_field_name("name") or p
                                if p_name.type == "identifier":
                                    clean_param = source_bytes[p_name.start_byte:p_name.end_byte].decode("utf-8", errors="ignore")
                            if clean_param and clean_param not in _TEST_PARAM_IGNORE:
                                line_num = p.start_point[0] + 1
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

        for child in ast_node.children:
            _walk(child)

    _walk(tree.root_node)
    return True


def _extract_python_frameworks_regex(path: Path, source_text: str, result: dict[str, Any]) -> None:
    """Fallback regex extractor for FastAPI and Pytest dependencies."""
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


def extract_python_frameworks(path: Path, source_text: str, result: dict[str, Any]) -> None:
    """Extract framework dependency relationships for FastAPI and Pytest."""
    if not source_text:
        return
    source_bytes = source_text.encode("utf-8", errors="ignore")
    if _extract_python_frameworks_ast(path, source_bytes, result):
        return
    _extract_python_frameworks_regex(path, source_text, result)


def _extract_js_frameworks_ast(path: Path, source_bytes: bytes, result: dict[str, Any]) -> bool:
    """Extract Express and React dependencies via Tree-sitter AST queries."""
    parser, lang_name = get_parser_for_path(path.suffix.lower() or ".js")
    if not parser or lang_name not in ("javascript", "typescript", "tsx"):
        return False

    try:
        tree = parser.parse(source_bytes)
        if not tree or not tree.root_node:
            return False
    except Exception:
        return False

    nodes = result.setdefault("nodes", [])
    edges = result.setdefault("edges", [])
    file_nid = _make_id(str(path))
    str_path = str(path)

    func_nodes: dict[str, dict[str, Any]] = {
        str(n.get("label", "")): n
        for n in nodes
        if str(n.get("file_type", "")).lower() in ("function", "method", "class", "component")
    }

    def _find_enclosing_js_func(ast_node: Any) -> dict[str, Any] | None:
        curr = ast_node.parent
        while curr:
            if curr.type in ("function_declaration", "method_definition", "function"):
                name_node = curr.child_by_field_name("name")
                if name_node:
                    fn_name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")
                    if fn_name in func_nodes:
                        return func_nodes[fn_name]
            elif curr.type == "variable_declarator":
                val = curr.child_by_field_name("value")
                if val and val.type in ("arrow_function", "function_expression"):
                    name_node = curr.child_by_field_name("name")
                    if name_node:
                        fn_name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")
                        if fn_name in func_nodes:
                            return func_nodes[fn_name]
            curr = curr.parent
        # Fallback to line proximity
        line_num = ast_node.start_point[0] + 1
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
        return matched_fn

    def _walk(ast_node: Any) -> None:
        if ast_node.type == "call_expression":
            fn = ast_node.child_by_field_name("function")
            if fn:
                # 1. Express middleware: app.use(...) or router.use(...)
                if fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop and source_bytes[prop.start_byte:prop.end_byte] == b"use":
                        args = ast_node.child_by_field_name("arguments")
                        if args:
                            for arg in args.children:
                                mw_name = ""
                                if arg.type == "identifier":
                                    mw_name = source_bytes[arg.start_byte:arg.end_byte].decode("utf-8", errors="ignore")
                                elif arg.type == "member_expression":
                                    prop_node = arg.child_by_field_name("property")
                                    if prop_node:
                                        mw_name = source_bytes[prop_node.start_byte:prop_node.end_byte].decode("utf-8", errors="ignore")
                                if mw_name:
                                    line_num = ast_node.start_point[0] + 1
                                    edges.append({
                                        "source": file_nid,
                                        "target": mw_name,
                                        "relation": "uses",
                                        "context": "express_middleware",
                                        "confidence": "INFERRED",
                                        "source_file": str_path,
                                        "source_location": f"L{line_num}",
                                        "weight": 1.0,
                                    })
                # 2. React custom hooks: use*()
                elif fn.type == "identifier":
                    hook_name = source_bytes[fn.start_byte:fn.end_byte].decode("utf-8", errors="ignore")
                    if hook_name.startswith("use") and len(hook_name) > 3 and hook_name[3].isupper():
                        if hook_name not in _REACT_CORE_BUILTINS:
                            matched_fn = _find_enclosing_js_func(ast_node)
                            src_id = matched_fn["id"] if matched_fn else file_nid
                            line_num = ast_node.start_point[0] + 1
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

        for child in ast_node.children:
            _walk(child)

    _walk(tree.root_node)
    return True


def _extract_js_frameworks_regex(path: Path, source_text: str, result: dict[str, Any]) -> None:
    """Fallback regex extractor for Express and React dependencies."""
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


def extract_js_frameworks(path: Path, source_text: str, result: dict[str, Any]) -> None:
    """Extract framework dependency relationships for Express and React."""
    if not source_text:
        return
    source_bytes = source_text.encode("utf-8", errors="ignore")
    if _extract_js_frameworks_ast(path, source_bytes, result):
        return
    _extract_js_frameworks_regex(path, source_text, result)
