"""CMake build graph extractor and target-scoped include registry.

Extracts CMake build graphs:
- Targets: add_executable, add_library
- Target sources: target_sources
- Target link libraries: target_link_libraries (depends_on edges with PUBLIC/PRIVATE/INTERFACE scoping)
- Target include directories: target_include_directories (with transitive inheritance across linked targets)
- Subdirectories: add_subdirectory
- External packages: find_package
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _file_stem, _make_id
from graph_fy.resolvers.common import _resolve_cached

# Global / per-session registry of CMake targets and include directories
# target_name -> dict containing {"sources": set[Path], "include_dirs": list[Path], "interface_include_dirs": list[Path], "deps": list[str]}
_CMAKE_TARGET_REGISTRY: dict[str, dict[str, Any]] = {}
# source_file_path_str -> target_name
_CMAKE_SOURCE_TO_TARGET: dict[str, str] = {}
# cmake_dir_path_str -> list of include directories
_CMAKE_DIR_INCLUDE_DIRS: dict[str, list[Path]] = {}


def is_cmake_file(path: Path) -> bool:
    """Return True if path is CMakeLists.txt or a .cmake file."""
    name = path.name.lower()
    if name == "cmakelists.txt":
        return True
    if path.suffix.lower() == ".cmake":
        return True
    return False


def clear_cmake_registry() -> None:
    """Clear in-memory CMake target and include directory registries."""
    _CMAKE_TARGET_REGISTRY.clear()
    _CMAKE_SOURCE_TO_TARGET.clear()
    _CMAKE_DIR_INCLUDE_DIRS.clear()


def register_cmake_target(
    target_name: str,
    *,
    source_files: list[Path],
    include_dirs: list[Path],
    interface_include_dirs: list[Path] | None = None,
    deps: list[str] | None = None,
) -> None:
    """Register a CMake target with its sources and include directories."""
    clean_target = target_name.strip()
    if not clean_target:
        return
    existing = _CMAKE_TARGET_REGISTRY.setdefault(clean_target, {
        "sources": set(),
        "include_dirs": [],
        "interface_include_dirs": [],
        "deps": [],
    })
    existing["sources"].update(source_files)
    for inc in include_dirs:
        if inc not in existing["include_dirs"]:
            existing["include_dirs"].append(inc)
    if interface_include_dirs:
        for inc in interface_include_dirs:
            if inc not in existing["interface_include_dirs"]:
                existing["interface_include_dirs"].append(inc)
    if deps:
        for dep in deps:
            if dep not in existing["deps"]:
                existing["deps"].append(dep)

    for sf in source_files:
        _CMAKE_SOURCE_TO_TARGET[str(sf.resolve())] = clean_target


def get_cmake_include_dirs(source_file: Path | str) -> list[Path]:
    """Return target-scoped include directories for a given C/C++ source file.

    Traverses target dependencies (target_link_libraries DAG) to transitively
    include PUBLIC and INTERFACE include directories from linked libraries.
    """
    p = Path(source_file).resolve()
    target_name = _CMAKE_SOURCE_TO_TARGET.get(str(p))
    include_dirs: list[Path] = []
    seen_dirs: set[Path] = set()

    def _add_dir(d: Path) -> None:
        if d not in seen_dirs and d.is_dir():
            seen_dirs.add(d)
            include_dirs.append(d)

    # 1. Target-scoped resolution via target_link_libraries DAG
    if target_name and target_name in _CMAKE_TARGET_REGISTRY:
        visited_targets: set[str] = set()
        queue = [target_name]
        is_root = True

        while queue:
            curr = queue.pop(0)
            if curr in visited_targets:
                continue
            visited_targets.add(curr)
            info = _CMAKE_TARGET_REGISTRY.get(curr)
            if not info:
                continue

            # Root target adds all its include dirs
            if is_root:
                for inc in info.get("include_dirs", []):
                    _add_dir(inc)
                for inc in info.get("interface_include_dirs", []):
                    _add_dir(inc)
                is_root = False
            else:
                # Transitive dependencies only contribute interface/public include dirs
                for inc in info.get("interface_include_dirs", []):
                    _add_dir(inc)

            # Traverse linked targets
            for dep in info.get("deps", []):
                if dep not in visited_targets:
                    queue.append(dep)

    # 2. Directory-level include directories (from CMakeLists.txt in parent hierarchy)
    curr_dir = p.parent
    while curr_dir != curr_dir.parent:
        dirs = _CMAKE_DIR_INCLUDE_DIRS.get(str(curr_dir))
        if dirs:
            for inc in dirs:
                _add_dir(inc)
        curr_dir = curr_dir.parent

    return include_dirs


# Regex to tokenize CMake commands and handle comments/quotes
_CMAKE_CMD_RE = re.compile(
    r"""
    (?P<cmd>[A-Za-z_][A-Za-z0-9_]*)  # Command name
    \s*\(                            # Open paren
    (?P<args>[^)]*)                  # Arguments
    \)                               # Close paren
    """,
    re.VERBOSE | re.DOTALL,
)

_CMAKE_ARG_TOKEN_RE = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"|(\S+)')


def _clean_cmake_arg(arg: str, base_dir: Path) -> str:
    """Clean variable expansions like ${CMAKE_CURRENT_SOURCE_DIR}/foo -> foo."""
    val = re.sub(r"\$\{[^}]*CMAKE_CURRENT_SOURCE_DIR[^}]*\}[\/\\]?", "", arg)
    val = re.sub(r"\$\{[^}]*\}[\/\\]?", "", val)
    return val.strip()


def parse_cmake_commands(content: str) -> list[tuple[str, list[str], int]]:
    """Parse CMake content into (command_name, [arguments], line_no) tuples."""
    results: list[tuple[str, list[str], int]] = []
    # Strip line comments (# ...) except inside strings
    # For robust parsing without stripping # inside quotes, line-by-line comment stripping
    clean_lines = []
    for line in content.splitlines(keepends=True):
        in_quote = False
        res = []
        for i, ch in enumerate(line):
            if ch == '"':
                in_quote = not in_quote
            elif ch == '#' and not in_quote:
                break
            res.append(ch)
        clean_lines.append("".join(res))
    clean_content = "".join(clean_lines)

    for match in _CMAKE_CMD_RE.finditer(clean_content):
        cmd = match.group("cmd").lower()
        args_str = match.group("args")
        line = content.count("\n", 0, match.start()) + 1

        tokens: list[str] = []
        for m in _CMAKE_ARG_TOKEN_RE.finditer(args_str):
            quoted = m.group(1)
            unquoted = m.group(2)
            if quoted is not None:
                tokens.append(quoted.replace('\\"', '"'))
            elif unquoted is not None:
                tokens.append(unquoted)
        results.append((cmd, tokens, line))
    return results


def extract_cmake(path: Path) -> dict[str, Any]:
    """Extract CMakeLists.txt or *.cmake file into build_target nodes and dependency edges."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)
    base_dir = path.parent.resolve()

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": f"CMake:{path.name}",
        "type": "cmake_file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": f"CMake file {path.name}",
    }]
    edges: list[dict[str, Any]] = []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    commands = parse_cmake_commands(content)

    # Local target tracking for this file
    # target_name -> dict
    file_targets: dict[str, dict[str, Any]] = {}

    def _ensure_target(name: str, subtype: str, line: int) -> dict[str, Any]:
        if name not in file_targets:
            target_nid = _make_id("cmake_target", name)
            node = {
                "id": target_nid,
                "label": f"CMake:Target:{name}",
                "type": "build_target",
                "subtype": subtype,
                "file_type": "code",
                "target_name": name,
                "sources": [],
                "deps": [],
                "include_dirs": [],
                "interface_include_dirs": [],
                "source_file": str_path,
                "source_location": f"L{line}",
                "text": f"CMake build target {name} ({subtype})",
            }
            nodes.append(node)
            edges.append({
                "source": file_nid,
                "target": target_nid,
                "relation": "declares_target",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
            })
            file_targets[name] = {
                "node": node,
                "sources": [],
                "deps": [],
                "include_dirs": [],
                "interface_include_dirs": [],
            }
        return file_targets[name]

    _EXCLUDE_SOURCE_TOKENS = frozenset({
        "win32", "macosx_bundle", "exclude_from_all", "alias", "imported",
        "static", "shared", "module", "interface", "object",
        "private", "public", "system", "before",
    })

    dir_includes: list[Path] = []

    for cmd, args, line in commands:
        if not args:
            continue
        loc = f"L{line}"

        # 1. add_executable(name [WIN32] [MACOSX_BUNDLE] [EXCLUDE_FROM_ALL] src1 src2...)
        if cmd == "add_executable":
            target_name = args[0]
            t = _ensure_target(target_name, "cmake_executable", line)
            for a in args[1:]:
                clean = _clean_cmake_arg(a, base_dir)
                if clean.lower() not in _EXCLUDE_SOURCE_TOKENS and clean:
                    t["sources"].append(clean)

        # 2. add_library(name [STATIC|SHARED|MODULE|INTERFACE|OBJECT] src1 src2...)
        elif cmd == "add_library":
            target_name = args[0]
            t = _ensure_target(target_name, "cmake_library", line)
            for a in args[1:]:
                clean = _clean_cmake_arg(a, base_dir)
                if clean.lower() not in _EXCLUDE_SOURCE_TOKENS and clean:
                    t["sources"].append(clean)

        # 3. target_sources(name [PRIVATE|PUBLIC|INTERFACE] src1 src2...)
        elif cmd == "target_sources":
            target_name = args[0]
            t = _ensure_target(target_name, "cmake_library", line)
            for a in args[1:]:
                clean = _clean_cmake_arg(a, base_dir)
                if clean.lower() not in _EXCLUDE_SOURCE_TOKENS and clean:
                    t["sources"].append(clean)

        # 4. target_link_libraries(name [PRIVATE|PUBLIC|INTERFACE] dep1 dep2...)
        elif cmd == "target_link_libraries":
            target_name = args[0]
            t = _ensure_target(target_name, "cmake_library", line)
            for a in args[1:]:
                clean = _clean_cmake_arg(a, base_dir)
                if clean.lower() not in _EXCLUDE_SOURCE_TOKENS and clean:
                    t["deps"].append(clean)

        # 5. target_include_directories(name [SYSTEM] [BEFORE] [PRIVATE|PUBLIC|INTERFACE] dir1 dir2...)
        elif cmd == "target_include_directories":
            target_name = args[0]
            t = _ensure_target(target_name, "cmake_library", line)
            visibility = "PUBLIC"
            for a in args[1:]:
                low = a.lower()
                if low in ("private", "public", "interface"):
                    visibility = a.upper()
                    continue
                if low in ("system", "before"):
                    continue
                clean = _clean_cmake_arg(a, base_dir)
                if clean:
                    dir_path = (base_dir / clean).resolve() if not Path(clean).is_absolute() else Path(clean).resolve()
                    if visibility in ("PUBLIC", "INTERFACE"):
                        t["interface_include_dirs"].append(dir_path)
                    if visibility in ("PUBLIC", "PRIVATE"):
                        t["include_dirs"].append(dir_path)

        # 6. include_directories([AFTER|BEFORE] [SYSTEM] dir1 dir2...)
        elif cmd == "include_directories":
            for a in args:
                if a.lower() in ("after", "before", "system"):
                    continue
                clean = _clean_cmake_arg(a, base_dir)
                if clean:
                    dir_path = (base_dir / clean).resolve() if not Path(clean).is_absolute() else Path(clean).resolve()
                    dir_includes.append(dir_path)

        # 7. add_subdirectory(dir_name)
        elif cmd == "add_subdirectory":
            sub_dir_name = args[0]
            sub_path = (base_dir / sub_dir_name / "CMakeLists.txt").resolve()
            sub_nid = _make_id(str(sub_path))
            edges.append({
                "source": file_nid,
                "target": sub_nid,
                "relation": "includes",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": loc,
            })

        # 8. find_package(pkg_name ...)
        elif cmd == "find_package":
            pkg_name = args[0]
            pkg_nid = _make_id("package", "cmake", pkg_name)
            nodes.append({
                "id": pkg_nid,
                "label": f"Package:CMake:{pkg_name}",
                "type": "package",
                "file_type": "code",
                "package_name": pkg_name,
                "ecosystem": "cmake",
                "source_file": str_path,
                "source_location": loc,
                "text": f"CMake external package {pkg_name}",
            })
            edges.append({
                "source": file_nid,
                "target": pkg_nid,
                "relation": "depends_on",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": loc,
            })

    # Register directory-level include dirs
    if dir_includes:
        _CMAKE_DIR_INCLUDE_DIRS[str(base_dir)] = dir_includes

    # Process targets, emit edges, and update global registry
    for name, data in file_targets.items():
        node = data["node"]
        target_nid = node["id"]
        sources = data["sources"]
        deps = data["deps"]
        inc_dirs = data["include_dirs"]
        interface_inc_dirs = data["interface_include_dirs"]

        node["sources"] = sources
        node["deps"] = deps
        node["include_dirs"] = [str(d) for d in inc_dirs]

        resolved_source_paths: list[Path] = []
        for src in sources:
            src_path = (base_dir / src).resolve() if not Path(src).is_absolute() else Path(src).resolve()
            resolved_source_paths.append(src_path)
            src_nid = _make_id(str(src_path))
            edges.append({
                "source": target_nid,
                "target": src_nid,
                "relation": "compiles",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": node["source_location"],
            })

        for dep in deps:
            dep_nid = _make_id("cmake_target", dep)
            edges.append({
                "source": target_nid,
                "target": dep_nid,
                "relation": "depends_on",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": node["source_location"],
            })

        # Register in global registry for C/C++ target-scoped include resolution
        register_cmake_target(
            name,
            source_files=resolved_source_paths,
            include_dirs=inc_dirs,
            interface_include_dirs=interface_inc_dirs,
            deps=deps,
        )

    return {"nodes": nodes, "edges": edges}
