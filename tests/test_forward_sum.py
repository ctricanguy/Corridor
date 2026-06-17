"""Adversarial: a 4Q sum built from 2 real quarterly + 2 derived-from-annual.

Asserts the construction_method records exactly how each quarter was built and the
coverage score drops to reflect the derived quarters. Never fabricates a quarter
that is neither available nor derivable.
"""

from __future__ import annotations

from datetime import date

import pytest

from corridor.constants import METHOD_DERIVED_FROM_ANNUAL, METHOD_REAL_QUARTERLY
from corridor.ingest.forward_sum import build_forward_eps_sum
from corridor.ingest.records import FiscalPeriod, WindowResult


def _window() -> WindowResult:
    periods = [
        FiscalPeriod("FY2026Q2", date(2025, 7, 27), date(2025, 8, 27)),
        FiscalPeriod("FY2026Q3", date(2025, 10, 26), date(2025, 11, 19)),
        FiscalPeriod("FY2026Q4", date(2026, 1, 25), date(2026, 2, 25)),
        FiscalPeriod("FY2027Q1", date(2026, 4, 26), date(2026, 5, 27)),
    ]
    return WindowResult(periods=periods, requested=4, available=4)


def test_two_real_two_derived_sum_and_coverage() -> None:
    quarterly = {"FY2026Q2": 1.00, "FY2026Q3": 1.20}  # two real
    annual = {"FY2026": 4.40, "FY2027": 6.00}

    result = build_forward_eps_sum(_window(), quarterly, annual)

    # FY2026Q4 derived: (4.40 - (1.00+1.20)) / (4-2) = 2.20/2 = 1.10
    # FY2027Q1 derived: (6.00 - 0) / (4-0) = 1.50
    assert result.value == pytest.approx(1.00 + 1.20 + 1.10 + 1.50)  # 4.80
    assert result.value == pytest.approx(4.80)
    assert result.coverage_score == pytest.approx(0.5)  # 2 real of 4
    assert result.n_real == 2 and result.n_derived == 2
    assert result.complete

    methods = {c.fiscal_period: c.method for c in result.components}
    assert methods["FY2026Q2"] == METHOD_REAL_QUARTERLY
    assert methods["FY2026Q4"] == METHOD_DERIVED_FROM_ANNUAL
    # construction_method names the real and derived quarters explicitly.
    assert "2 real quarterly" in result.construction_method
    assert "2 derived from annual" in result.construction_method
    assert "FY2026Q4" in result.construction_method


def test_derived_quarter_subtracts_reported_actual_and_shrinks_divisor() -> None:
    """ADVERSARIAL: an earlier quarter of the fiscal year is already REPORTED.

    FY2026Q1 has an EDGAR actual, so deriving FY2026Q4 must subtract that actual
    from the annual AND divide by the count of GENUINELY unknown quarters (just Q4
    -> /1), not by 2. This is the correctness check on the derived-from-annual path.
    """
    quarterly = {"FY2026Q2": 1.00, "FY2026Q3": 1.20}
    annual = {"FY2026": 4.40, "FY2027": 6.00}
    reported_actuals = {"FY2026Q1": 0.80}  # Q1 already reported

    result = build_forward_eps_sum(_window(), quarterly, annual, reported_actuals)
    derived = {c.fiscal_period: c for c in result.components if c.is_derived}

    # FY2026Q4 = (4.40 - 0.80 actual - 2.20 est) / 1 unknown = 1.40
    assert derived["FY2026Q4"].value == pytest.approx(1.40)
    assert "/1 unknown" in derived["FY2026Q4"].detail
    assert "actuals 0.8000" in derived["FY2026Q4"].detail
    # FY2027 has nothing known -> divide the annual by all 4 quarters.
    assert derived["FY2027Q1"].value == pytest.approx(1.50)

    # Contrast: WITHOUT the actual, Q1 is treated as unknown too -> divisor 2.
    no_actual = build_forward_eps_sum(_window(), quarterly, annual)
    q4 = next(c for c in no_actual.components if c.fiscal_period == "FY2026Q4")
    assert q4.value == pytest.approx(1.10)  # (4.40 - 2.20) / 2 — the fallback path


def test_reported_actual_for_two_quarters_leaves_divisor_one() -> None:
    """Two of the four FY quarters reported, one estimated -> only one unknown (/1)."""
    # FY2026: Q1 actual, Q2 actual, Q3 estimate, Q4 derived.
    quarterly = {"FY2026Q3": 1.20}
    annual = {"FY2026": 4.40, "FY2027": 6.00}
    reported = {"FY2026Q1": 0.80, "FY2026Q2": 0.90}
    result = build_forward_eps_sum(_window(), quarterly, annual, reported)
    q4 = next(c for c in result.components if c.fiscal_period == "FY2026Q4")
    # (4.40 - (0.80 + 0.90) actuals - 1.20 est) / 1 = 1.50
    assert q4.value == pytest.approx(1.50)
    assert "/1 unknown" in q4.detail


def test_all_real_has_full_coverage() -> None:
    quarterly = {"FY2026Q2": 1.0, "FY2026Q3": 1.2, "FY2026Q4": 1.3, "FY2027Q1": 1.5}
    result = build_forward_eps_sum(_window(), quarterly, {})
    assert result.coverage_score == pytest.approx(1.0)
    assert result.value == pytest.approx(5.0)
    assert result.complete


def test_missing_quarter_with_no_annual_is_incomplete_not_fabricated() -> None:
    quarterly = {"FY2026Q2": 1.0, "FY2026Q3": 1.2}  # Q4 missing, no FY2027 annual either
    annual = {"FY2026": 4.40}  # covers Q4 but NOT FY2027Q1
    result = build_forward_eps_sum(_window(), quarterly, annual)
    assert not result.complete  # FY2027Q1 could be neither found nor derived
    assert "MISSING" in result.construction_method
    # Only the three buildable quarters contribute; the missing one is NOT zero-filled.
    assert {c.fiscal_period for c in result.components} == {"FY2026Q2", "FY2026Q3", "FY2026Q4"}
