#!/usr/bin/env python
r"""SYNTHETIC NVDA True P/E worked example — validates the MATH end to end.

  !!!  THE NUMBERS BELOW ARE SYNTHETIC / ILLUSTRATIVE — NOT LIVE MARKET DATA.  !!!
  !!!  This exists ONLY to hand-verify the pipeline arithmetic. For a REAL,    !!!
  !!!  hand-verifiable NVDA number, run scripts/validate_live.py (needs a key  !!!
  !!!  + network).                                                              !!!

It drives the ACTUAL pipeline functions (unreported_window -> build_forward_eps_sum
-> compute_true_pe -> assemble_valuation), so what you verify here is the real code
path, just with clean made-up inputs. The scenario deliberately mixes 2 real
quarterly estimates with 2 derived-from-annual quarters to exercise the
construction_method + coverage logic.

Run:  python scripts/nvda_worked_example.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.datasources.base import ForwardEstimateRecord  # noqa: E402
from corridor.ingest.consistency import compute_true_pe  # noqa: E402
from corridor.ingest.fiscal import FiscalCalendar  # noqa: E402
from corridor.ingest.forward_sum import build_forward_eps_sum  # noqa: E402
from corridor.ingest.job import assemble_valuation  # noqa: E402
from corridor.ingest.records import FiscalPeriod, PricePoint  # noqa: E402
from corridor.ingest.window import unreported_window  # noqa: E402

# --- SYNTHETIC inputs -------------------------------------------------------
TICKER = "NVDA"
AS_OF = date(2025, 6, 16)  # made-up observation date
NVDA_CAL = FiscalCalendar(fy_end_month=1, fy_end_day=31, note="FY ends late January")

# NVDA-style fiscal quarters (off-calendar: end Apr/Jul/Oct/Jan). FY2026Q1 already
# reported (confirmed) as of AS_OF, so the window must roll past it.
PERIODS = [
    FiscalPeriod("FY2026Q1", date(2025, 4, 27), date(2025, 5, 28), confirmed=True),   # reported
    FiscalPeriod("FY2026Q2", date(2025, 7, 27), date(2025, 8, 27), confirmed=False),
    FiscalPeriod("FY2026Q3", date(2025, 10, 26), date(2025, 11, 19), confirmed=False),
    FiscalPeriod("FY2026Q4", date(2026, 1, 25), date(2026, 2, 25), confirmed=False),
    FiscalPeriod("FY2027Q1", date(2026, 4, 26), date(2026, 5, 27), confirmed=False),
]

# FY2026Q1 has already REPORTED — its EDGAR actual must be subtracted from the
# FY2026 annual and EXCLUDED from the derivation divisor (so FY2026Q4 divides by 1,
# not 2). This is the correctness check on the derived-from-annual path.
REPORTED_ACTUALS = {"FY2026Q1": 0.80}  # synthetic reported actual (EDGAR)

# Provider estimates as snapshotted on AS_OF: 2 real quarterly, then only annuals
# for the back half (so Q4 + next-FY Q1 are DERIVED).
QUARTERLY = {"FY2026Q2": 1.00, "FY2026Q3": 1.20}  # real per-quarter consensus
ANNUAL = {"FY2026": 4.40, "FY2027": 6.00}          # annual consensus (for derivation)

# Synthetic price paired with the SAME date as the estimate observation.
# Chosen so the final True P/E lands on a clean 25.0 (127.50 / 5.10).
PRICE = PricePoint(
    price_date=AS_OF, raw_close=127.50, adj_close=127.50, split_ratio=1.0,
    currency="USD", source="synthetic",
)


def _hr(title: str = "") -> None:
    print(("== " + title + " ").ljust(74, "=") if title else "=" * 74)


def main() -> None:
    _hr()
    print("  SYNTHETIC NVDA True P/E WORKED EXAMPLE  —  NOT LIVE DATA")
    print("  (validates pipeline arithmetic only; bands/signals are Stage 2)")
    _hr()
    print(f"Ticker            : {TICKER}")
    print(f"As-of date        : {AS_OF}  (estimate observation date)")
    print(f"Fiscal calendar   : FY ends month {NVDA_CAL.fy_end_month} — {NVDA_CAL.note}")
    print("Price basis       : RAW contemporaneous close (split-consistent)")
    print("EPS basis         : adjusted_diluted (non-GAAP consensus)")

    _hr("1. Fiscal periods (company calendar, NOT calendar quarters)")
    for p in PERIODS:
        reported = "REPORTED" if p.is_reported_as_of(AS_OF) else "unreported"
        print(f"  {p.fiscal_period}  end={p.period_end_date}  report={p.report_date}  "
              f"confirmed={p.confirmed!s:5}  -> {reported} as of {AS_OF}")

    _hr("2. Next-4 UNREPORTED window (rolls past confirmed-reported quarters)")
    window = unreported_window(PERIODS, AS_OF, n=4)
    for p in window.periods:
        print(f"  {p.fiscal_period}  (fiscal period end {p.period_end_date})")
    print(f"  window complete : {window.complete}  (4 of {window.available} unreported)")

    _hr("3. Forward-EPS sum (real quarterly preferred; else derive from annual)")
    print(f"  reported actuals (EDGAR) : {REPORTED_ACTUALS}")
    print(f"  real quarterly estimates : {QUARTERLY}")
    print(f"  annual estimates         : {ANNUAL}")
    fwd = build_forward_eps_sum(window, QUARTERLY, ANNUAL, REPORTED_ACTUALS)
    print()
    for c in fwd.components:
        kind = "DERIVED" if c.is_derived else "real   "
        print(f"  {c.fiscal_period}  {kind}  EPS={c.value:6.4f}   [{c.detail}]")
    print()
    print(f"  forward EPS sum    = {' + '.join(f'{c.value:.4f}' for c in fwd.components)}")
    print(f"                     = {fwd.value:.4f}")
    print(f"  construction_method= {fwd.construction_method}")
    print(f"  coverage_score     = {fwd.coverage_score:.2f}  "
          f"({fwd.n_real} real / {len(fwd.components)} quarters)")

    _hr("4. Paired price + consistency checks")
    print(f"  price date         = {PRICE.price_date}  (== as-of? "
          f"{PRICE.price_date == AS_OF})")
    print(f"  raw close          = {PRICE.raw_close:.4f} {PRICE.currency}")
    print(f"  split_ratio        = {PRICE.split_ratio}  (1.0 => no split today)")
    print(f"  currency match     = report USD vs price {PRICE.currency} -> OK")

    _hr("5. True P/E")
    true_pe = compute_true_pe(PRICE.raw_close, fwd.value)
    print("  True P/E = raw price / forward EPS sum")
    print(f"          = {PRICE.raw_close:.4f} / {fwd.value:.4f}")
    print(f"          = {true_pe:.4f}")

    # Confirm the orchestrator returns the identical number via the real path.
    result = assemble_valuation(
        ticker=TICKER, as_of=AS_OF, price_point=PRICE, report_currency="USD",
        estimate_records=_as_records(), fiscal_periods=PERIODS,
        reported_actuals=REPORTED_ACTUALS,
    )
    _hr("6. Full-pipeline cross-check (assemble_valuation)")
    print(f"  status             = {result.status}")
    assert result.valuation is not None
    print(f"  True P/E (pipeline)= {result.valuation.true_pe:.4f}  "
          f"(matches step 5: {abs(result.valuation.true_pe - true_pe) < 1e-9})")
    print(f"  coverage_score     = {result.valuation.coverage_score:.2f}")
    _hr()
    print("  SYNTHETIC — do not treat 25.00 as a real NVDA multiple.")
    print("  Real, hand-verifiable number: scripts/validate_live.py (key + network).")
    _hr()


def _as_records() -> list[ForwardEstimateRecord]:
    """The synthetic estimates as ForwardEstimateRecords (what an adapter emits)."""
    recs: list[ForwardEstimateRecord] = []
    label_to_end = {p.fiscal_period: p.period_end_date for p in PERIODS}
    for label, eps in QUARTERLY.items():
        recs.append(ForwardEstimateRecord(
            ticker=TICKER, as_of_date=AS_OF, period_type="quarter", fiscal_period=label,
            period_end_date=label_to_end.get(label), metric="eps", value=eps,
            num_analysts=40, source="synthetic"))
    for fy, eps in ANNUAL.items():
        recs.append(ForwardEstimateRecord(
            ticker=TICKER, as_of_date=AS_OF, period_type="annual", fiscal_period=fy,
            period_end_date=None, metric="eps", value=eps, num_analysts=40,
            source="synthetic", construction_method="provider_annual"))
    return recs


if __name__ == "__main__":
    main()
