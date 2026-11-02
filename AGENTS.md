<!-- GRAPH_FY_PRIME_START -->
### graph_fy Prime Briefing (Zero-LLM Fast Path)
- **Bounded Contexts**:
  - Context 2 (222 nodes): `_read_text()`, `_extract_generic()`
  - Context 13 (214 nodes): `detect()`, `_is_sensitive()`
  - Context 1 (195 nodes): `_edge_labels()`, `extract_java()`
- **Key Entry Points**: `_reenter_main()`, `_run_cli()`, `cli.py`, `dispatch_command()`
- **High-Risk Hubs**: `extract()` (757 edges), `build_from_json()` (279 edges), `_read_text()` (206 edges)
- **Protocol**: Run `graph_fy context "<task>"` for AST skeletons & blast radius before edits.
<!-- GRAPH_FY_PRIME_END -->

# graph_fy

This project has a graph_fy knowledge graph at graph_fy_out/ with community structure, god nodes, and deterministic AST relationships.

## Rules for Coding Agents (Claude Code, Antigravity, Gemini CLI)

- **Pre-Agent Analysis Briefing (Zero-LLM First)**: Before querying frontier models or reading raw files, run `graph_fy context "<task>"` (or MCP `get_agent_context`). Retrieves:
  - Subsystem community classification & god nodes
  - Mermaid architectural flow diagram
  - Tier 1 AST skeletons (1-line docstrings, exact types, elided bodies)
  - Bidirectional execution flow (callers & callees with compaction)
  - Git temporal co-changes
  - Downstream blast radius risk tier & verification test targets
  Reduces token usage by >95% and eliminates 5–10 exploratory round trips.
- Before answering architecture or codebase questions, read graph_fy_out/GRAPH_REPORT.md for god nodes and community structure
- If graph_fy_out/wiki/index.md exists, navigate it instead of reading raw files
- Token-Efficient RAG Queries: Use `graph_fy query "<concept>" --skeleton` or `--rag-prompt` to retrieve AST skeletons and PPR-ranked ego networks without whole-file reads
- **Zero-Hallucination AST Invariant**: Ground code edits strictly in verified AST definitions from `graph_fy context`. If a symbol, method, or API is not present in the graph briefing, do NOT hallucinate or guess — verify against the indicated source file first!
- After modifying code files in this session, run `graph_fy update .` to keep the graph current (AST-only, no API cost)

## Ponytail: Anti-Overengineering ("The best code is the code you never wrote")

- YAGNI: Challenge unnecessary abstractions, bloated wrappers, and speculative generality.
- Graph-First Code Reuse: Before creating any new helper function, model, or utility, query `graph_fy query "<concept>"` or inspect the knowledge graph. Reuse existing code first!
- Standard Library First: Always prefer native runtime/language standard library over adding third-party packages.
- Surgical Diffs: Keep code modifications minimal, focused, and precise. Never refactor surrounding untouched code.

## Caveman: Terse, High-Signal Output (Token Efficiency)

- Cut conversational filler, pleasantries, preambles, apologies, and unnecessary restatements.
- Output in dense, high-signal bullet points and concise technical fragments.
- Preserve 100% technical fidelity: exact file paths, line ranges, symbol names, shell commands, and code diffs must always be verbatim and uncompressed.

## Testing Best Practices & Anti-Bloat Protocol

- **No Micro-File Sprawl**: Never create a new standalone test file for a single bug, issue, or PR (e.g. `test_issue_123.py`). Always add regression test cases directly into the existing domain test suite (e.g., `tests/test_javascript.py`, `tests/test_csharp.py`, `tests/test_indirect_dispatch.py`).
- **Parameterize Over Proliferation**: When testing syntax variations or edge cases, group them into `@pytest.mark.parametrize` matrices rather than proliferating separate single-assertion test functions.
- **Explicit Behavioral Assertions**: Every test MUST assert concrete AST outputs (node IDs, labels, edge types, confidence levels, or reachability). Never write zero-assertion "did it not crash?" tests.
- **In-Process CLI Execution**: Prefer invoking in-process entry points (`graph_fy.__main__.main()`) over `subprocess.run(["graph_fy", ...])` to avoid paying Python interpreter cold-start penalties (~150ms per call).
- **Fast Parallel Execution**: Always run test suites with `uv run pytest -n auto -q` to utilize all local CPU cores.
- **Selective Blast-Radius Testing**: During iterative refactoring, run only the tests reached by the modified symbols via `graph_fy affected` before executing the broader suite.
