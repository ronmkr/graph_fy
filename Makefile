# Makefile — Standard Unix fallback contracts for graph_fy
.PHONY: test test-target lint-fix check update-graph audit

test:
	uv run pytest -q --tb=short

test-target:
	uv run pytest -q --tb=short $(TARGET)

lint-fix:
	uv run ruff check --fix .
	uv run ruff format .
	npx --yes markdownlint-cli --fix "README.md" "UPSTREAM_SYNC.md" "SECURITY.md" "AGENTS.md" "CLAUDE.md" "GEMINI.md" "docs/**/*.md" "evals/**/*.md" ".claude/skills/**/*.md"

check:
	uv run ruff check .
	uv run ruff format --check .
	npx --yes markdownlint-cli "README.md" "UPSTREAM_SYNC.md" "SECURITY.md" "AGENTS.md" "CLAUDE.md" "GEMINI.md" "docs/**/*.md" "evals/**/*.md" ".claude/skills/**/*.md"
	uv run pyright

update-graph:
	uv run graph_fy update .

audit:
	uv run pip-audit
