"""Financial Modeling Prep adapter — PRIMARY forward-estimate source.

!!! UNVALIDATED AGAINST LIVE ENDPOINT !!!
This adapter is written to FMP's documented legacy v3 ``analyst-estimates`` shape
(``/api/v3/analyst-estimates/{symbol}?period=quarter``) but has NOT been run
against the live API in this build (the environment blocks outbound calls and no
key is configured). Field names differ between FMP's legacy v3 and newer "stable"
endpoints, so the most likely real-world break is a renamed JSON key. Run
``scripts/validate_live.py`` with a real key + network to confirm the parsed output
shape before trusting it. Until then, treat its output as structurally-plausible,
not verified.

Network fetch and JSON parsing are separated so the parser can be unit-tested
against fixtures and the live validator can assert the live shape matches them.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from ..constants import EPS_BASIS_ADJUSTED_DILUTED, UNVALIDATED_MARKER
from ..ingest.fiscal import FiscalCalendar, fiscal_year_label, label_period
from .base import ForwardEstimateRecord, ForwardEstimateSource

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://financialmodelingprep.com"
# How far before as_of to retain a row, so a just-ended-but-unreported quarter is
# kept while deep history is dropped (the report-date window then selects).
_TRAILING_BUFFER_DAYS = 120


class FMPForwardEstimateSource(ForwardEstimateSource):
    """Forward quarterly + annual EPS consensus from FMP. UNVALIDATED (see module docstring)."""

    name = "fmp"
    validated = False  # flipped to True only after scripts/validate_live.py passes

    def __init__(
        self,
        api_key: str,
        fiscal_calendars: dict[str, FiscalCalendar],
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.fiscal_calendars = fiscal_calendars
        self.base_url = base_url.rstrip("/")

    # --- network (untested here) ---------------------------------------------
    def _fetch(self, ticker: str, period: str) -> list[dict[str, Any]]:
        """GET analyst-estimates for ``period`` ('quarter'|'annual'). Network call."""
        import requests  # local import so the package imports without the dep present

        url = f"{self.base_url}/api/v3/analyst-estimates/{ticker}"
        params = {"period": period, "apikey": self.api_key}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise ValueError(f"FMP returned non-list payload for {ticker} {period}: {payload!r}")
        return payload

    def get_forward_estimates(
        self, ticker: str, as_of: date | None = None
    ) -> list[ForwardEstimateRecord]:
        as_of = as_of or datetime.now(UTC).date()
        cal = self.fiscal_calendars.get(ticker)
        if cal is None:
            raise KeyError(f"No fiscal calendar configured for {ticker}")
        q = self.parse_estimates(self._fetch(ticker, "quarter"), ticker, as_of, "quarter", cal)
        a = self.parse_estimates(self._fetch(ticker, "annual"), ticker, as_of, "annual", cal)
        return q + a

    # --- parsing (pure; unit-tested against fixtures) ------------------------
    def parse_estimates(
        self,
        payload: list[dict[str, Any]],
        ticker: str,
        as_of: date,
        period_type: str,
        cal: FiscalCalendar,
        observed_at: datetime | None = None,
    ) -> list[ForwardEstimateRecord]:
        """Map FMP analyst-estimate rows to ForwardEstimateRecords.

        Keeps EPS rows from roughly the last quarter onward (so a just-ended,
        unreported quarter survives) and forward; drops deep history. Rows missing
        a consensus EPS are skipped (never zero-filled).
        """
        observed_at = observed_at or datetime.now(UTC)
        cutoff = date.fromordinal(as_of.toordinal() - _TRAILING_BUFFER_DAYS)
        records: list[ForwardEstimateRecord] = []
        for row in payload:
            raw_date = row.get("date")
            eps = row.get("estimatedEpsAvg")
            if raw_date is None or eps is None:
                continue
            period_end = date.fromisoformat(str(raw_date)[:10])
            if period_end < cutoff:
                continue
            if period_type == "quarter":
                fiscal_period = label_period(period_end, cal)
                method = "real_quarterly"
            else:
                fiscal_period = fiscal_year_label(period_end, cal)
                method = "provider_annual"
            records.append(
                ForwardEstimateRecord(
                    ticker=ticker,
                    as_of_date=as_of,
                    period_type=period_type,
                    fiscal_period=fiscal_period,
                    period_end_date=period_end,
                    metric="eps",
                    value=float(eps),
                    num_analysts=row.get("numberAnalystsEstimatedEps"),
                    source=self.name,
                    basis=EPS_BASIS_ADJUSTED_DILUTED,
                    currency=str(row.get("reportedCurrency", "USD")),
                    construction_method=method,
                    is_derived=False,
                    observation_timestamp=observed_at,
                )
            )
        if not records:
            logger.warning("%s: FMP %s estimates parsed to ZERO rows (%s)", ticker, period_type,
                           UNVALIDATED_MARKER)
        return records

    # --- price cross-check ---------------------------------------------------
    def fetch_price(self, ticker: str, on_date: date) -> float | None:
        """Fetch FMP's close for ``on_date`` (cross-check vs yfinance). Network call."""
        import requests

        url = f"{self.base_url}/api/v3/historical-price-full/{ticker}"
        params = {"from": on_date.isoformat(), "to": on_date.isoformat(), "apikey": self.api_key}
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return self.parse_price(resp.json(), on_date)

    def parse_price(self, payload: dict[str, Any], on_date: date) -> float | None:
        """Extract the close for ``on_date`` from FMP historical-price-full JSON."""
        for row in payload.get("historical", []):
            if str(row.get("date", ""))[:10] == on_date.isoformat():
                close = row.get("close")
                return float(close) if close is not None else None
        return None
