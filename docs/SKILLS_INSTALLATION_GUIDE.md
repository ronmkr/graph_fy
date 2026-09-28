# Complete Skills & Tools Installation Guide

> **Objective**: Step-by-step instructions to install, configure, and verify the entire token-efficient, deterministic AI coding stack across **Claude Code**, **Cursor**, **Antigravity**, and **Claude Desktop**.

---

## 1. Stack Overview & Prerequisites

### Prerequisites
* Python 3.10+ (recommend 3.12 or 3.13)
* `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh` or `brew install uv`)
* Node.js 18+ (for `npx` skills manager)

### The 5 Stack Components
1. **Matt Pocock's AI Skills** (`/grill-me`, `/to-spec`, `/to-tickets`, `/implement`, `/code-review`): Workflow & interrogation skills.
2. **`graph_fy`**: Macro codebase knowledge graph, Leiden communities, blast radius, and pre-agent briefing MCP server.
3. **`headroom`**: Runtime HTTP proxy, prompt cache freezer, SmartCrusher, and log compressor.
4. **`serena`**: Micro Language Server Protocol (LSP) code navigation and atomic AST refactoring MCP server.
5. **`ponytail` & `caveman`**: Behavioral prompt rules enforcing YAGNI, standard library first, and zero-conversational output.

---

## 2. Installing Matt Pocock's AI Skills

Matt Pocock’s skills (`mattpocock/skills`) integrate professional engineering fundamentals into AI agents.

### Option A: Install via Claude Code Plugins (Recommended for Claude Code)
```bash
claude plugins install mattpocock-skills
```

### Option B: Install via `skills.sh` (Universal for Any Client)
Run this inside your project root to copy the markdown skills into your repo:
```bash
npx skills@latest add mattpocock/skills
```
This adds the skill commands directly into your project:
* `/grill-me`: Interrogates requirements and edge cases before code is written.
* `/grill-with-docs`: Cross-references specs against existing codebase documentation.
* `/to-spec`: Converts conversation into formal Given-When-Then specifications.
* `/to-tickets`: Slices specifications into atomic, 1-turn Jira/GitHub issues.
* `/implement`: Executes test-driven development (TDD red-green cycle).
* `/code-review`: Dual-axis review checking spec compliance and code standards.

---

## 3. Installing & Configuring `graph_fy` MCP Server

### Installation
```bash
pip install graph_fy
```

### Verify Local CLI
```bash
# Verify installation
graph_fy --version

# Generate initial codebase graph in current repo (zero API cost)
graph_fy update .
```

### Wire `graph_fy` into MCP Clients

#### For Claude Code (`~/.claude/mcp.json` or project `.mcp.json`):
```json
{
  "mcpServers": {
    "graph_fy": {
      "command": "python",
      "args": ["-m", "graph_fy.serve"]
    }
  }
}
```

#### For Cursor (`Settings` -> `Features` -> `MCP`):
* Name: `graph_fy`
* Type: `command`
* Command: `python -m graph_fy.serve`

#### For Antigravity / Gemini CLI (`~/.gemini/antigravity-cli/mcp/` or config):
```json
{
  "mcpServers": {
    "graph_fy": {
      "command": "python",
      "args": ["-m", "graph_fy.serve"]
    }
  }
}
```

---

## 4. Installing & Configuring `headroom` Proxy

Headroom acts as the local compression layer and prompt-cache protector.

### Installation
```bash
pip install headroom-ai
```

### Option A: Direct Wrapper Mode (Simplest)
Wrap your agent CLI in a single command. Headroom starts a local proxy on `127.0.0.1:8787`, routes traffic through it, and terminates when the agent exits:
```bash
# For Claude Code:
headroom wrap claude

# For Cursor / Custom IDEs:
headroom wrap cursor
```

### Option B: Standalone Daemon Mode
Run Headroom as a persistent background proxy:
```bash
headroom proxy --port 8787
```
Then configure your agent's API base URL:
```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
```

---

## 5. Installing & Configuring `serena` MCP Server

Serena provides compiler-grade Language Server Protocol (LSP) navigation.

### Installation via `uv`
```bash
# 1. Install tool
uv tool install -p 3.13 serena-agent

# 2. Initialize in project
serena init
```

### Wire `serena` into MCP Clients

#### For Claude Code (`.mcp.json`):
```json
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": ["mcp"]
    }
  }
}
```

#### For Cursor:
* Name: `serena`
* Type: `command`
* Command: `serena mcp`

---

## 6. Configuring `ponytail` & `caveman` Behavioral Rules

These behavioral rules require zero package installs; they are activated by placing them in your project's rule files:

### Create Project `AGENTS.md` and `CLAUDE.md`:
```markdown
# AGENTS.md / CLAUDE.md

## 1. Caveman Mode (Output Token Conservation)
- Zero conversational pleasantries, preambles, or restatements.
- Output ONLY code diffs and shell execution commands. Output tokens cost 5x more than input.

## 2. Ponytail Mode (Anti-Overengineering & YAGNI)
- Strict YAGNI. Write the shortest code satisfying the spec.
- Prefer native language features (Java 21 Records, TS types, Python dataclasses, Go structs).
- Never add speculative helper classes, premature wrappers, or unnecessary abstractions.
- Surgical Diffs: Modify only targeted lines. Never reformat or touch surrounding code.

## 3. Zero-LLM Exploration (graph_fy First)
- NEVER read whole files with `cat` or `view_file`.
- Call `graph_fy get_agent_context(task="<spec>")` for AST skeletons and blast radius.
- Call `graph_fy get_symbol_implementation(node_id)` to expand only necessary function bodies.
- After code modifications, run `graph_fy update .` (0-cost local AST sync).

## 4. Symbol Navigation (Serena First)
- Use Serena's `find_symbol` and `find_referencing_symbols` for cross-file definitions.
- Use `replace_symbol_body` for atomic edits instead of full file overwrites.

## 5. Muted Test Execution
- Run only the specific affected test target discovered by graph_fy.
- Always use quiet flags (e.g. `./gradlew test --quiet`, `mvn test -q`, `pytest -q`).
```

---

## 7. Verification Checklist

Run this 30-second verification before starting development:

```bash
# 1. Check graph_fy
python -m graph_fy.serve --help > /dev/null && echo " graph_fy ready"

# 2. Check headroom
headroom --version > /dev/null && echo " headroom ready"

# 3. Check serena
serena --help > /dev/null && echo " serena ready"

# 4. Check skills
ls -la .skills/ 2>/dev/null || echo "ℹ Matt Pocock skills installed via CLI/plugins"
```

Once verified, launch your workflow with:
```bash
headroom wrap claude
```
