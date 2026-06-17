"""Factor 2b — growth-adjusted valuation: forward PEG.

    Forward_PEG = Forward_PE / forward_EPS_growth_rate(%)

The corridor alone penalizes high-growth names and flatters decliners; PEG
normalizes for growth so names are comparable. It is a SEPARATE view from the
corridor (and from any future company-premium term) — computed alongside, shown
side by side, NEVER blended into one opaque score. Their disagreement is signal.

PEG is only as honest as its growth input, so the growth basis is EXPLICIT and
LOGGED on every calculation:

  * ntm_vs_ltm (default) — next-twelve-month EPS vs last-twelve-month EPS.
  * fwd_cagr            — multi-year forward EPS CAGR from the annual curve.

Guardrails (the colleague's metric, used honestly): when growth is near zero or
negative the ratio blows up / flips sign, so PEG is SUPPRESSED (fall back to the
corridor). The <1 cheap / >2 rich thresholds are CONTEXT only — the actual signal
always defers to the corridor + earnings trend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..ingest.fiscal import FiscalCalendar, fiscal_year_of, label_period_parts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PegResult:
    forward_pe: float | None
    ntm_eps: float | None
    growth_input: float | None  # LTM EPS (ntm_vs_ltm) or out-year EPS (fwd_cagr)
    growth_rate: float | None  # fraction, e.g. 0.20 = 20%
    growth_basis: str  # 'ntm_vs_ltm' | 'fwd_cagr' — EXPLICIT, never implicit
    forward_peg: float | None
    suppressed: bool
    suppression_reason: str | None
    context: str  # 'cheap (<1.0)' | 'fair' | 'rich (>2.0)' | 'suppressed' — CONTEXT only
    coverage_score: float | None
    notes: str


def ntm_vs_ltm_growth(ntm_eps: float, ltm_eps: float | None) -> tuple[float | None, str | None]:
    """Forward growth as NTM EPS vs LTM EPS. None (with reason) on non-positive LTM."""
    if ltm_eps is None or ltm_eps <= 0:
        return None, f"non-positive/missing LTM EPS ({ltm_eps})"
    return (ntm_eps - ltm_eps) / ltm_eps, None


def fwd_cagr_growth(
    eps_year0: float | None, eps_year_n: float | None, years: int
) -> tuple[float | None, str | None]:
    """Forward EPS CAGR over ``years`` from the annual curve. None on non-positive base."""
    if eps_year0 is None or eps_year0 <= 0 or eps_year_n is None or years <= 0:
        return None, f"non-positive/missing base-year EPS ({eps_year0})"
    return (eps_year_n / eps_year0) ** (1.0 / years) - 1.0, None


def forward_peg(
    forward_pe: float | None,
    ntm_eps: float | None,
    growth_rate: float | None,
    growth_basis: str,
    *,
    growth_input: float | None = None,
    growth_reason: str | None = None,
    min_growth_rate: float = 0.02,
    cheap_threshold: float = 1.0,
    rich_threshold: float = 2.0,
    coverage_score: float | None = None,
    label: str = "",
) -> PegResult:
    """Compute forward PEG, suppressing it near zero/negative growth.

    The growth basis + rate are LOGGED on every call (PEG is only as honest as its
    growth input). PEG is suppressed (and the caller falls back to the corridor) when
    growth is missing or below ``min_growth_rate``.
    """
    suppressed = False
    reason: str | None = None
    peg: float | None = None

    if forward_pe is None or growth_rate is None:
        suppressed, reason = True, growth_reason or "missing forward P/E or growth rate"
    elif growth_rate < min_growth_rate:
        suppressed = True
        reason = (f"growth {growth_rate:.1%} below {min_growth_rate:.1%} floor "
                  "(near-zero/negative) — PEG unreliable, defer to corridor")
    else:
        peg = forward_pe / (growth_rate * 100.0)

    if peg is None:
        context = "suppressed"
    elif peg < cheap_threshold:
        context = f"cheap (<{cheap_threshold:g})"
    elif peg > rich_threshold:
        context = f"rich (>{rich_threshold:g})"
    else:
        context = "fair"

    logger.info(
        "PEG %s: basis=%s growth=%s pe=%s peg=%s%s",
        label or "?", growth_basis,
        f"{growth_rate:.4f}" if growth_rate is not None else "n/a",
        f"{forward_pe:.2f}" if forward_pe is not None else "n/a",
        f"{peg:.3f}" if peg is not None else "SUPPRESSED",
        f" ({reason})" if reason else "",
    )
    notes = "" if not suppressed else f"PEG suppressed: {reason}"
    return PegResult(
        forward_pe=forward_pe, ntm_eps=ntm_eps, growth_input=growth_input,
        growth_rate=growth_rate, growth_basis=growth_basis, forward_peg=peg,
        suppressed=suppressed, suppression_reason=reason, context=context,
        coverage_score=coverage_score, notes=notes,
    )


# --- LTM (trailing) EPS from EDGAR actuals ----------------------------------
def quarterly_actuals_from_edgar(
    fundamentals: list, cal: FiscalCalendar
) -> tuple[dict[tuple[int, int], float], dict[int, float]]:
    """Map EDGAR records to {(fy, q): eps} quarterly + {fy: eps} annual (earliest-filed)."""
    q_filed: dict[tuple[int, int], object] = {}
    a_filed: dict[int, object] = {}
    quarterly: dict[tuple[int, int], float] = {}
    annual: dict[int, float] = {}
    for f in fundamentals:
        if f.period_end_date is None or f.filed_date is None:
            continue
        if "Q" in (f.source_fiscal_period or f.fiscal_period):
            key = label_period_parts(f.period_end_date, cal)
            if key not in q_filed or f.filed_date < q_filed[key]:
                q_filed[key] = f.filed_date
                quarterly[key] = f.value
        else:
            fy = fiscal_year_of(f.period_end_date, cal)
            if fy not in a_filed or f.filed_date < a_filed[fy]:
                a_filed[fy] = f.filed_date
                annual[fy] = f.value
    return quarterly, annual


def trailing_ltm_eps(
    quarterly: dict[tuple[int, int], float], annual: dict[int, float]
) -> float | None:
    """LTM EPS = sum of the 4 most recent reported quarters (Q4 = FY - Q1 - Q2 - Q3).

    Returns None if fewer than 4 consecutive recent quarters can be assembled.
    """
    complete: dict[tuple[int, int], float] = dict(quarterly)
    for fy, ann in annual.items():
        q1 = quarterly.get((fy, 1))
        q2 = quarterly.get((fy, 2))
        q3 = quarterly.get((fy, 3))
        if q1 is not None and q2 is not None and q3 is not None and (fy, 4) not in complete:
            complete[(fy, 4)] = ann - q1 - q2 - q3
    if len(complete) < 4:
        return None
    recent = sorted(complete, key=lambda k: k[0] * 4 + k[1])[-4:]
    return sum(complete[k] for k in recent)
