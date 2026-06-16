#!/usr/bin/env python
"""Daily refresh job — STAGE 1 (stub).

This is the engine of point-in-time history: run once per day, it snapshots the
CURRENT forward consensus estimates for each watchlist ticker WITH today's
as-of date and appends them immutably. Over time, this accumulates the
forward-estimate history the corridor/True P/E depends on. It will also refresh
prices, backfill EDGAR fundamentals, and log every gap to ingestion_log.

Not implemented in Stage 0 — the scaffold defines where it lives and what it
does so the cron/schedule story is clear up front.

Usage (once implemented):
    python scripts/daily_refresh.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    raise SystemExit(
        "daily_refresh is a Stage 1 deliverable and is not implemented yet.\n"
        "Stage 0 (scaffold) is complete: run `python scripts/init_db.py` first."
    )


if __name__ == "__main__":
    main()
