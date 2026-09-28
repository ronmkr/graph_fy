---
name: lint-fix
description: Run deterministic ruff auto-fix and formatting (zero-LLM token fixes).
---

## Objective
Auto-correct code formatting and lint violations deterministically without consuming LLM reasoning tokens.

## Execution Command
```bash
uv run ruff check --fix . && uv run ruff format .
```

## Guardrails
- Always run this command before proposing code diffs or reporting task completion.
- Do not manually rewrite files for whitespace or simple import reordering; delegate to this skill.
