.DEFAULT_GOAL := help

UV ?= uv
HOST ?= 127.0.0.1
PORT ?= 8000
PIP_AUDIT_VERSION := 2.10.1
PYTEST := $(UV) run pytest --cov=risk_ledger --cov-report=term-missing --cov-fail-under=80
PYTEST_FOCUSED := $(UV) run pytest --no-cov

.PHONY: help sync lint format format-check test test-unit test-integration test-e2e check audit run serve docker-build

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "; printf "RiskLedger targets:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## Install locked runtime and development dependencies
	$(UV) sync --locked --all-groups

lint: ## Run Ruff lint checks
	$(UV) run ruff check .

format: ## Apply Ruff formatting
	$(UV) run ruff format .

format-check: ## Verify Ruff formatting without changing files
	$(UV) run ruff format --check .

test: ## Run the full suite with the 80% coverage gate
	$(PYTEST)

test-unit: ## Run only isolated domain tests
	$(PYTEST_FOCUSED) -m unit

test-integration: ## Run only in-process HTTP integration tests
	$(PYTEST_FOCUSED) -m integration

test-e2e: ## Run only the complete local user journey
	$(PYTEST_FOCUSED) -m e2e

check: sync lint format-check test ## Run the same quality gates as CI

audit: ## Audit the frozen production dependency export (network required)
	@audit_file="$$(mktemp)"; \
		trap 'rm -f "$$audit_file"' EXIT; \
		$(UV) export --frozen --no-dev --no-emit-project --no-hashes --output-file "$$audit_file"; \
		$(UV) tool run --from pip-audit==$(PIP_AUDIT_VERSION) pip-audit --requirement "$$audit_file"

run: ## Start the local development server with reload
	$(UV) run uvicorn risk_ledger.main:app --host $(HOST) --port $(PORT) --reload

serve: ## Start the container-style server without reload
	$(UV) run uvicorn risk_ledger.main:app --host 0.0.0.0 --port $(PORT) --no-server-header

docker-build: ## Build the production-style non-root image
	docker build --tag risk-ledger:local .
