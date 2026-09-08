# Developer shortcuts. Run `make help` for the list.
.DEFAULT_GOAL := help
PY ?= python

.PHONY: help install dev run test test-all test-unit test-cov lint format typecheck check docker docker-down seed apikey clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime dependencies
	$(PY) -m pip install -e .

dev:  ## Install runtime + development dependencies
	$(PY) -m pip install -e ".[dev]"

run:  ## Run the API with reload
	uvicorn app.main:app --reload

test:  ## Run the default suite (fast, no credentials)
	pytest

test-all:  ## Run everything including the subprocess suite
	pytest -m "not live"

test-unit:  ## Run unit tests only
	pytest -m unit

test-cov:  ## Run tests with a coverage report
	pytest --cov=app --cov=mcp_servers --cov-report=term-missing --cov-report=html

lint:  ## Check formatting and lint rules
	ruff check app mcp_servers tests
	ruff format --check app mcp_servers tests

format:  ## Auto-format and fix what can be fixed
	ruff check app mcp_servers tests --fix
	ruff format app mcp_servers tests

typecheck:  ## Run mypy
	mypy app mcp_servers

check: lint test  ## Lint then test -- what CI runs

docker:  ## Build and start the full stack
	docker compose up --build

docker-down:  ## Stop the stack and remove volumes
	docker compose down -v

seed:  ## Insert demo business data
	$(PY) -c "import asyncio; from app.db.seed import seed_demo_data; asyncio.run(seed_demo_data())"

apikey:  ## Create a user and print an API key (EMAIL=... ROLE=operator)
	$(PY) scripts/create_api_key.py --email $(EMAIL) --role $(or $(ROLE),operator)

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
