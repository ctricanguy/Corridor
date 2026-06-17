"""Compare forward-quarter derivation methods (ANALYSIS ONLY — not used by the engine).

v1 DECISION (adopted): FLAT is the production derivation; yfinance stays an
INDEPENDENT cross-check flag and is NOT blended into the sum; (a) blend and (b)
seasonality are REJECTED for v1. Rationale: within a fiscal year the unreported
quarters must sum to (annual − reported actuals), so flat and (a) yield the SAME
forward sum / True P/E — the number is already anchored by real annual consensus —
and (b) moves it only via a far next-FY quarter using historical shares distorted by
NVDA's ramp (complexity on the weakest data). This module is kept as the evaluation
record; re-run scripts/compare_derivation.py if growth matures and seasonality
becomes a clean proxy.

Three ways to turn the FMP multi-year ANNUAL curve into the next-4 forward quarters:

  * ``flat``        — current production: split the (annual − reported actuals)
                      residual EVENLY across a fiscal year's unknown quarters.
  * ``blend``       — (a) use yfinance's real next-quarter (0q) and quarter-after
                      (+1q) consensus for the nearest 1-2 window quarters; flat for
                      the rest. Accurate near-term but makes yfinance non-independent
                      for those quarters.
  * ``seasonality`` — (b) split the residual by each quarter's HISTORICAL share of
                      the fiscal year (from EDGAR actuals; Q4 = FY − Q1 − Q2 − Q3),
                      recent-weighted. Keeps yfinance fully independent.

Each method is compared against yfinance's independent next-quarter estimates so we
can pick the production method from real numbers. Pure functions; unit-tested.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from ..ingest.forward_sum import build_forward_eps_sum
from ..ingest.records import WindowResult

_PERIOD_RE = re.compile(r"^FY(\d{4})Q([1-4])$")
_FLAT_SHARES = {1: 0.25, 2: 0.25, 3: 0.25, 4: 0.25}


def _fy_q(label: str) -> tuple[int, int]:
    m = _PERIOD_RE.match(label)
    if not m:
        raise ValueError(f"bad quarter label {label!r}")
    return int(m.group(1)), int(m.group(2))


@dataclass(frozen=True)
class MethodResult:
    name: str
    quarters: list[tuple[str, float]]  # (label, eps) in window order
    forward_sum: float
    true_pe: float | None
    q0_div_pct: float | None  # |q0 − yfinance 0q| / yfinance 0q
    q1_div_pct: float | None  # |q1 − yfinance +1q| / yfinance +1q
    complete: bool
    note: str = ""


def quarterly_shares_from_edgar(
    quarterly_actuals: dict[tuple[int, int], float],
    annual_actuals: dict[int, float],
    recent_years: int = 3,
) -> tuple[dict[int, float], dict[int, dict[int, float]]]:
    """Historical Q1-Q4 shares of the fiscal year, recent-weighted.

    ``quarterly_actuals`` maps (fy, q)->EPS (Q1-Q3 from 10-Qs); ``annual_actuals``
    maps fy->FY EPS (10-K). Q4 is inferred as FY − Q1 − Q2 − Q3. Returns
    ``(weighted_shares, per_year_shares)`` so the caller can SEE whether a steep
    ramp distorts the shares year to year. Falls back to flat 0.25s if no complete
    history exists.
    """
    per_year: dict[int, dict[int, float]] = {}
    for fy, ann in annual_actuals.items():
        q1 = quarterly_actuals.get((fy, 1))
        q2 = quarterly_actuals.get((fy, 2))
        q3 = quarterly_actuals.get((fy, 3))
        if None in (q1, q2, q3) or not ann:
            continue
        q4 = ann - q1 - q2 - q3  # type: ignore[operator]
        per_year[fy] = {1: q1 / ann, 2: q2 / ann, 3: q3 / ann, 4: q4 / ann}  # type: ignore[operator]

    years = sorted(per_year)[-recent_years:]
    if not years:
        return dict(_FLAT_SHARES), per_year
    weights = {y: i + 1 for i, y in enumerate(years)}  # more recent -> larger weight
    total_w = sum(weights.values())
    avg = {q: sum(per_year[y][q] * weights[y] for y in years) / total_w for q in (1, 2, 3, 4)}
    norm = sum(avg.values()) or 1.0
    return {q: avg[q] / norm for q in avg}, per_year


def _quarters(components) -> list[tuple[str, float]]:  # type: ignore[no-untyped-def]
    return [(c.fiscal_period, c.value) for c in components]


def flat_method(
    window: WindowResult, annual: dict[str, float], reported_actuals: dict[str, float]
) -> tuple[list[tuple[str, float]], bool]:
    res = build_forward_eps_sum(window, {}, annual, reported_actuals)
    return _quarters(res.components), res.complete


def blend_method(
    window: WindowResult,
    annual: dict[str, float],
    reported_actuals: dict[str, float],
    yf_0q: float | None,
    yf_1q: float | None,
) -> tuple[list[tuple[str, float]], bool]:
    """(a) Inject yfinance 0q/+1q as the nearest quarters; flat for the rest."""
    labels = [p.fiscal_period for p in window.periods]
    quarterly: dict[str, float] = {}
    if yf_0q is not None and labels:
        quarterly[labels[0]] = yf_0q
    if yf_1q is not None and len(labels) > 1:
        quarterly[labels[1]] = yf_1q
    res = build_forward_eps_sum(window, quarterly, annual, reported_actuals)
    return _quarters(res.components), res.complete


def seasonality_method(
    window: WindowResult,
    annual: dict[str, float],
    reported_actuals: dict[str, float],
    shares: dict[int, float],
) -> tuple[list[tuple[str, float]], bool]:
    """(b) Split each FY's residual across its unknown quarters by seasonal share."""
    by_fy: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for p in window.periods:
        fy, q = _fy_q(p.fiscal_period)
        by_fy[fy].append((p.fiscal_period, q))

    out: dict[str, float] = {}
    complete = True
    for fy, quarters in by_fy.items():
        ann = annual.get(f"FY{fy}")
        if ann is None:
            complete = False
            continue
        known = sum(v for k, v in reported_actuals.items() if k.startswith(f"FY{fy}Q"))
        residual = ann - known
        unknown_all = [qq for qq in (1, 2, 3, 4) if f"FY{fy}Q{qq}" not in reported_actuals]
        denom = sum(shares.get(qq, 0.25) for qq in unknown_all) or 1.0
        for label, q in quarters:
            out[label] = residual * shares.get(q, 0.25) / denom

    ordered = [(p.fiscal_period, out[p.fiscal_period])
               for p in window.periods if p.fiscal_period in out]
    return ordered, complete and len(ordered) == len(window.periods)


def _div(value: float | None, ref: float | None) -> float | None:
    if value is None or ref is None or ref == 0:
        return None
    return abs(value - ref) / abs(ref)


def build_method_result(
    name: str,
    quarters: list[tuple[str, float]],
    complete: bool,
    price: float | None,
    yf_0q: float | None,
    yf_1q: float | None,
    note: str = "",
) -> MethodResult:
    fwd_sum = sum(e for _, e in quarters)
    true_pe = (price / fwd_sum) if (price and fwd_sum) else None
    q0 = quarters[0][1] if quarters else None
    q1 = quarters[1][1] if len(quarters) > 1 else None
    return MethodResult(
        name=name, quarters=quarters, forward_sum=fwd_sum, true_pe=true_pe,
        q0_div_pct=_div(q0, yf_0q), q1_div_pct=_div(q1, yf_1q), complete=complete, note=note,
    )
