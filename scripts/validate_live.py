#!/usr/bin/env python
r"""LIVE validation for NVDA — run this LOCALLY (needs network + FMP_API_KEY).

This is the one-command step that turns the UNVALIDATED adapters into trusted ones.
It does three things:

  1. Fetches NVDA from FMP (quarterly + annual estimates), yfinance (price), and
     EDGAR (realized diluted EPS).
  2. Asserts the LIVE parsed records match the SAME shape contract the fixtures
     satisfy (corridor.datasources.shape). A renamed/absent provider field — the
     single most likely real-world break — fails loudly here, naming the field.
  3. Prints every intermediate value in the same format as the synthetic worked
     example, then computes the REAL True P/E so you can hand-verify it.

It will NOT run in the build sandbox (outbound network is blocked). Run it on your
machine:

    export FMP_API_KEY=...                 # your key
    export SEC_EDGAR_USER_AGENT="You <you@email>"
    python scripts/validate_live.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import get_settings, load_config  # noqa: E402
from corridor.datasources.edgar_source import EdgarFundamentalsSource  # noqa: E402
from corridor.datasources.fmp_source import FMPForwardEstimateSource  # noqa: E402
from corridor.datasources.shape import ShapeMismatch, assert_records_shape  # noqa: E402
from corridor.datasources.yfinance_source import YFinancePriceSource  # noqa: E402
from corridor.ingest.job import (  # noqa: E402
    actuals_from_fundamentals,
    assemble_valuation,
    build_fiscal_periods,
)
from corridor.ingest.records import PricePoint  # noqa: E402

TICKER = "NVDA"
NVDA_CIK = "0001045810"  # SEC Central Index Key for NVIDIA Corp


def _hr(t: str = "") -> None:
    print(("== " + t + " ").ljust(74, "=") if t else "=" * 74)


def main() -> int:
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        print("FMP_API_KEY is not set. This script needs a real key + network.")
        print("  export FMP_API_KEY=...   then re-run.  (See README 'live validation'.)")
        return 2

    settings = get_settings()
    config = load_config()
    cal = config.fiscal_calendars().get(TICKER)
    if cal is None:
        print(f"No fiscal calendar configured for {TICKER}.")
        return 2
    today = datetime.now(UTC).date()

    _hr()
    print(f"  LIVE VALIDATION — {TICKER} — {today} (UTC)")
    _hr()

    # 1. FMP forward estimates --------------------------------------------------
    fmp = FMPForwardEstimateSource(api_key, {TICKER: cal}, base_url=config.data["fmp"]["base_url"])
    estimates = fmp.get_forward_estimates(TICKER, as_of=today)
    _check_shape("FMP estimates", estimates)

    # 2. yfinance price ---------------------------------------------------------
    yfs = YFinancePriceSource()
    start = date(today.year - 1, today.month, 1)
    prices = yfs.get_prices(TICKER, start=start)
    _check_shape("yfinance prices", prices)
    latest = prices[-1]
    fmp_cross = fmp.fetch_price(TICKER, latest.price_date)

    # 3. EDGAR actuals ----------------------------------------------------------
    edgar = EdgarFundamentalsSource(settings.sec_edgar_user_agent)
    actuals = edgar.get_fundamentals(TICKER, cik=NVDA_CIK)
    _check_shape("EDGAR actuals", actuals)

    # Derive confirmed report dates AND reported quarterly actuals from EDGAR.
    reported, reported_actuals = actuals_from_fundamentals(actuals)
    periods = build_fiscal_periods(estimates, reported, cal, latest.price_date)

    _hr("Pairing price with estimate (same observation date)")
    point = PricePoint(
        price_date=latest.price_date, raw_close=latest.close or 0.0, adj_close=latest.adj_close,
        split_ratio=latest.split_ratio, currency=latest.currency, source=latest.source,
    )
    # Estimates were observed today; pair with today's price for alignment.
    result = assemble_valuation(
        ticker=TICKER, as_of=latest.price_date, price_point=point,
        report_currency=estimates[0].currency, estimate_records=estimates,
        fiscal_periods=periods, reported_actuals=reported_actuals,
        crosscheck_price_fmp=fmp_cross, thresholds=config.thresholds,
    )

    _hr("RESULT")
    print(f"  status             = {result.status}")
    if result.valuation is not None:
        v = result.valuation
        print(f"  price (raw)        = {v.price:.4f} {v.price_currency}")
        print(f"  forward EPS sum    = {v.forward_eps_sum:.4f}")
        print(f"  construction_method= {v.construction_method}")
        print(f"  coverage_score     = {v.coverage_score:.2f}")
        print(f"  REAL True P/E      = {v.true_pe:.4f}")
        if v.price_disagreement_flag:
            print(f"  ! price disagreement yf={v.price_yf} fmp={v.price_fmp}")
    else:
        print(f"  reason_code        = {result.reason_code}")
        print(f"  detail             = {result.detail}")
    _hr()
    print("  Shapes matched the fixture contract — adapters validated against live.")
    print("  You may now flip the 'validated' flags / README note for these sources.")
    _hr()
    return 0


def _check_shape(label: str, records: list) -> None:
    try:
        assert_records_shape(records)
        print(f"[shape OK]  {label}: {len(records)} records match the fixture contract")
    except ShapeMismatch as exc:
        print(f"[SHAPE MISMATCH]  {label}: {exc}")
        print("  -> A provider field likely changed. Fix the adapter parse before trusting it.")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
