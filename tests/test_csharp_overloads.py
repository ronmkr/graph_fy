"""Tests for C# method overload distinction and parameter-arity call dispatch (#4584, #4591 / #70)."""
from pathlib import Path
import pytest

from graph_fy.extract import extract


def test_csharp_overload_nodes_separated_with_stable_single(tmp_path: Path):
    f = tmp_path / "Worker.cs"
    f.write_text(
        "public class Worker {\n"
        "    public void Run() {}\n"
        "    public void Run(int count) {}\n"
        "    public void Single() {}\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    nodes = {n["id"]: n for n in result["nodes"]}

    run_a0_id = next((nid for nid in nodes if "run_a0" in nid), None)
    run_a1_id = next((nid for nid in nodes if "run_a1" in nid), None)
    single_id = next((nid for nid in nodes if nid.endswith("single")), None)

    assert run_a0_id is not None, "Overload with arity 0 must have distinct node ID"
    assert run_a1_id is not None, "Overload with arity 1 must have distinct node ID"
    assert single_id is not None, "Non-overloaded method must maintain stable ID"
    assert "_a" not in single_id, "Non-overloaded method must NOT have an arity suffix"

    # Line numbers must be distinct
    assert nodes[run_a0_id]["source_location"] == "L2"
    assert nodes[run_a1_id]["source_location"] == "L3"
    assert nodes[single_id]["source_location"] == "L4"

    # Metadata
    assert nodes[run_a0_id].get("metadata", {}).get("csharp_overload") is True
    assert nodes[run_a0_id].get("metadata", {}).get("arity") == 0
    assert nodes[run_a1_id].get("metadata", {}).get("csharp_overload") is True
    assert nodes[run_a1_id].get("metadata", {}).get("arity") == 1


def test_csharp_in_file_call_dispatch(tmp_path: Path):
    f = tmp_path / "App.cs"
    f.write_text(
        "public class App {\n"
        "    public void Process() {}\n"
        "    public void Process(string name, int flags) {}\n"
        "    public void Start() {\n"
        "        Process();\n"
        "        Process(\"task\", 1);\n"
        "    }\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    calls = [(e["source"], e["target"]) for e in result["edges"] if e["relation"] == "calls"]

    start_nid = next(n["id"] for n in result["nodes"] if "start" in n["id"])
    p_a0 = next(n["id"] for n in result["nodes"] if "process_a0" in n["id"])
    p_a2 = next(n["id"] for n in result["nodes"] if "process_a2" in n["id"])

    assert (start_nid, p_a0) in calls
    assert (start_nid, p_a2) in calls


def test_csharp_receiver_typed_member_call_overload_dispatch(tmp_path: Path):
    f = tmp_path / "Service.cs"
    f.write_text(
        "public class Service {\n"
        "    public void Handle() {}\n"
        "    public void Handle(int id) {}\n"
        "}\n"
        "public class Client {\n"
        "    private Service _svc = new Service();\n"
        "    public void Run() {\n"
        "        _svc.Handle();\n"
        "        _svc.Handle(99);\n"
        "    }\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    calls = [(e["source"], e["target"]) for e in result["edges"] if e["relation"] == "calls"]

    client_run = next(n["id"] for n in result["nodes"] if "client_run" in n["id"])
    svc_h0 = next(n["id"] for n in result["nodes"] if "service_handle_a0" in n["id"])
    svc_h1 = next(n["id"] for n in result["nodes"] if "service_handle_a1" in n["id"])

    assert (client_run, svc_h0) in calls
    assert (client_run, svc_h1) in calls


def test_csharp_interface_dispatch_overloads(tmp_path: Path):
    f = tmp_path / "Interfaces.cs"
    f.write_text(
        "public interface IEngine {\n"
        "    void Start();\n"
        "    void Start(int speed);\n"
        "}\n"
        "public class Engine : IEngine {\n"
        "    public void Start() {}\n"
        "    public void Start(int speed) {}\n"
        "}\n"
    )
    result = extract([f], root=tmp_path)
    dispatch = [(e["source"], e["target"]) for e in result["edges"] if e["relation"] == "dispatches_to"]

    i_start_a0 = next(n["id"] for n in result["nodes"] if "iengine_start_a0" in n["id"])
    i_start_a1 = next(n["id"] for n in result["nodes"] if "iengine_start_a1" in n["id"])
    c_start_a0 = next(n["id"] for n in result["nodes"] if "engine_start_a0" in n["id"] and "iengine" not in n["id"])
    c_start_a1 = next(n["id"] for n in result["nodes"] if "engine_start_a1" in n["id"] and "iengine" not in n["id"])

    assert (i_start_a0, c_start_a0) in dispatch
    assert (i_start_a1, c_start_a1) in dispatch
