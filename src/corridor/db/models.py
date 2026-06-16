"""Corridor SQLite schema — point-in-time, immutable-snapshot storage.

THE single most important architectural decision in this project lives here.

The whole methodology depends on an honest history of *forward* estimates.
Every time we pull "next-4-quarter EPS" we snapshot it WITH the as-of date and
NEVER overwrite it. A naive overwrite destroys the True P/E history and silently
lookahead-biases every backtest. So the storage layer is built around immutable,
timestamped snapshots from day one.

Two categories of data, treated differently on purpose:

  * FORWARD ESTIMATES (``forward_estimate_snapshots``) — what consensus *expected*
    on a given date. Free sources only give you TODAY's view, so this history is
    THIN at launch and GROWS as the daily job accumulates it. Backfill by
    pretending today's estimate applied in the past is FORBIDDEN. Only real
    dated snapshots count. A paid historical-estimates provider can later load
    rows into this same table (distinguished by ``source``) without any engine
    change.

  * TRAILING / AS-REPORTED FINANCIALS (``fundamentals``) — what a company actually
    reported. EDGAR gives this accurately with filing dates, so backfill is
    honest. We still keep ``filed_date`` so a backtest knows when each fact
    became public (a Q4 result filed Feb 15 was not knowable on Feb 1).

Derived outputs (``valuation_snapshots``, ``overlay_snapshots``) are fully
recomputable from the raw tables; they are cached for speed and to record what
the engine concluded on each date.

Source-of-truth note: these ORM models ARE the schema. ``scripts/dump_schema.py``
prints the generated DDL for human review; there is no separately maintained
.sql file to drift out of sync.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all Corridor tables."""


# Tables whose rows are an immutable historical record. Application code must
# only ever INSERT into these; init_db installs DB triggers that reject UPDATE
# and DELETE so point-in-time integrity cannot be violated even by accident.
IMMUTABLE_TABLES: tuple[str, ...] = (
    "forward_estimate_snapshots",
    "fundamentals",
    "realized_actuals",
)


class Security(Base):
    """The watchlist universe — one row per tracked ticker (a dimension table)."""

    __tablename__ = "securities"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    cik: Mapped[str | None] = mapped_column(String(10), index=True)  # SEC Central Index Key
    name: Mapped[str | None] = mapped_column(String(128))
    sector: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    price_snapshots: Mapped[list[PriceSnapshot]] = relationship(back_populates="security")
    forward_estimates: Mapped[list[ForwardEstimateSnapshot]] = relationship(
        back_populates="security"
    )


