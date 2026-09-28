# CLAUDE.md Templates for Every Architecture

This document provides production-ready, copy-pasteable `CLAUDE.md` templates tailored for different development environments, toolchains, and token budgets.

> **Standalone Sample Files**: Standalone drop-in files are available in [`docs/claude_md_samples/`](claude_md_samples/README.md).

---

## Template Index

1. [Case 1: Pure Unix Side-by-Side Toolchain](#case-1-pure-unix-side-by-side-toolchain) (Zero Schema Tax: `graph_fy` + `ast-grep` + `ripgrep` + `just`) &rarr; [View Standalone File](claude_md_samples/CLAUDE_case1_unix_toolchain.md)
2. [Case 2: MCP-Driven Agent Stack](#case-2-mcp-driven-agent-stack) (Structured Protocol: `graph_fy` MCP + `serena` LSP + `ast-grep` Skill) &rarr; [View Standalone File](claude_md_samples/CLAUDE_case2_mcp_stack.md)
3. [Case 3: Enterprise Monorepo Spec-Driven](#case-3-enterprise-monorepo-spec-driven) (10k–500k Files: Bounded Contexts, Invariant IDs, Git Worktrees) &rarr; [View Standalone File](claude_md_samples/CLAUDE_case3_enterprise_monorepo.md)
4. [Case 4: Minimal Ponytail / Extreme $10 Budget](#case-4-minimal-ponytail--extreme-10-budget) (Ultra-Lean: Radical YAGNI, Caveman Output, Stdlib-First) &rarr; [View Standalone File](claude_md_samples/CLAUDE_case4_minimal_ponytail.md)
5. [Case 5: Spec-Driven TDD Workflow (Matt Pocock Skills + ADRs)](#case-5-spec-driven-tdd-workflow) &rarr; [View Standalone File](claude_md_samples/CLAUDE_case5_spec_driven_tdd.md)

---

## Case 1: Pure Unix Side-by-Side Toolchain

> **Best For**: Fast, terminal-native CLI workflows using Claude Code. Zero MCP schema overhead. Composes small, sharp Unix binaries.

```markdown
# CLAUDE.md

## Project Toolchain (Unix Philosophy)
This repository uses small, sharp CLI tools. Claude acts strictly as an orchestrator. Never guess commands, never read entire source files.

### 1. Build, Test & Lint Contracts
Always run tests and linters through `just`. Do not hallucinate compiler or test flags.
- Run unit tests: `just test`
- Run targeted test: `just test <path/to/test_file.py>`
- Run deterministic linters (zero-token fix): `just lint-fix`
- Run type checker: `just check`

### 2. Codebase Navigation (No Whole-File Reads)
- **Macro Topology (graph_fy)**:
  - Before writing code, run `graph_fy context "<task>"` to inspect affected communities, god nodes, and Tier-1 skeletons.
  - To trace concepts without full reads: `graph_fy query "<symbol>" --skeleton`.
- **Micro Syntax & AST (ast-grep)**:
  - For structural searches (functions, classes, calls): `ast-grep -p '<pattern>' <path>`.
  - For syntax refactors: `ast-grep --rewrite '<target>' -r '<replacement>' <path>`.
- **Fast Lexical Search (ripgrep)**:
  - For string constants, environment variables, route names: `rg '<string>' -g '!tests'`.

### 3. Output Muting & Log Hygiene
- Never allow commands to dump >30 lines into context.
- Slice large outputs: `pytest | tail -n 25` or `curl ... | jq '{status, error}'`.

### 4. Git & Workflow Rules
- **CRITICAL**: Never run `git checkout`, `git switch`, `git commit`, or `git push` unless explicitly instructed with "Commit these changes".
- Work inside your assigned worktree (`.worktrees/<task-name>`).
- Run `graph_fy update .` after modifying source code.

### 5. Ponytail & Caveman Style
- **Ponytail**: Prefer native language standard library over adding new dependencies. Keep diffs surgical.
- **Caveman**: Omit conversational pleasantries, preambles, and summaries. Output in dense, high-signal technical bullet points.
```

---

## Case 2: MCP-Driven Agent Stack

> **Best For**: Environments leveraging Model Context Protocol (`graph_fy` MCP, Serena LSP, ast-grep skill) inside Claude Code, Cursor, or Claude Desktop.

```markdown
# CLAUDE.md

## Agent Tool Protocols (MCP-Driven)
This repository is configured with Model Context Protocol (MCP) servers for deterministic codebase indexing and language server intelligence.

### 1. Mandatory Tool Usage Hierarchy
1. **Initial Briefing**: Call `get_agent_context(task="<task>")` before querying any files.
   - Retrieves subsystem classification, god nodes, Mermaid architecture flow, and AST skeletons.
2. **Symbol Implementation**: If an AST skeleton includes `// [On-Demand Body: get_symbol_implementation("{nid}")]`, call `get_symbol_implementation(node_id="{nid}")` to expand only that specific body.
3. **References & Callers**:
   - Use `find_references(node_id="...")` and `get_callers(node_id="...")` to trace data flow.
   - Do NOT grep for symbol names across the whole repo.
4. **Impact Analysis**:
   - Call `get_impact_analysis(symbol="...")` before modifying core functions to assess downstream blast radius.
5. **Structural AST Pattern Matching**:
   - Call `ast-grep` skill or `ast_grep_search` to verify syntax patterns before and after edits.

### 2. Verification & Safety Checks
- Before proposing edits, run `get_diff_context(base_ref="origin/main")` to review touched symbols against blast radius.
- Run local tests: `just test` or `.venv/bin/pytest`.
- Run linter: `just lint`.

### 3. Execution Discipline
- **Zero Full-File Dumps**: Never use `view_file` or `cat` on files exceeding 100 lines unless targeted line ranges are specified.
- **Cache Preservation**: Maintain consistent prompt structure so MCP schemas remain cached.
- **No Git Operations**: Never stage or commit code without explicit user instruction.
```

---

## Case 3: Enterprise Monorepo Spec-Driven

> **Best For**: Massive codebases (10k–500k+ files) with multiple teams, bounded contexts, strict domain invariants, and CI blast-radius gates.

```markdown
# CLAUDE.md

## Enterprise Monorepo Rules (Spec-Driven Architecture)
Repository Size: >50,000 files. Strict token-budgeting and bounded-context isolation enforced.

### 1. The 3-Gate Specification Lifecycle
Do NOT write application code until Gates 1 and 2 are satisfied:
1. **Gate 1 (Spec & Invariants)**:
   - Verify `specs/<feature>/spec.md` exists with explicit invariant IDs (`INV-001`, `INV-002`) and Gherkin scenarios.
   - If architecture changes, verify `docs/adr/ADR-XXXX.md` exists.
2. **Gate 2 (Subsystem Topology & Blast Radius)**:
   - Run `graph_fy context "<feature-name>"` to confirm package boundaries.
   - If blast radius tier is **Tier 1 (High)**, require approval before editing shared kernels.
3. **Gate 3 (TDD Implementation)**:
   - Write failing package tests matching Gherkin specs (`just test-package <package>`).
   - Implement minimal code to turn tests green.

### 2. Bounded Context Isolation (Worktrees Only)
- Never work directly in the repository root.
- All edits must occur within an isolated worktree:
  `git worktree add ../.worktrees/<ticket-id> -b feature/<ticket-id>`
- Never run `git checkout` or `git switch`.

### 3. Retrieval Policy
- Global searches (`grep -r`, `find .`) are strictly forbidden.
- Use `graph_fy context` to identify target package.
- Use `ast-grep -p '<pattern>' packages/<pkg>/` for AST queries.
- Use `rg '<literal>' packages/<pkg>/` for string queries.

### 4. Post-Edit Gate
- Run `graph_fy diff origin/main` to verify no cross-boundary leaks occurred.
- Run `graph_fy update .` to keep knowledge graph current.
- Respond in Caveman format (dense technical bullets, exact file links).
```

---

## Case 4: Minimal Ponytail / Extreme $10 Budget

> **Best For**: Ultra-lean projects, solo founders, or strict budget caps. Squeezes maximum programming work out of every cent.

```markdown
# CLAUDE.md

## Minimalist Ponytail Protocol ($10 Budget Mode)
Every token costs money. The best code is the code you never wrote.

### 1. Radical Token Efficiency
- **Read Nothing You Don't Need**: Run `graph_fy context "<task>"` once. Read only the skeleton output.
- **Never Read Raw Files**: Never run `cat` or read files >50 lines. Use `ast-grep` or `rg -C 2` for snippets.
- **Zero Hallucination Commands**: Use `just test` and `just lint-fix`. Never guess arguments.
- **Mute Test Noise**: Always run `just test | tail -n 20`.

### 2. Radical Simplicity (Ponytail & YAGNI)
- No speculative generality, no extra layers, no unnecessary abstractions.
- Use native standard library first (`pathlib`, `json`, `dataclasses`, `collections`).
- Zero new third-party dependencies without explicit user permission.
- Surgical diffs: edit only the 5–10 lines required. Never reformat surrounding code.

### 3. Context Reset Hygiene
- Once a single task/ticket is verified, save state in markdown and tell the user to `/clear`.
- Never let conversation context exceed 30,000 tokens.

### 4. Caveman Communication
- Zero conversational pleasantries ("Sure!", "I'd be happy to help", "Here is the summary").
- Dense, high-signal bullet points only.
- 100% technical accuracy on paths, line numbers, and commands.
```

---

## Case 5: Spec-Driven TDD Workflow

> **Best For**: Feature teams, agile squads, and specification-first engineering workflows using Matt Pocock's skills (`/grill-me`, `/to-spec`, `/to-tickets`, `/implement`, `/code-review`), ADR gates, and TDD red-green cycles.

```markdown
# CLAUDE.md

## Spec-Driven TDD Workflow (Matt Pocock Skills + ADRs)
Strict specification-first development and Test-Driven Development (TDD) enforced.

### 1. The 6-Phase Lifecycle
Every feature, refactor, or bug fix MUST transition through these sequential phases:
`/grill-me` -> `/to-spec` -> `ADR` (if architectural) -> `/to-tickets` -> `/implement` -> `/code-review`

1. **Phase 1: Requirements Interrogation (`/grill-me` & `/grill-with-docs`)**:
   - Interrogate hidden assumptions, failure modes, and concurrency boundaries.
2. **Phase 2: Formal Specification (`/to-spec`)**:
   - Record invariants with explicit IDs (`INV-001`, `INV-002`) and Given-When-Then scenarios in `specs/<feature>/spec.md`.
3. **Phase 3: Architectural Decision Record (ADR Gate)**:
   - Create `docs/adr/ADR-XXXX.md` if cross-boundary schemas or patterns change.
4. **Phase 4: Atomic Tasks (`/to-tickets`)**:
   - Decompose spec into single-turn tasks in `tasks.md`.
5. **Phase 5: Test-Driven Implementation (`/implement`)**:
   - Red: Write failing test (`just test-target <path>`).
   - Green: Write minimal code to pass test.
   - Refactor: Deterministic lint fix (`just lint-fix`).
6. **Phase 6: Dual-Axis Review (`/code-review`)**:
   - Check spec compliance against `INV-XXX` and code standards.

### 2. Codebase Topology & Retrieval (graph_fy)
- Run `graph_fy context "implement <feature>"` before Phase 5.
- Inspect Mermaid architectural diagram, god nodes, and affected test targets.
- Expand method bodies via `graph_fy get_symbol_implementation` on-demand. Never dump full files.

### 3. Build & Verification Contracts
- Run tests: `just test` or `just test-target <path>`
- Mute logs: `just test | tail -n 25`
- Zero unauthorized git operations (Never commit without explicit "Commit these changes").
- Run `graph_fy update .` post-edit.

### 4. Ponytail & Caveman Style
- Standard library first, strict YAGNI, surgical diffs.
- Dense technical bullets, exact invariant verification status, zero conversational filler.
```

