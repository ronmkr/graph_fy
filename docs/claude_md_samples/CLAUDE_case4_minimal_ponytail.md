# CLAUDE.md — Case 4: Minimal Ponytail ($10 Extreme Budget)

> **Architecture Profile**: Ultra-lean, solo-founder, or strict budget cap. Squeezes maximum software delivery out of every single cent. Every token is dollars.

---

## 1. The $10 Budget Law: Radical Token Efficiency
The best code is the code you never wrote. The best token is the token you never spent.

- **Read Nothing You Don't Need**:
  - Run `graph_fy context "<task>"` once at task start. Read only the skeleton output.
  - NEVER call `cat` or read raw files >50 lines.
  - For targeted lines: use `sed -n '40,65p' <file>` or `rg -C 2 '<pattern>' <file>`.
- **Mute All Terminal Output**:
  - Raw test output and compiler logs waste thousands of context tokens.
  - Always pipe output: `just test | tail -n 20` or `pytest -q --tb=short`.
- **Zero Hallucination Commands**:
  - Use `just test` and `just lint-fix`. Never invent custom flags.
- **Run Under Headroom Proxy**:
  - Always launch with `headroom wrap claude`.
  - Automatically freezes prompt caches and compresses redundant logs.

---

## 2. Radical Simplicity (Ponytail & YAGNI)
- **Standard Library First**:
  - Python: `pathlib`, `json`, `dataclasses`, `collections`, `urllib.request`.
  - TypeScript: Native `fetch`, `URL`, `Map`, `Set`, `Record`.
  - Go: Standard library `net/http`, `encoding/json`, `errors`.
- **Zero New Dependencies**: Never add a third-party package if it can be written in 10 lines of standard library.
- **No Premature Abstractions**:
  - Do NOT create generic base classes, repository interfaces, abstract factories, or wrapper services for single implementations.
  - Keep logic inline and straightforward until proven necessary.
- **Surgical Diffs**:
  - Touch only the 5–15 lines necessary to solve the issue.
  - Never touch or reformat surrounding untouched code.

---

## 3. Context Reset Hygiene (The 30k Token Rule)
- When a single vertical ticket is implemented and tests pass:
  1. Record completed state in `tasks.md`.
  2. Tell the user: `Task complete. Tests green. Run /clear to reset context.`
- NEVER accumulate multiple features in a single session past 30,000 tokens. Context rot degrades reasoning and explodes cost.

---

## 4. Git & Filesystem Rules
- Work in an isolated worktree when possible:
  ```bash
  git worktree add ../.worktrees/<task> -b feature/<task>
  ```
- **Git Safety Contract**: CRITICAL: NEVER run `git commit`, `git push`, `git checkout`, or `git switch` unless explicitly instructed with the exact phrase `"Commit these changes"`.
- After code modifications: run `graph_fy update .` (0-cost AST sync).

---

## 5. Caveman Output (Zero Filler)
- Ban all conversational filler:
  - NO: "I'd be glad to help with that!"
  - NO: "Here is what I plan to do..."
  - NO: "I have successfully modified the code..."
- YES: Output dense, high-signal technical bullet points and exact shell commands.
- Preserve 100% technical fidelity: exact file paths, line numbers, and symbol names.
