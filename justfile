# justfile — Deterministic developer & agent toolchain contracts for graph_fy
# All recipes use uv for fast, reproducible execution.

default:
    @just --list

# Run entire test suite with quiet reporting
test:
    uv run pytest -q --tb=short

# Run a specific test file or directory (e.g. `just test-target tests/test_retrieval.py`)
test-target path:
    uv run pytest -q --tb=short {{path}}

# Run deterministic linters and auto-formatters (zero-token fixes)
lint-fix:
    uv run ruff check --fix .
    uv run ruff format .

# Check code formatting and static typing without modifications
check:
    uv run ruff check .
    uv run ruff format --check .
    uv run pyright

# Run local AST knowledge graph sync (zero API cost)
update-graph:
    uv run python -m graph_fy.cli update .

# Run security audit on dependencies
audit:
    uv run pip-audit
