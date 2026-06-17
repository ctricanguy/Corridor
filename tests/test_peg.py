"""Forward PEG — hand-checkable numbers (Factor 2b)."""

from __future__ import annotations

from datetime import date

import pytest

from corridor.datasources.base import FundamentalRecord
from corridor.engine.peg import (
    forward_peg,
    fwd_cagr_growth,
    ntm_vs_ltm_growth,
    quarterly_actuals_from_edgar,
    trailing_ltm_eps,
)
from corridor.ingest.fiscal import FiscalCalendar

NVDA_CAL = FiscalCalendar(fy_end_month=1)


def test_ntm_vs_ltm_growth() -> None:
    assert ntm_vs_ltm_growth(6.0, 5.0) == (pytest.approx(0.2), None)  # +20%
    rate, reason = ntm_vs_ltm_growth(6.0, 0.0)  # non-positive LTM
    assert rate is None and "LTM" in reason


def test_fwd_cagr_growth() -> None:
    # 1.2^3 = 1.728, so 8.64/5 = 1.728 -> 20% CAGR over 3 years.
    assert fwd_cagr_growth(5.0, 8.64, 3) == (pytest.approx(0.2), None)
    rate, reason = fwd_cagr_growth(0.0, 8.64, 3)
    assert rate is None and reason is not None


def test_forward_peg_fair_cheap_rich() -> None:
    fair = forward_peg(24.0, 6.0, 0.2, "ntm_vs_ltm")  # 24 / (0.2*100) = 1.2
    assert fair.forward_peg == pytest.approx(1.2)
    assert fair.context == "fair" and not fair.suppressed
    assert forward_peg(15.0, 6.0, 0.2, "ntm_vs_ltm").context.startswith("cheap")  # 0.75
    assert forward_peg(50.0, 6.0, 0.2, "ntm_vs_ltm").context.startswith("rich")  # 2.5


def test_forward_peg_suppressed_near_zero_growth() -> None:
    r = forward_peg(24.0, 6.0, 0.01, "ntm_vs_ltm", min_growth_rate=0.02)
    assert r.suppressed and r.forward_peg is None and r.context == "suppressed"
    assert "below" in r.suppression_reason
    # Missing growth (e.g. non-positive LTM) also suppresses, with the reason carried.
    r2 = forward_peg(24.0, 6.0, None, "ntm_vs_ltm", growth_reason="non-positive LTM")
    assert r2.suppressed and "LTM" in r2.suppression_reason
    assert r2.growth_basis == "ntm_vs_ltm"  # basis stays EXPLICIT even when suppressed


def test_trailing_ltm_eps_infers_q4() -> None:
    quarterly = {(2026, 1): 1.0, (2026, 2): 1.1, (2026, 3): 1.2, (2027, 1): 1.4}
    annual = {2026: 4.6}  # Q4 inferred = 4.6 - 1.0 - 1.1 - 1.2 = 1.3
    # 4 most recent: FY2027Q1 1.4 + FY2026Q4 1.3 + FY2026Q3 1.2 + FY2026Q2 1.1 = 5.0
    assert trailing_ltm_eps(quarterly, annual) == pytest.approx(5.0)


def test_trailing_ltm_eps_insufficient() -> None:
    assert trailing_ltm_eps({(2026, 1): 1.0, (2026, 2): 1.1}, {}) is None


def test_quarterly_actuals_from_edgar() -> None:
    recs = [
        FundamentalRecord("NVDA", "1045810", "FY2026Q1", date(2025, 4, 27), date(2025, 5, 28),
                          "eps_diluted", 1.0, "USD/shares", "10-Q", "edgar",
                          source_fiscal_period="FY2026Q1"),
        FundamentalRecord("NVDA", "1045810", "FY2025", date(2025, 1, 26), date(2025, 2, 26),
                          "eps_diluted", 3.5, "USD/shares", "10-K", "edgar",
                          source_fiscal_period="FY2025"),
    ]
    quarterly, annual = quarterly_actuals_from_edgar(recs, NVDA_CAL)
    assert quarterly == {(2026, 1): 1.0}  # date-derived (fy, q)
    assert annual == {2025: 3.5}
