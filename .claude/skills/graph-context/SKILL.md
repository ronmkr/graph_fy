---
name: graph-context
description: Extract AST skeletons, god nodes, and blast radius for a task in graph_fy.
---

## Objective
Generate a zero-LLM architectural briefing for the requested task before reading or modifying source files.

## Execution Command
```bash
python3 -m graph_fy.cli context "$ARGUMENTS"
```

## Guardrails
- Inspect the reported Leiden community classification and god nodes.
- Use the Tier-1 AST skeletons to understand signatures and types.
- Never dump or cat full source files when skeleton signatures are present.
- Identify the affected test targets from the blast radius section for subsequent testing.
