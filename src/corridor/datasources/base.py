"""Data-source interfaces — the swappable seam.

The engine must NOT care whether a forward-estimate snapshot came from our own
daily yfinance accumulation or from a purchased historical-estimates dataset.
These abstract base classes define that boundary. Concrete adapters (yfinance,
EDGAR, and later a paid provider) implement them in Stage 1; the corridor/PEG
math depends only on the interface and the shape of the returned records.

Records are intentionally plain dataclasses (not ORM rows) so adapters stay
ignorant of storage. The ingestion layer maps them onto the immutable snapshot
tables. Nothing here fetches anything yet — Stage 1 fills these in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class PriceRecord:
    ticker: str
    price_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None  # RAW (unadjusted) close — the canonical True P/E numerator
    adj_close: float | None  # split/dividend adjusted
    volume: int | None
    source: str
    split_ratio: float = 1.0  # this bar's split (1.0 if none)
    currency: str = "USD"
    observation_timestamp: datetime | None = None  # UTC fetch instant


@dataclass(frozen=True)
class ForwardEstimateRecord:
    """One point-in-time forward consensus estimate.

    ``as_of_date`` is when this estimate was observed — the field that makes the
    whole series honest. ``source`` distinguishes our accumulation from a paid
    backfill loaded into the same table. Provenance (basis, currency,
    construction_method) travels with every value and is never anonymous.
    """

    ticker: str
    as_of_date: date
    period_type: str  # 'quarter' | 'annual'
    fiscal_period: str  # 'FY2026Q1' | 'FY2027'
    period_end_date: date | None
    metric: str  # 'eps' | 'revenue'
    value: float
    num_analysts: int | None
    source: str
    basis: str = "adjusted_diluted"  # non-GAAP consensus basis
    currency: str = "USD"
    construction_method: str = "real_quarterly"
    is_derived: bool = False
    observation_timestamp: datetime | None = None  # UTC fetch instant


@dataclass(frozen=True)
class FundamentalRecord:
    """One as-reported financial fact from EDGAR (trailing; backfill is honest)."""

    ticker: str
    cik: str | None
    fiscal_period: str  # AUTHORITATIVE, DATE-derived (label_period on period_end)
    period_end_date: date | None
    filed_date: date | None
    metric: str
    value: float
    unit: str | None
    form: str | None
    source: str
    basis: str = "gaap_diluted_continuing_ops"  # EDGAR GAAP basis
    # EDGAR's OWN fy/fp label (e.g. 'FY2027Q1'). Filing-relative, so it drifts for
    # comparative periods — kept ONLY to cross-check against the date-derived label,
    # never trusted for matching. See ingest.job.check_label_alignment.
    source_fiscal_period: str | None = None
    observation_timestamp: datetime | None = None  # UTC fetch instant


class PriceSource(ABC):
    """Provides historical/daily price bars."""

    name: str

    @abstractmethod
    def get_prices(self, ticker: str, start: date | None = None) -> list[PriceRecord]:
        """Return price bars for ``ticker`` from ``start`` (inclusive) to latest."""
        raise NotImplementedError


class ForwardEstimateSource(ABC):
    """Provides forward consensus estimates.

    A free source returns only TODAY's view (one ``as_of_date`` == today). A paid
    historical provider returns many past ``as_of_date`` rows. Both satisfy this
    interface; only the breadth of history differs.
    """

    name: str

    @abstractmethod
    def get_forward_estimates(
        self, ticker: str, as_of: date | None = None
    ) -> list[ForwardEstimateRecord]:
        """Return forward estimate records observed as of ``as_of`` (default: today)."""
        raise NotImplementedError


class FundamentalsSource(ABC):
    """Provides trailing as-reported financials (EDGAR companyfacts)."""

    name: str

    @abstractmethod
    def get_fundamentals(self, ticker: str, cik: str | None = None) -> list[FundamentalRecord]:
        """Return as-reported fundamental facts for ``ticker``."""
        raise NotImplementedError
