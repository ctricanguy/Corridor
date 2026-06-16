"""Build the next-4-quarter forward-EPS sum with full provenance.

For each quarter in the window we prefer a REAL provider quarterly estimate. When
a quarter has no quarterly estimate but its fiscal year has an annual estimate, we
DERIVE it by splitting the annual across that year's not-yet-known quarters:

    per_missing_quarter = (annual_FY - sum(known real quarters of FY)) / (4 - k)

where ``k`` is how many of the fiscal year's quarters we already have as real
estimates. (So two known + two missing in one fiscal year derives the pair as
``(annual - known)/2`` — never a bare annual/4 guess when better info exists.)

Every component records its method, and the coverage score = real / total. A
quarter that is neither available as quarterly nor derivable from an annual is
NOT fabricated — the sum is marked incomplete and the caller logs/quarantines it.
"""

from __future__ import annotations

import logging
import re

from ..constants import METHOD_DERIVED_FROM_ANNUAL, METHOD_REAL_QUARTERLY
from .records import EstimateComponent, ForwardSumResult, WindowResult

logger = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"^FY(\d{4})Q([1-4])$")


def _parse_period(fiscal_period: str) -> tuple[int, int]:
    """'FY2026Q1' -> (2026, 1). Raises ValueError on a malformed label."""
    m = _PERIOD_RE.match(fiscal_period)
    if not m:
        raise ValueError(f"Unrecognized fiscal period label: {fiscal_period!r} (want 'FY2026Q1')")
    return int(m.group(1)), int(m.group(2))


def _fy_key(year: int) -> str:
    return f"FY{year}"


def build_forward_eps_sum(
    window: WindowResult,
    quarterly_estimates: dict[str, float],
    annual_estimates: dict[str, float],
) -> ForwardSumResult:
    """Construct the forward-EPS sum for the window's quarters.

    Args:
        window: the next-N unreported quarters (from ``unreported_window``).
        quarterly_estimates: {fiscal_period -> eps} real provider quarterly estimates.
        annual_estimates: {'FY2027' -> eps} provider annual estimates (for derivation).

    Returns:
        ForwardSumResult with per-quarter components, the full construction string,
        the coverage score, and a completeness flag (False if any quarter could be
        neither found nor derived — never fabricated).
    """
    # How many real quarterly estimates we hold per fiscal year (for the divisor).
    known_per_fy: dict[int, list[float]] = {}
    for label, value in quarterly_estimates.items():
        try:
            year, _ = _parse_period(label)
        except ValueError:
            continue
        known_per_fy.setdefault(year, []).append(value)

    components: list[EstimateComponent] = []
    complete = True

    for period in window.periods:
        label = period.fiscal_period
        if label in quarterly_estimates:
            components.append(
                EstimateComponent(
                    fiscal_period=label,
                    value=quarterly_estimates[label],
                    method=METHOD_REAL_QUARTERLY,
                    is_derived=False,
                    detail="real provider quarterly estimate",
                )
            )
            continue

        # No quarterly estimate — try to derive from the fiscal year's annual.
        year, _q = _parse_period(label)
        annual = annual_estimates.get(_fy_key(year))
        known_values = known_per_fy.get(year, [])
        k = len(known_values)
        remaining_quarters = 4 - k
        if annual is None or remaining_quarters <= 0:
            complete = False
            logger.warning(
                "Cannot build quarter %s: no quarterly estimate and no usable FY%d annual "
                "(annual=%s, known_quarters=%d). Not fabricating; marking incomplete.",
                label,
                year,
                annual,
                k,
            )
            continue

        per_missing = (annual - sum(known_values)) / remaining_quarters
        components.append(
            EstimateComponent(
                fiscal_period=label,
                value=per_missing,
                method=METHOD_DERIVED_FROM_ANNUAL,
                is_derived=True,
                detail=(
                    f"FY{year} annual {annual:.4f} minus {sum(known_values):.4f} known, "
                    f"/{remaining_quarters} remaining quarter(s)"
                ),
            )
        )

    value = sum(c.value for c in components)
    n_total = len(window.periods)
    n_real = sum(1 for c in components if not c.is_derived)
    coverage = (n_real / n_total) if n_total else 0.0
    if not window.complete:
        complete = False

    construction_method = _describe(components, n_total)
    return ForwardSumResult(
        value=value,
        components=components,
        construction_method=construction_method,
        coverage_score=coverage,
        complete=complete and len(components) == n_total,
    )


def _describe(components: list[EstimateComponent], n_total: int) -> str:
    """Human-readable construction string stored on the valuation snapshot."""
    real = [c.fiscal_period for c in components if not c.is_derived]
    derived = [c for c in components if c.is_derived]
    parts: list[str] = []
    if real:
        parts.append(f"{len(real)} real quarterly ({', '.join(real)})")
    if derived:
        labels = ", ".join(f"{c.fiscal_period}[{c.detail}]" for c in derived)
        parts.append(f"{len(derived)} derived from annual ({labels})")
    missing = n_total - len(components)
    if missing > 0:
        parts.append(f"{missing} MISSING (not fabricated)")
    return " + ".join(parts) if parts else "empty window"
