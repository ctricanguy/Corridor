"""Factor 1 — the EarningsModel seam.

The earnings figure is the anchor of the whole framework. v1 is a pure consensus
passthrough: the "model" simply returns the snapshotted next-4-quarter consensus
EPS. The point of this interface is that LATER you can drop in your OWN
per-segment estimates that diverge from consensus WITHOUT touching the corridor
or PEG math — they consume an ``EarningsModel``, not yfinance.

This file defines the seam only. The concrete consensus implementation that
reads from the snapshot tables is wired up in Stage 2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EarningsEstimate:
    """The earnings inputs the valuation engine needs for one ticker on one date."""

    ticker: str
    as_of_date: date
    # Sum of the next ``forward_window_quarters`` unreported-quarter EPS estimates.
    forward_eps_ntm: float | None
    # Last-twelve-month (trailing) EPS, for the PEG ntm_vs_ltm growth basis.
    trailing_eps_ltm: float | None
    # Per-quarter detail kept so callers can inspect / chart the build-up.
    quarterly_eps: dict[str, float]
    # Whether the figure is rising vs the prior snapshot (feeds "earnings trend").
    trend_rising: bool | None
    source: str


class EarningsModel(ABC):
    """Interface producing the earnings anchor used by Factor 2 / 2b."""

    name: str

    @abstractmethod
    def estimate(self, ticker: str, as_of: date) -> EarningsEstimate:
        """Return the earnings estimate for ``ticker`` as of ``as_of``."""
        raise NotImplementedError


# NOTE (Stage 2): implement ConsensusEarningsModel(EarningsModel) here. It reads
# ForwardEstimateSnapshot rows for `as_of`, sums the next N unreported quarters,
# pulls trailing LTM EPS from Fundamental rows, and never fabricates a missing
# quarter — a gap is surfaced as None and logged, not guessed.
