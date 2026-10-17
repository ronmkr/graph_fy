# CLAUDE.md

## graph_fy

This repository uses graph_fy for codebase indexing, structural AST navigation, architectural flow visualization, and blast radius analysis located at `graph_fy_out/`.

### Core Rules for Claude Code
- **Pre-Agent Analysis Briefing (Zero-LLM First)**: Before asking frontier models (Claude 3.7 Sonnet, etc.) or planning changes, run `graph_fy context "<task>"` (or call MCP tool `get_agent_context`). This generates a prompt-cache-friendly architectural briefing with:
  - Subsystem community classification and god nodes
  - Mermaid architectural flow diagram
  - Tier 1 AST skeletons (compact 1-line docstrings, exact types, elided bodies)
  - Bidirectional execution flow (callers and callees with compaction)
  - Historical temporal co-changes mined from git log
  - Downstream blast radius risk tier and affected test targets
  This eliminates 5–10 exploratory round trips and saves 95%+ prompt tokens.
- **Use graph_fy MCP Tools**: Use `get_agent_context`, `find_definitions`, `find_references`, `get_impact_analysis`, `get_code_skeleton`, and `get_symbol_implementation` instead of reading whole raw files.
- **Graph-First Query**: Query `graph_fy query "<concept>"` or read `graph_fy_out/GRAPH_REPORT.md` before reading raw files.
- **Diff-Scoped Code Review**: Run `graph_fy diff [base_ref]` (or MCP `get_diff_context`) to review touched symbols, 1-hop callers, and blast radius before submitting edits.
- **Iterative Feedback Querying**: Use `graph_fy refine --code "<draft>"` (or MCP `refine_context`) to resolve incomplete identifiers and missing types from draft code.
- **High-Density Code RAG**: Run `graph_fy query "<concept>" --skeleton` or `--rag-prompt --budget N` for token-capped prompt-ready context blocks with 1-hop interface bundling.
- **Hierarchical Community Context**: Navigate `graph_fy_out/wiki/index.md` for high-level architectural context in large codebases to save context tokens.
- **Zero-Hallucination AST Invariant**: Ground code edits strictly in verified AST definitions from `graph_fy context`. If a symbol, method, or API is not present in the graph briefing, do NOT hallucinate or guess — verify against the indicated source file first!
- **Pre-Edit Safety**: Run `graph_fy blast <symbol>` before modifying code to inspect blast radius and downstream dependencies.
- **Incremental Graph Update**: Run `graph_fy update .` locally after editing code to keep the knowledge graph in sync (fast AST-only update, zero API cost).

### Ponytail: Anti-Overengineering ("The best code is the code you never wrote")
- **YAGNI**: Challenge unnecessary abstractions, bloated wrappers, and speculative generality.
- **Graph-First Code Reuse**: Before creating any new helper function, class, or utility, query `graph_fy query "<concept>"` or inspect the knowledge graph. Reuse existing code first!
- **Standard Library First**: Always prefer native runtime/language standard library over adding third-party packages.
- **Surgical Diffs**: Keep code modifications minimal, focused, and precise. Never refactor surrounding untouched code.

### Caveman: Terse, High-Signal Output (Token Conservation)
- Cut conversational filler, pleasantries, preambles, apologies, and redundant restatements.
- Output in dense, high-signal bullet points and concise technical fragments.
- Preserve 100% technical fidelity: exact file paths, line ranges, symbol names, shell commands, and code diffs must always be verbatim and uncompressed.
