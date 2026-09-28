# ADR-001: Project-Specific Unix Agent Skills Over Monolithic Generic Frameworks

* **Status**: Accepted
* **Deciders**: Project Maintainers
* **Date**: 2026-09-26
* **Technical Invariants**: `INV-TOOL-001`, `INV-TOOL-002`

---

## 1. Context & Problem Statement

AI coding agents (e.g., Claude Code, Cursor, Antigravity) are frequently configured with third-party skill frameworks containing 200+ generic skills and 60+ specialized agents (e.g., ECC, universal plugin packs).

In practice, these monolithic frameworks introduce three critical points of failure:
1. **The Context/Schema Tax**: Loading hundreds of unused skills or heavy MCP tool schemas dilutes the model's attention window before the first turn completes.
2. **Framework Bloat & Maintenance Rot**: Generic multi-framework skills frequently collide, fail on local environments, and attempt to "solve the world's problems" rather than respecting repository boundaries.
3. **Hallucinated Execution Flags**: Without strict, deterministic command contracts, agents guess compiler flags, build targets, and linter parameters.

---

## 2. Decision

We reject monolithic, universal skill frameworks for this repository. Instead, we enforce the **Unix Philosophy for Agent Skills**:

1. **Repo-Specific Scoping**: Skills must be defined in `.claude/skills/` and strictly bound to `graph_fy`'s own CLI and build contracts (`uv`, `ruff`, `pytest`).
2. **Sharp Single Responsibility**: Each skill performs exactly one operation (e.g., `graph-context`, `targeted-test`, `lint-fix`, `sync`).
3. **Deterministic Tool Contracts**: All developer and agent commands are declared in a root `justfile` (with a standard `Makefile` fallback) powered by `uv`.
4. **Log Muting by Default**: All test and command runners must pipe output through slicing filters (`tail -n 25`, `jq`) to eliminate context rot.

---

## 3. Consequences

### Positive
- **Near-Zero Token Overhead**: System prompts and skill files remain under 40 lines of markdown each.
- **Deterministic Execution**: Agents invoke standardized recipes (`just test-target <path>`, `just lint-fix`) instead of hallucinating shell commands.
- **Immediate Portability**: Any contributor or CI environment with `uv` and `python` can run the exact same commands.

### Negative / Trade-offs
- Developers must manually add repository-specific skills when introducing new tools, rather than relying on a global third-party library.

---

## 4. Compliance Invariants

- `INV-TOOL-001`: No skill in `.claude/skills/` shall exceed 50 lines of Markdown or declare dependencies outside the repo's declared tools.
- `INV-TOOL-002`: All command invocations must be supported by recipes in `justfile` and `Makefile`.
