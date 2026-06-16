.PHONY: install init-db schema test lint typecheck app refresh clean

# Set up the environment (uses uv if available; falls back to venv + pip).
install:
	@if command -v uv >/dev/null 2>&1; then \
		uv venv && uv pip install -e ".[dev]"; \
	else \
		python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"; \
	fi

# Create the SQLite schema + immutability guards and seed the watchlist.
init-db:
	python scripts/init_db.py

# Print the generated DDL for review (no DB write).
schema:
	python scripts/dump_schema.py

test:
	pytest

lint:
	ruff check .

typecheck:
	mypy

# Launch the Streamlit dashboard (Stage 4).
app:
	streamlit run app/dashboard.py

# Daily point-in-time snapshot job (Stage 1).
refresh:
	python scripts/daily_refresh.py

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache **/__pycache__
