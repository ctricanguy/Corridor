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
IMMUTABLE_TABLES: tuple[str, ...] = ("forward_estimate_snapshots", "fundamentals")


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
    close: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32), default="yfinance")
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
    source: Mapped[str] = mapped_column(String(32), default="yfinance", index=True)
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

    price: Mapped[float | None] = mapped_column(Float)
    forward_eps_ntm: Mapped[float | None] = mapped_column(Float)  # sum of next 4 qtr EPS
    true_pe: Mapped[float | None] = mapped_column(Float)

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
    status: Mapped[str] = mapped_column(String(16))  # ok | partial | missing | error
    detail: Mapped[str | None] = mapped_column(Text)
    rows_written: Mapped[int | None] = mapped_column(Integer)
