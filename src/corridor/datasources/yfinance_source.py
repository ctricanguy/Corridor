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
        rows, splits, currency = self._fetch(ticker, start)
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
