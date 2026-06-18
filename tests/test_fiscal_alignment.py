"""CRITICAL: FMP annual estimates and EDGAR actuals must refer to the SAME fiscal
year, tied by period_end DATE — never by EDGAR's fy/fp label string.

Verified against NVIDIA's own filings: the quarter ENDED 2025-04-27 is "First Quarter
Fiscal 2026" (nvidianews.nvidia.com; 10-Q nvda-20250427.htm). So FY2026Q1 by date is
correct. EDGAR companyfacts repeats that quarter as a COMPARATIVE in the next year's
10-Q tagged fy=2027 — a filing-relative artifact. These tests reproduce that live
drift and prove the date-derived labeling + earliest-filed alignment handle it.
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


def _q1_fy2026(source_label: str, filed: date, val: float = 0.85) -> FundamentalRecord:
    """NVIDIA fiscal-2026 Q1 actual (quarter ENDED 2025-04-27).

    ``fiscal_period`` is the AUTHORITATIVE date-derived label; ``source_label`` is
    EDGAR's raw fy/fp ('FY2026Q1' original, 'FY2027Q1' drifted comparative).
    """
    return FundamentalRecord(
        ticker="NVDA", cik="1045810", fiscal_period="FY2026Q1",
        period_end_date=date(2025, 4, 27), filed_date=filed, metric="eps_diluted",
        value=val, unit="USD/shares", form="10-Q", source="edgar",
        source_fiscal_period=source_label,
    )


def test_date_label_matches_nvidia_convention() -> None:
    # Quarter ended 2025-04-27 is NVIDIA fiscal 2026 Q1 (per the 10-Q cover).
    assert label_period(date(2025, 4, 27), NVDA_CAL) == "FY2026Q1"
    assert fiscal_year_of(date(2026, 1, 25), NVDA_CAL) == 2026


def test_original_and_comparative_align_via_earliest_filed() -> None:
    # The live case: original (fy=2026) + comparative (fy=2027, filed a year later).
    original = _q1_fy2026("FY2026Q1", filed=date(2025, 5, 28))
    comparative = _q1_fy2026("FY2027Q1", filed=date(2026, 5, 28))  # drifted +1yr
    report = check_label_alignment([comparative, original], NVDA_CAL)
    assert len(report.in_window) == 1
    assert report.aligned  # anchored to the ORIGINAL -> aligned (gate reads True)
    assert report.in_window[0].edgar_label == "FY2026Q1"
    assert report.in_window[0].date_label == "FY2026Q1"
    # Actuals keyed by DATE; the comparative's drift is ignored.
    _d, actuals = actuals_from_fundamentals([comparative, original], NVDA_CAL)
    assert actuals == {"FY2026Q1": 0.85}


def test_drift_is_surfaced_when_only_comparative_seen() -> None:
    # If only the drifted comparative is present, the gate SURFACES the drift...
    comparative = _q1_fy2026("FY2027Q1", filed=date(2026, 5, 28))
    report = check_label_alignment([comparative], NVDA_CAL)
    assert not report.aligned
    assert report.in_window[0].edgar_label == "FY2027Q1"
    assert report.in_window[0].date_label == "FY2026Q1"
    # ...but the actual is STILL keyed to the correct fiscal year by date.
    _d, actuals = actuals_from_fundamentals([comparative], NVDA_CAL)
    assert actuals == {"FY2026Q1": 0.85}


def test_gate_scopes_to_recent_window_and_exempts_old_drift() -> None:
    # Recent quarter (in window) aligns; an OLD quarter (pre-window) drifts but is
    # EXEMPT — so the verdict reads True. (NVIDIA's pre-2023 boundaries shifted.)
    recent = _q1_fy2026("FY2026Q1", filed=date(2025, 5, 28))  # FY2026, aligns
    old_drift = FundamentalRecord(  # 2009-04-26: date FY2010Q1 but EDGAR FY2010Q2
        ticker="NVDA", cik="1045810", fiscal_period="FY2010Q1",
        period_end_date=date(2009, 4, 26), filed_date=date(2009, 5, 1),
        metric="eps_diluted", value=0.01, unit="USD/shares", form="10-Q",
        source="edgar", source_fiscal_period="FY2010Q2",
    )
    report = check_label_alignment([recent, old_drift], NVDA_CAL, trailing_years=3)
    assert report.anchor_fy == 2026 and report.window_start_fy == 2024
    assert [a.period_end for a in report.in_window] == [date(2025, 4, 27)]
    assert report.aligned  # only the in-window quarter counts, and it aligns
    assert len(report.older) == 1 and len(report.older_drift) == 1  # old drift exempt + logged


def test_actual_ties_to_fmp_annual_by_date_bounds() -> None:
    fy = fiscal_year_of(date(2026, 1, 25), NVDA_CAL)  # FMP annual FY2026 end
    start, end = fiscal_year_bounds(fy, NVDA_CAL)
    assert start <= date(2025, 4, 27) <= end  # the Q1 actual is inside FY2026


def test_52_53_week_boundary_shift_does_not_fire_false_in_window_drift() -> None:
    # AMD / AVGO live case: original 10-Q and comparative in a later 10-Q report the
    # SAME quarter but with period_end dates that differ by 1-2 days (52/53-week FY
    # boundary shift). The old gate (keyed by raw period_end) saw two distinct entries
    # and picked the comparative alone -> false drift. The fixed gate (keyed by
    # date-derived label) collapses both to the same label and picks the earliest-filed
    # (the original) -> no drift.
    AMD_CAL = FiscalCalendar(fy_end_month=12)  # calendar-year AMD
    # Original Q2 2024 10-Q filed Aug 2024: period ended 2024-06-30 (last Saturday)
    original_q2 = FundamentalRecord(
        ticker="AMD", cik="0000002488", fiscal_period="FY2024Q2",
        period_end_date=date(2024, 6, 30), filed_date=date(2024, 8, 1),
        metric="eps_diluted", value=0.69, unit="USD/shares", form="10-Q",
        source="edgar", source_fiscal_period="FY2024Q2",
    )
    # Comparative in Q2 2025 10-Q filed Jul 2025: same quarter, period ended 2024-06-29
    # (1-day shift), EDGAR fy drifted to 2025.
    comparative_q2 = FundamentalRecord(
        ticker="AMD", cik="0000002488", fiscal_period="FY2024Q2",
        period_end_date=date(2024, 6, 29), filed_date=date(2025, 7, 29),
        metric="eps_diluted", value=0.69, unit="USD/shares", form="10-Q",
        source="edgar", source_fiscal_period="FY2025Q2",
    )
    as_of = date(2026, 6, 18)
    report = check_label_alignment([original_q2, comparative_q2], AMD_CAL,
                                   trailing_years=3, as_of=as_of)
    # FY2024 is in-window (anchor=2026, start=2024); gate must agree (no false drift).
    in_w_labels = {a.date_label for a in report.in_window}
    assert "FY2024Q2" in in_w_labels
    q2_align = next(a for a in report.in_window if a.date_label == "FY2024Q2")
    assert q2_align.agree, (
        f"Gate should agree for FY2024Q2 (original filing picked), "
        f"but got edgar_label={q2_align.edgar_label!r}"
    )
    assert report.aligned or len(report.in_window) == 1  # at least Q2 present and agrees

    # Actuals also correct regardless of the date-shift.
    _d, actuals = actuals_from_fundamentals([original_q2, comparative_q2], AMD_CAL)
    assert actuals.get("FY2024Q2") == pytest.approx(0.69)


def test_old_edgar_data_only_does_not_produce_false_in_window_drift() -> None:
    # GOOGL live case: if EDGAR companyfacts returns only old quarterly entries (e.g.
    # the continuing-ops EPS tag is absent for recent years), the anchor derived from
    # max(aligns) is very old (e.g. 2015). That puts FY2014Q3 in-window (2014 >= 2013)
    # and the drift fires for a 12-year-old comparative. With as_of anchoring the window
    # covers the actual last 3 fiscal years, so the old entry goes to older (exempt).
    GOOGL_CAL = FiscalCalendar(fy_end_month=12)
    old_comparative = FundamentalRecord(
        ticker="GOOGL", cik="0001652044", fiscal_period="FY2014Q3",
        period_end_date=date(2014, 9, 30), filed_date=date(2015, 10, 29),  # filed in 2015 10-Q
        metric="eps_diluted", value=7.35, unit="USD/shares", form="10-Q",
        source="edgar", source_fiscal_period="FY2015Q3",  # drifted
    )
    as_of = date(2026, 6, 18)

    # WITHOUT as_of: anchor = max(aligns) = FY2014 -> window_start = 2012; entry is in-window.
    report_no_asof = check_label_alignment([old_comparative], GOOGL_CAL, trailing_years=3)
    assert report_no_asof.anchor_fy == 2014
    assert len(report_no_asof.in_window) == 1 and not report_no_asof.in_window[0].agree

    # WITH as_of: anchor = FY2026 -> window_start = 2024; entry goes to older (exempt).
    report_with_asof = check_label_alignment([old_comparative], GOOGL_CAL,
                                             trailing_years=3, as_of=as_of)
    assert report_with_asof.anchor_fy == 2026 and report_with_asof.window_start_fy == 2024
    assert len(report_with_asof.in_window) == 0
    assert len(report_with_asof.older) == 1


def test_drifted_actual_subtracts_from_correct_fy_in_derivation() -> None:
    # April-2026 quarter (FY2027Q1) with a drifted comparative source (FY2028Q1).
    drifted = FundamentalRecord(
        ticker="NVDA", cik="1045810", fiscal_period="FY2027Q1",
        period_end_date=date(2026, 4, 26), filed_date=date(2026, 5, 28),
        metric="eps_diluted", value=0.80, unit="USD/shares", form="10-Q",
        source="edgar", source_fiscal_period="FY2028Q1",
    )
    _d, reported_actuals = actuals_from_fundamentals([drifted], NVDA_CAL)
    assert reported_actuals == {"FY2027Q1": 0.80}  # keyed by date, not the drift

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
    assert q2.value == pytest.approx((4.40 - 0.80) / 3)  # FY2027 actual subtracted -> /3
    assert "/3 unknown" in q2.detail
