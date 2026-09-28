# CLAUDE.md — Case 1: Pure Unix Side-by-Side Toolchain

> **Architecture Profile**: Zero MCP schema tax. Small, sharp CLI tools composed via Unix pipes and `justfile`. Claude acts strictly as an orchestrator.

---

## 1. Project Toolchain & Build Contracts
All builds, tests, and formatting MUST be executed through `just`. Never guess compiler or test flags.
- **Run all tests**: `just test`
- **Run targeted test file**: `just test-target <path/to/test.py>`
- **Run deterministic lint fix (0-token auto-fix)**: `just lint-fix`
- **Run static type check**: `just check`
- **Run security audit**: `just audit`

---

## 2. Codebase Navigation (Strictly Zero Full-File Dumps)
Never read files >50 lines with `cat` or `view_file`. Use the three sharp retrieval tools:

### A. Macro Topology (`graph_fy`)
- Before writing code, run `graph_fy context "<task>"` to extract:
  - Subsystem Leiden community classification
  - God nodes and architectural flow diagram
  - Tier-1 AST skeletons (exact signatures, elided bodies)
  - Affected blast radius tier and test targets
- To search a concept across AST without full reads: `graph_fy query "<symbol>" --skeleton`

### B. Micro Structural AST Search (`ast-grep`)
- Search function/class structures: `ast-grep -p 'def $FUNC($$$ARGS): $$$BODY' <path>`
- Structural code refactoring: `ast-grep --rewrite '<pattern>' -r '<replacement>' <path>`

### C. Fast Lexical Search (`ripgrep`)
- Search string literals, route paths, env vars: `rg '<string>' -g '!tests' -g '!*.lock'`
- Inspect symbol context with 2 lines padding: `rg -C 2 '<string>' <path>`

---

## 3. Output Slicing & Log Hygiene
Output tokens cost 5x more than input tokens and rot attention. Never dump unfiltered CLI output.
- **Slice test output**: `just test | tail -n 25`
- **Filter JSON responses**: `curl -s <url> | jq '{status, code, errors}'`
- **Filter directory listings**: `ls -la <dir> | head -n 20`

---

## 4. Execution & Filesystem Isolation
- **Git Worktrees Only**: Never work directly in the repository root or switch branches.
  ```bash
  git worktree add ../.worktrees/<ticket-id> -b feature/<ticket-id>
  cd ../.worktrees/<ticket-id>
  ```
- **Git Safety Rule**: CRITICAL: NEVER run `git commit`, `git push`, `git checkout`, or `git switch` unless explicitly instructed with the exact phrase `"Commit these changes"`.
- **Incremental Graph Sync**: After modifying code files, run `graph_fy update .` (0-cost local AST update).
- **Blast Radius Verification**: Before finalizing edits, run `graph_fy diff origin/main` to verify no cross-boundary leaks occurred.

---

## 5. Runtime Compression (`headroom`)
- Ensure the agent session runs wrapped under Headroom or routes through the local proxy:
  ```bash
  headroom wrap claude
  # Or verify proxy is listening on 127.0.0.1:8787
  ```
- Headroom preserves prompt cache integrity and compresses tool log output.

---

## 6. Coding & Communication Standards
- **Ponytail (Anti-Overengineering & YAGNI)**:
  - Standard library first (`pathlib`, `json`, `dataclasses`, `collections`, `typing`).
  - No premature abstractions, generic wrappers, or speculative helper classes.
  - Surgical diffs: edit only the targeted lines (5–15 lines). Never touch or reformat surrounding code.
- **Caveman (Dense Output)**:
  - Omit conversational filler, pleasantries, preambles, and post-summaries.
  - Output in dense, high-signal technical bullet points and exact shell commands.
  - Preserve 100% technical fidelity: exact file paths, line numbers, and symbol names.
