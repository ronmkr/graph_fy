"""Unit tests for Gradle multi-project build extractor (Groovy and Kotlin DSL)."""
from __future__ import annotations

from pathlib import Path

from graph_fy.extract import _get_extractor
from graph_fy.extractors.gradle import extract_gradle, is_gradle_file


def test_is_gradle_file():
    assert is_gradle_file(Path("build.gradle"))
    assert is_gradle_file(Path("build.gradle.kts"))
    assert is_gradle_file(Path("settings.gradle"))
    assert is_gradle_file(Path("settings.gradle.kts"))
    assert is_gradle_file(Path("subproject/build.gradle"))
    assert not is_gradle_file(Path("gradlew"))
    assert not is_gradle_file(Path("pom.xml"))
    assert not is_gradle_file(Path("package.json"))


def test_extract_gradle_settings(tmp_path: Path):
    settings_file = tmp_path / "settings.gradle"
    settings_file.write_text("""
    rootProject.name = 'my-super-service'

    include ':app', ':core', ':services:auth'
    include ':common:utils'
    """)

    assert _get_extractor(settings_file) == extract_gradle

    res = extract_gradle(settings_file)
    nodes = {n["id"]: n for n in res["nodes"]}
    edges = res["edges"]

    file_node = next((n for n in nodes.values() if n.get("type") == "gradle_file"), None)
    assert file_node is not None
    assert file_node.get("root_project") == "my-super-service"

    subprojects = [n for n in nodes.values() if n.get("subtype") == "gradle_subproject"]
    subproject_names = {s["subproject_name"] for s in subprojects}
    assert subproject_names == {"app", "core", "services:auth", "common:utils"}

    declares_edges = [e for e in edges if e["relation"] == "declares_target"]
    assert len(declares_edges) == 4


def test_extract_gradle_build_groovy(tmp_path: Path):
    build_file = tmp_path / "build.gradle"
    build_file.write_text("""
    plugins {
        id 'java'
        id 'org.springframework.boot' version '3.0.0'
    }

    dependencies {
        implementation project(':core')
        testImplementation project(path: ':testing')
        implementation 'com.google.guava:guava:31.1-jre'
        api group: 'org.slf4j', name: 'slf4j-api', version: '2.0.0'
    }

    task customTask {
        doLast { println "Done" }
    }

    tasks.register('buildApp') {
        dependsOn 'customTask'
    }
    """)

    assert _get_extractor(build_file) == extract_gradle

    res = extract_gradle(build_file)
    nodes = {n["id"]: n for n in res["nodes"]}
    edges = res["edges"]

    # Plugins
    plugin_nodes = [n for n in nodes.values() if n.get("type") == "gradle_plugin"]
    plugin_ids = {p["plugin_id"] for p in plugin_nodes}
    assert "java" in plugin_ids
    assert "org.springframework.boot" in plugin_ids

    uses_edges = [e for e in edges if e["relation"] == "uses"]
    assert len(uses_edges) == 2

    # Inter-project dependencies
    dep_edges = [e for e in edges if e["relation"] == "depends_on"]
    # Should link to project(:core), project(:testing), guava, and slf4j
    assert len(dep_edges) == 4

    # External packages
    pkg_nodes = [n for n in nodes.values() if n.get("type") == "package"]
    pkg_labels = {p["label"] for p in pkg_nodes}
    assert "Package:com.google.guava:guava" in pkg_labels
    assert "Package:org.slf4j:slf4j-api" in pkg_labels

    # Tasks
    task_nodes = [n for n in nodes.values() if n.get("subtype") == "gradle_task"]
    task_names = {t["task_name"] for t in task_nodes}
    assert "customTask" in task_names
    assert "buildApp" in task_names


def test_extract_gradle_build_kotlin_dsl(tmp_path: Path):
    build_file = tmp_path / "build.gradle.kts"
    build_file.write_text("""
    plugins {
        id("application")
        id("org.jetbrains.kotlin.jvm")
    }

    dependencies {
        implementation(project(":core"))
        implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.6.4")
    }

    tasks.register<Jar>("fatJar") {
        archiveClassifier.set("all")
    }
    """)

    assert _get_extractor(build_file) == extract_gradle

    res = extract_gradle(build_file)
    nodes = {n["id"]: n for n in res["nodes"]}
    edges = res["edges"]

    # Plugin nodes
    plugins = [n for n in nodes.values() if n.get("type") == "gradle_plugin"]
    assert len(plugins) == 2

    # Dependencies
    deps = [e for e in edges if e["relation"] == "depends_on"]
    assert len(deps) == 2

    # Task
    tasks = [n for n in nodes.values() if n.get("subtype") == "gradle_task"]
    assert any(t["task_name"] == "fatJar" for t in tasks)
