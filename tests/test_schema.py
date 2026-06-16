"""Stage 0 schema smoke tests.

These lock in the single most important architectural property: forward-estimate
snapshots are append-only and cannot be overwritten. If a future change weakens
the immutability guarantee, these tests fail loudly.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from corridor.db import database, session_scope
from corridor.db.models import ForwardEstimateSnapshot, Security


def test_all_tables_created(db_url: str) -> None:
    engine = database.get_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    expected = {
        "securities",
        "price_snapshots",
        "forward_estimate_snapshots",
        "fundamentals",
        "valuation_snapshots",
        "overlay_snapshots",
        "earnings_calendar",
        "ingestion_log",
    }
    assert expected <= tables


def _seed_estimate(ticker: str = "NVDA") -> None:
    with session_scope() as s:
        s.add(Security(ticker=ticker, name="NVIDIA Corp"))
    with session_scope() as s:
        s.add(
            ForwardEstimateSnapshot(
                ticker=ticker,
                as_of_date=date(2026, 6, 16),
                period_type="quarter",
                fiscal_period="2026Q3",
                period_end_date=date(2026, 9, 30),
                metric="eps",
                value=1.23,
                num_analysts=40,
                source="yfinance",
            )
        )


def test_forward_estimate_insert_then_read(db_url: str) -> None:
    database.get_engine(db_url)
    _seed_estimate()
    with session_scope() as s:
        rows = s.query(ForwardEstimateSnapshot).all()
        assert len(rows) == 1
        assert rows[0].value == pytest.approx(1.23)


def test_forward_estimate_update_is_blocked(db_url: str) -> None:
    """Point-in-time integrity: UPDATE on snapshots must be rejected by the DB."""
    database.get_engine(db_url)
    _seed_estimate()
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(
            text(
                "UPDATE forward_estimate_snapshots SET value = 9.99 "
                "WHERE ticker = 'NVDA'"
            )
        )


def test_forward_estimate_delete_is_blocked(db_url: str) -> None:
    """Point-in-time integrity: DELETE on snapshots must be rejected by the DB."""
    database.get_engine(db_url)
    _seed_estimate()
    with pytest.raises(IntegrityError), session_scope() as s:
        s.execute(text("DELETE FROM forward_estimate_snapshots WHERE ticker = 'NVDA'"))
    # Row still present after the blocked delete.
    with session_scope() as s:
        assert s.query(ForwardEstimateSnapshot).count() == 1


def test_new_observation_is_a_new_row_not_an_overwrite(db_url: str) -> None:
    """A later snapshot for the same period/metric on a NEW as_of_date coexists."""
    database.get_engine(db_url)
    _seed_estimate()
    with session_scope() as s:
        s.add(
            ForwardEstimateSnapshot(
                ticker="NVDA",
                as_of_date=date(2026, 6, 17),  # next day
                period_type="quarter",
                fiscal_period="2026Q3",
                period_end_date=date(2026, 9, 30),
                metric="eps",
                value=1.25,
                num_analysts=41,
                source="yfinance",
            )
        )
    with session_scope() as s:
        vals = sorted(r.value for r in s.query(ForwardEstimateSnapshot).all())
        assert vals == pytest.approx([1.23, 1.25])
