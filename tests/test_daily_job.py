"""Daily-job hardening: self-seeding (FK), price gaps, dry-run, resilience, retry.

These reproduce the two bugs from the first unattended Pi run — FK rejection on a
fresh DB, and yfinance returning zero bars — and lock in the fixes.
"""

from __future__ import annotations

from datetime import date, timezone, timedelta
from unittest.mock import patch

import pytest

from corridor.config import Config, TickerSpec
from corridor.datasources.base import ForwardEstimateRecord, FundamentalRecord, PriceRecord
from corridor.ingest.job import run_daily

AS_OF = date(2025, 6, 16)


def _annual(ticker: str = "NVDA") -> list[ForwardEstimateRecord]:
    return [
        ForwardEstimateRecord(ticker, AS_OF, "annual", "FY2026", date(2026, 1, 25),
                              "eps", 4.40, 40, "fmp"),
        ForwardEstimateRecord(ticker, AS_OF, "annual", "FY2027", date(2027, 1, 31),
                              "eps", 6.00, 40, "fmp"),
    ]


class _Estimates:
    def get_forward_estimates(self, ticker: str, as_of: date | None = None):
        return _annual(ticker)


class _PricesOK:
    def get_prices(self, ticker: str, start: date | None = None):
        return [PriceRecord(ticker, AS_OF, 120.0, 121.0, 119.0, 120.0, 120.0, 1_000, "yfinance")]


class _PricesEmpty:
    """Simulates yfinance returning ZERO bars (the GOOGL/AMD case)."""

    def get_prices(self, ticker: str, start: date | None = None):
        return []


class _NoFundamentals:
    def get_fundamentals(self, ticker: str, cik: str | None = None):
        return []


def _config(universe=None) -> Config:  # type: ignore[no-untyped-def]
    return Config(
        universe=universe or [TickerSpec("NVDA", "NVIDIA", cik="1045810")],
        data={"engine_version": "0.1.0",
              "fiscal_calendars": {"NVDA": {"fy_end_month": 1, "fy_end_day": 31}}},
    )


def _run(session, *, prices=None, estimates=None, config=None, dry_run=False):  # type: ignore[no-untyped-def]
    return run_daily(
        config or _config(), price_source=prices or _PricesOK(),
        estimate_source=estimates or _Estimates(), fundamentals_source=_NoFundamentals(),
        session=session, as_of=AS_OF, dry_run=dry_run,
    )


def test_run_daily_self_seeds_securities_on_fresh_db(db_url: str) -> None:
    # Bug 1: the schema exists but NO securities are seeded. run_daily must upsert the
    # parent row itself, so child inserts are NOT FK-rejected.
    from corridor.db import session_scope
    from corridor.db.models import (
        ForwardEstimateSnapshot,
        PriceSnapshot,
        Security,
        ValuationSnapshot,
    )

    with session_scope() as s:
        assert s.query(Security).count() == 0  # genuinely empty
    with session_scope() as s:
        results = _run(s)
    assert results[0].status == "ok"
    with session_scope() as s:
        assert s.query(Security).filter_by(ticker="NVDA").one_or_none() is not None
        assert s.query(ForwardEstimateSnapshot).count() > 0  # NOT FK-rejected
        assert s.query(PriceSnapshot).count() == 1
        assert s.query(ValuationSnapshot).count() == 1


def test_price_gap_writes_estimates_but_no_valuation(db_url: str) -> None:
    # Bug 2: zero price bars -> log a price gap, write estimates, skip price-dependent rows.
    from corridor.db import session_scope
    from corridor.db.models import (
        ForwardEstimateSnapshot,
        IngestionLog,
        PriceSnapshot,
        ValuationSnapshot,
    )

    with session_scope() as s:
        results = _run(s, prices=_PricesEmpty())
    assert results[0].status == "price_gap"
    with session_scope() as s:
        assert s.query(ForwardEstimateSnapshot).count() > 0  # estimates STILL written
        assert s.query(PriceSnapshot).count() == 0  # no price row from a zero-bar return
        assert s.query(ValuationSnapshot).count() == 0  # no True P/E without a price
        gap = s.query(IngestionLog).filter_by(reason_code="price_gap_no_bars").one_or_none()
        assert gap is not None and gap.status == "missing"


def test_dry_run_writes_nothing(db_url: str) -> None:
    from corridor.db import session_scope
    from corridor.db.models import ForwardEstimateSnapshot, IngestionLog, ValuationSnapshot

    with session_scope() as s:
        results = _run(s, dry_run=True)
    assert results[0].status == "ok"  # computed...
    with session_scope() as s:
        assert s.query(ForwardEstimateSnapshot).count() == 0  # ...but nothing persisted
        assert s.query(ValuationSnapshot).count() == 0
        assert s.query(IngestionLog).count() == 0


