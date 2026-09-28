# Ready-to-Use CLAUDE.md Samples Directory

This directory contains standalone, copy-pasteable sample `CLAUDE.md` files configured for different engineering environments, team sizes, and token budgets.

---

## Sample Index & Selection Matrix

| File | Target Environment | Primary Tools | Token Profile |
| :--- | :--- | :--- | :--- |
| [**`CLAUDE_case1_unix_toolchain.md`**](CLAUDE_case1_unix_toolchain.md) | Terminal CLI / Claude Code | `graph_fy` CLI, `ast-grep`, `rg`, `just`, `headroom` | Zero schema tax, ultra-fast CLI |
| [**`CLAUDE_case2_mcp_stack.md`**](CLAUDE_case2_mcp_stack.md) | MCP-Enabled IDEs (Claude Code, Cursor, Antigravity) | `graph_fy` MCP, `serena` LSP, `ast-grep` skill, Matt Pocock skills | Structured protocols & compiler LSP |
| [**`CLAUDE_case3_enterprise_monorepo.md`**](CLAUDE_case3_enterprise_monorepo.md) | Large Repos (10k–500k files), Multi-Team | `graph_fy`, Bounded Contexts, Git Worktrees, ADRs, Invariants | Bounded context isolation & blast radius gates |
| [**`CLAUDE_case4_minimal_ponytail.md`**](CLAUDE_case4_minimal_ponytail.md) | Solo Founders, Lean Teams, $10 Budget | `graph_fy`, `just`, Stdlib, Headroom, Caveman style | Extreme token conservation (<30k ctx) |
| [**`CLAUDE_case5_spec_driven_tdd.md`**](CLAUDE_case5_spec_driven_tdd.md) | Spec-First Teams & Feature Delivery | `/grill-me`, `/to-spec`, `/to-tickets`, `/implement`, `/code-review` | Formal Gherkin contracts & red-green TDD |

---

## How to Use These Samples

1. Choose the sample matching your workflow.
2. Copy the file into your project root as `CLAUDE.md`:
   ```bash
   cp docs/claude_md_samples/CLAUDE_case1_unix_toolchain.md ./CLAUDE.md
   ```
3. Customize project-specific commands in the `justfile` or test runner sections.
4. Launch your agent session (e.g., `headroom wrap claude` or `claude`).
