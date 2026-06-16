#!/usr/bin/env python
"""Initialize the Corridor SQLite database.

Creates all tables and installs the point-in-time immutability guards, then
seeds the `securities` table from config.yaml's watchlist. Idempotent — safe to
re-run; existing securities are left untouched.

Usage:
    python scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import get_settings, load_config  # noqa: E402
from corridor.db import init_db, session_scope  # noqa: E402
from corridor.db.models import Security  # noqa: E402


def main() -> None:
    settings = get_settings()
    config = load_config()

    print(f"Database URL : {settings.database_url}")
    print(f"Snapshot dir : {settings.snapshot_path}")
    print(f"Watchlist    : {', '.join(config.tickers)}")

    init_db(settings.database_url)
    print("Schema created + immutability guards installed.")

    seeded = 0
    with session_scope() as session:
        existing = {row[0] for row in session.query(Security.ticker).all()}
        for spec in config.universe:
            if spec.ticker not in existing:
                session.add(Security(ticker=spec.ticker, name=spec.name, cik=spec.cik))
                seeded += 1
    print(f"Seeded {seeded} new securities ({len(config.universe) - seeded} already present).")
    print("Done.")


if __name__ == "__main__":
    main()