def test_one_bad_ticker_does_not_abort_the_run(db_url: str) -> None:
    from corridor.db import session_scope
    from corridor.db.models import ValuationSnapshot

    class _Boom:
        def get_forward_estimates(self, ticker: str, as_of: date | None = None):
            if ticker == "BAD":
                raise RuntimeError("network blip")
            return _annual(ticker)

    cfg = _config(universe=[TickerSpec("BAD", "Bad"), TickerSpec("NVDA", "NVIDIA", cik="1045810")])
    with session_scope() as s:
        results = _run(s, estimates=_Boom(), config=cfg)
    statuses = {r.ticker: r.status for r in results}
    assert statuses["BAD"] == "error"
    assert statuses["NVDA"] == "ok"  # the good ticker still ran AND persisted
    with session_scope() as s:
        assert s.query(ValuationSnapshot).filter_by(ticker="NVDA").count() == 1


def test_cik_enables_actual_subtraction(db_url: str) -> None:
    # The CIK fix: a resolved CIK -> EDGAR fetched -> reported actual subtracted, so the
    # stored True P/E is the CERTIFIED number, not the biased annual/4.
    from corridor.db import session_scope
    from corridor.db.models import ValuationSnapshot

    seen: dict = {}

    class _Fund:
        def get_fundamentals(self, ticker: str, cik: str | None = None):
            seen["cik"] = cik
            return [FundamentalRecord("NVDA", cik, "FY2026Q1", date(2025, 4, 27), date(2025, 5, 28),
                    "eps_diluted", 0.80, "USD/shares", "10-Q", "edgar",
                    source_fiscal_period="FY2026Q1")]

    with session_scope() as s:
        run_daily(_config(), price_source=_PricesOK(), estimate_source=_Estimates(),
                  fundamentals_source=_Fund(), session=s, as_of=AS_OF,
                  cik_by_ticker={"NVDA": "0001045810"})
    assert seen["cik"] == "0001045810"  # the resolved CIK reached EDGAR
    with session_scope() as s:
        true_pe = s.query(ValuationSnapshot).filter_by(ticker="NVDA").one().true_pe
    # actual 0.80 subtracted: FY2026 Q2/Q3/Q4 = (4.40-0.80)/3, FY2027Q1 = 6.00/4,
    # sum = 1.20*3 + 1.50 = 5.10 -> 120/5.10 = 23.53 (NOT the old 120/4.80 = 25.0).
    assert true_pe == pytest.approx(120.0 / 5.10)
    assert abs(true_pe - 25.0) > 1.0


def _fake_market_as_of(fake_et_dt) -> "date":
    """Call _market_as_of() with datetime.now patched to return fake_et_dt."""
    import datetime as _dt
    from corridor.ingest import job as job_mod

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            return fake_et_dt

    with patch.object(job_mod, "datetime", _FakeDatetime):
        return job_mod._market_as_of()


def test_market_as_of_post_et_midnight_before_close() -> None:
    # Regression: 04:22 UTC = 00:22 ET on June 18 (Thursday).
    # Market last closed June 17. Must return June 17, not June 18.
    from zoneinfo import ZoneInfo
    import datetime as _dt

    et = ZoneInfo("America/New_York")
    fake = _dt.datetime(2026, 6, 18, 0, 22, 0, tzinfo=et)  # 00:22 ET = 04:22 UTC
    result = _fake_market_as_of(fake)
    assert result == date(2026, 6, 17), f"Expected 2026-06-17, got {result}"


def test_market_as_of_evening_cron() -> None:
    # Normal cron: 22:00 ET on June 17 (Wednesday, after close).
    # Market closed today at 16:00; as_of = June 17.
    from zoneinfo import ZoneInfo
    import datetime as _dt

    et = ZoneInfo("America/New_York")
    fake = _dt.datetime(2026, 6, 17, 22, 0, 0, tzinfo=et)
    result = _fake_market_as_of(fake)
    assert result == date(2026, 6, 17), f"Expected 2026-06-17, got {result}"


def test_market_as_of_monday_midnight_skips_weekend() -> None:
    # 00:22 ET on Monday June 22 (before close): rolls back to Friday June 19.
    from zoneinfo import ZoneInfo
    import datetime as _dt

    et = ZoneInfo("America/New_York")
    fake = _dt.datetime(2026, 6, 22, 0, 22, 0, tzinfo=et)  # Monday pre-close
    result = _fake_market_as_of(fake)
    assert result == date(2026, 6, 19), f"Expected 2026-06-19 (Friday), got {result}"


def test_yfinance_retries_on_empty_then_succeeds() -> None:
    from corridor.datasources.yfinance_source import YFinancePriceSource

    src = YFinancePriceSource(retries=2, backoff_sec=0.0)  # no real sleep
    calls = {"n": 0}

    def fake_fetch(ticker: str, start):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 3:
            return [], {}, "USD"  # empty twice (yfinance flakiness)
        rows = [{"date": AS_OF, "open": 1.0, "high": 1.0, "low": 1.0,
                 "close": 120.0, "adj_close": 120.0, "volume": 1}]
        return rows, {}, "USD"

    src._fetch = fake_fetch  # type: ignore[method-assign]
    recs = src.get_prices("NVDA")
    assert calls["n"] == 3  # retried until data arrived
    assert len(recs) == 1 and recs[0].close == 120.0
