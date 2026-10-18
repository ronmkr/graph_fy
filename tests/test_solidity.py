"""Tests for Solidity contract extraction and C3 linearization inherited call/modifier resolution (#39, #4379, #4384)."""
from pathlib import Path
from graph_fy.extract import extract
from graph_fy.extractors.solidity import extract_solidity, c3_linearize


def test_c3_linearization_ordering():
    """Verify C3 linearization produces identical resolution order to solc."""
    # Diamond inheritance: C is A, B (B listed after A, so B overrides A)
    bases = {
        "O": [],
        "A": ["O"],
        "B": ["O"],
        "C": ["A", "B"],
    }
    order = c3_linearize("C", bases)
    assert order == ["C", "B", "A", "O"]


def test_solidity_extractor_basic(tmp_path):
    """Test Solidity extractor parses contract, interface, library, events, modifiers."""
    sol_file = tmp_path / "Tokens.sol"
    sol_file.write_text(
        "// SPDX-License-Identifier: MIT\n"
        "pragma solidity ^0.8.0;\n"
        "\n"
        "interface IERC20 {\n"
        "    function totalSupply() external view returns (uint256);\n"
        "}\n"
        "\n"
        "library Math {\n"
        "    function add(uint a, uint b) internal pure returns (uint) {\n"
        "        return a + b;\n"
        "    }\n"
        "}\n"
        "\n"
        "contract Token is IERC20 {\n"
        "    event Transfer(address indexed from, address indexed to, uint256 val);\n"
        "    modifier onlyOwner() { require(msg.sender == address(0)); _; }\n"
        "    constructor() onlyOwner() {}\n"
        "    function totalSupply() public view override returns (uint256) { return 1000; }\n"
        "}\n",
        encoding="utf-8",
    )

    res = extract_solidity(sol_file)
    assert not res.get("error")

    nodes = {n["label"]: n for n in res["nodes"]}
    assert "IERC20" in nodes
    assert "Math" in nodes
    assert "Token" in nodes
    assert "totalSupply()" in nodes
    assert "onlyOwner()" in nodes
    assert "constructor()" in nodes
    assert "event Transfer" in nodes

    edges = [(e["source"], e["relation"], e["target"]) for e in res["edges"]]
    token_nid = nodes["Token"]["id"]
    # Token inherits IERC20
    assert any(e[0] == token_nid and e[1] == "inherits" and e[2] == "IERC20" for e in edges)
    # constructor uses onlyOwner
    cons_nid = nodes["constructor()"]["id"]
    only_owner_nid = nodes["onlyOwner()"]["id"]
    assert (cons_nid, "uses", only_owner_nid) in edges


