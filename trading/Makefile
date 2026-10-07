# ATLAS — convenience targets for macOS and Linux.
# On Windows use the PowerShell scripts in scripts/ instead.
#
#   make setup      install everything
#   make backend    run the API
#   make frontend   run the dashboard
#   make test       run the test suite
#   make lint       check formatting and lint
#   make format     apply formatting
#   make check      lint + typecheck + test  (run this before committing)

VENV    := backend/.venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
RUFF    := $(VENV)/bin/ruff

.PHONY: help setup backend frontend test test-cov lint format typecheck check \
        build clean kill-switch release-switch

help:
	@grep -E '^#   make' Makefile | sed 's/^#   /  /'

# --- setup -------------------------------------------------------------------

setup: $(VENV) node_modules .env
	@echo ""
	@echo "  Setup complete."
	@echo "  Put your Alpaca PAPER keys in .env, then: make backend"
	@echo ""

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r backend/requirements-dev.txt

node_modules:
	cd frontend && npm install --no-fund --no-audit

.env:
	cp .env.example .env
	@echo "  Created .env from the template."

# --- run ---------------------------------------------------------------------

backend:
	cd backend && ../$(VENV)/bin/python -m uvicorn app.main:app \
		--host 127.0.0.1 --port 8000 --reload

frontend:
	cd frontend && npm run dev

# --- quality -----------------------------------------------------------------

test:
	cd backend && ../$(VENV)/bin/python -m pytest -q

test-cov:
	cd backend && ../$(VENV)/bin/python -m pytest \
		--cov=app --cov-report=term-missing

lint:
	cd backend && ../$(RUFF) check app tests
	cd backend && ../$(RUFF) format --check app tests

format:
	cd backend && ../$(RUFF) check --fix app tests
	cd backend && ../$(RUFF) format app tests

typecheck:
	cd frontend && npm run typecheck

check: lint typecheck test
	@echo ""
	@echo "  All checks passed."
	@echo ""

build:
	cd frontend && npm run build

# --- operations --------------------------------------------------------------

kill-switch:
	@mkdir -p data
	@printf 'ATLAS KILL SWITCH\nengaged_at: %s\ntriggered_by: make\nreason: engaged from the command line\n\nDelete this file to allow trading again.\n' \
		"$$(date -u +%Y-%m-%dT%H:%M:%SZ)" > data/KILL_SWITCH
	@echo "  KILL SWITCH ENGAGED. All new orders are blocked."

release-switch:
	@rm -f data/KILL_SWITCH
	@echo "  Kill switch released."

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.coverage
	rm -rf frontend/dist frontend/.vite
	@echo "  Cleaned build artefacts. The database and logs were left alone."
