"""Forward-window construction.

"Next 4 unreported quarters as of date X" = the next N fiscal periods that have
NOT reported by X, ordered by the company's own fiscal-period-end dates — never
calendar quarters. A period rolls out of the window the moment its earnings are
confirmed-reported on or before X.

Pure function; no I/O. The earnings calendar (fiscal periods + report dates) is
supplied by the caller, who builds it from the provider's calendar verified
against EDGAR.
"""

from __future__ import annotations

import logging
from datetime import date

from .records import FiscalPeriod, WindowResult

logger = logging.getLogger(__name__)


def unreported_window(periods: list[FiscalPeriod], as_of: date, n: int = 4) -> WindowResult:
    """Return the next ``n`` unreported fiscal periods as of ``as_of``.

    A period is REPORTED (excluded) only when its report date is confirmed AND on
    or before ``as_of`` — so the window rolls exactly on the confirmed report
    date and not before. Remaining periods are ordered by fiscal-period end date
    (the company's own calendar), and the first ``n`` are returned.

    If fewer than ``n`` unreported periods exist, the result is marked incomplete
    so the caller can log the gap rather than silently proceed on a short window.
    """
    unreported = [p for p in periods if not p.is_reported_as_of(as_of)]
    unreported.sort(key=lambda p: (p.period_end_date, p.fiscal_period))
    selected = unreported[:n]

    if len(selected) < n:
        logger.warning(
            "Forward window incomplete as of %s: requested %d, only %d unreported periods "
            "available (%s).",
            as_of,
            n,
            len(selected),
            ", ".join(p.fiscal_period for p in selected) or "none",
        )
    return WindowResult(periods=selected, requested=n, available=len(unreported))
