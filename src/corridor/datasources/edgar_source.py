"""SEC EDGAR XBRL adapter — realized actuals (diluted EPS, continuing ops).

!!! UNVALIDATED AGAINST LIVE ENDPOINT !!!
Written to the ``data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` shape but
not run live in this build. EDGAR requires a descriptive User-Agent (with contact
email) and is otherwise stable, but per-company tag availability varies (some names
only report ``EarningsPerShareDiluted``, not the continuing-operations tag). Confirm
via ``scripts/validate_live.py``.

We prefer ``IncomeLossFromContinuingOperationsPerDilutedShare`` and fall back to
``EarningsPerShareDiluted``, recording which tag (basis) was used. ``filed`` dates
are retained so a point-in-time backtest knows when each fact became public.
Quarterly vs annual entries are classified by period span; YTD/Q4 disaggregation
is a documented later refinement.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from ..constants import EPS_BASIS_GAAP_DILUTED_CONTINUING
from .base import FundamentalRecord, FundamentalsSource

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://data.sec.gov"
# Diluted-EPS tags in preference order: continuing-ops first, then total.
_EPS_TAGS = (
    ("IncomeLossFromContinuingOperationsPerDilutedShare", EPS_BASIS_GAAP_DILUTED_CONTINUING),
    ("EarningsPerShareDiluted", "gaap_diluted_total"),
)


class EdgarFundamentalsSource(FundamentalsSource):
    """As-reported diluted EPS from EDGAR companyfacts. UNVALIDATED (see module docstring)."""

    name = "edgar"
    validated = False

    def __init__(self, user_agent: str, base_url: str = DEFAULT_BASE_URL) -> None:
        if not user_agent or "example.com" in user_agent:
            logger.warning("EDGAR User-Agent looks like a placeholder: %r", user_agent)
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")

    # --- network (untested here) ---------------------------------------------
    def _fetch(self, cik: str) -> dict[str, Any]:
        import requests

        cik10 = str(cik).lstrip("CIK").zfill(10)
        url = f"{self.base_url}/api/xbrl/companyfacts/CIK{cik10}.json"
        resp = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=30)
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        return payload

    def get_fundamentals(self, ticker: str, cik: str | None = None) -> list[FundamentalRecord]:
        if cik is None:
            raise ValueError(f"EDGAR fundamentals require a CIK for {ticker}")
        return self.parse_companyfacts(self._fetch(cik), ticker, cik)

    # --- parsing (pure; unit-tested against fixtures) ------------------------
    def parse_companyfacts(
        self,
        payload: dict[str, Any],
        ticker: str,
        cik: str | None = None,
        observed_at: datetime | None = None,
    ) -> list[FundamentalRecord]:
        observed_at = observed_at or datetime.now(UTC)
        gaap = payload.get("facts", {}).get("us-gaap", {})
        records: list[FundamentalRecord] = []
        for tag, basis in _EPS_TAGS:
            node = gaap.get(tag)
            if not node:
                continue
            for entry in node.get("units", {}).get("USD/shares", []):
                rec = self._entry_to_record(entry, ticker, cik, basis, observed_at)
                if rec is not None:
                    records.append(rec)
            if records:  # used the preferred tag; don't double-count the fallback
                break
        if not records:
            logger.warning("%s: no diluted-EPS facts found in EDGAR companyfacts", ticker)
        return records

    def _entry_to_record(
        self,
        entry: dict[str, Any],
        ticker: str,
        cik: str | None,
        basis: str,
        observed_at: datetime,
    ) -> FundamentalRecord | None:
        val = entry.get("val")
        end_raw = entry.get("end")
        fy = entry.get("fy")
        fp = entry.get("fp")
        if val is None or end_raw is None or fy is None or fp is None:
            return None
        end = date.fromisoformat(str(end_raw)[:10])
        start_raw = entry.get("start")
        period_type = self._classify(start_raw, end)
        if period_type is None:
            return None  # skip 6mo/9mo YTD spans
        fiscal_period = f"FY{fy}" if (period_type == "annual" or fp == "FY") else f"FY{fy}{fp}"
        filed = entry.get("filed")
        return FundamentalRecord(
            ticker=ticker,
            cik=str(cik) if cik else None,
            fiscal_period=fiscal_period,
            period_end_date=end,
            filed_date=date.fromisoformat(str(filed)[:10]) if filed else None,
            metric="eps_diluted",
            value=float(val),
            unit="USD/shares",
            form=entry.get("form"),
            source=self.name,
            basis=basis,
            observation_timestamp=observed_at,
        )

    @staticmethod
    def _classify(start_raw: Any, end: date) -> str | None:
        """Classify an EPS entry by its period span: 'quarter', 'annual', or None."""
        if not start_raw:
            return None
        start = date.fromisoformat(str(start_raw)[:10])
        span = (end - start).days
        if 80 <= span <= 100:
            return "quarter"
        if 350 <= span <= 380:
            return "annual"
        return None
