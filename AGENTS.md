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
