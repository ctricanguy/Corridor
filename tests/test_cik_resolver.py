"""EDGAR ticker -> CIK resolver (parse + cached resolve; no network)."""

from __future__ import annotations

import json
from pathlib import Path

from corridor.datasources.cik_resolver import CikResolver, parse_company_tickers

PAYLOAD = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
}


def test_parse_company_tickers_zero_pads_to_10() -> None:
    m = parse_company_tickers(PAYLOAD)
    assert m["NVDA"] == "0001045810"
    assert m["AAPL"] == "0000320193"


def test_resolver_uses_cache_without_network(tmp_path: Path) -> None:
    cache = tmp_path / "cik_map.json"
    cache.write_text(json.dumps(parse_company_tickers(PAYLOAD)))
    out = CikResolver("Test test@corridor.local", cache).resolve(["nvda", "MSFT"])
    assert out == {"NVDA": "0001045810"}  # case-insensitive; MSFT absent -> skipped
