"""yfinance adapter — PRIMARY price source (raw + adjusted, with split ratios).

!!! UNVALIDATED AGAINST LIVE ENDPOINT !!!
Written to yfinance's ``Ticker.history(auto_adjust=False)`` shape but not run live
in this build. yfinance's Yahoo backend periodically changes columns / cookie
handling; the most likely break is a missing column or an empty frame. Confirm via
``scripts/validate_live.py``.

We store RAW ``Close`` (the canonical True P/E numerator), ``Adj Close``, and each
bar's split ratio so the True P/E sanity gate can distinguish a legitimate split
from an anomaly. Fetch and parse are separated for offline unit testing.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, date, datetime
from typing import Any

from .base import PriceRecord, PriceSource

logger = logging.getLogger(__name__)


class YFinancePriceSource(PriceSource):
    """Daily OHLCV + splits from yfinance. UNVALIDATED (see module docstring)."""

    name = "yfinance"
    validated = False

    def __init__(self, retries: int = 2, backoff_sec: float = 1.5) -> None:
        # yfinance's scraper is flaky and intermittently returns ZERO bars; retry a
        # couple of times with a short backoff before declaring a price gap.
        self.retries = retries
        self.backoff_sec = backoff_sec

    # --- network (untested here) ---------------------------------------------
    def _fetch(
        self, ticker: str, start: date | None
    ) -> tuple[list[dict[str, Any]], dict[date, float], str]:
        """Return (bar rows, {split_date: ratio}, currency) from yfinance."""
        import yfinance as yf  # local import so the package imports without the dep

        t = yf.Ticker(ticker)
        start_str = start.isoformat() if start else None
        hist = t.history(start=start_str, auto_adjust=False, actions=True)
        rows: list[dict[str, Any]] = []
        splits: dict[date, float] = {}
        for idx, row in hist.iterrows():
            d = idx.date()
            rows.append(
                {
                    "date": d,
                    "open": _f(row.get("Open")),
                    "high": _f(row.get("High")),
                    "low": _f(row.get("Low")),
                    "close": _f(row.get("Close")),
                    "adj_close": _f(row.get("Adj Close")),
                    "volume": _i(row.get("Volume")),
                }
            )
            split = row.get("Stock Splits")
            if split and float(split) not in (0.0, 1.0):
                splits[d] = float(split)
        currency = "USD"
        with suppress(Exception):  # fast_info is best-effort
            currency = (t.fast_info.get("currency") or "USD").upper()
        return rows, splits, currency

    def get_prices(self, ticker: str, start: date | None = None) -> list[PriceRecord]:
        """Fetch price bars, RETRYING on a zero-bar return (yfinance flakiness).

        Never raises and never silently returns a bad bar: after exhausting retries it
        returns ``[]`` (which the daily job records as a price gap and skips the
        ticker's price-dependent rows — the forward estimates are still written).
        """
        import time

        rows: list[dict[str, Any]] = []
        splits: dict[date, float] = {}
        currency = "USD"
        for attempt in range(self.retries + 1):
            try:
                rows, splits, currency = self._fetch(ticker, start)
            except Exception as exc:  # network blip / scraper error
                logger.warning("%s: yfinance fetch error (attempt %d/%d): %s",
                               ticker, attempt + 1, self.retries + 1, exc)
                rows = []
            if rows:
                break
            if attempt < self.retries:
                logger.warning("%s: yfinance returned 0 price bars (attempt %d/%d); retrying",
                               ticker, attempt + 1, self.retries + 1)
                time.sleep(self.backoff_sec * (attempt + 1))
        return self.parse_history(rows, splits, ticker, currency)

    # --- parsing (pure; unit-tested against fixtures) ------------------------
    def parse_history(
        self,
        rows: list[dict[str, Any]],
        splits: dict[date, float],
        ticker: str,
        currency: str = "USD",
        observed_at: datetime | None = None,
    ) -> list[PriceRecord]:
        """Build PriceRecords, attaching each bar's split ratio (1.0 if none)."""
        observed_at = observed_at or datetime.now(UTC)
        records: list[PriceRecord] = []
        for r in rows:
            d = r["date"]
            records.append(
                PriceRecord(
                    ticker=ticker,
                    price_date=d,
                    open=r.get("open"),
                    high=r.get("high"),
                    low=r.get("low"),
                    close=r.get("close"),
                    adj_close=r.get("adj_close"),
                    volume=r.get("volume"),
                    source=self.name,
                    split_ratio=splits.get(d, 1.0),
                    currency=currency,
                    observation_timestamp=observed_at,
                )
            )
        if not records:
            logger.warning("%s: yfinance returned ZERO price bars", ticker)
        return records

    # --- forward-EPS cross-check (UNVALIDATED; best-effort, never raises) -----
    def fetch_forward_eps(self, ticker: str) -> dict[str, float]:
        """Best-effort forward EPS consensus by period from yfinance.

        Returns a mapping like ``{'0q': 1.05, '+1q': 1.20, '0y': 4.40, '+1y': 6.00}``
        ('0q' = current/next-to-report quarter — the quarterly granularity FMP's
        annual curve lacks). Returns ``{}`` on any problem; NEVER raises, so it can
        only ever ADD a cross-check, never break the pipeline. UNVALIDATED.
        """
        try:
            import yfinance as yf

            df = yf.Ticker(ticker).get_earnings_estimate()
            rows = [{"period": str(idx), "avg": row.get("avg")} for idx, row in df.iterrows()]
            return self._parse_earnings_estimate(rows)
        except Exception:  # yfinance estimate fields are flaky; degrade silently
            logger.warning("%s: yfinance forward EPS estimate unavailable (cross-check skipped)",
                           ticker)
            return {}

    @staticmethod
    def _parse_earnings_estimate(rows: list[dict[str, Any]]) -> dict[str, float]:
        """Map yfinance earnings-estimate rows to {period: avg_eps} (pure; testable)."""
        out: dict[str, float] = {}
        for r in rows:
            period = r.get("period")
            avg = r.get("avg")
            if not period or avg is None:
                continue
            try:
                out[str(period)] = float(avg)
            except (TypeError, ValueError):
                continue
        return out


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None
