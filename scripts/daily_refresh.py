#!/usr/bin/env python
"""Daily refresh — snapshot today's forward estimates + price for every name.

This is the engine of point-in-time history: run once per (UTC) day, it appends
today's per-quarter forward consensus (immutably), the paired price, runs the
consistency + sanity pipeline, writes clean valuation inputs, and quarantines
anything impossible to ingestion_log. Over time it ACCUMULATES the forward-estimate
history the corridor depends on.

Needs network + FMP_API_KEY. The adapters are UNVALIDATED until you have run
scripts/validate_live.py. Every ticker lands in ingestion_log — no silent gaps.

Usage:
    export FMP_API_KEY=...
    python scripts/daily_refresh.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import get_settings, load_config  # noqa: E402
from corridor.datasources.edgar_source import EdgarFundamentalsSource  # noqa: E402
from corridor.datasources.fmp_source import FMPForwardEstimateSource  # noqa: E402
from corridor.datasources.yfinance_source import YFinancePriceSource  # noqa: E402
from corridor.db import init_db, session_scope  # noqa: E402
from corridor.ingest.job import run_daily  # noqa: E402


def main() -> int:
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        print("FMP_API_KEY not set — forward estimates cannot be fetched. Aborting.")
        return 2

    settings = get_settings()
    config = load_config()
    init_db(settings.database_url)

    fmp_cfg = config.data["fmp"]
    price_source = YFinancePriceSource()
    estimate_source = FMPForwardEstimateSource(
        api_key,
        config.fiscal_calendars(),
        base_url=fmp_cfg["base_url"],
        page_limit=fmp_cfg.get("estimates_page_limit", 100),
    )
    fundamentals_source = EdgarFundamentalsSource(
        settings.sec_edgar_user_agent, config.fiscal_calendars()
    )

    print("Adapters are UNVALIDATED against live endpoints — run validate_live.py first.")
    with session_scope() as session:
        results = run_daily(
            config,
            price_source=price_source,
            estimate_source=estimate_source,
            fundamentals_source=fundamentals_source,
            session=session,
        )

    counts = Counter(r.status for r in results)
    print("\nDaily refresh summary:")
    for status, n in sorted(counts.items()):
        print(f"  {status:12s}: {n}")
    print("See the ingestion_log table for per-ticker detail (including quarantines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
