# Blybot developer workflow. `make help` lists targets.

.DEFAULT_GOAL := help

.PHONY: help install hooks lint format typecheck test check clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install all dependencies (incl. dev)
	uv sync

hooks: install ## Install pre-commit, commit-msg and pre-push hooks
	uv run pre-commit install --install-hooks -t pre-commit -t commit-msg -t pre-push

lint: ## Ruff lint (no changes)
	uv run ruff check

format: ## Ruff auto-fix + format
	uv run ruff check --fix
	uv run ruff format

typecheck: ## mypy --strict over src and tests
	uv run mypy

test: ## pytest with coverage gate
	uv run pytest

check: lint typecheck test ## Everything CI runs
	uv run pre-commit run --all-files

clean: ## Remove caches and build artifacts
	rm -rf .venv .mypy_cache .pytest_cache .ruff_cache .coverage dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
