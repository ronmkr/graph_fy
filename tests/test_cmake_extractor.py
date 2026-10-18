"""Unit tests for CMake build graph extractor and target-scoped include resolution."""
from __future__ import annotations

import tempfile
from pathlib import Path

from graph_fy.extract import _get_extractor
from graph_fy.extractors.cmake import (
    clear_cmake_registry,
    extract_cmake,
    get_cmake_include_dirs,
    is_cmake_file,
    parse_cmake_commands,
)
from graph_fy.resolvers.c_cpp import _resolve_c_include_path


def test_is_cmake_file():
    assert is_cmake_file(Path("CMakeLists.txt"))
    assert is_cmake_file(Path("cmakelists.txt"))
    assert is_cmake_file(Path("cmake/FindFoo.cmake"))
    assert is_cmake_file(Path("modules/Engine.cmake"))
    assert not is_cmake_file(Path("Makefile"))
    assert not is_cmake_file(Path("CMakeLists.txt.bak"))
    assert not is_cmake_file(Path("main.cpp"))


def test_parse_cmake_commands():
    content = """
    # CMake configuration file
    project(MyEngine VERSION 1.0)
    
    add_library(core STATIC
        src/core.cpp
        "src/utils with space.cpp"
    )
    
    target_link_libraries(core
        PUBLIC
        Threads::Threads
    )
    """
    cmds = parse_cmake_commands(content)
    assert len(cmds) == 3
    assert cmds[0][0] == "project"
    assert cmds[0][1] == ["MyEngine", "VERSION", "1.0"]
    assert cmds[1][0] == "add_library"
    assert "core" in cmds[1][1]
    assert "src/core.cpp" in cmds[1][1]
    assert "src/utils with space.cpp" in cmds[1][1]
    assert cmds[2][0] == "target_link_libraries"


def test_extract_cmake_and_target_scoping(tmp_path: Path):
    clear_cmake_registry()

    # Create directory structure:
    # root/
    #   CMakeLists.txt
    #   main.cpp
    #   core/
    #     include/
    #       core/
    #         engine.h
    #     src/
    #       core.cpp
    include_dir = tmp_path / "core" / "include" / "core"
    include_dir.mkdir(parents=True)
    header_file = include_dir / "engine.h"
    header_file.write_text("#pragma once\nclass Engine {};\n")

    main_cpp = tmp_path / "main.cpp"
    main_cpp.write_text('#include "core/engine.h"\nint main() { return 0; }\n')

    core_cpp = tmp_path / "core" / "src" / "core.cpp"
    core_cpp.parent.mkdir(parents=True, exist_ok=True)
    core_cpp.write_text('#include "core/engine.h"\n')

    cmake_file = tmp_path / "CMakeLists.txt"
    cmake_file.write_text("""
    cmake_minimum_required(VERSION 3.15)
    project(SampleApp)

    find_package(OpenSSL REQUIRED)

    add_library(core STATIC core/src/core.cpp)
    target_include_directories(core PUBLIC core/include)

    add_executable(app main.cpp)
    target_link_libraries(app PRIVATE core)

    add_subdirectory(plugins)
    """)

    # Verify extractor dispatch
    assert _get_extractor(cmake_file) == extract_cmake

    res = extract_cmake(cmake_file)
    nodes = {n["id"]: n for n in res["nodes"]}
    edges = res["edges"]

    # Target nodes
    target_core = next((n for n in nodes.values() if n.get("target_name") == "core"), None)
    target_app = next((n for n in nodes.values() if n.get("target_name") == "app"), None)
    assert target_core is not None
    assert target_core["type"] == "build_target"
    assert target_core["subtype"] == "cmake_library"

    assert target_app is not None
    assert target_app["type"] == "build_target"
    assert target_app["subtype"] == "cmake_executable"

    # Declares target edges
    declares_edges = [e for e in edges if e["relation"] == "declares_target"]
    assert len(declares_edges) == 2

    # Compiles edges
    compiles_edges = [e for e in edges if e["relation"] == "compiles"]
    assert len(compiles_edges) >= 2

    # Depends_on edge between app and core
    dep_edge = next((e for e in edges if e["relation"] == "depends_on" and e["source"] == target_app["id"]), None)
    assert dep_edge is not None
    assert dep_edge["target"] == target_core["id"]

    # External package dependency
    pkg_node = next((n for n in nodes.values() if n.get("package_name") == "OpenSSL"), None)
    assert pkg_node is not None
    assert pkg_node["type"] == "package"
    assert pkg_node["ecosystem"] == "cmake"

    # Transitive include directory scoping verification
    # main.cpp belongs to target "app", which links "core" (with PUBLIC include dir "core/include")
    app_inc_dirs = get_cmake_include_dirs(main_cpp)
    assert (tmp_path / "core" / "include").resolve() in [d.resolve() for d in app_inc_dirs]

    # Verify that _resolve_c_include_path successfully resolves "core/engine.h" from main.cpp
    resolved = _resolve_c_include_path("core/engine.h", str(main_cpp))
    assert resolved is not None
    assert resolved.resolve() == header_file.resolve()
