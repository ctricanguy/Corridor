"""CRITICAL: FMP annual estimates and EDGAR actuals must refer to the SAME fiscal
year. We tie them by DATE (period_end + FiscalCalendar), never by trusting two
independent label-string conventions, so an off-by-one provider label cannot
silently subtract an actual from the wrong year.

NVDA names fiscal years by their END year (fiscal 2027 ends Jan 2027; its Q1 ends
late April 2026) — which is exactly our date-derived rule, so for NVDA EDGAR's
labels and ours agree. These tests lock that in AND prove a drifted label is
corrected by date.
"""

from __future__ import annotations

from datetime import date

import pytest

from corridor.datasources.base import FundamentalRecord
from corridor.ingest.fiscal import (
    FiscalCalendar,
    fiscal_year_bounds,
    fiscal_year_of,
    label_period,
)
from corridor.ingest.forward_sum import build_forward_eps_sum
from corridor.ingest.job import actuals_from_fundamentals, check_label_alignment
from corridor.ingest.records import FiscalPeriod, WindowResult

NVDA_CAL = FiscalCalendar(fy_end_month=1, fy_end_day=31)


def _q1_fy2027_actual(edgar_label: str) -> FundamentalRecord:
    """NVDA Q1 fiscal-2027 actual (quarter ENDED 2026-04-26), labeled as given."""
    return FundamentalRecord(
        ticker="NVDA", cik="1045810", fiscal_period=edgar_label,
        period_end_date=date(2026, 4, 26), filed_date=date(2026, 5, 28),
        metric="eps_diluted", value=0.80, unit="USD/shares", form="10-Q", source="edgar",
    )


def test_nvda_edgar_and_date_derived_labels_agree() -> None:
    aligns = check_label_alignment([_q1_fy2027_actual("FY2027Q1")], NVDA_CAL)
    assert len(aligns) == 1
    assert aligns[0].agree  # EDGAR fy/fp == our date-derived label -> no off-by-one
    assert aligns[0].date_label == "FY2027Q1"


def test_actual_ties_to_fmp_annual_by_date_bounds() -> None:
    # FMP annual FY2027 ends 2027-01-31; the Q1 actual (ended 2026-04-26) must fall
    # INSIDE FY2027's date bounds — that is the by-DATE tie, not a label compare.
    actual = _q1_fy2027_actual("FY2027Q1")
    fy = fiscal_year_of(date(2027, 1, 31), NVDA_CAL)
    assert fy == 2027
    start, end = fiscal_year_bounds(fy, NVDA_CAL)
    assert start <= actual.period_end_date <= end
    # And label_period independently agrees the quarter is in FY2027.
    assert label_period(actual.period_end_date, NVDA_CAL) == "FY2027Q1"


def test_off_by_one_edgar_label_is_corrected_by_date() -> None:
    # Hypothetical drift: EDGAR (or a provider) labels the April-2026 quarter
    # 'FY2026Q1'. Our date rule says FY2027Q1. Matching must use the DATE.
    aligns = check_label_alignment([_q1_fy2027_actual("FY2026Q1")], NVDA_CAL)
    assert not aligns[0].agree  # drift is SURFACED, not silent
    assert aligns[0].edgar_label == "FY2026Q1"
    assert aligns[0].date_label == "FY2027Q1"

    _dates, actuals = actuals_from_fundamentals([_q1_fy2027_actual("FY2026Q1")], NVDA_CAL)
    assert actuals == {"FY2027Q1": 0.80}  # re-keyed by date, NOT the drifted label


def test_drifted_actual_subtracts_from_correct_fy_in_derivation() -> None:
    """End-to-end: even a mislabeled actual subtracts from the RIGHT fiscal year."""
    _d, reported_actuals = actuals_from_fundamentals(
        [_q1_fy2027_actual("FY2026Q1")], NVDA_CAL  # drifted label
    )
    window = WindowResult(
        periods=[
            FiscalPeriod("FY2027Q2", date(2026, 7, 27), date(2026, 8, 27)),
            FiscalPeriod("FY2027Q3", date(2026, 10, 26), date(2026, 11, 19)),
            FiscalPeriod("FY2027Q4", date(2027, 1, 31), date(2027, 2, 25)),
            FiscalPeriod("FY2028Q1", date(2027, 4, 25), date(2027, 5, 27)),
        ],
        requested=4, available=4,
    )
    res = build_forward_eps_sum(window, {}, {"FY2027": 4.40, "FY2028": 6.00}, reported_actuals)
    q2 = next(c for c in res.components if c.fiscal_period == "FY2027Q2")
    # FY2027: Q1 actual 0.80 subtracted; Q2/Q3/Q4 unknown -> (4.40-0.80)/3 = 1.20.
    assert q2.value == pytest.approx(1.20)
    assert "actuals 0.8000" in q2.detail and "/3 unknown" in q2.detail
