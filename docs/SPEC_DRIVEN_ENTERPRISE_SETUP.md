# The $10 Spec-Driven Enterprise Developer Setup

> **Goal**: Build an enterprise-grade application (Java, TypeScript, Python, Go, Rust) on a **$10 Claude budget** where **Claude writes all the code** and you provide **only specs and documentation**.

---

## 1. The Economics: Why $10 Is Plenty

On Claude 3.7 / 3.5 Sonnet:
* **Standard Turn (Unoptimized)**: 80k input + 2k output = **~$0.27 to $0.50 / turn**. Budget exhausted in **~20–30 turns** before core services are built.
* **Optimized Turn (This Stack)**: 50k cached input ($0.015) + 200 surgical output tokens ($0.003) = **~$0.018 to $0.022 / turn**. 
* **Capacity**: **$10 yields ~450–500 productive turns**.

---

## 2. The Required Dev Setup (3 Local Tools)

Install these three tools. They require zero cloud accounts or API keys:

```bash
# 1. Runtime In-Flight Proxy & Prompt Cache Freezer
pip install headroom-ai

# 2. Macro Codebase Knowledge Graph & Blast Radius
pip install graph_fy

# 3. Micro Language Server Protocol (LSP) Engine
uv tool install -p 3.13 serena-agent
serena init
```

---

## 3. Wire MCP Tools into Claude / IDE

Add to your Claude Code / Cursor / Antigravity config (`~/.claude/mcp.json` or `.mcp.json`):

```json
{
  "mcpServers": {
    "graph_fy": {
      "command": "python",
      "args": ["-m", "graph_fy.serve"]
    },
    "serena": {
      "command": "serena",
      "args": ["mcp"]
    }
  }
}
```

* **`graph_fy`**: Provides macroscopic codebase topology, Leiden communities, God nodes, and blast radius (eliminates 90% of file reads).
* **`serena`**: Provides microscopic LSP symbol lookups and atomic AST refactoring (replaces full-file rewrites).

---

## 4. Launch Claude Wrapped in Headroom

Always run your agent inside Headroom's proxy wrapper:

```bash
headroom wrap claude
```

What Headroom does automatically:
1. **Freezes prompt cache prefixes**: Ensures every turn hits the 90% Anthropic cache read discount ($0.30/M vs $3.00/M).
2. **Compresses terminal/build noise**: Strips verbose build logs (Maven, Gradle, Cargo, npm) down to errors only.
3. **Applies CCR (Compress-Cache-Retrieve)**: Automatically caches large tool results and injects compact handles.

---

## 5. The Universal Agent Rules (`AGENTS.md` / `CLAUDE.md`)

Place this exact file in the root of your project:

```markdown
# AGENTS.md

## 1. Caveman Mode (Output Token Conservation)
- Zero conversational pleasantries, preambles, or restatements.
- Output ONLY verbatim code edits and shell execution commands. Output tokens cost 5x more than inputs.

## 2. Ponytail Mode (Anti-Overengineering & YAGNI)
- Implement strictly what is specified in the spec. No speculative generality.
- Use native modern language features (Java 21 Records, TypeScript types, Python dataclasses, Go structs).
- Never add unnecessary helper libraries or boilerplate wrappers.
- Surgical Diffs: Modify only targeted lines. Never reformat or touch surrounding code.

## 3. Zero-LLM Exploration (graph_fy First)
- NEVER read entire source files with `cat` or `view_file`.
- Call `graph_fy get_agent_context(task="<spec>")` to retrieve AST skeletons and blast radius.
- Call `graph_fy get_symbol_implementation(node_id)` to expand only the specific function body needed.
- After code modifications, run `graph_fy update .` (0-cost local AST sync).

## 4. Symbol Navigation & Refactoring (Serena First)
- Use Serena's `find_symbol` and `find_referencing_symbols` for cross-file definitions.
- Use `replace_symbol_body` for atomic edits instead of full file overwrites.

## 5. Muted Test Execution
- Run only the specific affected test target identified by graph_fy.
- Always use quiet flags (e.g. `./gradlew test --tests "..." --quiet`, `mvn test -q`, `pytest -q`).
```

---

## 6. The Spec-Driven Workflow (Your Role: Specs Only)

You never write code; you provide clean, unambiguous specifications.

### Step 1: Write the Spec
Create `docs/specs/<feature>.md`:
```markdown
# Spec: Order Fulfillment Service
- Model: Order(id: UUID, customerId: UUID, items: List<Item>, status: Status)
- Port: OrderRepository.save(Order) -> Order
- Port: InventoryClient.reserve(List<Item>) -> Result
- UseCase: FulfillOrderUseCase.execute(orderId: UUID)
  - Fetch order; if not found -> throw OrderNotFoundException
  - Reserve inventory; if failed -> set status to CANCELLED and return
  - Set status to FULFILLED, persist, return success
```

### Step 2: Prompt Claude (1 Line)
```
Implement docs/specs/01-order-fulfillment.md. Follow AGENTS.md rules.
```

### Step 3: Claude Executes Autonomously
1. Claude calls `graph_fy get_agent_context` (1,500 tokens).
2. Claude creates domain models and interfaces.
3. Claude writes a quiet unit test and runs it (fails).
4. Claude implements the service to pass the test.
5. Claude runs `graph_fy update .` to update the graph locally.
6. **Total cost for feature**: **~$0.04 (4 cents)**.
