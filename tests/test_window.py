"""Adversarial: off-calendar fiscal year-end + window roll on confirmed report.

Asserts the next-4 window is built on the COMPANY's fiscal calendar (NVDA ends late
January) — not calendar quarters — and that it rolls the moment a quarter's
confirmed report date passes.
"""

from __future__ import annotations

from datetime import date

from corridor.ingest.fiscal import FiscalCalendar, label_period
from corridor.ingest.records import FiscalPeriod
from corridor.ingest.window import unreported_window

NVDA_CAL = FiscalCalendar(fy_end_month=1)


def _nvda_periods(q2_confirmed: bool = False) -> list[FiscalPeriod]:
    return [
        FiscalPeriod("FY2026Q1", date(2025, 4, 27), date(2025, 5, 28), confirmed=True),
        FiscalPeriod("FY2026Q2", date(2025, 7, 27), date(2025, 8, 27), confirmed=q2_confirmed),
        FiscalPeriod("FY2026Q3", date(2025, 10, 26), date(2025, 11, 19), confirmed=False),
        FiscalPeriod("FY2026Q4", date(2026, 1, 25), date(2026, 2, 25), confirmed=False),
        FiscalPeriod("FY2027Q1", date(2026, 4, 26), date(2026, 5, 27), confirmed=False),
    ]


def test_labels_use_company_fiscal_calendar_not_calendar_quarters() -> None:
    # NVDA quarter ending late October is fiscal Q3, not calendar Q4.
    assert label_period(date(2025, 10, 26), NVDA_CAL) == "FY2026Q3"
    assert label_period(date(2026, 1, 25), NVDA_CAL) == "FY2026Q4"
    # AAPL (Sept year-end): quarter ending late December is fiscal Q1 of next FY.
    aapl = FiscalCalendar(fy_end_month=9)
    assert label_period(date(2025, 12, 27), aapl) == "FY2026Q1"


def test_window_skips_reported_quarter_and_uses_fiscal_period_ends() -> None:
    window = unreported_window(_nvda_periods(), date(2025, 6, 16), n=4)
    labels = [p.fiscal_period for p in window.periods]
    assert labels == ["FY2026Q2", "FY2026Q3", "FY2026Q4", "FY2027Q1"]  # Q1 already reported
    # Period-end MONTHS are the off-calendar fiscal ones (Jul, Oct, Jan, Apr),
    # never calendar quarter-ends (Mar/Jun/Sep/Dec).
    assert [p.period_end_date.month for p in window.periods] == [7, 10, 1, 4]
    assert window.complete


def test_window_rolls_on_confirmed_report_date() -> None:
    periods = _nvda_periods(q2_confirmed=True)  # Q2 confirmed to report 2025-08-27
    # Day BEFORE the confirmed report: Q2 is still unreported and leads the window.
    before = unreported_window(periods, date(2025, 8, 26), n=4)
    assert before.periods[0].fiscal_period == "FY2026Q2"
    # ON the confirmed report date: Q2 has reported, window rolls to start at Q3.
    on_report = unreported_window(periods, date(2025, 8, 27), n=4)
    assert on_report.periods[0].fiscal_period == "FY2026Q3"


def test_incomplete_window_is_flagged_not_padded() -> None:
    # Only one unreported period available -> window is short and marked incomplete.
    periods = [
        FiscalPeriod("FY2026Q1", date(2025, 4, 27), date(2025, 5, 28), confirmed=True),
        FiscalPeriod("FY2026Q2", date(2025, 7, 27), date(2025, 8, 27), confirmed=False),
    ]
    window = unreported_window(periods, date(2025, 6, 16), n=4)
    assert len(window.periods) == 1
    assert not window.complete
