"""Gradle multi-project build extractor (Groovy and Kotlin DSL).

Extracts:
- Settings: root project and subprojects (include ':app', ':core')
- Dependencies: inter-project subproject dependencies (project(':core')) and external packages
- Plugins: plugins { id '...' } and apply plugin: '...'
- Tasks: task declarations (tasks.register('...'), task foo)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graph_fy.extractors.base import _file_stem, _make_id


def is_gradle_file(path: Path) -> bool:
    """Return True if path is a Gradle build or settings file."""
    name = path.name.lower()
    if name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
        return True
    if path.suffix.lower() in (".gradle", ".kts") and "gradle" in name:
        return True
    return False


def _clean_subproject_name(raw: str) -> str:
    """Normalize Gradle subproject identifier e.g. ':subproject:lib' -> 'subproject:lib'."""
    clean = raw.strip().strip("'\"").strip()
    return clean.lstrip(":")


# Settings patterns
_ROOT_PROJECT_RE = re.compile(r"""rootProject\.name\s*=\s*['"]([^'"]+)['"]""")
_INCLUDE_PROJECT_RE = re.compile(r"""include\s*\(?([^)\r\n]+)\)?""")
_QUOTED_ITEM_RE = re.compile(r"""['"]([^'"]+)['"]""")

# Dependencies patterns
_PROJECT_DEP_RE = re.compile(
    r"""(?:\b(?:implementation|api|testImplementation|compileOnly|runtimeOnly|classpath)\s*(?:\(\s*)?)?project\s*\(\s*(?:path\s*[:=]\s*)?['"]([^'"]+)['"]\s*\)"""
)
_EXTERNAL_DEP_STRING_RE = re.compile(
    r"""\b(?:implementation|api|testImplementation|compileOnly|runtimeOnly|classpath)\s*\(?\s*['"]([A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?)['"]"""
)
_EXTERNAL_DEP_MAP_RE = re.compile(
    r"""group\s*[:=]\s*['"]([^'"]+)['"]\s*,\s*name\s*[:=]\s*['"]([^'"]+)['"]"""
)

# Plugins patterns
_PLUGIN_ID_RE = re.compile(r"""\bid\s*\(?\s*['"]([^'"]+)['"]\s*\)?""")
_APPLY_PLUGIN_RE = re.compile(r"""apply\s+plugin\s*[:=]\s*['"]([^'"]+)['"]""")

# Tasks patterns
_TASK_REGISTER_RE = re.compile(r"""tasks\.register\s*(?:<[^>]+>)?\s*\(\s*['"]([^'"]+)['"]""")
_TASK_DEF_RE = re.compile(r"""\btask\s+(?:(?:\([^\)]*\)\s+)?)([A-Za-z_][A-Za-z0-9_]*)\b(?:\s*\{|\s*\()""")


def extract_gradle(path: Path) -> dict[str, Any]:
    """Extract Gradle build or settings file into project, package, and task nodes with dependency edges."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)
    file_stem = _file_stem(path)
    file_name = path.name.lower()
    is_settings = "settings" in file_name

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": f"Gradle:{path.name}",
        "type": "gradle_file",
        "file_type": "code",
        "source_file": str_path,
        "source_location": "L1",
        "text": f"Gradle file {path.name}",
    }]
    edges: list[dict[str, Any]] = []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    # Strip single-line comments for reliable regex matching
    clean_lines = []
    for line in content.splitlines():
        idx = line.find("//")
        if idx != -1:
            clean_lines.append(line[:idx])
        else:
            clean_lines.append(line)
    clean_content = "\n".join(clean_lines)

    # 1. Settings file: root project and subprojects
    if is_settings:
        m_root = _ROOT_PROJECT_RE.search(clean_content)
        if m_root:
            root_name = m_root.group(1).strip()
            nodes[0]["root_project"] = root_name
            nodes[0]["label"] = f"Gradle:Settings:{root_name}"

        # Subprojects declared via include ':sub1', ':sub2'
        seen_subprojects: set[str] = set()
        for m in _INCLUDE_PROJECT_RE.finditer(clean_content):
            args_str = m.group(1)
            line = content.count("\n", 0, m.start()) + 1
            loc = f"L{line}"
            for item_match in _QUOTED_ITEM_RE.finditer(args_str):
                raw_name = item_match.group(1)
                sub_name = _clean_subproject_name(raw_name)
                if not sub_name or sub_name in seen_subprojects:
                    continue
                seen_subprojects.add(sub_name)

                canonical_sub_id = sub_name.replace(":", "_")
                sub_nid = _make_id("gradle_subproject", canonical_sub_id)
                nodes.append({
                    "id": sub_nid,
                    "label": f"Gradle:Subproject:{sub_name}",
                    "type": "build_target",
                    "subtype": "gradle_subproject",
                    "file_type": "code",
                    "subproject_name": sub_name,
                    "source_file": str_path,
                    "source_location": loc,
                    "text": f"Gradle subproject {sub_name}",
                })
                edges.append({
                    "source": file_nid,
                    "target": sub_nid,
                    "relation": "declares_target",
                    "confidence": "EXTRACTED",
                    "weight": 1.0,
                    "source_file": str_path,
                    "source_location": loc,
                })
        return {"nodes": nodes, "edges": edges}

    # 2. Build file: Plugins
    seen_plugins: set[str] = set()
    for m in _PLUGIN_ID_RE.finditer(clean_content):
        plugin_id = m.group(1).strip()
        if plugin_id and plugin_id not in seen_plugins:
            seen_plugins.add(plugin_id)
            line = content.count("\n", 0, m.start()) + 1
            p_nid = _make_id("gradle_plugin", plugin_id)
            nodes.append({
                "id": p_nid,
                "label": f"Plugin:{plugin_id}",
                "type": "gradle_plugin",
                "file_type": "code",
                "plugin_id": plugin_id,
                "source_file": str_path,
                "source_location": f"L{line}",
                "text": f"Gradle plugin {plugin_id}",
            })
            edges.append({
                "source": file_nid,
                "target": p_nid,
                "relation": "uses",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    for m in _APPLY_PLUGIN_RE.finditer(clean_content):
        plugin_id = m.group(1).strip()
        if plugin_id and plugin_id not in seen_plugins:
            seen_plugins.add(plugin_id)
            line = content.count("\n", 0, m.start()) + 1
            p_nid = _make_id("gradle_plugin", plugin_id)
            nodes.append({
                "id": p_nid,
                "label": f"Plugin:{plugin_id}",
                "type": "gradle_plugin",
                "file_type": "code",
                "plugin_id": plugin_id,
                "source_file": str_path,
                "source_location": f"L{line}",
                "text": f"Gradle plugin {plugin_id}",
            })
            edges.append({
                "source": file_nid,
                "target": p_nid,
                "relation": "uses",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    # 3. Inter-project subproject dependencies: project(':subproject')
    seen_deps: set[str] = set()
    for m in _PROJECT_DEP_RE.finditer(clean_content):
        raw_dep = m.group(1)
        sub_name = _clean_subproject_name(raw_dep)
        if sub_name and sub_name not in seen_deps:
            seen_deps.add(sub_name)
            canonical_sub_id = sub_name.replace(":", "_")
            target_sub_nid = _make_id("gradle_subproject", canonical_sub_id)
            line = content.count("\n", 0, m.start()) + 1
            edges.append({
                "source": file_nid,
                "target": target_sub_nid,
                "relation": "depends_on",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    # 4. External package dependencies
    seen_packages: set[str] = set()
    for m in _EXTERNAL_DEP_STRING_RE.finditer(clean_content):
        coord = m.group(1).strip()
        # Ensure it's not a relative path or file
        if ":" in coord and coord not in seen_packages:
            seen_packages.add(coord)
            pkg_name = coord.split(":")[0] + ":" + coord.split(":")[1]
            line = content.count("\n", 0, m.start()) + 1
            pkg_nid = _make_id("package", "gradle", pkg_name)
            nodes.append({
                "id": pkg_nid,
                "label": f"Package:{pkg_name}",
                "type": "package",
                "file_type": "code",
                "ecosystem": "gradle",
                "coordinates": coord,
                "source_file": str_path,
                "source_location": f"L{line}",
                "text": f"Gradle package dependency {coord}",
            })
            edges.append({
                "source": file_nid,
                "target": pkg_nid,
                "relation": "depends_on",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    for m in _EXTERNAL_DEP_MAP_RE.finditer(clean_content):
        group = m.group(1).strip()
        name = m.group(2).strip()
        pkg_name = f"{group}:{name}"
        if pkg_name not in seen_packages:
            seen_packages.add(pkg_name)
            line = content.count("\n", 0, m.start()) + 1
            pkg_nid = _make_id("package", "gradle", pkg_name)
            nodes.append({
                "id": pkg_nid,
                "label": f"Package:{pkg_name}",
                "type": "package",
                "file_type": "code",
                "ecosystem": "gradle",
                "coordinates": pkg_name,
                "source_file": str_path,
                "source_location": f"L{line}",
                "text": f"Gradle package dependency {pkg_name}",
            })
            edges.append({
                "source": file_nid,
                "target": pkg_nid,
                "relation": "depends_on",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    # 5. Tasks
    seen_tasks: set[str] = set()
    for m in _TASK_REGISTER_RE.finditer(clean_content):
        task_name = m.group(1).strip()
        if task_name and task_name not in seen_tasks:
            seen_tasks.add(task_name)
            line = content.count("\n", 0, m.start()) + 1
            task_nid = _make_id(file_stem, "task", task_name)
            nodes.append({
                "id": task_nid,
                "label": f"Gradle:Task:{task_name}",
                "type": "build_target",
                "subtype": "gradle_task",
                "file_type": "code",
                "task_name": task_name,
                "source_file": str_path,
                "source_location": f"L{line}",
                "text": f"Gradle task {task_name}",
            })
            edges.append({
                "source": file_nid,
                "target": task_nid,
                "relation": "declares_target",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    for m in _TASK_DEF_RE.finditer(clean_content):
        task_name = m.group(1).strip()
        if task_name and task_name not in seen_tasks and task_name not in ("register", "named"):
            seen_tasks.add(task_name)
            line = content.count("\n", 0, m.start()) + 1
            task_nid = _make_id(file_stem, "task", task_name)
            nodes.append({
                "id": task_nid,
                "label": f"Gradle:Task:{task_name}",
                "type": "build_target",
                "subtype": "gradle_task",
                "file_type": "code",
                "task_name": task_name,
                "source_file": str_path,
                "source_location": f"L{line}",
                "text": f"Gradle task {task_name}",
            })
            edges.append({
                "source": file_nid,
                "target": task_nid,
                "relation": "declares_target",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    return {"nodes": nodes, "edges": edges}