def test_solidity_inherited_calls_super_and_modifiers(tmp_path):
    """Ensure C3 linearization resolves inherited calls, super calls, and modifiers (#39, #4379, #4384)."""
    sol_file = tmp_path / "Hierarchy.sol"
    sol_file.write_text(
        "// SPDX-License-Identifier: MIT\n"
        "pragma solidity ^0.8.0;\n"
        "\n"
        "contract Admin {\n"
        "    modifier onlyAdmin() { _; }\n"
        "    function logAction() public {}\n"
        "}\n"
        "\n"
        "contract BaseA is Admin {\n"
        "    function ping() public virtual {}\n"
        "}\n"
        "\n"
        "contract BaseB is Admin {\n"
        "    function ping() public virtual {}\n"
        "    function helper() public {\n"
        "        super.logAction();\n"
        "    }\n"
        "}\n"
        "\n"
        "contract Derived is BaseA, BaseB {\n"
        "    function invoke() public onlyAdmin {\n"
        "        ping();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    res = extract([sol_file], cache_root=tmp_path / "graph_fy_out", parallel=False)
    assert not res.get("error")

    edges = res["edges"]
    nodes_by_id = {n["id"]: n for n in res["nodes"]}

    calls = [
        (nodes_by_id[e["source"]]["label"], nodes_by_id[e["target"]]["label"])
        for e in edges if e.get("relation") == "calls"
    ]
    uses = [
        (nodes_by_id[e["source"]]["label"], nodes_by_id[e["target"]]["label"])
        for e in edges if e.get("relation") == "uses"
    ]

    # 1. Derived is BaseA, BaseB: ping() in Derived resolves to BaseB's ping (BaseB is listed last)
    # Target node for BaseB.ping
    base_b_ping_nid = next(n["id"] for n in res["nodes"] if n["label"] == "ping()" and "baseb" in n["id"])
    derived_invoke_nid = next(n["id"] for n in res["nodes"] if n["label"] == "invoke()")
    assert any(e["source"] == derived_invoke_nid and e["target"] == base_b_ping_nid for e in edges)

    # 2. super.logAction() in BaseB resolves to Admin.logAction
    base_b_helper_nid = next(n["id"] for n in res["nodes"] if n["label"] == "helper()")
    admin_log_nid = next(n["id"] for n in res["nodes"] if n["label"] == "logAction()" and "admin" in n["id"])
    assert any(e["source"] == base_b_helper_nid and e["target"] == admin_log_nid for e in edges)

    # 3. onlyAdmin modifier on Derived.invoke() resolves to Admin.onlyAdmin two levels up
    admin_mod_nid = next(n["id"] for n in res["nodes"] if n["label"] == "onlyAdmin()" and "admin" in n["id"])
    assert any(e["source"] == derived_invoke_nid and e["target"] == admin_mod_nid for e in edges)


def test_solidity_overload_resolution_by_arity(tmp_path):
    """Ensure inherited calls match by arity, while ambiguous same-arity overloads are left unbound."""
    sol_file = tmp_path / "Overloads.sol"
    sol_file.write_text(
        "// SPDX-License-Identifier: MIT\n"
        "pragma solidity ^0.8.0;\n"
        "\n"
        "contract Base {\n"
        "    function compute(uint a) public {}\n"
        "    function compute(uint a, uint b) public {}\n"
        "    function ambiguous(uint a) public {}\n"
        "    function ambiguous(int a) public {}\n"
        "}\n"
        "\n"
        "contract Child is Base {\n"
        "    function run() public {\n"
        "        compute(1);\n"
        "        compute(1, 2);\n"
        "        ambiguous(1);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    res = extract([sol_file], cache_root=tmp_path / "graph_fy_out", parallel=False)
    edges = res["edges"]
    nodes_by_id = {n["id"]: n for n in res["nodes"]}

    child_run_nid = next(n["id"] for n in res["nodes"] if n["label"] == "run()")
    base_compute_1 = next(
        n["id"] for n in res["nodes"]
        if n["label"] == "compute()" and n.get("metadata", {}).get("arity") == 1
    )
    base_compute_2 = next(
        n["id"] for n in res["nodes"]
        if n["label"] == "compute()" and n.get("metadata", {}).get("arity") == 2
    )

    # compute(1) resolves to arity 1
    assert any(e["source"] == child_run_nid and e["target"] == base_compute_1 for e in edges)
    # compute(1, 2) resolves to arity 2
    assert any(e["source"] == child_run_nid and e["target"] == base_compute_2 for e in edges)

    # ambiguous(1) has 2 declarations of arity 1 in Base -> left unbound (no call edge)
    ambiguous_nids = {
        n["id"] for n in res["nodes"]
        if n["label"] == "ambiguous()"
    }
    for amb_nid in ambiguous_nids:
        assert not any(e["source"] == child_run_nid and e["target"] == amb_nid for e in edges)


def test_solidity_cross_file_inheritance(tmp_path):
    """Ensure inherited calls resolve across multiple .sol files."""
    parent_sol = tmp_path / "Parent.sol"
    parent_sol.write_text(
        "pragma solidity ^0.8.0;\n"
        "contract Parent {\n"
        "    function greet() public {}\n"
        "}\n",
        encoding="utf-8",
    )

    child_sol = tmp_path / "Child.sol"
    child_sol.write_text(
        "pragma solidity ^0.8.0;\n"
        "import './Parent.sol';\n"
        "contract Child is Parent {\n"
        "    function hello() public {\n"
        "        greet();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    res = extract([parent_sol, child_sol], cache_root=tmp_path / "graph_fy_out", parallel=False)
    edges = res["edges"]

    child_hello_nid = next(n["id"] for n in res["nodes"] if n["label"] == "hello()")
    parent_greet_nid = next(n["id"] for n in res["nodes"] if n["label"] == "greet()")

    assert any(
        e["source"] == child_hello_nid and e["target"] == parent_greet_nid and e["relation"] == "calls"
        for e in edges
    )
