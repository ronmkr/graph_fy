# CLAUDE.md — Case 5: Spec-Driven TDD Workflow (Matt Pocock Skills + ADRs)

> **Architecture Profile**: Feature teams and agile engineering squads adhering to strict specification-first development, Matt Pocock interrogation skills, Gherkin acceptance contracts, and Test-Driven Development (TDD).

---

## 1. The 6-Phase Feature Delivery Lifecycle

Every feature, refactor, or bug fix MUST transition through these sequential phases:

```
/grill-me ──> /to-spec ──> ADR (if needed) ──> /to-tickets ──> /implement (TDD) ──> /code-review
```

### Phase 1: Requirements Interrogation (`/grill-me` & `/grill-with-docs`)
- Before writing any specification or code, run `/grill-me` on the user's prompt.
- Interrogate:
  - Hidden business assumptions and non-functional requirements.
  - Failure modes, concurrency boundaries, and data edge cases.
  - Cross-references to existing architecture docs (`/grill-with-docs`).

### Phase 2: Formal Specification (`/to-spec`)
- Structure the specification in `specs/<feature>/spec.md`:
  - **Invariants**: Explicit machine-traceable IDs (`INV-001`, `INV-002`, `INV-003`).
  - **Scenarios**: Formal Given-When-Then Gherkin syntax.
  - **Boundary Definitions**: Inputs, outputs, error states, and idempotency guarantees.

### Phase 3: Architectural Decision Record (ADR Gate)
- If the feature introduces a new pattern, modifies shared database tables, or crosses package boundaries:
  - Create `docs/adr/ADR-XXXX-<title>.md`.
  - Record: Context, Decision, Consequences, and Rejected Alternatives.

### Phase 4: Atomic Task Decomposition (`/to-tickets`)
- Slice the spec into vertical 1-turn tasks in `tasks.md`.
- Each task must have:
  - Single bounded file/symbol scope.
  - Associated invariant ID.
  - Deterministic verification command.

### Phase 5: Test-Driven Implementation (`/implement`)
- **Red Phase**: Write the failing test for the current invariant:
  ```bash
  just test-target <path/to/test.py>  # Must fail with expected assertion
  ```
- **Green Phase**: Write the minimal application code to satisfy the test.
- **Refactor Phase**: Auto-format and clean up using deterministic linters:
  ```bash
  just lint-fix
  ```

### Phase 6: Dual-Axis Review (`/code-review`)
- Evaluate changes on two distinct axes:
  1. **Spec Compliance**: Does the code satisfy all `INV-XXX` contracts and Gherkin scenarios?
  2. **Code Standards**: Does it violate Ponytail (anti-overengineering) or introduce unnecessary dependencies?

---

## 2. Codebase Topology & Retrieval (`graph_fy`)
- Before beginning Phase 5 (`/implement`), run:
  ```bash
  graph_fy context "implement <feature-name> according to specs/<feature>/spec.md"
  ```
- Inspect:
  - Mermaid architectural flow diagram.
  - Existing models and utilities (Reuse existing code before creating new classes).
  - Downstream blast radius risk tier.
- On-demand expansion:
  - Use `graph_fy get_symbol_implementation` for method bodies.
  - Never dump full files >50 lines.

---

## 3. Tool Contracts & Execution Safety
- **Build Contracts**:
  - Run all tests: `just test`
  - Run single test: `just test-target <path>`
  - Format/Lint: `just lint-fix`
- **Output Muting**: Always pipe noisy terminal output: `just test | tail -n 25`.
- **Git Safety Contract**: CRITICAL: NEVER run `git commit`, `git push`, `git checkout`, or `git switch` unless explicitly instructed with the exact phrase `"Commit these changes"`.
- **Incremental Sync**: Run `graph_fy update .` post-implementation.

---

## 4. Engineering Standards (Ponytail & Caveman)
- **Ponytail (Anti-Overengineering & YAGNI)**:
  - Standard library first.
  - No speculative wrappers, bloated repositories, or unnecessary abstraction layers.
  - Surgical diffs: edit only the targeted code lines.
- **Caveman (Dense Output)**:
  - Cut conversational filler, pleasantries, and lengthy summaries.
  - Output dense technical fragments, exact invariant status (`INV-001: Verified [PASS]`), and verbatim commands.
