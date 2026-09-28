# CLAUDE.md — Case 3: Enterprise Monorepo Spec-Driven

> **Architecture Profile**: Massive monorepo (10k–500k files), multiple bounded contexts, strict domain invariants, ADR gates, and blast-radius controls.

---

## 1. Monorepo Constraints & Context Isolation
Repository Size: >50,000 files. Strict token budgeting and domain boundaries enforced.

- **Worktrees Strictly Required**: NEVER work in the repository root. All development must occur in an isolated git worktree:
  ```bash
  git worktree add ../.worktrees/<ticket-id> -b feature/<ticket-id>
  cd ../.worktrees/<ticket-id>
  ```
- **Prohibited Global Commands**:
  - `grep -r`, `find .`, or repo-wide recursive file walks are STRICTLY FORBIDDEN.
  - All searches must be bounded to the target package: `packages/<domain>/<pkg>/`.
- **Git Safety Contract**: CRITICAL: NEVER run `git commit`, `git push`, `git checkout`, or `git switch` unless explicitly instructed with the exact phrase `"Commit these changes"`.

---

## 2. The 3-Gate Specification Lifecycle
Claude must NOT write application code until Gates 1 and 2 are satisfied:

```
[ Gate 1: Spec & Invariants ] ──> [ Gate 2: Blast Radius & Topology ] ──> [ Gate 3: Package TDD ]
```

### Gate 1: Specification & Domain Invariants
- Verify `specs/<domain>/<feature>/spec.md` exists with:
  - Explicit Invariant IDs (`INV-001`, `INV-002`)
  - Given-When-Then Gherkin acceptance scenarios
- If cross-boundary changes, database schema modifications, or public API changes occur:
  - Verify Architecture Decision Record exists: `docs/adr/ADR-XXXX.md`
- Run `/grill-me` if business logic or failure modes are underspecified.

### Gate 2: Subsystem Topology & Blast Radius
- Run pre-agent briefing:
  ```bash
  graph_fy context "<feature-title>"
  ```
- Inspect **Downstream Blast Radius Risk Tier**:
  - **Tier 3 (Low / Leaf Node)**: Proceed to Gate 3 immediately.
  - **Tier 2 (Moderate)**: Proceed with targeted regression tests.
  - **Tier 1 (High / Shared Kernel / God Node)**: HALT. Display affected caller communities to user and request approval before proceeding.

### Gate 3: Deterministic TDD Implementation
1. Write failing unit/integration tests covering Gherkin scenarios:
   ```bash
   just test-package packages/<domain>/<pkg>
   ```
2. Implement minimal code to turn tests green.
3. Auto-fix formatting without LLM tokens:
   ```bash
   just lint-package packages/<domain>/<pkg>
   ```

---

## 3. Sharp Code Retrieval (Bounded Scopes Only)
- **Macro Navigation**: `graph_fy context "<concept>"` or `graph_fy query "<symbol>" --skeleton`
- **Micro AST Search**: `ast-grep -p '<pattern>' packages/<domain>/<pkg>/`
- **Lexical Search**: `rg '<string>' packages/<domain>/<pkg>/ -g '!tests'`
- **Expand Symbol Bodies**: Use `graph_fy` CCR handles (`get_symbol_implementation`) or `sed` for targeted slices. Never dump whole files.

---

## 4. Post-Edit Gate & Verification
Before declaring a task complete:
1. Run local package test suite: `just test-package packages/<domain>/<pkg>`
2. Verify boundary integrity: `graph_fy diff origin/main` (confirms no unintended shared kernel mutations).
3. Update local knowledge graph: `graph_fy update .`
4. Report completion in Caveman format.

---

## 5. Engineering Standards (Ponytail & Caveman)
- **Ponytail (Anti-Overengineering)**:
  - Reuse existing domain models and value objects before creating new ones.
  - Standard library first; zero new third-party dependencies without ADR approval.
  - Surgical diffs: edit only the 5–20 lines required to satisfy the invariant.
- **Caveman (Output Token Conservation)**:
  - Zero conversational pleasantries ("Understood!", "Certainly!", "I have updated...").
  - Output dense technical fragments, exact invariant IDs (`INV-001: Verified`), file paths, line ranges, and test outputs.
