"""Tests for the ANALYSIS-ONLY derivation comparison (flat / blend / seasonality).

Note the structural finding these lock in: within a fiscal year all three methods
preserve the FY total (= annual − reported actuals), so the forward SUM differs
between methods only through next-FY quarters. The methods mainly reshape the
PER-QUARTER values that drive the yfinance next-quarter cross-check.
"""

from __future__ import annotations

from datetime import date

import pytest

from corridor.analysis.derivation_compare import (
    blend_method,
    build_method_result,
    flat_method,
    quarterly_shares_from_edgar,
    seasonality_method,
)
from corridor.ingest.records import FiscalPeriod, WindowResult

# NVDA-style: FY2027 Q1 reported; window = FY2027 Q2/Q3/Q4 + FY2028 Q1.
WINDOW = WindowResult(
    periods=[
        FiscalPeriod("FY2027Q2", date(2026, 7, 27), date(2026, 8, 27)),
        FiscalPeriod("FY2027Q3", date(2026, 10, 26), date(2026, 11, 19)),
        FiscalPeriod("FY2027Q4", date(2027, 1, 31), date(2027, 2, 25)),
        FiscalPeriod("FY2028Q1", date(2027, 4, 25), date(2027, 5, 27)),
    ],
    requested=4, available=4,
)
ANNUAL = {"FY2027": 4.40, "FY2028": 6.00}
ACTUALS = {"FY2027Q1": 0.80}


def test_flat_even_spread() -> None:
    quarters, complete = flat_method(WINDOW, ANNUAL, ACTUALS)
    assert complete
    # FY2027 residual 3.60 / 3 unknown = 1.20 each; FY2028Q1 = 6.00/4 = 1.50.
    assert [round(e, 4) for _, e in quarters] == [1.20, 1.20, 1.20, 1.50]
    assert sum(e for _, e in quarters) == pytest.approx(5.10)


def test_blend_uses_yfinance_for_near_quarters() -> None:
    quarters, _ = blend_method(WINDOW, ANNUAL, ACTUALS, yf_0q=1.10, yf_1q=1.30)
    by = dict(quarters)
    assert by["FY2027Q2"] == pytest.approx(1.10)  # from yfinance 0q
    assert by["FY2027Q3"] == pytest.approx(1.30)  # from yfinance +1q
    # Q4 absorbs so FY2027 still totals annual−actual: (4.40-0.80-1.10-1.30)=1.20.
    assert by["FY2027Q4"] == pytest.approx(1.20)
    # Sum unchanged vs flat (FY total is preserved); only the SHAPE moved.
    assert sum(e for _, e in quarters) == pytest.approx(5.10)


def test_seasonality_distributes_by_share() -> None:
    shares = {1: 0.15, 2: 0.20, 3: 0.25, 4: 0.40}
    quarters, complete = seasonality_method(WINDOW, ANNUAL, ACTUALS, shares)
    assert complete
    by = dict(quarters)
    # FY2027 unknown = Q2,Q3,Q4; residual 3.60 split by shares (denom 0.85).
    assert by["FY2027Q2"] == pytest.approx(3.60 * 0.20 / 0.85)
    assert by["FY2027Q4"] == pytest.approx(3.60 * 0.40 / 0.85)
    # FY2028 fully unknown; Q1 = 6.00 * 0.15 (denom = 1.0).
    assert by["FY2028Q1"] == pytest.approx(0.90)
    # FY2027 quarters still total the residual 3.60 (FY total preserved).
    assert by["FY2027Q2"] + by["FY2027Q3"] + by["FY2027Q4"] == pytest.approx(3.60)


def test_shares_from_edgar_infers_q4_and_recent_weights() -> None:
    quarterly = {(2025, 1): 0.5, (2025, 2): 0.7, (2025, 3): 0.9}
    annual = {2025: 4.0}  # Q4 inferred = 4.0 - 0.5 - 0.7 - 0.9 = 1.9
    shares, per_year = quarterly_shares_from_edgar(quarterly, annual)
    assert per_year[2025][4] == pytest.approx(1.9 / 4.0)
    assert shares == pytest.approx({1: 0.125, 2: 0.175, 3: 0.225, 4: 0.475})


def test_method_result_divergence_vs_yfinance() -> None:
    quarters = [("FY2027Q2", 1.20), ("FY2027Q3", 1.20), ("FY2027Q4", 1.20), ("FY2028Q1", 1.50)]
    r = build_method_result("flat", quarters, True, price=94.0, yf_0q=1.10, yf_1q=1.30)
    assert r.forward_sum == pytest.approx(5.10)
    assert r.true_pe == pytest.approx(94.0 / 5.10)
    assert r.q0_div_pct == pytest.approx(abs(1.20 - 1.10) / 1.10)  # next-quarter divergence
    assert r.q1_div_pct == pytest.approx(abs(1.20 - 1.30) / 1.30)
