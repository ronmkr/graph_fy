# CLAUDE.md — Case 2: MCP-Driven Agent Stack

> **Architecture Profile**: Structured Model Context Protocol stack (`graph_fy` MCP + `serena` LSP MCP + `ast-grep` skill + Matt Pocock skills + `headroom`).

---

## 1. Tool Protocol Hierarchy (Strict Order of Operations)

### Step 1: Pre-Agent Briefing (Zero-LLM First)
- **Tool**: `get_agent_context(task="<task-description>")`
- **Output**: Subsystem Leiden community, god nodes, Mermaid architecture flow, Tier-1 AST skeletons, and downstream test targets.
- **Contract**: NEVER query raw source files before running this tool.

### Step 2: Invariant Interrogation & Specification (Matt Pocock Skills)
- Use `/grill-me` to challenge edge cases and clarify ambiguities.
- Use `/to-spec` to lock Given-When-Then criteria and domain invariants (`INV-XXX`).
- Use `/to-tickets` to decompose into single-turn tasks.

### Step 3: Precise Symbol Resolution & Navigation (`serena` LSP)
- Locate definitions: `find_symbol(name_path="<SymbolName>")`
- Find all usages & callers: `find_referencing_symbols(symbol_id="<id>")`
- **Rule**: Do NOT perform whole-codebase regex searches for symbol definitions.

### Step 4: On-Demand Implementation Expansion (`graph_fy` CCR)
- When a skeleton contains `// [On-Demand Body: get_symbol_implementation("{nid}")]`:
  - Call `get_symbol_implementation(node_id="{nid}")` to retrieve only that exact function/method body.
  - Never call `view_file` or `cat` on the parent file.

### Step 5: Structural Mutation (`serena` & `ast-grep`)
- Perform atomic method replacements: `replace_symbol_body(symbol_id="<id>", new_body="...")`
- Verify structural patterns before/after edits using the `ast-grep` skill.

### Step 6: Blast Radius Verification
- Check impact before editing shared core modules: `get_impact_analysis(symbol="<SymbolName>")`
- Check git diff before finalizing: `get_diff_context(base_ref="origin/main")`

---

## 2. Test & Verification Contracts
- Run only the specific affected test target discovered in Step 1:
  - Python: `pytest <target_test_file>` or `just test-target <target>`
  - Node: `npm test -- <test_file>`
  - Go: `go test -run <TestName> <package>`
- Deterministic lint auto-fix: `just lint-fix` (or `ruff format .` / `biome check --apply`)

---

## 3. Session & Token Guardrails
- **Prompt Cache Protection**: Maintain static prefixes. Do not alter system instructions mid-session.
- **Zero Full-File Reads**: Reading raw files >100 lines is blocked. Use `get_symbol_implementation` or targeted line-slice reads only.
- **Incremental Sync**: After editing code, execute `graph_fy update .` (0-cost AST sync).
- **Git Safety Rule**: CRITICAL: NEVER run `git commit`, `git push`, `git checkout`, or `git switch` unless explicitly instructed with the exact phrase `"Commit these changes"`.

---

## 4. Behavioral & Engineering Rules
- **Ponytail**:
  - Prefer native language standard library over adding third-party packages.
  - Strict YAGNI: Reject speculative generality, unused helper classes, and premature wrappers.
  - Surgical diffs: Modify only the necessary lines. Never reformat untouched lines.
- **Caveman**:
  - Zero conversational filler, pleasantries, preambles, or summaries.
  - High-signal bullet points, exact file paths, line numbers, and commands.
