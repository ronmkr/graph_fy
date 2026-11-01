"""Python import and symbol resolution fact collector."""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from graph_fy.resolvers.common import _file_stem, _make_id, _read_text, _resolve_cached
from graph_fy.resolvers.models import (
    _SymbolExportFact,
    _SymbolImportFact,
    _SymbolResolutionFacts,
    _SymbolUseFact,
)

_PYTHON_WORKSPACE_ROOTS_CACHE: dict[str, list[Path]] = {}


@functools.lru_cache(maxsize=2048)
def _parse_python_tree_cached(path_str: str, _mtime_ns: int, _size: int):
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser
    source = Path(path_str).read_bytes()
    parser = Parser(Language(tspython.language()))
    return source, parser.parse(source).root_node


def _parse_python_tree(path: Path):
    """Parse one Python file to (source, root_node), memoized."""
    try:
        st = path.stat()
        return _parse_python_tree_cached(str(path), st.st_mtime_ns, st.st_size)
    except Exception:
        return None


def _walk_python_tree(node):
    """Preorder walk of a tree-sitter tree, iteratively."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _python_import_from_module(node, source: bytes) -> tuple[int, str] | None:
    level = 0
    module_name = ""
    for child in node.children:
        if child.type == "import":
            break
        if child.type == "relative_import":
            raw = _read_text(child, source)
            level = len(raw) - len(raw.lstrip("."))
            remainder = raw.lstrip(".")
            if remainder:
                module_name = remainder
            for sub in child.children:
                if sub.type == "dotted_name":
                    module_name = _read_text(sub, source)
        elif child.type == "dotted_name":
            module_name = _read_text(child, source)
    if level == 0 and not module_name:
        return None
    return level, module_name


def _python_imported_names(node, source: bytes) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    past_import = False
    for child in node.children:
        if child.type == "import":
            past_import = True
            continue
        if not past_import:
            continue
        if child.type == "dotted_name":
            name = _read_text(child, source)
            names.append((name, name.split(".")[-1]))
        elif child.type == "aliased_import":
            name_node = child.child_by_field_name("name")
            alias_node = child.child_by_field_name("alias")
            if name_node is None:
                continue
            name = _read_text(name_node, source)
            local = _read_text(alias_node, source) if alias_node is not None else name.split(".")[-1]
            names.append((name, local))
    return names


def _probe_python_module_candidate(candidate: Path) -> Path | None:
    """Resolve module candidate to a .py file (dir+__init__, exact, or .py suffix)."""
    if candidate.is_dir():
        init_path = candidate / "__init__.py"
        if init_path.is_file():
            return init_path
    if candidate.is_file():
        return candidate
    if not candidate.name:
        return None
    py_candidate = candidate.with_suffix(".py")
    if py_candidate.is_file():
        return py_candidate
    return None


def _python_workspace_package_roots(root: Path) -> list[Path]:
    key = str(root)
    if key in _PYTHON_WORKSPACE_ROOTS_CACHE:
        return _PYTHON_WORKSPACE_ROOTS_CACHE[key]
    roots = []
    try:
        for child in root.iterdir():
            if child.is_dir() and not child.name.startswith((".", "_")) and child.name not in ("node_modules", "dist", "build", "venv", ".venv", "graph_fy_out"):
                roots.append(child)
                src = child / "src"
                if src.is_dir():
                    roots.append(src)
    except Exception:
        pass
    _PYTHON_WORKSPACE_ROOTS_CACHE[key] = roots
    return roots


_PYTHON_STDLIB_MODULE_NAMES = frozenset({
    "abc", "aifc", "annotationlib", "antigravity", "argparse", "array", "ast", "asynchat",
    "asyncio", "asyncore", "atexit", "audioop", "base64", "bdb", "binascii", "bisect",
    "builtins", "bz2", "cProfile", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd",
    "code", "codecs", "codeop", "collections", "colorsys", "compileall", "compression",
    "concurrent", "configparser", "contextlib", "contextvars", "copy", "copyreg", "crypt",
    "csv", "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal", "difflib",
    "dis", "doctest", "email", "encodings", "ensurepip", "enum", "errno", "faulthandler",
    "fcntl", "filecmp", "fileinput", "fnmatch", "fractions", "ftplib", "functools", "gc",
    "genericpath", "getopt", "getpass", "gettext", "glob", "graphlib", "grp", "gzip",
    "hashlib", "heapq", "hmac", "html", "http", "idlelib", "imaplib", "imghdr", "imp",
    "importlib", "inspect", "io", "ipaddress", "itertools", "json", "keyword", "lib2to3",
    "linecache", "locale", "logging", "lzma", "mailbox", "mailcap", "marshal", "math",
    "mimetypes", "mmap", "modulefinder", "msilib", "msvcrt", "multiprocessing", "netrc",
    "nis", "nntplib", "nt", "ntpath", "nturl2path", "numbers", "opcode", "operator",
    "optparse", "os", "ossaudiodev", "pathlib", "pdb", "pickle", "pickletools", "pipes",
    "pkgutil", "platform", "plistlib", "poplib", "posix", "posixpath", "pprint", "profile",
    "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc", "pydoc_data", "pyexpat",
    "queue", "quopri", "random", "re", "readline", "reprlib", "resource", "rlcompleter",
    "runpy", "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil",
    "signal", "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "spwd",
    "sqlite3", "sre_compile", "sre_constants", "sre_parse", "ssl", "stat", "statistics",
    "string", "stringprep", "struct", "subprocess", "sunau", "symtable", "sys", "sysconfig",
    "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile", "termios", "textwrap", "this",
    "threading", "time", "timeit", "tkinter", "token", "tokenize", "tomllib", "trace",
    "traceback", "tracemalloc", "tty", "turtle", "turtledemo", "types", "typing",
    "unicodedata", "unittest", "urllib", "uu", "uuid", "venv", "warnings", "wave",
    "weakref", "webbrowser", "winreg", "winsound", "wsgiref", "xdrlib", "xml", "xmlrpc",
    "zipapp", "zipfile", "zipimport", "zlib", "zoneinfo",
})


def _resolve_python_module_path(module_name: str, current_path: Path, root: Path, level: int) -> Path | None:
    if level > 0:
        base = current_path.parent
        for _ in range(level - 1):
            base = base.parent
        candidate = base / module_name.replace(".", "/") if module_name else base
        return _probe_python_module_candidate(candidate)

    rel = module_name.replace(".", "/")
    stdlib_shadow = module_name.split(".", 1)[0] in _PYTHON_STDLIB_MODULE_NAMES

    def _accept(candidate: Path | None) -> Path | None:
        if (
            candidate is not None
            and stdlib_shadow
            and candidate.name not in ("__init__.py", "__init__.pyi")
        ):
            return None
        return candidate

    hit = _accept(_probe_python_module_candidate(root / rel))
    if hit is not None:
        return hit
    for anc in current_path.parents:
        try:
            anc.relative_to(root)
        except ValueError:
            break
        if anc == root:
            continue
        if (anc / "__init__.py").is_file():
            continue
        cand = _probe_python_module_candidate(anc / rel)
        if cand is not None:
            return cand

    for sub in _python_workspace_package_roots(root):
        cand = _probe_python_module_candidate(sub / rel)
        if cand is not None:
            return cand
    return None


def _resolve_python_namespace_dir(module_name: str, current_path: Path, root: Path, level: int) -> "Path | None":
    """Resolve directory named by import when PEP 420 namespace package (no __init__.py)."""
    def _namespace(candidate: Path) -> "Path | None":
        if not candidate.is_dir() or (candidate / "__init__.py").is_file():
            return None
        try:
            _resolve_cached(candidate).relative_to(_resolve_cached(root))
        except ValueError:
            return None
        return candidate

    if level > 0:
        base = current_path.parent
        for _ in range(level - 1):
            base = base.parent
        return _namespace(base / module_name.replace(".", "/") if module_name else base)
    if not module_name:
        return None
    rel = module_name.replace(".", "/")
    hit = _namespace(root / rel)
    if hit is not None:
        return hit
    for anc in current_path.parents:
        try:
            anc.relative_to(root)
        except ValueError:
            break
        if anc == root or (anc / "__init__.py").is_file():
            continue
        hit = _namespace(anc / rel)
        if hit is not None:
            return hit
    return None


def _python_top_level_function_bodies(path: Path, root_node, source: bytes) -> list[tuple[str, object]]:
    bodies: list[tuple[str, object]] = []
    stem = _file_stem(path)
    for node in root_node.children:
        if node.type != "function_definition":
            continue
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is not None and body is not None:
            bodies.append((_make_id(stem, _read_text(name_node, source)), body))
    return bodies


def _python_call_identifier(node, source: bytes) -> str | None:
    if node.type != "call":
        return None
    function_node = node.child_by_field_name("function")
    if function_node is not None and function_node.type == "identifier":
        return _read_text(function_node, source)
    return None


def _collect_python_symbol_resolution_facts(
    paths: list[Path],
    root: Path,
    facts: _SymbolResolutionFacts,
) -> None:
    py_paths = [path for path in paths if path.suffix == ".py"]
    if not py_paths:
        return

    trees: dict[Path, tuple[bytes, object]] = {}
    for path in py_paths:
        parsed = _parse_python_tree(path)
        if parsed is None:
            continue
        source, root_node = parsed
        trees[_resolve_cached(path)] = parsed

        for node in _walk_python_tree(root_node):
            if node.type != "import_from_statement":
                continue
            module = _python_import_from_module(node, source)
            if module is None:
                continue
            level, module_name = module
            target_path = _resolve_python_module_path(module_name, path, root, level)
            if target_path is not None:
                pkg_dir = target_path.parent if target_path.name == "__init__.py" else None
            else:
                pkg_dir = _resolve_python_namespace_dir(module_name, path, root, level)
                if pkg_dir is None:
                    continue
            for imported_name, local_name in _python_imported_names(node, source):
                line = node.start_point[0] + 1
                if pkg_dir is not None:
                    sub_py = pkg_dir / f"{imported_name}.py"
                    sub_pkg = pkg_dir / imported_name / "__init__.py"
                    submodule = sub_py if sub_py.is_file() else (sub_pkg if sub_pkg.is_file() else None)
                    if submodule is not None:
                        facts.module_imports.append((path, submodule, line, local_name))
                        continue
                if target_path is None:
                    continue
                facts.imports.append(
                    _SymbolImportFact(path, local_name, target_path, imported_name, line)
                )
                if path.name == "__init__.py":
                    facts.exports.append(
                        _SymbolExportFact(
                            path,
                            local_name,
                            line,
                            target_path=target_path,
                            target_name=imported_name,
                        )
                    )

    for path in py_paths:
        parsed = trees.get(_resolve_cached(path))
        if parsed is None:
            continue
        source, root_node = parsed
        for source_id, body in _python_top_level_function_bodies(path, root_node, source):
            for node in _walk_python_tree(body):
                imported_name = _python_call_identifier(node, source)
                if imported_name is None:
                    continue
                facts.uses.append(
                    _SymbolUseFact(
                        path,
                        source_id,
                        imported_name,
                        "calls",
                        "call",
                        node.start_point[0] + 1,
                    )
                )
