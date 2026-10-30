# Upstream Fork Synchronization Strategy & Architecture

This document outlines the hexagonal boundary architecture, automated drift tracking, and upstream patch contribution playbook between `ronmkr/graph_fy` and upstream `Graphify-Labs/graphify`.

---

## 1. Hexagonal Boundary: Core vs Outer Extensions

To minimize merge conflicts and prevent fork rot as upstream evolves Tree-sitter parsers and AST extractors, `graph_fy` enforces strict hexagonal architectural boundaries:

```text
+--------------------------------------------------------------------------+
| Outer Agent & Tooling Layer (Fork-Specific, Zero Upstream Conflict)      |
|                                                                          |
|  - graph_fy/commands/          (12 decoupled subcommand modules)         |
|  - graph_fy/templates/         (Decoupled CSS/JS for visualizers)        |
|  - graph_fy/exporters/         (Interactive browser dashboard)           |
|  - graph_fy/comparative_eval.py(Empirical benchmarks & subword BPE evals)|
|  - graph_fy/retrieval_context.py(Pre-agent briefings & prompt cache RAG) |
|  - graph_fy/impact.py          (Dynamic language confidence tiers)       |
+--------------------------------------------------------------------------+
                                     |
                                     v (Consumes graph data structures)
+--------------------------------------------------------------------------+
| Upstream Core Layer (Aligned with Graphify-Labs/graphify conventions)   |
|                                                                          |
|  - graph_fy/extractors/        (Language AST parsers & schema extractors)|
|  - graph_fy/build.py           (Core graph pipeline & cross-resolutions) |
|  - graph_fy/analyze.py         (Betweenness, Louvain, PageRank algorithms|
|  - graph_fy/extract.py         (Multi-language source tree routing)      |
+--------------------------------------------------------------------------+
```

### Invariant Rules

1. **Zero Core Pollution**: Outer agent tools (`graph_fy/commands/`, `graph_fy/context/`, `graph_fy/templates/`) MUST NEVER modify internal AST parser hooks or inject agent-specific schemas into core extractor outputs.
2. **Schema Invariant**: Edge confidence levels emitted in core extraction MUST strictly adhere to standard schemas: `EXTRACTED`, `INFERRED`, or `AMBIGUOUS`.

---

## 2. Automated Upstream Drift Tracking

An automated GitHub Action runs daily at `02:00 UTC` (`.github/workflows/upstream_sync.yml`):

- Fetches `upstream/main` and `upstream/v8` (`https://github.com/Graphify-Labs/graphify.git`).
- Calculates commit distance between `HEAD` and `upstream/main`.
- If new upstream commits or releases are detected, it compiles a diff summary and opens/updates an automated tracking issue with tag `upstream-sync`.

### Manual Sync Command

```bash
# Add upstream remote if not already present
git remote add upstream https://github.com/Graphify-Labs/graphify.git

# Fetch latest upstream commits and tags
git fetch upstream main v8 --tags

# Check commit difference
git log --oneline HEAD..upstream/main

# Merge upstream into a dedicated sync branch
git checkout -b sync/upstream-main
git merge upstream/main

# Run test suite to verify zero regressions
pytest tests/
```

---

## 3. Synchronized Batches & History

### Batch 2: Upstream v0.9.97 Release Track Parity (PR #84)
- **Resolved Issues**: #72 through #83 (12 issues closed).
- **Hardened Components**:
  - Multi-language AST: SystemVerilog task calls, Pascal records, OCaml chained module/class bindings, Razor `@implements`, Fortran subroutine resolution, Blade `wire:click` modifier trimming, COBOL nested program stack, Zig bare vs member call dispatch, Dart unnamed extensions, TypeScript parenthesized decorator chain recovery, Rust multi-crate workspace scoping.
  - Type Aliases: Go `type A = B`, C++ `using`/`typedef`, Rust `type`/`union` aliases extracted as first-class type nodes.
  - Manifests & Dependencies: `package.json` explicit dependency nodes, Maven POM `dependencyManagement` scoping, external JS `require()` call retention.
  - SQL Lineage: `GO` batch separator normalization, stored procedure `reads_from`/`writes_to` lineage, column default table error recovery, T-SQL bracketed identifier stripping.
  - Security & Storage: SSRF IPv4 (`224.0.0.0/4`) and IPv6 (`ff00::/8`) multicast blocking, frontmatter description colon/newline sanitization, safe case-only community relabeling on case-insensitive filesystems, edge `relation` key invariant, and shrink guard semantic file twin tracking.

---

## 4. Upstream Contribution Playbook (Giving Back)

Generic algorithmic improvements, parser bug fixes, and performance optimizations developed in `graph_fy` are isolated into surgical branches for upstream submission:

1. **Sampled Pivot Betweenness Speedup**:
   - Upstream betweenness calculation is $O(V \cdot E)$. On large codebases (>10k nodes), exact betweenness takes minutes.
   - The sampled pivot betweenness algorithm reduces runtime to <3s while preserving rank correlation ($r > 0.94$).
2. **Incident Edge Map Traversal**:
   - Pre-indexing edges into adjacency dicts eliminates repeated linear scans during BFS/DFS and pathfinding.
3. **Regex Escape & Sanitization Fixes**:
   - Safe parsing of escaped characters in route paths, template literals, and regex parameters.
4. **FastAPI & NestJS Route Resolution**:
   - Full-stack cross-boundary contract resolution between frontend client calls and backend route handlers.
