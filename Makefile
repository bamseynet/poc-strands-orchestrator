.DEFAULT_GOAL := help
.PHONY: help install dev lint format typecheck test check sample web-fixtures synth deploy

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install: ## Install dependencies
	uv sync --python 3.12 --all-extras
	npm --prefix web ci
	npm --prefix infra ci

API_PORT ?= 8000
WEB_PORT ?= 5173
dev: ## Run the API and UI on localhost. Uses the fake LLM unless LLM_MODE=bedrock
	@echo "UI: http://localhost:$(WEB_PORT)"
	@# If either side exits, stop both: a UI left running would proxy /api to whatever owns the port.
	@trap 'kill 0' INT TERM EXIT; \
	(uv run uvicorn incident_api.main:app --reload --reload-dir src --port $(API_PORT); kill 0) & \
	(API_PORT=$(API_PORT) npm --prefix web run dev -- --port $(WEB_PORT) --strictPort; kill 0) & \
	wait

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .
	npm --prefix web run lint

format: ## Fix lint and formatting where possible
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Type check
	uv run mypy
	npm --prefix web run typecheck
	npm --prefix infra run typecheck

test: ## Run the tests (offline, with a fake LLM)
	uv run pytest --cov=incident_agent --cov=incident_api --cov-fail-under=90
	npm --prefix web test
	npm --prefix infra test

web-fixtures: ## Regenerate the runs the UI tests replay, after changing the event shapes
	UPDATE_FIXTURES=1 uv run pytest tests/test_web_contract.py -q

check: lint typecheck test ## Everything CI runs

SAMPLE ?= checkout-5xx
sample: ## Run a sample incident in the terminal, e.g. make sample SAMPLE=batch-disk-full
	uv run python -m incident_agent --sample $(SAMPLE)

synth: ## Build the UI and synthesize the CloudFormation template. Touches no AWS account
	npm --prefix web run build
	npm --prefix infra run synth

deploy: ## Deploy to the AWS account you are logged in to. Costs money: read the README first
	npm --prefix web run build
	cd infra && npx cdk deploy