class PriceSnapshot(Base):
    """Daily OHLCV price bars.

    Prices are market-observed and not strictly point-in-time fragile the way
    forward estimates are (the close on date T is the close on date T). We do
    keep ``adj_close`` separately because split/dividend adjustment changes it
    over time, and we record ``ingested_at`` for provenance. This table is NOT
    immutable: re-pulling to refresh split adjustments is legitimate.
    """

    __tablename__ = "price_snapshots"
    __table_args__ = (
        UniqueConstraint("ticker", "price_date", "source", name="uq_price_ticker_date_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    price_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)  # RAW (unadjusted) close
    adj_close: Mapped[float | None] = mapped_column(Float)  # split/dividend adjusted
    volume: Mapped[int | None] = mapped_column(Integer)
    # This bar's split ratio (e.g. 10.0 on a 10:1 ex-date, else 1.0). Lets the
    # True P/E sanity gate tell a legitimate split-driven rescale apart from an
    # anomalous jump. The canonical True P/E uses RAW close paired with RAW EPS,
    # which is split-consistent by construction (both rescale on the same date).
    split_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")  # price currency
    source: Mapped[str] = mapped_column(String(32), default="yfinance")
    # UTC instant the bar was fetched (provenance; distinct from price_date).
    observation_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    security: Mapped[Security] = relationship(back_populates="price_snapshots")


class ForwardEstimateSnapshot(Base):
    """IMMUTABLE point-in-time forward consensus estimates — the heart of Corridor.

    Each row is: "as of ``as_of_date``, consensus for fiscal period
    ``fiscal_period`` (a quarter or a full year) of ``metric`` was ``value``,
    per ``source``." We snapshot the next several unreported quarters (and out
    years for PEG's forward CAGR) every day and NEVER overwrite them.

    True_PE_t = Price_t / sum(next 4 unreported-quarter EPS, as snapshotted on t).

    ``source`` carries provenance so a purchased historical-estimates dataset can
    be loaded alongside our own daily accumulation; the corridor math treats both
    identically. The unique constraint allows exactly one observation per
    (ticker, as_of_date, period, metric, source) — re-runs on the same day are
    idempotent rather than creating duplicates or overwrites.
    """

    __tablename__ = "forward_estimate_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "ticker",
            "as_of_date",
            "fiscal_period",
            "metric",
            "source",
            name="uq_fwd_ticker_asof_period_metric_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    # POINT IN TIME: the date this estimate was observed/snapshotted.
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    # 'quarter' or 'annual' — annual rows feed PEG's multi-year forward CAGR.
    period_type: Mapped[str] = mapped_column(String(8))
    # Which period the estimate is FOR, e.g. '2026Q1' or 'FY2027'.
    fiscal_period: Mapped[str] = mapped_column(String(12))
    period_end_date: Mapped[date | None] = mapped_column(Date)
    # 'eps' or 'revenue'.
    metric: Mapped[str] = mapped_column(String(16))
    value: Mapped[float] = mapped_column(Float)
    num_analysts: Mapped[int | None] = mapped_column(Integer)
    # --- provenance carried on EVERY stored value (Stage 1 invariant) ---
    # EPS basis, e.g. 'adjusted_diluted' (non-GAAP consensus). Never mixed
    # silently with the GAAP basis of realized actuals.
    basis: Mapped[str] = mapped_column(String(32), default="adjusted_diluted")
    # Report currency for this estimate (drives the hard currency-match guard).
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    # EXACTLY how this value was built: a raw provider quarterly estimate, or a
    # quarter derived by splitting an annual estimate. Never anonymous.
    construction_method: Mapped[str] = mapped_column(String(48), default="real_quarterly")
    # True when this quarter was derived from an annual figure (lowers coverage).
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="fmp", index=True)
    # UTC instant observed — finer-grained companion to as_of_date.
    observation_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    security: Mapped[Security] = relationship(back_populates="forward_estimates")


class Fundamental(Base):
    """IMMUTABLE trailing / as-reported financials from EDGAR.

    Backfill here is honest — these are facts a company actually reported. We
    keep ``filed_date`` so a point-in-time backtest knows when each fact became
    public. A restatement arrives as a NEW row (different filed_date), never an
    overwrite, preserving the original observation.
    """

    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint(
            "ticker",
            "fiscal_period",
            "metric",
            "filed_date",
            "source",
            name="uq_fund_ticker_period_metric_filed_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    cik: Mapped[str | None] = mapped_column(String(10), index=True)
    fiscal_period: Mapped[str] = mapped_column(String(12))  # e.g. '2025Q4', 'FY2025'
    period_end_date: Mapped[date | None] = mapped_column(Date)
    filed_date: Mapped[date | None] = mapped_column(Date, index=True)
    metric: Mapped[str] = mapped_column(String(32))  # e.g. 'eps_diluted', 'revenue'
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(16))  # 'USD', 'USD/shares', ...
    form: Mapped[str | None] = mapped_column(String(8))  # '10-Q', '10-K', '8-K'
    source: Mapped[str] = mapped_column(String(32), default="edgar")
    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ValuationSnapshot(Base):
    """DERIVED Factor-2 / 2b output for one ticker on one date.

    Fully recomputable from the raw snapshots; cached here for speed and to keep
    a record of what the engine concluded. ``history_days`` (how many days of
    REAL snapshot history back this calc) and ``growth_basis`` (the explicit PEG
    growth input) are first-class columns so the honesty constraints — "never
    present a thin-history chart as deep history" and "PEG is only as honest as
    its growth input" — are enforced by the data model, not left to the UI.
    """

    __tablename__ = "valuation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "as_of_date", "engine_version", name="uq_val_ticker_asof_engine"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)

    price: Mapped[float | None] = mapped_column(Float)  # the RAW close paired with the sum
    forward_eps_ntm: Mapped[float | None] = mapped_column(Float)  # sum of next 4 qtr EPS
    true_pe: Mapped[float | None] = mapped_column(Float)

    # --- Stage 1 provenance + quality (populated by the data pipeline) ---
    # EXACTLY how the 4Q sum was built, e.g.
    # "2 real quarterly (2026Q3,2026Q4) + 2 derived from FY2027 annual".
    construction_method: Mapped[str | None] = mapped_column(String(160))
    # Fraction of the 4Q sum that is real quarterly estimates vs derived (0..1).
    coverage_score: Mapped[float | None] = mapped_column(Float)
    price_basis: Mapped[str | None] = mapped_column(String(24))  # 'raw'
    eps_basis: Mapped[str | None] = mapped_column(String(32))  # 'adjusted_diluted'
    price_currency: Mapped[str | None] = mapped_column(String(8))
    report_currency: Mapped[str | None] = mapped_column(String(8))
    # Cross-check: provider-native NTM EPS vs our strict 4Q sum.
    ntm_eps_native: Mapped[float | None] = mapped_column(Float)
    ntm_divergence_pct: Mapped[float | None] = mapped_column(Float)
    window_divergence_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    # Multi-source price reconciliation (both values logged, flag set on disagree).
    price_yf: Mapped[float | None] = mapped_column(Float)
    price_fmp: Mapped[float | None] = mapped_column(Float)
    price_disagreement_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    observation_timestamp: Mapped[datetime | None] = mapped_column(DateTime)  # UTC

    # Historical forward-P/E distribution (the corridor, in multiple space).
    pe_median: Mapped[float | None] = mapped_column(Float)
    pe_pctl_low: Mapped[float | None] = mapped_column(Float)
    pe_pctl_high: Mapped[float | None] = mapped_column(Float)
    # Corridor translated into price space = band percentile x current fwd EPS.
    corridor_low: Mapped[float | None] = mapped_column(Float)
    corridor_high: Mapped[float | None] = mapped_column(Float)
    pe_percentile: Mapped[float | None] = mapped_column(Float)  # where price sits now (0-100)

    # Factor 2b — forward PEG. Kept SEPARATE from any premium term on purpose.
    forward_peg: Mapped[float | None] = mapped_column(Float)
    growth_rate: Mapped[float | None] = mapped_column(Float)
    growth_basis: Mapped[str | None] = mapped_column(String(16))  # 'ntm_vs_ltm' | 'fwd_cagr'
    peg_suppressed: Mapped[bool] = mapped_column(Boolean, default=False)  # growth guardrail

    # Honesty: how much REAL snapshot history backs the valuation charts.
    history_days: Mapped[int | None] = mapped_column(Integer)
    is_thin_history: Mapped[bool] = mapped_column(Boolean, default=True)

    signal: Mapped[str | None] = mapped_column(String(16))  # hard_buy|buy|hold|trim|hard_trim
    notes: Mapped[str | None] = mapped_column(Text)

    engine_version: Mapped[str] = mapped_column(String(16), default="0.1.0")
    computed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OverlaySnapshot(Base):
    """DERIVED Factor-3 output (technicals + base-rate read) for one ticker/date.

    Recomputable from price history; cached alongside valuation for the dashboard
    and memo. The streak study itself lives as a standalone, testable function in
    the engine — this table just stores its per-ticker read.
    """

    __tablename__ = "overlay_snapshots"
    __table_args__ = (
        UniqueConstraint("ticker", "as_of_date", name="uq_overlay_ticker_asof"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)

    rsi: Mapped[float | None] = mapped_column(Float)
    dist_50ma: Mapped[float | None] = mapped_column(Float)  # % distance from 50d MA
    dist_200ma: Mapped[float | None] = mapped_column(Float)  # % distance from 200d MA
    weekly_streak: Mapped[int | None] = mapped_column(Integer)  # consecutive weekly gains
    streak_percentile: Mapped[float | None] = mapped_column(Float)
    days_to_earnings: Mapped[int | None] = mapped_column(Integer)
    earnings_flag: Mapped[str | None] = mapped_column(String(16))  # pre|post|none

    computed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EarningsCalendar(Base):
    """Upcoming/observed earnings dates per ticker, snapshotted with as-of date.

    Used for pre/post-earnings positioning flags. Snapshotting the as-of date
    keeps the calendar point-in-time (expected dates shift before they confirm).
    """

    __tablename__ = "earnings_calendar"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "fiscal_period", "as_of_date", name="uq_earncal_ticker_period_asof"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    fiscal_period: Mapped[str | None] = mapped_column(String(12))
    expected_date: Mapped[date | None] = mapped_column(Date)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(32), default="yfinance")
    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IngestionLog(Base):
    """Audit trail for every ingestion run — NO silent failures on data gaps.

    Each fetch records whether it succeeded, was partial, found nothing, or
    errored, plus a human-readable detail. The dashboard surfaces recent
    'partial'/'missing'/'error' rows so data holes are visible, not hidden.
    """

    __tablename__ = "ingestion_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    job: Mapped[str] = mapped_column(String(32))  # 'daily_refresh', 'backfill_edgar', ...
    ticker: Mapped[str | None] = mapped_column(String(16), index=True)
    source: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # ok|partial|missing|error|quarantine
    detail: Mapped[str | None] = mapped_column(Text)
    rows_written: Mapped[int | None] = mapped_column(Integer)
    # For quarantined values: the machine reason code (see corridor.constants).
    reason_code: Mapped[str | None] = mapped_column(String(48), index=True)


class RealizedActual(Base):
    """IMMUTABLE realized EPS actuals + the beat/miss vs the pre-report estimate.

    When a quarter reports, we pull the EDGAR actual (GAAP diluted from continuing
    operations) with its ``filed_date`` and compare it to the estimate snapshot
    that was LIVE the day before the report. This is the feedback loop that makes
    the data useful over time: it accumulates evidence about whether a given
    source's consensus is any good for these names.

    BASIS HONESTY: estimates are non-GAAP ``adjusted_diluted`` while EDGAR actuals
    are ``gaap_diluted_continuing_ops``. These bases differ, so ``basis_mismatch``
    is set whenever they do and the surprise is marked low-confidence rather than
    silently treated as like-for-like. A same-basis (non-GAAP actual) comparison
    is a later refinement; the hook is here.
    """

    __tablename__ = "realized_actuals"
    __table_args__ = (
        UniqueConstraint(
            "ticker",
            "fiscal_period",
            "estimate_source",
            "filed_date",
            name="uq_actual_ticker_period_estsrc_filed",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("securities.ticker"), index=True)
    fiscal_period: Mapped[str] = mapped_column(String(12))
    period_end_date: Mapped[date | None] = mapped_column(Date)
    report_date: Mapped[date | None] = mapped_column(Date, index=True)
    filed_date: Mapped[date | None] = mapped_column(Date)

    actual_eps: Mapped[float | None] = mapped_column(Float)
    actual_basis: Mapped[str | None] = mapped_column(String(32))  # gaap_diluted_continuing_ops

    # The estimate snapshot that was live the day BEFORE the report.
    estimate_eps: Mapped[float | None] = mapped_column(Float)
    estimate_basis: Mapped[str | None] = mapped_column(String(32))  # adjusted_diluted
    estimate_source: Mapped[str] = mapped_column(String(32))
    estimate_as_of_date: Mapped[date | None] = mapped_column(Date)

    surprise_abs: Mapped[float | None] = mapped_column(Float)  # actual - estimate
    surprise_pct: Mapped[float | None] = mapped_column(Float)
    beat: Mapped[bool | None] = mapped_column(Boolean)  # sign of the surprise
    basis_mismatch: Mapped[bool] = mapped_column(Boolean, default=False)

    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SourceAccuracy(Base):
    """Recomputable rollup of per-source estimate accuracy (MUTABLE / upsert).

    Aggregates RealizedActual rows so we can later weight or distrust a source on
    evidence. ``ticker`` is NULL for an all-names rollup. Recomputable from the
    immutable actuals, so this table is upserted, not point-in-time.
    """

    __tablename__ = "source_accuracy"
    __table_args__ = (
        UniqueConstraint("source", "ticker", name="uq_srcacc_source_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    ticker: Mapped[str | None] = mapped_column(String(16), index=True)  # NULL = overall
    n_observations: Mapped[int] = mapped_column(Integer, default=0)
    mean_abs_pct_error: Mapped[float | None] = mapped_column(Float)
    median_pct_error: Mapped[float | None] = mapped_column(Float)
    hit_rate: Mapped[float | None] = mapped_column(Float)  # fraction where sign matched
    same_basis_only: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
