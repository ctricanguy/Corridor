#!/usr/bin/env python
"""Daily refresh — snapshot today's forward estimates + price for every name.

Designed to run UNATTENDED from cron (e.g. on a Raspberry Pi):

* SELF-CONTAINED  — loads .env itself (no shell env needed) and resolves the DB to an
  ABSOLUTE path, so it works regardless of cron's working directory.
* IDEMPOTENT      — insert-or-ignore on the natural keys; running twice in a day never
  duplicates snapshots or trips the immutability triggers. A retry after a partial
  failure is safe.
* RESILIENT       — one failing ticker is isolated (committed per-ticker) and the run
  continues; every skip/gap is written to ingestion_log with a reason. The process
  exits non-zero ONLY on total failure, so cron surfaces real breakage but not a
  single flaky ticker.
* OBSERVABLE      — writes a timestamped run summary to logs/daily_refresh.log.

    python scripts/daily_refresh.py            # real run
    python scripts/daily_refresh.py --dry-run  # fetch + log, write NOTHING (test the wiring)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import PROJECT_ROOT, get_settings, load_config  # noqa: E402

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "daily_refresh.log"


def _emit(line: str, fh) -> None:  # type: ignore[no-untyped-def]
    """Write a line to BOTH stdout and the run-summary log file."""
    print(line)
    fh.write(line + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily point-in-time snapshot job.")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and log, but write NOTHING to the database")
    args = ap.parse_args()

    settings = get_settings()  # loads .env, resolves DB to an absolute path
    api_key = os.getenv("FMP_API_KEY")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stamp = datetime.now(UTC).isoformat(timespec="seconds")

    with LOG_FILE.open("a", encoding="utf-8") as fh:
        bar = "=" * 80
        _emit(bar, fh)
        _emit(f"Corridor daily_refresh — {stamp}   [dry_run={args.dry_run}]", fh)
        _emit(f"DB: {settings.database_url}", fh)
        _emit("-" * 80, fh)

        if not api_key:
            _emit("FATAL: FMP_API_KEY not set — cannot fetch estimates. Aborting.", fh)
            _emit(bar, fh)
            return 2

        try:
            results, counts, rows = _run(settings, api_key, args.dry_run, fh)
        except Exception as exc:  # total failure (DB/setup) — surface to cron
            _emit(f"FATAL: run aborted: {exc!r}", fh)
            _emit(bar, fh)
            return 1

        elapsed = time.monotonic() - started
        gaps = counts.get("price_gap", 0) + counts.get("error", 0) + counts.get("incomplete", 0)
        had_progress = any(r.status != "error" for r in results)
        exit_code = 0 if (results and had_progress) else 1

        _emit("-" * 80, fh)
        _emit("captured: " + "  ".join(f"{k}={counts.get(k, 0)}" for k in
              ("ok", "price_gap", "quarantine", "incomplete", "unsupported", "error")), fh)
        if rows is not None:
            _emit("table rows: " + "  ".join(f"{k}={v}" for k, v in rows.items()), fh)
        _emit(f"gaps logged: {gaps}   |   elapsed: {elapsed:.1f}s   |   exit: {exit_code}", fh)
        _emit(bar, fh)
        return exit_code


def _run(settings, api_key, dry_run, fh):  # type: ignore[no-untyped-def]
    from corridor.datasources.edgar_source import EdgarFundamentalsSource
    from corridor.datasources.fmp_source import FMPForwardEstimateSource
    from corridor.datasources.yfinance_source import YFinancePriceSource
    from corridor.db import init_db, session_scope
    from corridor.ingest.job import run_daily

    config = load_config()
    init_db(settings.database_url)  # create schema if missing (run_daily self-seeds securities)
    fmp_cfg = config.data["fmp"]

    price_source = YFinancePriceSource()  # retries on zero-bar returns
    estimate_source = FMPForwardEstimateSource(
        api_key, config.fiscal_calendars(), base_url=fmp_cfg["base_url"],
        page_limit=fmp_cfg.get("estimates_page_limit", 10),
        fetch_quarterly=fmp_cfg.get("fetch_quarterly", False),
    )
    fundamentals_source = EdgarFundamentalsSource(
        settings.sec_edgar_user_agent, config.fiscal_calendars()
    )

    with session_scope() as session:
        results = run_daily(
            config, price_source=price_source, estimate_source=estimate_source,
            fundamentals_source=fundamentals_source, session=session, dry_run=dry_run,
        )
        for r in results:
            detail = ""
            if r.valuation is not None:
                detail = f"True P/E {r.valuation.true_pe:.2f}  cov {r.valuation.coverage_score:.2f}"
            elif r.detail:
                detail = r.detail
            _emit(f"  {r.ticker:6} {r.status:11} {detail}", fh)
        counts = Counter(r.status for r in results)
        rows = None if dry_run else _row_counts(session)
    return results, counts, rows


def _row_counts(session):  # type: ignore[no-untyped-def]
    from corridor.db.models import (
        ForwardEstimateSnapshot,
        IngestionLog,
        PriceSnapshot,
        ValuationSnapshot,
    )

    return {
        "forward_estimate_snapshots": session.query(ForwardEstimateSnapshot).count(),
        "price_snapshots": session.query(PriceSnapshot).count(),
        "valuation_snapshots": session.query(ValuationSnapshot).count(),
        "ingestion_log": session.query(IngestionLog).count(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
