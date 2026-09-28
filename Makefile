# Makefile — Standard Unix fallback contracts for graph_fy
.PHONY: test test-target lint-fix check update-graph audit

test:
	uv run pytest -q --tb=short

test-target:
	uv run pytest -q --tb=short $(TARGET)

lint-fix:
	uv run ruff check --fix .
	uv run ruff format .

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright

update-graph:
	uv run python -m graph_fy.cli update .

audit:
	uv run pip-audit
