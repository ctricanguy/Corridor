#!/usr/bin/env python
r"""LIVE validation for NVDA — run this LOCALLY (needs network + FMP_API_KEY).

The one-command step that turns the UNVALIDATED adapters into trusted ones. It:

  1. Fetches NVDA from FMP (/stable ANNUAL estimates — the v1 source on Starter;
     period=quarter is Premium-gated), yfinance (price + a next-quarter EPS
     cross-check), and EDGAR (realized diluted EPS).
  2. Asserts the LIVE parsed records match the SAME shape contract the fixtures
     satisfy. A renamed/absent provider field fails loudly here, naming the field.
  3. Prints the FULL annual-path breakdown so you can hand-verify the number:
     the multi-year annual curve, EDGAR actuals subtracted for reported quarters,
     each derived quarter, the forward-EPS sum + construction_method + coverage,
     the yfinance quarterly cross-check, the paired price, and the True P/E.

It will NOT run in the build sandbox (outbound network is blocked). Run it on your
machine:

    export FMP_API_KEY=...                 # your Starter key
    export SEC_EDGAR_USER_AGENT="You <you@email>"
    python scripts/validate_live.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import get_settings, load_config  # noqa: E402
from corridor.datasources.edgar_source import EdgarFundamentalsSource  # noqa: E402
from corridor.datasources.fmp_source import FMPForwardEstimateSource  # noqa: E402
from corridor.datasources.shape import ShapeMismatch, assert_records_shape  # noqa: E402
from corridor.datasources.yfinance_source import YFinancePriceSource  # noqa: E402
from corridor.ingest.fiscal import fiscal_year_bounds, fiscal_year_of  # noqa: E402
from corridor.ingest.job import (  # noqa: E402
    actuals_from_fundamentals,
    assemble_valuation,
    build_fiscal_periods,
    check_label_alignment,
)
from corridor.ingest.records import PricePoint  # noqa: E402

TICKER = "NVDA"
NVDA_CIK = "0001045810"  # SEC Central Index Key for NVIDIA Corp


def _hr(t: str = "") -> None:
    print(("== " + t + " ").ljust(74, "=") if t else "=" * 74)


def main() -> int:  # noqa: C901 - linear top-to-bottom diagnostic
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
    fmp_cfg = config.data["fmp"]

    _hr()
    print(f"  LIVE VALIDATION (annual path) — {TICKER} — {today} (UTC)")
    _hr()

    # 1. FMP ANNUAL estimates (multi-year curve; Starter, limit<=10) -------------
    fmp = FMPForwardEstimateSource(
        api_key, {TICKER: cal}, base_url=fmp_cfg["base_url"],
        page_limit=fmp_cfg.get("estimates_page_limit", 10),
        fetch_quarterly=fmp_cfg.get("fetch_quarterly", False),
    )
    estimates = fmp.get_forward_estimates(TICKER, as_of=today)
    _check_shape("FMP annual estimates", estimates)

    # 2. yfinance price + forward-EPS cross-check --------------------------------
    yfs = YFinancePriceSource()
    prices = yfs.get_prices(TICKER, start=date(today.year - 1, today.month, 1))
    _check_shape("yfinance prices", prices)
    latest = prices[-1]
    fmp_cross = fmp.fetch_price(TICKER, latest.price_date)
    yf_fwd = yfs.fetch_forward_eps(TICKER)
    yf_next_q = yf_fwd.get("0q")

    # 3. EDGAR actuals ----------------------------------------------------------
    edgar = EdgarFundamentalsSource(settings.sec_edgar_user_agent, config.fiscal_calendars())
    actuals = edgar.get_fundamentals(TICKER, cik=NVDA_CIK)
    _check_shape("EDGAR actuals", actuals)
    reported, reported_actuals = actuals_from_fundamentals(actuals, cal)
    _print_fy_alignment(estimates, actuals, cal)  # critical FY-label verification
    periods = build_fiscal_periods(estimates, reported, cal, latest.price_date)

    result = assemble_valuation(
        ticker=TICKER, as_of=latest.price_date,
        price_point=PricePoint(
            price_date=latest.price_date, raw_close=latest.close or 0.0,
            adj_close=latest.adj_close, split_ratio=latest.split_ratio,
            currency=latest.currency, source=latest.source,
        ),
        report_currency=estimates[0].currency, estimate_records=estimates,
        fiscal_periods=periods, reported_actuals=reported_actuals,
        crosscheck_price_fmp=fmp_cross, yf_next_quarter_eps=yf_next_q,
        thresholds=config.thresholds,
    )

    _print_breakdown(estimates, reported_actuals, yf_fwd, latest, result)
    return 0 if result.status == "ok" else 1


def _print_fy_alignment(estimates: list, edgar_actuals: list, cal: Any) -> None:
    """Verify FMP and EDGAR refer to the SAME fiscal year — by DATE, not by label."""
    _hr("0. FISCAL-YEAR ALIGNMENT (by DATE — the critical check)")
    aligns = check_label_alignment(edgar_actuals, cal)
    print("  EDGAR quarter   period_end    EDGAR label   date-derived   match?")
    for a in aligns:
        mark = "ok" if a.agree else "** DRIFT **"
        print(f"    {a.period_end}   {a.edgar_label:11}  {a.date_label:11}  {mark}")
    if aligns and all(a.agree for a in aligns):
        print("  => EDGAR's FY labels equal our date-derived labels: NO off-by-one.")
    elif aligns:
        print("  => Convention DRIFT — but actuals are matched by DATE, so still correct.")
    else:
        print("  => No EDGAR quarterly actuals parsed (check CIK / User-Agent).")

    print("\n  Each FMP annual -> the EDGAR actuals inside its fiscal-year DATE bounds:")
    by_date = [(a.period_end, a.edgar_label) for a in aligns]
    annuals = [e for e in estimates if e.period_type == "annual" and e.period_end_date]
    for r in sorted(annuals, key=lambda e: e.period_end_date):
        fy = fiscal_year_of(r.period_end_date, cal)
        start, end = fiscal_year_bounds(fy, cal)
        matched = [lbl for (pe, lbl) in by_date if start <= pe <= end]
        print(f"    {r.fiscal_period} (end {r.period_end_date}) spans {start}..{end}"
              f"  -> actuals: {matched or '(none reported yet)'}")
    _hr()


def _print_breakdown(
    estimates: list, reported_actuals: dict, yf_fwd: dict, latest: Any, result: Any
) -> None:
    _hr("1. FMP annual estimates (REAL, multi-year curve)")
    for r in sorted(estimates, key=lambda r: r.period_end_date or date.min):
        print(f"  {r.fiscal_period}  EPS={r.value:.4f}  period_end={r.period_end_date}  "
              f"analysts={r.num_analysts}")

    _hr("2. EDGAR reported actuals (subtracted from the current FY annual)")
    if reported_actuals:
        for fp, val in sorted(reported_actuals.items()):
            print(f"  {fp}  actual EPS={val:.4f}")
    else:
        print("  (none parsed — derivation will treat all current-FY quarters as unknown)")

    if result.valuation is None:
        _hr("RESULT")
        print(f"  status      = {result.status}")
        print(f"  reason_code = {result.reason_code}")
        print(f"  detail      = {result.detail}")
        _hr()
        return

    v = result.valuation
    _hr("3. Forward-EPS sum (every quarter DERIVED from the annual curve)")
    for c in v.components:
        kind = "DERIVED" if c.is_derived else "real   "
        print(f"  {c.fiscal_period}  {kind}  EPS={c.value:7.4f}   [{c.detail}]")
    print(f"\n  forward EPS sum     = {v.forward_eps_sum:.4f}")
    print(f"  construction_method = {v.construction_method}")
    print(f"  coverage_score      = {v.coverage_score:.2f}  (0.00 = all derived-from-annual)")

    _hr("4. yfinance quarterly cross-check (granularity FMP annual lacks)")
    print(f"  yfinance forward EPS by period : {yf_fwd or '(unavailable)'}")
    derived_next_q = v.components[0].value if v.components else None
    print(f"  our derived next-quarter EPS   : "
          f"{derived_next_q:.4f}" if derived_next_q is not None else "  (none)")
    print(f"  yfinance next-quarter EPS      : "
          f"{v.yf_next_q_eps if v.yf_next_q_eps is not None else '(none)'}")
    if v.quarterly_xcheck_divergence_pct is not None:
        flag = "  <-- DISAGREEMENT" if v.quarterly_xcheck_flag else ""
        print(f"  divergence                     : {v.quarterly_xcheck_divergence_pct:.2%}{flag}")

    _hr("5. Paired price + True P/E")
    print(f"  price (raw)  = {v.price:.4f} {v.price_currency}  on {latest.price_date}")
    if v.price_disagreement_flag:
        print(f"  ! price disagreement: yfinance={v.price_yf} fmp={v.price_fmp}")
    print(f"  True P/E     = {v.price:.4f} / {v.forward_eps_sum:.4f} = {v.true_pe:.4f}")
    _hr()
    print("  Shapes matched the fixture contract — adapters validated against live.")
    print("  coverage_score is 0.00 by design on the annual path (every quarter derived).")
    _hr()


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
