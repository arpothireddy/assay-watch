.PHONY: help install lint format typecheck test check crawl dry-run backfill-check up down migrate backup restore-check

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install all deps
	uv sync

lint: ## Ruff lint
	uv run ruff check src tests

format: ## Ruff format (write)
	uv run ruff format src tests

typecheck: ## mypy --strict
	uv run mypy src tests

test: ## Run tests with coverage
	uv run pytest

check: lint typecheck test ## Everything CI runs

crawl: ## Run a real crawl
	uv run assay crawl

dry-run: ## Fetch + parse, write nothing
	uv run assay crawl --dry-run

backfill-check: ## Per-reference days-of-history report
	uv run assay backfill-check

up: ## Start Postgres (loopback only)
	docker compose up -d db

down: ## Stop the stack
	docker compose down

migrate: ## Apply migrations
	uv run alembic upgrade head

backup: ## Nightly pg_dump to ./backups
	./scripts/backup.sh

restore-check: ## Verify the latest dump restores into a throwaway database
	./scripts/restore-check.sh
