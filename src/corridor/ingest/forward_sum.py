"""Build the next-4-quarter forward-EPS sum with full provenance.

For each quarter in the window we prefer a REAL provider quarterly estimate. When a
quarter has no quarterly estimate but its fiscal year has an annual estimate, we
DERIVE it by splitting the annual across that year's GENUINELY UNKNOWN quarters —
the ones with neither a reported actual nor a real estimate:

    derived_quarter = (annual_FY
                       - Σ reported_actuals_in_FY        # already-reported quarters (EDGAR)
                       - Σ real_quarterly_ests_in_FY)    # quarters we have an estimate for
                      / count(quarters in FY with NEITHER an actual nor an estimate)

This is the correctness-critical part: an earlier quarter of the same fiscal year
that has already REPORTED must be subtracted from the annual AND excluded from the
divisor. (NVDA mid-FY2026: Q1 reported, Q2/Q3 estimated, only Q4 unknown -> divide
by 1, not 2.) Reported actuals come from EDGAR and are threaded in by the caller;
when they are absent, an unreported-and-unestimated quarter is conservatively
treated as unknown (it stays in the divisor).

Every component records its method, and the coverage score = real / total. A
quarter that is neither available as quarterly nor derivable from an annual is NOT
fabricated — the sum is marked incomplete and the caller logs/quarantines it.
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


def _fy_breakdown(
    year: int,
    quarterly_estimates: dict[str, float],
    reported_actuals: dict[str, float],
) -> tuple[float, float, list[str]]:
    """Split a fiscal year's four quarters into known actuals, known estimates, unknowns.

    Actual takes precedence over estimate for the same quarter. Returns
    ``(actual_sum, estimate_sum, unknown_labels)`` where ``unknown_labels`` are the
    FY quarters with NEITHER an actual nor an estimate — the divisor set.
    """
    actual_sum = 0.0
    estimate_sum = 0.0
    unknown: list[str] = []
    for q in (1, 2, 3, 4):
        label = f"FY{year}Q{q}"
        if label in reported_actuals:
            actual_sum += reported_actuals[label]
        elif label in quarterly_estimates:
            estimate_sum += quarterly_estimates[label]
        else:
            unknown.append(label)
    return actual_sum, estimate_sum, unknown


def build_forward_eps_sum(
    window: WindowResult,
    quarterly_estimates: dict[str, float],
    annual_estimates: dict[str, float],
    reported_actuals: dict[str, float] | None = None,
) -> ForwardSumResult:
    """Construct the forward-EPS sum for the window's quarters.

    Args:
        window: the next-N unreported quarters (from ``unreported_window``).
        quarterly_estimates: {fiscal_period -> eps} real provider quarterly estimates.
        annual_estimates: {'FY2027' -> eps} provider annual estimates (for derivation).
        reported_actuals: {fiscal_period -> eps} already-reported quarterly actuals
            (EDGAR). Subtracted from the annual and excluded from the divisor when
            deriving a quarter in the same fiscal year. Defaults to none.

    Returns:
        ForwardSumResult with per-quarter components, the full construction string,
        the coverage score, and a completeness flag (False if any quarter could be
        neither found nor derived — never fabricated).
    """
    reported_actuals = reported_actuals or {}
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

        # No quarterly estimate — derive from the fiscal year's annual, subtracting
        # already-known quarters (actuals + estimates) and dividing only by the
        # genuinely unknown quarters of that fiscal year.
        year, _q = _parse_period(label)
        annual = annual_estimates.get(_fy_key(year))
        actual_sum, estimate_sum, unknown = _fy_breakdown(
            year, quarterly_estimates, reported_actuals
        )
        if annual is None or not unknown:
            complete = False
            logger.warning(
                "Cannot build quarter %s: no quarterly estimate and no usable FY%d annual "
                "(annual=%s, unknown_quarters=%d). Not fabricating; marking incomplete.",
                label,
                year,
                annual,
                len(unknown),
            )
            continue

        remainder = annual - actual_sum - estimate_sum
        per_unknown = remainder / len(unknown)
        components.append(
            EstimateComponent(
                fiscal_period=label,
                value=per_unknown,
                method=METHOD_DERIVED_FROM_ANNUAL,
                is_derived=True,
                detail=(
                    f"FY{year} annual {annual:.4f} - actuals {actual_sum:.4f} "
                    f"- est {estimate_sum:.4f} = {remainder:.4f}, "
                    f"/{len(unknown)} unknown ({', '.join(unknown)})"
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
