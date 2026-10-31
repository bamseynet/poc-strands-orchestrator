.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test check sample

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install: ## Install dependencies
	uv sync --python 3.12

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format: ## Fix lint and formatting where possible
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Type check
	uv run mypy

test: ## Run the tests (offline, with a fake LLM)
	uv run pytest --cov=incident_agent --cov-fail-under=90

check: lint typecheck test ## Everything CI runs

SAMPLE ?= checkout-5xx
sample: ## Run a sample incident in the terminal, e.g. make sample SAMPLE=batch-disk-full
	uv run python -m incident_agent --sample $(SAMPLE)
