---
name: targeted-test
description: Run isolated pytest on a specific test target with log muting.
---

## Objective
Execute isolated tests against the specific test target identified by graph_fy, muting verbose terminal output.

## Execution Command
```bash
uv run pytest -q --tb=short "$ARGUMENTS" | tail -n 25
```

## Guardrails
- If no argument is provided, ask for the specific test target path before running the full suite.
- If failures occur, read only the truncated failure assertion diff.
- Never allow test execution logs exceeding 30 lines into agent context.
