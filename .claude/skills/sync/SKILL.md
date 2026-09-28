---
name: sync
description: Synchronize local AST knowledge graph after modifying code files (0 API cost).
---

## Objective
Update the graph_fy knowledge graph locally after any code modifications to keep AST definitions and call graphs in sync.

## Execution Command
```bash
python3 -m graph_fy.cli update .
```

## Guardrails
- Must be executed after every batch of file edits.
- Operates 100% locally via Tree-sitter AST with zero LLM API cost.
