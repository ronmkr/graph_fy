"""Unit tests for Qt Designer UI (.ui) XML and C++ signal-to-slot connection extraction."""
from __future__ import annotations

from pathlib import Path

from graph_fy.extract import _get_extractor
from graph_fy.extractors.c_cpp import extract_cpp
from graph_fy.extractors.qt_ui import extract_qt_ui, is_qt_ui_file


def test_is_qt_ui_file(tmp_path: Path):
    valid_ui = tmp_path / "mainwindow.ui"
    valid_ui.write_text('<ui version="4.0"><class>MainWindow</class></ui>')
    assert is_qt_ui_file(valid_ui)

    non_ui = tmp_path / "not_ui.txt"
    non_ui.write_text('<ui version="4.0"></ui>')
    assert not is_qt_ui_file(non_ui)

    invalid_ext = tmp_path / "dialog.ui"
    invalid_ext.write_text("This is plain text without XML")
    assert not is_qt_ui_file(invalid_ext)


def test_extract_qt_ui_safe_xml(tmp_path: Path):
    ui_file = tmp_path / "mainwindow.ui"
    ui_file.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<ui version="4.0">
 <class>MainWindow</class>
 <widget class="QMainWindow" name="MainWindow">
  <property name="geometry">
   <rect><x>0</x><y>0</y><width>800</width><height>600</height></rect>
  </property>
  <widget class="QWidget" name="centralwidget">
   <widget class="QPushButton" name="submitButton">
    <property name="text">
     <string>Submit</string>
    </property>
   </widget>
   <widget class="QLineEdit" name="usernameInput"/>
  </widget>
 </widget>
 <connections>
  <connection>
   <sender>submitButton</sender>
   <signal>clicked()</signal>
   <receiver>MainWindow</receiver>
   <slot>onSubmitClicked()</slot>
  </connection>
 </connections>
</ui>
""")

    assert _get_extractor(ui_file) == extract_qt_ui

    res = extract_qt_ui(ui_file)
    nodes = {n["id"]: n for n in res["nodes"]}
    edges = res["edges"]

    # Class node
    class_node = next((n for n in nodes.values() if n.get("class_name") == "MainWindow"), None)
    assert class_node is not None
    assert class_node["type"] == "ui_class"

    # Widgets
    btn_widget = next((n for n in nodes.values() if n.get("widget_name") == "submitButton"), None)
    input_widget = next((n for n in nodes.values() if n.get("widget_name") == "usernameInput"), None)
    assert btn_widget is not None
    assert btn_widget["widget_class"] == "QPushButton"
    assert input_widget is not None
    assert input_widget["widget_class"] == "QLineEdit"

    # Contains edges
    contains_edges = [e for e in edges if e["relation"] == "contains"]
    assert len(contains_edges) >= 3

    # Signal-to-slot connection edge
    conn_edge = next((e for e in edges if e["relation"] == "connects_signal_slot"), None)
    assert conn_edge is not None
    assert conn_edge["source"] == btn_widget["id"]
    assert conn_edge["target"] == class_node["id"]
    assert conn_edge["signal"] == "clicked()"
    assert conn_edge["slot"] == "onSubmitClicked()"
    assert conn_edge["sender"] == "submitButton"
    assert conn_edge["receiver"] == "MainWindow"


def test_extract_qt_ui_blocks_entity_expansion(tmp_path: Path):
    malicious_ui = tmp_path / "exploit.ui"
    malicious_ui.write_bytes(b'<!DOCTYPE ui [<!ENTITY lol "lol">]><ui version="4.0"><class>Hack</class></ui>')
    res = extract_qt_ui(malicious_ui)
    assert "unsafe XML" in res.get("error", "")


def test_extract_cpp_qt_connections(tmp_path: Path):
    cpp_file = tmp_path / "mainwindow.cpp"
    cpp_file.write_text("""
#include "mainwindow.h"
#include <QPushButton>

void MainWindow::setupConnections() {
    // Classic syntax
    connect(ui->saveButton, SIGNAL(clicked()), this, SLOT(onSave()));
    
    // Modern PMF syntax
    QObject::connect(senderObj, &Worker::finished, this, &MainWindow::handleFinished);

    // Lambda slot syntax
    connect(ui->cancelButton, &QPushButton::clicked, [=]() {
        close();
    });
}
""")

    res = extract_cpp(cpp_file)
    edges = res.get("edges", [])
    conn_edges = [e for e in edges if e.get("relation") == "connects_signal_slot"]

    assert len(conn_edges) == 3

    # Classic connection
    classic = next((e for e in conn_edges if e.get("signal") == "clicked()"), None)
    assert classic is not None
    assert classic["slot"] == "onSave()"
    assert "saveButton" in classic["sender"]

    # Modern PMF connection
    pmf = next((e for e in conn_edges if "Worker::finished" in e.get("signal", "")), None)
    assert pmf is not None
    assert "handleFinished" in pmf["slot"]
    assert pmf["sender"] == "senderObj"

    # Lambda connection
    lambda_conn = next((e for e in conn_edges if e.get("slot") == "<lambda>"), None)
    assert lambda_conn is not None
    assert "QPushButton::clicked" in lambda_conn["signal"]
