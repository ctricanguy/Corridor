"""Realized-actuals feedback loop.

When a quarter reports, compare the EDGAR actual to the estimate snapshot that was
LIVE the day before the report, and accumulate per-source accuracy so we can later
learn whether a source's consensus is any good for these names.

BASIS HONESTY: EDGAR actuals are GAAP diluted (continuing ops); estimates are
non-GAAP adjusted. When the bases differ we set ``basis_mismatch`` and the surprise
is recorded but marked not-like-for-like — never silently treated as comparable.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EstimateObservation:
    """A dated estimate snapshot for one fiscal period from one source."""

    as_of_date: date
    value: float
    basis: str
    source: str


def select_pre_report_estimate(
    estimates: Sequence[EstimateObservation], report_date: date
) -> EstimateObservation | None:
    """Return the estimate observed latest BEFORE the report date (the 'live' one).

    Strictly before: an estimate stamped on the report date itself may already
    reflect the result and would contaminate the beat/miss.
    """
    prior = [e for e in estimates if e.as_of_date < report_date]
    if not prior:
        return None
    return max(prior, key=lambda e: e.as_of_date)


@dataclass(frozen=True)
class BeatMiss:
    fiscal_period: str
    actual_eps: float
    actual_basis: str
    estimate_eps: float
    estimate_basis: str
    estimate_source: str
    estimate_as_of_date: date
    surprise_abs: float
    surprise_pct: float | None
    beat: bool
    basis_mismatch: bool


def compute_beat_miss(
    fiscal_period: str,
    actual_eps: float,
    actual_basis: str,
    estimate: EstimateObservation,
) -> BeatMiss:
    """Compute the surprise of an actual vs its pre-report estimate."""
    surprise_abs = actual_eps - estimate.value
    surprise_pct = surprise_abs / abs(estimate.value) if estimate.value != 0 else None
    return BeatMiss(
        fiscal_period=fiscal_period,
        actual_eps=actual_eps,
        actual_basis=actual_basis,
        estimate_eps=estimate.value,
        estimate_basis=estimate.basis,
        estimate_source=estimate.source,
        estimate_as_of_date=estimate.as_of_date,
        surprise_abs=surprise_abs,
        surprise_pct=surprise_pct,
        beat=surprise_abs > 0,
        basis_mismatch=actual_basis != estimate.basis,
    )


@dataclass(frozen=True)
class SourceAccuracyStats:
    source: str
    ticker: str | None
    n_observations: int
    mean_abs_pct_error: float | None
    median_pct_error: float | None
    hit_rate: float | None
    same_basis_only: bool


def aggregate_source_accuracy(
    beatmisses: Sequence[BeatMiss],
    source: str,
    ticker: str | None = None,
    same_basis_only: bool = False,
) -> SourceAccuracyStats:
    """Roll up beat/miss records into per-source accuracy stats.

    With ``same_basis_only`` set, only like-for-like comparisons are counted, which
    is the honest stat once non-GAAP actuals are available. Records whose surprise
    percentage is undefined (zero estimate) are excluded from the error metrics.
    """
    rows = [b for b in beatmisses if b.estimate_source == source]
    if same_basis_only:
        rows = [b for b in rows if not b.basis_mismatch]
    pct_errors = [b.surprise_pct for b in rows if b.surprise_pct is not None]
    hits = [b.beat for b in rows]
    return SourceAccuracyStats(
        source=source,
        ticker=ticker,
        n_observations=len(rows),
        mean_abs_pct_error=(statistics.fmean(abs(e) for e in pct_errors) if pct_errors else None),
        median_pct_error=(statistics.median(pct_errors) if pct_errors else None),
        hit_rate=(sum(hits) / len(hits) if hits else None),
        same_basis_only=same_basis_only,
    )
