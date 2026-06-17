"""Per-company fiscal calendars.

Large-cap tech is full of off-calendar fiscal years (NVDA ends late January, AAPL
late September, AVGO early November, MSFT June, MU late August). The corridor's
window must use the company's OWN fiscal quarters, so we label provider period-end
dates into 'FY{year}Q{q}' using a month-based rule that is robust to the exact day.

The month/day here are seeds; ``scripts/validate_live.py`` cross-checks them
against EDGAR's reported period-end dates before they are trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FiscalCalendar:
    """A company's fiscal-year-end month/day. ``fy_end_month`` drives labeling."""

    fy_end_month: int  # 1..12 (calendar month in which the fiscal year ends)
    fy_end_day: int = 31
    note: str = ""


def label_period(period_end: date, cal: FiscalCalendar) -> str:
    """Map a fiscal-period END date to a 'FY{year}Q{q}' label.

    The fiscal year is named for the calendar year in which it ends (NVIDIA's year
    ending Jan 2026 is 'fiscal 2026'). The quarter is counted from the fiscal-year
    start month (the month after the fiscal-year-end month).
    """
    fy_year, q = label_period_parts(period_end, cal)
    return f"FY{fy_year}Q{q}"


def label_period_parts(period_end: date, cal: FiscalCalendar) -> tuple[int, int]:
    """Return ``(fiscal_year, quarter)`` for a period-end date. See ``label_period``."""
    m = period_end.month
    fy_end_month = cal.fy_end_month
    fiscal_year = period_end.year + 1 if m > fy_end_month else period_end.year
    start_month = fy_end_month % 12 + 1  # month the fiscal year starts
    quarter = ((m - start_month) % 12) // 3 + 1
    return fiscal_year, quarter


def fiscal_year_of(period_end: date, cal: FiscalCalendar) -> int:
    """The fiscal-year NUMBER a period-end date belongs to (named by its END year).

    A fiscal year is named for the calendar year in which it ENDS (NVIDIA's year
    ending Jan 2027 is fiscal 2027). This single date-derived rule is THE source of
    truth for tying FMP estimates and EDGAR actuals to the same fiscal year — never
    a provider's label string.
    """
    return period_end.year + 1 if period_end.month > cal.fy_end_month else period_end.year


def fiscal_year_label(period_end: date, cal: FiscalCalendar) -> str:
    """Annual label, e.g. 'FY2026', for an annual period-end date (date-derived)."""
    return f"FY{fiscal_year_of(period_end, cal)}"


def fiscal_year_bounds(fiscal_year: int, cal: FiscalCalendar) -> tuple[date, date]:
    """Approximate (start, end) calendar dates of a fiscal year.

    end  = the fiscal-year-end (cal month/day in calendar year ``fiscal_year``).
    start = the day after the prior fiscal-year-end. A few days of 52/53-week drift
    are immaterial for assigning a quarterly period-end to its fiscal year.
    """
    end = _safe_date(fiscal_year, cal.fy_end_month, cal.fy_end_day)
    prev_end = _safe_date(fiscal_year - 1, cal.fy_end_month, cal.fy_end_day)
    start = date.fromordinal(prev_end.toordinal() + 1)
    return start, end


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    from datetime import date as _date

    return (_date(year, month + 1, 1) - _date(year, month, 1)).days


def _safe_date(year: int, month: int, day: int) -> date:
    """A date with ``day`` clamped to the month length (handles 31 in short months)."""
    return date(year, month, min(day, _last_day(year, month)))


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    return _safe_date(year, month, d.day)


def enumerate_fiscal_quarters(
    cal: FiscalCalendar, as_of: date, horizon: int = 8, trailing_days: int = 150
) -> list[tuple[str, date]]:
    """Enumerate (label, period_end) for fiscal quarters around ``as_of``.

    Generates quarters from the company's fiscal calendar so the window can include
    a quarter we will DERIVE from an annual estimate (i.e. one with no quarterly
    estimate of its own). Returns quarters whose period-end is from roughly one
    quarter before ``as_of`` (so a just-ended, unreported quarter survives) forward.
    """
    out: list[tuple[str, date]] = []
    for fy in range(as_of.year - 1, as_of.year + 3):
        fy_end = _safe_date(fy, cal.fy_end_month, cal.fy_end_day)
        for k in (1, 2, 3, 4):
            q_end = _add_months(fy_end, -3 * (4 - k))
            out.append((f"FY{fy}Q{k}", q_end))
    cutoff = date.fromordinal(as_of.toordinal() - trailing_days)
    out = sorted({(lbl, d) for lbl, d in out if d >= cutoff}, key=lambda t: (t[1], t[0]))
    return out[:horizon]
