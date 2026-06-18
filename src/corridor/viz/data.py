"""DB read layer for the dashboard — all SQL lives here, charts stay pure.

Every query filters to the current engine_version so the dashboard never
blends certified and uncertified rows. The caller receives plain dicts /
DataFrames, not ORM objects.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd


def _session():
    """Lazily open a read-only session (avoids import-time DB init)."""
    from corridor.config import get_settings
    from corridor.db import init_db, session_scope

    settings = get_settings()
    init_db(settings.database_url)
    return session_scope


def load_universe() -> list[str]:
    """Return all tickers that have at least one certified valuation snapshot."""
    from corridor.db.models import ValuationSnapshot
    from corridor.config import load_config

    cfg = load_config()
    scope = session_scope = _session()
    with scope() as s:
        rows = (
            s.query(ValuationSnapshot.ticker)
            .filter(ValuationSnapshot.engine_version == cfg.engine_version)
            .distinct()
            .order_by(ValuationSnapshot.ticker)
            .all()
        )
    return [r.ticker for r in rows]


def load_valuation_history(ticker: str) -> pd.DataFrame:
    """Full valuation_snapshots history for one ticker (certified rows only).

    Returns a DataFrame indexed by as_of_date, sorted ascending. Columns map
    1:1 to the ValuationSnapshot columns the dashboard cares about.
    """
    from corridor.db.models import ValuationSnapshot
    from corridor.config import load_config

    cfg = load_config()
    scope = _session()
    with scope() as s:
        rows = (
            s.query(ValuationSnapshot)
            .filter(
                ValuationSnapshot.ticker == ticker,
                ValuationSnapshot.engine_version == cfg.engine_version,
            )
            .order_by(ValuationSnapshot.as_of_date)
            .all()
        )
        data = [
            {
                "as_of_date": r.as_of_date,
                "price": r.price,
                "forward_eps_ntm": r.forward_eps_ntm,
                "true_pe": r.true_pe,
                "pe_median": r.pe_median,
                "pe_pctl_low": r.pe_pctl_low,
                "pe_pctl_high": r.pe_pctl_high,
                "corridor_low": r.corridor_low,
                "corridor_high": r.corridor_high,
                "pe_percentile": r.pe_percentile,
                "forward_peg": r.forward_peg,
                "growth_rate": r.growth_rate,
                "growth_basis": r.growth_basis,
                "peg_suppressed": r.peg_suppressed,
                "history_days": r.history_days,
                "is_thin_history": r.is_thin_history,
                "signal": r.signal,
                "notes": r.notes,
                "coverage_score": r.coverage_score,
                "construction_method": r.construction_method,
                "window_divergence_flag": r.window_divergence_flag,
                "price_disagreement_flag": r.price_disagreement_flag,
                "quarterly_xcheck_flag": r.quarterly_xcheck_flag,
            }
            for r in rows
        ]
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["as_of_date"] = pd.to_datetime(df["as_of_date"])
    return df.set_index("as_of_date").sort_index()


def load_overlay(ticker: str) -> dict[str, Any] | None:
    """Latest overlay snapshot (technicals) for one ticker."""
    from corridor.db.models import OverlaySnapshot

    scope = _session()
    with scope() as s:
        row = (
            s.query(OverlaySnapshot)
            .filter(OverlaySnapshot.ticker == ticker)
            .order_by(OverlaySnapshot.as_of_date.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "as_of_date": row.as_of_date,
            "rsi": row.rsi,
            "dist_50ma": row.dist_50ma,
            "dist_200ma": row.dist_200ma,
            "weekly_streak": row.weekly_streak,
            "streak_percentile": row.streak_percentile,
            "days_to_earnings": row.days_to_earnings,
            "earnings_flag": row.earnings_flag,
        }


def load_annual_estimates(ticker: str) -> pd.DataFrame:
    """Latest annual EPS estimates per fiscal year for the forward projection line.

    Returns future-only rows (period_end_date > today), sorted by period end.
    Each row has: fiscal_period, period_end_date (datetime), value (annual EPS).
    Empty DataFrame when no annual estimates exist.
    """
    from corridor.db.models import ForwardEstimateSnapshot
    from sqlalchemy import func
    import datetime as _dt

    scope = _session()
    with scope() as s:
        latest_date = (
            s.query(func.max(ForwardEstimateSnapshot.as_of_date))
            .filter(
                ForwardEstimateSnapshot.ticker == ticker,
                ForwardEstimateSnapshot.period_type == "annual",
                ForwardEstimateSnapshot.metric == "eps",
            )
            .scalar()
        )
        if latest_date is None:
            return pd.DataFrame()
        rows = (
            s.query(ForwardEstimateSnapshot)
            .filter(
                ForwardEstimateSnapshot.ticker == ticker,
                ForwardEstimateSnapshot.as_of_date == latest_date,
                ForwardEstimateSnapshot.period_type == "annual",
                ForwardEstimateSnapshot.metric == "eps",
                ForwardEstimateSnapshot.period_end_date.isnot(None),
            )
            .order_by(ForwardEstimateSnapshot.period_end_date)
            .all()
        )
        data = [
            {
                "fiscal_period": r.fiscal_period,
                "period_end_date": r.period_end_date,
                "value": r.value,
                "as_of_date": r.as_of_date,
            }
            for r in rows
        ]
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["period_end_date"] = pd.to_datetime(df["period_end_date"])
    today = pd.Timestamp.now().normalize()
    return df[df["period_end_date"] > today].sort_values("period_end_date").reset_index(drop=True)

    """One row per ticker: the most-recent certified valuation snapshot.

    Used for the watchlist overview table.
    """
    from corridor.db.models import ValuationSnapshot
    from corridor.config import load_config
    from sqlalchemy import func

    cfg = load_config()
    scope = _session()
    with scope() as s:
        # Subquery: max as_of_date per ticker
        sub = (
            s.query(
                ValuationSnapshot.ticker,
                func.max(ValuationSnapshot.as_of_date).label("max_date"),
            )
            .filter(ValuationSnapshot.engine_version == cfg.engine_version)
            .group_by(ValuationSnapshot.ticker)
            .subquery()
        )
        rows = (
            s.query(ValuationSnapshot)
            .join(
                sub,
                (ValuationSnapshot.ticker == sub.c.ticker)
                & (ValuationSnapshot.as_of_date == sub.c.max_date),
            )
            .filter(ValuationSnapshot.engine_version == cfg.engine_version)
            .order_by(ValuationSnapshot.ticker)
            .all()
        )
        data = [
            {
                "ticker": r.ticker,
                "as_of_date": r.as_of_date,
                "price": r.price,
                "true_pe": r.true_pe,
                "pe_percentile": r.pe_percentile,
                "corridor_low": r.corridor_low,
                "corridor_high": r.corridor_high,
                "forward_peg": r.forward_peg,
                "peg_suppressed": r.peg_suppressed,
                "signal": r.signal,
                "coverage_score": r.coverage_score,
                "history_days": r.history_days,
                "is_thin_history": r.is_thin_history,
                "window_divergence_flag": r.window_divergence_flag,
            }
            for r in rows
        ]
    return pd.DataFrame(data) if data else pd.DataFrame()
