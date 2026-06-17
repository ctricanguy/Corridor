"""EDGAR ticker -> CIK resolver.

Fetches SEC's ``company_tickers.json`` ONCE, caches it locally, and maps watchlist
tickers to zero-padded 10-digit CIKs. This is what lets the daily job + PEG pull
EDGAR actuals for the WHOLE watchlist (config CIKs are null), so the stored True P/E
subtracts reported actuals instead of falling back to an uncertified annual/4.

Fetch (network) and parse (pure) are separated so the parse is unit-tested.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def parse_company_tickers(payload: dict[str, Any]) -> dict[str, str]:
    """{TICKER: cik10} from SEC company_tickers.json (pure; testable)."""
    out: dict[str, str] = {}
    for entry in payload.values():
        ticker = entry.get("ticker")
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            out[str(ticker).upper()] = str(cik).zfill(10)
    return out


class CikResolver:
    """Resolve tickers to CIKs via a locally-cached company_tickers.json."""

    def __init__(self, user_agent: str, cache_path: Path) -> None:
        self.user_agent = user_agent
        self.cache_path = cache_path
        self._map: dict[str, str] | None = None

    def _fetch(self) -> dict[str, str]:
        import requests

        resp = requests.get(
            COMPANY_TICKERS_URL, headers={"User-Agent": self.user_agent}, timeout=30
        )
        resp.raise_for_status()
        return parse_company_tickers(resp.json())

    def _load(self, refresh: bool = False) -> dict[str, str]:
        if self._map is not None and not refresh:
            return self._map
        if self.cache_path.exists() and not refresh:
            self._map = json.loads(self.cache_path.read_text())
            return self._map
        fetched = self._fetch()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(fetched))
        self._map = fetched
        logger.info("CIK map cached (%d tickers) at %s", len(fetched), self.cache_path)
        return fetched

    def resolve(self, tickers: list[str], refresh: bool = False) -> dict[str, str]:
        """Return {TICKER: cik10} for the tickers found; logs any that are missing."""
        mapping = self._load(refresh=refresh)
        out: dict[str, str] = {}
        for ticker in tickers:
            cik = mapping.get(ticker.upper())
            if cik:
                out[ticker.upper()] = cik
            else:
                logger.warning("CIK not found for %s — EDGAR actuals will be skipped", ticker)
        return out
