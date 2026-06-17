"""Financial Modeling Prep adapter — PRIMARY forward-estimate source.

!!! UNVALIDATED AGAINST LIVE ENDPOINT !!!
Written to FMP's CURRENT "stable" API
(``/stable/analyst-estimates?symbol={SYMBOL}&period={quarter|annual}&page=&limit=``)
but not yet run against the live API in this build. The legacy ``/api/v3/`` path is
deprecated and 403s on current plans, so we use ``/stable``; its field names differ
from v3 (``epsAvg`` not ``estimatedEpsAvg``, ``numAnalystsEps`` not
``numberAnalystsEstimatedEps``). Run ``scripts/validate_live.py`` with a real key +
network to confirm the parsed output shape before trusting it.

Network fetch and JSON parsing are separated so the parser can be unit-tested
against fixtures and the live validator can assert the live shape matches them.
The api key is never logged — request errors are redacted before they propagate.
"""

from __future__ import annotations

import logging
import re
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

# Mask the apikey query param anywhere it might appear in a logged URL / error.
_APIKEY_RE = re.compile(r"(apikey=)[^&\s]+", re.IGNORECASE)


def _redact(text: str) -> str:
    """Replace the value of any ``apikey=`` query param with ``***REDACTED***``."""
    return _APIKEY_RE.sub(r"\1***REDACTED***", text)


class FMPForwardEstimateSource(ForwardEstimateSource):
    """Forward quarterly + annual EPS consensus from FMP /stable. UNVALIDATED (see docstring)."""

    name = "fmp"
    validated = False  # flipped to True only after scripts/validate_live.py passes

    def __init__(
        self,
        api_key: str,
        fiscal_calendars: dict[str, FiscalCalendar],
        base_url: str = DEFAULT_BASE_URL,
        page_limit: int = 100,
        max_pages: int = 10,
    ) -> None:
        self.api_key = api_key
        self.fiscal_calendars = fiscal_calendars
        self.base_url = base_url.rstrip("/")
        self.page_limit = page_limit
        self.max_pages = max_pages

    # --- network (untested here) ---------------------------------------------
    def _fetch(self, ticker: str, period: str) -> list[dict[str, Any]]:
        """GET /stable/analyst-estimates for ``period`` ('quarter'|'annual').

        ``symbol`` is a query param (not a path segment); pages through ``limit``-
        sized pages until a short/empty page (or ``max_pages``). Any request error
        is re-raised with the api key redacted so it never reaches a log.
        """
        import requests  # local import so the package imports without the dep present

        url = f"{self.base_url}/stable/analyst-estimates"
        rows: list[dict[str, Any]] = []
        for page in range(self.max_pages):
            params = {
                "symbol": ticker,
                "period": period,
                "page": page,
                "limit": self.page_limit,
                "apikey": self.api_key,
            }
            try:
                resp = requests.get(url, params=params, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"FMP analyst-estimates request failed for {ticker} {period}: "
                    f"{_redact(str(exc))}"
                ) from None
            payload = resp.json()
            if not isinstance(payload, list):
                raise ValueError(
                    f"FMP returned non-list payload for {ticker} {period} page {page}"
                )
            rows.extend(payload)
            if len(payload) < self.page_limit:
                break  # last page reached
        return rows

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
        """Map FMP /stable analyst-estimate rows to ForwardEstimateRecords.

        Stable field names: ``date`` (period end), ``epsAvg`` (consensus EPS),
        ``numAnalystsEps`` (analyst count). The stable analyst-estimates payload
        carries NO currency field, so currency defaults to USD (ADRs are excluded
        in v1 via the config unsupported-list + the hard currency guard). Keeps EPS
        rows from roughly the last quarter onward and forward; rows missing a
        consensus EPS are skipped (never zero-filled).
        """
        observed_at = observed_at or datetime.now(UTC)
        cutoff = date.fromordinal(as_of.toordinal() - _TRAILING_BUFFER_DAYS)
        records: list[ForwardEstimateRecord] = []
        for row in payload:
            raw_date = row.get("date")
            eps = row.get("epsAvg")
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
                    num_analysts=row.get("numAnalystsEps"),
                    source=self.name,
                    basis=EPS_BASIS_ADJUSTED_DILUTED,
                    currency=str(row.get("reportedCurrency", "USD")),
                    construction_method=method,
                    is_derived=False,
                    observation_timestamp=observed_at,
                )
            )
        if not records:
            logger.warning(
                "%s: FMP %s estimates parsed to ZERO rows (%s)",
                ticker,
                period_type,
                UNVALIDATED_MARKER,
            )
        return records

    # --- price cross-check ---------------------------------------------------
    def fetch_price(self, ticker: str, on_date: date) -> float | None:
        """Fetch FMP's /stable EOD close for ``on_date`` (cross-check vs yfinance)."""
        import requests

        url = f"{self.base_url}/stable/historical-price-eod/full"
        params = {
            "symbol": ticker,
            "from": on_date.isoformat(),
            "to": on_date.isoformat(),
            "apikey": self.api_key,
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"FMP price request failed for {ticker}: {_redact(str(exc))}"
            ) from None
        return self.parse_price(resp.json(), on_date)

    def parse_price(self, payload: Any, on_date: date) -> float | None:
        """Extract the close for ``on_date``.

        Stable returns a flat list of bars; the legacy endpoint wrapped them in
        ``{"historical": [...]}``. Both shapes are accepted defensively.
        """
        rows = payload if isinstance(payload, list) else payload.get("historical", [])
        for row in rows:
            if str(row.get("date", ""))[:10] == on_date.isoformat():
                close = row.get("close")
                return float(close) if close is not None else None
        return None
