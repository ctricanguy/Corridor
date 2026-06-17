"""Factor 2 — the forward-P/E "corridor" / True P/E history.

Reads the point-in-time True P/E history (accumulated in valuation_snapshots) and
turns it into a fair-value corridor:

  1. True P/E history  — the daily forward multiple series (one point per snapshot).
  2. Percentile bands  — median + low/high percentiles of that historical multiple.
  3. Price-space corridor — bands x CURRENT forward EPS, i.e. the prices that would
     put the stock at its historical 20th / median / 80th multiple given today's
     earnings. Price riding below the low band = cheap vs its own history.
  4. Position          — where today's multiple sits in its history (0-100).

HONESTY is structural here: the corridor is only as trustworthy as the snapshot
history behind it. ``history_days`` and ``is_thin`` are first-class; a corridor on a
handful of days is returned WITH a loud annotation, never as if it were years of data.

This module is PURE (no I/O): it takes a True P/E history + today's price/EPS and
returns a Corridor. The DB read lives in scripts/corridor.py. It computes the
corridor only — it does NOT emit buy/trim signals (that is the next, separate step).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TruePePoint:
    """One day's True P/E observation (from a stored valuation snapshot)."""

    as_of: date
    true_pe: float


@dataclass(frozen=True)
class CorridorBands:
    """Percentile bands of the historical forward-P/E distribution (multiple space)."""

    pctl_low: int  # e.g. 20
    pctl_high: int  # e.g. 80
    pe_low: float  # low-percentile multiple
    pe_median: float
    pe_high: float  # high-percentile multiple


@dataclass(frozen=True)
class Corridor:
    """The full corridor read for one ticker as of one date (no signal — position only)."""

    ticker: str
    as_of: date
    # Current state
    current_true_pe: float | None
    current_fwd_eps: float | None
    current_price: float | None
    # Bands (multiple space) + corridor (price space)
    bands: CorridorBands | None
    corridor_low_price: float | None
    corridor_mid_price: float | None
    corridor_high_price: float | None
    # Where today's multiple sits in its own history (0-100), and a DESCRIPTIVE
    # position vs the corridor (NOT a buy/trim signal — that comes later).
    pe_percentile: float | None
    position: str
    # Honesty
    history_days: int
    is_thin: bool
    min_history_days: int
    coverage_score: float | None
    notes: str


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (matches numpy's default 'linear' method).

    Implemented by hand so the corridor math is transparent and hand-checkable —
    e.g. percentile([10,20,30,40,50], 20) == 18.0, ([...], 80) == 42.0.
    """
    if not values:
        raise ValueError("percentile of empty sequence")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (p / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def percentile_rank(value: float, values: list[float]) -> float:
    """Percentile rank of ``value`` within ``values`` (0-100, 'mean' method).

    (count strictly below + half of ties) / n. So the cheapest multiple ranks low
    (buy zone) and the richest ranks high — e.g. rank(10, [10,20,30,40,50]) == 10.0,
    rank(30, ...) == 50.0, rank(50, ...) == 90.0.
    """
    n = len(values)
    if n == 0:
        return math.nan
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return 100.0 * (below + 0.5 * equal) / n


def compute_bands(values: list[float], pctl_low: int, pctl_high: int) -> CorridorBands:
    """Median + low/high percentile bands of a forward-P/E series."""
    return CorridorBands(
        pctl_low=pctl_low,
        pctl_high=pctl_high,
        pe_low=percentile(values, pctl_low),
        pe_median=percentile(values, 50),
        pe_high=percentile(values, pctl_high),
    )


def _position(price: float, low: float, mid: float, high: float) -> str:
    """Descriptive position of price vs the corridor (NOT a buy/trim signal)."""
    if price <= low:
        return "below low band (cheap vs history)"
    if price <= mid:
        return "lower half of corridor"
    if price <= high:
        return "upper half of corridor"
    return "above high band (rich vs history)"


def build_corridor(
    ticker: str,
    as_of: date,
    history: list[TruePePoint],
    current_fwd_eps: float | None,
    current_price: float | None,
    *,
    pctl_low: int = 20,
    pctl_high: int = 80,
    min_history_days: int = 60,
    lookback_days: int | None = None,
    coverage_score: float | None = None,
) -> Corridor:
    """Build the corridor from a True P/E history + today's price/EPS.

    The forward sum / True P/E are split-consistent and date-aligned upstream; here
    we only summarize the accumulated history. Returns a corridor WITHOUT bands when
    there is too little history to form a distribution (<2 points), and flags
    ``is_thin`` whenever fewer than ``min_history_days`` of real snapshots back it.
    """
    if lookback_days is not None:
        cutoff = as_of - timedelta(days=lookback_days)
        history = [p for p in history if p.as_of >= cutoff]
    history = sorted(history, key=lambda p: p.as_of)
    history_days = len({p.as_of for p in history})
    is_thin = history_days < min_history_days

    current_true_pe: float | None = None
    if current_price is not None and current_fwd_eps not in (None, 0):
        current_true_pe = current_price / current_fwd_eps  # type: ignore[operator]
    elif history:
        current_true_pe = history[-1].true_pe

    bands: CorridorBands | None = None
    low_p = mid_p = high_p = None
    pe_pct: float | None = None
    position = "insufficient history"
    notes = ""

    if history_days < 2:
        notes = (f"Only {history_days} day(s) of snapshot history — the corridor needs "
                 "accumulation. Run the daily job; bands form as history grows.")
    else:
        values = [p.true_pe for p in history]
        bands = compute_bands(values, pctl_low, pctl_high)
        position = "n/a (no current EPS)"
        if current_fwd_eps is not None and current_fwd_eps != 0:
            low_p = bands.pe_low * current_fwd_eps
            mid_p = bands.pe_median * current_fwd_eps
            high_p = bands.pe_high * current_fwd_eps
            if current_price is not None:
                position = _position(current_price, low_p, mid_p, high_p)
        if current_true_pe is not None:
            pe_pct = percentile_rank(current_true_pe, values)
        if is_thin:
            notes = (f"THIN HISTORY: corridor based on {history_days} day(s) of snapshots "
                     f"(< {min_history_days}). Bands are unstable — interpret with caution.")

    return Corridor(
        ticker=ticker, as_of=as_of, current_true_pe=current_true_pe,
        current_fwd_eps=current_fwd_eps, current_price=current_price, bands=bands,
        corridor_low_price=low_p, corridor_mid_price=mid_p, corridor_high_price=high_p,
        pe_percentile=pe_pct, position=position, history_days=history_days, is_thin=is_thin,
        min_history_days=min_history_days, coverage_score=coverage_score, notes=notes,
    )
