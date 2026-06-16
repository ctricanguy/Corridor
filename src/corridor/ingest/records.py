"""Plain dataclasses shared across the Stage 1 pipeline.

These are storage-agnostic value objects passed between the pure pipeline
functions (window -> forward-sum -> consistency/gates -> valuation input). Keeping
them separate from the ORM keeps the math testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class FiscalPeriod:
    """One fiscal quarter (the company's own calendar, not a calendar quarter).

    ``report_date`` is the earnings date; ``confirmed`` is True once that date is
    an actual/announced date rather than a forward estimate. A period counts as
    REPORTED as of X only when ``confirmed and report_date <= X`` — that is what
    rolls the unreported window the moment a quarter reports.
    """

    fiscal_period: str  # e.g. 'FY2026Q1'
    period_end_date: date
    report_date: date | None
    confirmed: bool = False

    def is_reported_as_of(self, as_of: date, unconfirmed_grace_days: int = 10) -> bool:
        """Whether this quarter's results were out by ``as_of``.

        A CONFIRMED quarter rolls out of the window exactly on its report date. An
        UNCONFIRMED quarter only counts as reported once its (estimated) report date
        is comfortably in the past (``unconfirmed_grace_days``), so we never roll a
        quarter out early on a soft date that might slip — but we also don't treat an
        obviously-past quarter as still pending.
        """
        if self.report_date is None:
            return False
        if self.confirmed:
            return self.report_date <= as_of
        grace_cutoff = date.fromordinal(as_of.toordinal() - unconfirmed_grace_days)
        return self.report_date <= grace_cutoff


@dataclass(frozen=True)
class WindowResult:
    """The next-N unreported fiscal periods as of a date."""

    periods: list[FiscalPeriod]
    requested: int
    available: int

    @property
    def complete(self) -> bool:
        return len(self.periods) == self.requested


@dataclass(frozen=True)
class EstimateComponent:
    """One quarter's contribution to the forward-EPS sum, with its provenance."""

    fiscal_period: str
    value: float
    method: str  # constants.METHOD_REAL_QUARTERLY | METHOD_DERIVED_FROM_ANNUAL
    is_derived: bool
    detail: str  # human description, e.g. 'FY2027 annual 8.00 / 2 missing quarters'


@dataclass(frozen=True)
class ForwardSumResult:
    """The next-4-quarter EPS sum plus EXACTLY how it was built."""

    value: float
    components: list[EstimateComponent]
    construction_method: str  # full human string stored on the valuation snapshot
    coverage_score: float  # fraction real-quarterly (0..1)
    complete: bool  # all requested quarters present (real or derived)

    @property
    def n_real(self) -> int:
        return sum(1 for c in self.components if not c.is_derived)

    @property
    def n_derived(self) -> int:
        return sum(1 for c in self.components if c.is_derived)


@dataclass(frozen=True)
class PricePoint:
    """A single day's price with its split basis and currency."""

    price_date: date
    raw_close: float
    adj_close: float | None
    split_ratio: float  # this day's split (1.0 if none)
    currency: str
    source: str


@dataclass(frozen=True)
class GateOutcome:
    """Result of a write-time sanity gate."""

    passed: bool
    reason_code: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ValuationInput:
    """The clean Stage 1 output for one ticker/date — the corridor's inputs.

    This is what gets written to ``valuation_snapshots`` (band/signal columns are
    left for Stage 2). Quarantined cases never produce one of these; they produce
    an ingestion_log row instead.
    """

    ticker: str
    as_of_date: date
    price: float
    forward_eps_sum: float
    true_pe: float
    coverage_score: float
    construction_method: str
    price_basis: str
    eps_basis: str
    price_currency: str
    report_currency: str
    ntm_eps_native: float | None = None
    ntm_divergence_pct: float | None = None
    window_divergence_flag: bool = False
    price_yf: float | None = None
    price_fmp: float | None = None
    price_disagreement_flag: bool = False
    components: list[EstimateComponent] = field(default_factory=list)
