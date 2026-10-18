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

# Pytest fixture definition: @pytest.fixture or @fixture or @pytest_asyncio.fixture
_PYTEST_FIXTURE_DEF_RE = re.compile(
    r"""@(?:pytest\.|pytest_asyncio\.)?fixture\b"""
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


def _unwrap_fixture_type(type_str: str) -> str:
    """Unwrap container types (Iterator, Generator, Optional) to get concrete returned class."""
    if not type_str:
        return "?"
    type_str = type_str.strip()
    type_str = re.sub(r"^(?:typing|collections\.abc|asyncio)\.", "", type_str)
    m = re.match(r"^(?:Iterator|Generator|AsyncIterator|AsyncGenerator|Optional)\s*\[\s*([A-Za-z0-9_\.]+)", type_str)
    if m:
        inner = m.group(1).rsplit(".", 1)[-1]
        return inner if inner[:1].isupper() else "?"
    m2 = re.match(r"^[A-Za-z0-9_\.]+", type_str)
    if m2:
        cls_name = m2.group(0).rsplit(".", 1)[-1]
        return cls_name if cls_name[:1].isupper() else "?"
    return "?"


def _infer_fixture_class_from_body(fn_node: Any, source_bytes: bytes) -> str:
    """Infer single returned/yielded class from assignments and returns/yields in body."""
    local_instantiations: dict[str, str] = {}
    returns_and_yields: list[str] = []

    def walk(n: Any) -> None:
        if n.type == "assignment":
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if left and right and left.type == "identifier" and right.type == "call":
                fn_c = right.child_by_field_name("function")
                if fn_c and fn_c.type == "identifier":
                    var_name = source_bytes[left.start_byte:left.end_byte].decode("utf-8", errors="ignore")
                    cls_name = source_bytes[fn_c.start_byte:fn_c.end_byte].decode("utf-8", errors="ignore")
                    if cls_name[:1].isupper():
                        local_instantiations[var_name] = cls_name
        elif n.type in ("return_statement", "yield"):
            for c in n.children:
                if c.type == "call":
                    fn_c = c.child_by_field_name("function")
                    if fn_c and fn_c.type == "identifier":
                        cls_name = source_bytes[fn_c.start_byte:fn_c.end_byte].decode("utf-8", errors="ignore")
                        returns_and_yields.append(cls_name if cls_name[:1].isupper() else "?")
                    else:
                        returns_and_yields.append("?")
                elif c.type == "identifier":
                    ident = source_bytes[c.start_byte:c.end_byte].decode("utf-8", errors="ignore")
                    if ident in local_instantiations:
                        returns_and_yields.append(local_instantiations[ident])
                    else:
                        returns_and_yields.append("?")
        for child in n.children:
            walk(child)

    walk(fn_node)
    distinct = set(returns_and_yields) - {"?"}
    if len(distinct) == 1 and "?" not in returns_and_yields:
        return next(iter(distinct))
    return "?"


def _get_fixture_params(fn_def_node: Any, source_bytes: bytes) -> set[str]:
    """Plain unannotated, non-default parameters of a test or fixture not locally rebound."""
    params_node = fn_def_node.child_by_field_name("parameters")
    if not params_node:
        return set()
    candidates: set[str] = set()
    for p in params_node.children:
        if p.type == "identifier":
            name = source_bytes[p.start_byte:p.end_byte].decode("utf-8", errors="ignore")
            if name not in _TEST_PARAM_IGNORE:
                candidates.add(name)
    rebound: set[str] = set()

    def check_rebound(n: Any) -> None:
        if n.type == "assignment":
            left = n.child_by_field_name("left")
            if left and left.type == "identifier":
                rebound.add(source_bytes[left.start_byte:left.end_byte].decode("utf-8", errors="ignore"))
        for c in n.children:
            check_rebound(c)

    body = fn_def_node.child_by_field_name("body")
    if body:
        check_rebound(body)
    return candidates - rebound


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

    func_nodes: dict[str, dict[str, Any]] = {}
    for n in nodes:
        lbl = str(n.get("label", ""))
        func_nodes[lbl] = n
        if lbl.endswith("()"):
            func_nodes[lbl[:-2]] = n

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
        elif ast_node.type == "decorated_definition":
            is_fixture = False
            for c in ast_node.children:
                if c.type == "decorator":
                    deco_text = source_bytes[c.start_byte:c.end_byte].decode("utf-8", errors="ignore")
                    if _PYTEST_FIXTURE_DEF_RE.search(deco_text):
                        is_fixture = True
                        break
            if is_fixture:
                inner = ast_node.child_by_field_name("definition")
                if inner and inner.type in ("function_definition", "async_function_definition"):
                    name_node = inner.child_by_field_name("name")
                    if name_node:
                        fix_name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")
                        rt_node = inner.child_by_field_name("return_type")
                        if rt_node:
                            rt_text = source_bytes[rt_node.start_byte:rt_node.end_byte].decode("utf-8", errors="ignore")
                            cls_name = _unwrap_fixture_type(rt_text)
                        else:
                            cls_name = _infer_fixture_class_from_body(inner, source_bytes)
                        fn_node = func_nodes.get(fix_name)
                        if fn_node:
                            fn_node["_py_fixture"] = cls_name
                        result.setdefault("_py_fixtures", {})[fix_name] = {
                            "class": cls_name,
                            "nid": fn_node["id"] if fn_node else f"{file_nid}_{fix_name}",
                            "file": str_path,
                        }
                        fix_params = _get_fixture_params(inner, source_bytes)
                        if fix_params and fn_node:
                            for rc in result.get("raw_calls", []):
                                if rc.get("caller_nid") == fn_node["id"] and rc.get("is_member_call"):
                                    if rc.get("receiver") in fix_params:
                                        rc["_python_fixture_param"] = True
                                        rc["lang"] = "python"
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
                    fix_params = _get_fixture_params(ast_node, source_bytes)
                    if fix_params and fn_node:
                        for rc in result.get("raw_calls", []):
                            if rc.get("caller_nid") == fn_node["id"] and rc.get("is_member_call"):
                                if rc.get("receiver") in fix_params:
                                    rc["_python_fixture_param"] = True
                                    rc["lang"] = "python"

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

    func_nodes: dict[str, dict[str, Any]] = {}
    for n in nodes:
        lbl = str(n.get("label", ""))
        func_nodes[lbl] = n
        if lbl.endswith("()"):
            func_nodes[lbl[:-2]] = n

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

    # 2. Pytest Test Fixtures (Definitions & Calls)
    is_test_or_conftest = (
        path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or path.name == "conftest.py"
    )
    if is_test_or_conftest:
        for match in re.finditer(
            r'@(?:pytest\.|pytest_asyncio\.)?fixture\b[^\n]*\n(?:async\s+)?def\s+([A-Za-z0-9_]+)\s*\([^)]*\)(?:\s*->\s*([A-Za-z0-9_\[\], \.]+))?:',
            source_text,
        ):
            fix_name = match.group(1)
            rt_text = match.group(2)
            cls_name = _unwrap_fixture_type(rt_text) if rt_text else "?"
            if cls_name == "?":
                pos = match.end()
                body_snippet = source_text[pos:pos + 500]
                m_ret = re.search(r'\breturn\s+([A-Z][A-Za-z0-9_]*)\s*\(', body_snippet)
                if m_ret:
                    cls_name = m_ret.group(1)
            fn_node = func_nodes.get(fix_name)
            if fn_node:
                fn_node["_py_fixture"] = cls_name
            result.setdefault("_py_fixtures", {})[fix_name] = {
                "class": cls_name,
                "nid": fn_node["id"] if fn_node else f"{file_nid}_{fix_name}",
                "file": str_path,
            }

    for match in _PYTEST_TEST_FUNC_RE.finditer(source_text):
        fn_name = match.group(1)
        params_str = match.group(2)
        pos = match.start()
        line_num = source_text[:pos].count("\n") + 1

        fn_node = func_nodes.get(fn_name)
        src_id = fn_node["id"] if fn_node else file_nid
        fix_params = set()

        for raw_param in params_str.split(","):
            raw_stripped = raw_param.strip()
            if ":" in raw_stripped or "=" in raw_stripped:
                continue
            clean_param = raw_stripped
            if not clean_param or clean_param in _TEST_PARAM_IGNORE:
                continue
            fix_params.add(clean_param)
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

        if fix_params and fn_node:
            for rc in result.get("raw_calls", []):
                if rc.get("caller_nid") == fn_node["id"] and rc.get("is_member_call"):
                    if rc.get("receiver") in fix_params:
                        rc["_python_fixture_param"] = True
                        rc["lang"] = "python"


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
