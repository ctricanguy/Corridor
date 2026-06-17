#!/usr/bin/env python
r"""OFFLINE STUDY — compare forward-quarter derivation methods for NVDA.

Run LOCALLY (needs FMP_API_KEY + network). Changes NOTHING in production — it only
reads live data and prints a comparison so you can pick the production method from
real numbers. It does two things, in order:

  1. FISCAL-YEAR ALIGNMENT first: confirms FMP and EDGAR refer to the same fiscal
     year for NVDA, BY DATE. If they don't, every derivation method is meaningless,
     so it warns loudly and the comparison below is untrustworthy.
  2. Compares three methods — flat (production), (a) yfinance-blended near quarters,
     (b) seasonality-weighted — printing for each: the 4 derived quarters, the
     forward-EPS sum, the True P/E, and the % divergence from yfinance's independent
     0q/+1q estimates. For (b) it also prints the historical Q1-Q4 EDGAR shares
     (per year + recent-weighted) so you can see if NVDA's ramp distorts them.

Usage:
    export FMP_API_KEY=...
    export SEC_EDGAR_USER_AGENT="You <you@email>"
    python scripts/compare_derivation.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.analysis.derivation_compare import (  # noqa: E402
    blend_method,
    build_method_result,
    flat_method,
    quarterly_shares_from_edgar,
    seasonality_method,
)
from corridor.config import get_settings, load_config  # noqa: E402
from corridor.datasources.edgar_source import EdgarFundamentalsSource  # noqa: E402
from corridor.datasources.fmp_source import FMPForwardEstimateSource  # noqa: E402
from corridor.datasources.yfinance_source import YFinancePriceSource  # noqa: E402
from corridor.ingest.fiscal import (  # noqa: E402
    fiscal_year_bounds,
    fiscal_year_of,
    label_period_parts,
)
from corridor.ingest.job import (  # noqa: E402
    actuals_from_fundamentals,
    build_fiscal_periods,
    check_label_alignment,
)
from corridor.ingest.window import unreported_window  # noqa: E402

TICKER = "NVDA"
NVDA_CIK = "0001045810"


def _hr(t: str = "") -> None:
    print(("== " + t + " ").ljust(78, "=") if t else "=" * 78)


def _edgar_maps(actuals: list, cal: Any) -> tuple[dict, dict]:
    """{(fy,q): eps} quarterly + {fy: eps} annual from EDGAR (earliest-filed wins)."""
    q_filed: dict = {}
    a_filed: dict = {}
    quarterly: dict = {}
    annual: dict = {}
    for f in actuals:
        if f.period_end_date is None or f.filed_date is None:
            continue
        if "Q" in f.fiscal_period:
            key = label_period_parts(f.period_end_date, cal)
            if key not in q_filed or f.filed_date < q_filed[key]:
                q_filed[key] = f.filed_date
                quarterly[key] = f.value
        else:
            fy = fiscal_year_of(f.period_end_date, cal)
            if fy not in a_filed or f.filed_date < a_filed[fy]:
                a_filed[fy] = f.filed_date
                annual[fy] = f.value
    return quarterly, annual


def main() -> int:  # noqa: C901 - linear diagnostic
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        print("FMP_API_KEY not set — this study needs a real key + network.")
        return 2
    settings = get_settings()
    config = load_config()
    cal = config.fiscal_calendars()[TICKER]
    fmp_cfg = config.data["fmp"]
    today = datetime.now(UTC).date()

    fmp = FMPForwardEstimateSource(
        api_key, {TICKER: cal}, base_url=fmp_cfg["base_url"],
        page_limit=fmp_cfg.get("estimates_page_limit", 10),
    )
    estimates = fmp.get_forward_estimates(TICKER, as_of=today)
    yfs = YFinancePriceSource()
    prices = yfs.get_prices(TICKER, start=date(today.year - 1, today.month, 1))
    latest = prices[-1]
    price = latest.close
    yf_fwd = yfs.fetch_forward_eps(TICKER)
    yf_0q, yf_1q = yf_fwd.get("0q"), yf_fwd.get("+1q")
    edgar = EdgarFundamentalsSource(settings.sec_edgar_user_agent)
    actuals = edgar.get_fundamentals(TICKER, cik=NVDA_CIK)

    reported_dates, reported_actuals = actuals_from_fundamentals(actuals, cal)

    # --- 1. ALIGNMENT FIRST -------------------------------------------------
    _hr("1. FISCAL-YEAR ALIGNMENT (by DATE — gate for the whole study)")
    aligns = check_label_alignment(actuals, cal)
    for a in aligns[-6:]:
        print(f"   {a.period_end}  EDGAR {a.edgar_label:10} vs date {a.date_label:10}  "
              f"{'ok' if a.agree else '** DRIFT **'}")
    aligned = bool(aligns) and all(a.agree for a in aligns)
    print(f"   => aligned: {aligned}" + ("" if aligned else "  (comparison UNTRUSTWORTHY)"))
    annual_ests = [e for e in estimates if e.period_type == "annual" and e.period_end_date]
    for r in sorted(annual_ests, key=lambda e: e.period_end_date)[:3]:
        fy = fiscal_year_of(r.period_end_date, cal)
        start, end = fiscal_year_bounds(fy, cal)
        inside = sorted({
            label_period_parts(f.period_end_date, cal)[1]
            for f in actuals
            if "Q" in f.fiscal_period and f.period_end_date and start <= f.period_end_date <= end
        })
        print(f"   FMP {r.fiscal_period} (end {r.period_end_date}) spans {start}..{end}"
              f"  -> EDGAR Q's inside: {inside or '(none)'}")

    # --- window -------------------------------------------------------------
    periods = build_fiscal_periods(estimates, reported_dates, cal, latest.price_date)
    window = unreported_window(periods, latest.price_date, n=4)
    annual = {r.fiscal_period: r.value for r in estimates if r.period_type == "annual"}

    # --- 2. seasonality shares ---------------------------------------------
    q_actuals, a_actuals = _edgar_maps(actuals, cal)
    shares, per_year = quarterly_shares_from_edgar(q_actuals, a_actuals)
    _hr("2. Historical Q1-Q4 EDGAR shares (does NVDA's ramp distort them?)")
    for fy in sorted(per_year):
        s = per_year[fy]
        print(f"   FY{fy}:  Q1 {s[1]:.1%}  Q2 {s[2]:.1%}  Q3 {s[3]:.1%}  Q4 {s[4]:.1%}")
    print(f"   recent-weighted -> Q1 {shares[1]:.1%}  Q2 {shares[2]:.1%}  "
          f"Q3 {shares[3]:.1%}  Q4 {shares[4]:.1%}   (flat = 25% each)")

    # --- 3. three methods ---------------------------------------------------
    flat_q, flat_ok = flat_method(window, annual, reported_actuals)
    blend_q, blend_ok = blend_method(window, annual, reported_actuals, yf_0q, yf_1q)
    seas_q, seas_ok = seasonality_method(window, annual, reported_actuals, shares)
    results = [
        build_method_result("flat (production)", flat_q, flat_ok, price, yf_0q, yf_1q),
        build_method_result("(a) yfinance-blend", blend_q, blend_ok, price, yf_0q, yf_1q,
                            note="0q/+1q sourced from yfinance -> div ~0 by construction"),
        build_method_result("(b) seasonality", seas_q, seas_ok, price, yf_0q, yf_1q),
    ]

    _hr("3. METHODS SIDE BY SIDE")
    print(f"   price={price}   yfinance 0q={yf_0q}  +1q={yf_1q}\n")
    for r in results:
        qs = "  ".join(f"{lbl}={eps:.3f}" for lbl, eps in r.quarters)
        print(f"   {r.name}")
        print(f"      quarters : {qs}")
        tpe = f"{r.true_pe:.2f}" if r.true_pe is not None else "n/a"
        print(f"      fwd sum  : {r.forward_sum:.4f}    True P/E : {tpe}    complete={r.complete}")
        d0 = f"{r.q0_div_pct:.1%}" if r.q0_div_pct is not None else "n/a"
        d1 = f"{r.q1_div_pct:.1%}" if r.q1_div_pct is not None else "n/a"
        print(f"      vs yfinance: next-Q div {d0}   +1Q div {d1}"
              f"{('   ('+r.note+')') if r.note else ''}")
    _hr()
    print("   Pick the production method from these numbers — no change made.")
    _hr()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
