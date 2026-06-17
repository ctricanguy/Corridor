"""Adapter PARSE tests against hand-built fixtures (no network).

The parse step is the part most likely to break when a provider renames a field,
so it is unit-tested here and re-checked live by scripts/validate_live.py via the
same shape contract.
"""

from __future__ import annotations

from datetime import date

import pytest

from corridor.datasources.edgar_source import EdgarFundamentalsSource
from corridor.datasources.fmp_source import FMPForwardEstimateSource
from corridor.datasources.shape import ShapeMismatch, assert_records_shape
from corridor.datasources.yfinance_source import YFinancePriceSource
from corridor.ingest.fiscal import FiscalCalendar

from .conftest import load_fixture

NVDA_CAL = FiscalCalendar(fy_end_month=1)
AS_OF = date(2025, 6, 16)


def _fmp() -> FMPForwardEstimateSource:
    return FMPForwardEstimateSource(api_key="x", fiscal_calendars={"NVDA": NVDA_CAL})


def test_fmp_quarter_parse_labels_offcalendar_and_drops_bad_rows() -> None:
    payload = load_fixture("fmp_nvda_estimates_quarter.json")
    recs = _fmp().parse_estimates(payload, "NVDA", AS_OF, "quarter", NVDA_CAL)
    by_label = {r.fiscal_period: r.value for r in recs}
    # Deep-history (2023) dropped by the trailing-buffer cutoff; null-EPS Q4 skipped.
    assert by_label == {
        "FY2026Q1": 0.90,
        "FY2026Q2": 1.00,
        "FY2026Q3": 1.20,
        "FY2027Q1": 1.50,
    }
    assert all(r.basis == "adjusted_diluted" and r.currency == "USD" for r in recs)
    assert_records_shape(recs)  # satisfies the live contract


def test_fmp_annual_parse() -> None:
    payload = load_fixture("fmp_nvda_estimates_annual.json")
    recs = _fmp().parse_estimates(payload, "NVDA", AS_OF, "annual", NVDA_CAL)
    by_label = {r.fiscal_period: r.value for r in recs}
    assert by_label == {"FY2026": 4.40, "FY2027": 6.00}


def test_fmp_v1_fetches_annual_only_and_clamps_limit() -> None:
    # Starter: limit clamps to 10, and only period=annual is fetched (quarter is
    # Premium-gated). Stub _fetch to prove quarter is never called.
    fmp = FMPForwardEstimateSource(api_key="x", fiscal_calendars={"NVDA": NVDA_CAL}, page_limit=100)
    assert fmp.page_limit == 10  # clamped to Starter cap
    assert fmp.fetch_quarterly is False

    calls: list[str] = []
    annual_payload = load_fixture("fmp_nvda_estimates_annual.json")

    def fake_fetch(ticker: str, period: str) -> list:
        calls.append(period)
        if period == "annual":
            return annual_payload
        raise AssertionError("quarter must NOT be fetched on the v1 annual path")

    fmp._fetch = fake_fetch  # type: ignore[method-assign]
    recs = fmp.get_forward_estimates("NVDA", as_of=AS_OF)
    assert calls == ["annual"]  # quarter never fetched
    assert {r.fiscal_period for r in recs} == {"FY2026", "FY2027"}
    assert all(r.period_type == "annual" for r in recs)


def test_yfinance_parse_earnings_estimate() -> None:
    rows = [
        {"period": "0q", "avg": 1.05},
        {"period": "+1q", "avg": 1.20},
        {"period": "0y", "avg": 4.40},
        {"period": "+1y", "avg": 6.00},
        {"period": "+5y", "avg": None},  # skipped (no value)
    ]
    out = YFinancePriceSource._parse_earnings_estimate(rows)
    assert out == {"0q": 1.05, "+1q": 1.20, "0y": 4.40, "+1y": 6.00}


def test_fmp_legacy_shape_yields_zero_rows_and_fails_shape() -> None:
    # Feeding DEPRECATED v3 data (estimatedEpsAvg) to the /stable parser (which
    # reads epsAvg) finds no consensus EPS, returns nothing, and the shape guard
    # fires loudly — exactly the ShapeMismatch case the migration must catch.
    payload = load_fixture("fmp_estimates_renamed_field.json")
    recs = _fmp().parse_estimates(payload, "NVDA", AS_OF, "quarter", NVDA_CAL)
    assert recs == []
    with pytest.raises(ShapeMismatch):
        assert_records_shape(recs)


def test_fmp_redacts_apikey_in_urls_and_errors() -> None:
    from corridor.datasources.fmp_source import _redact

    leaky = "https://financialmodelingprep.com/stable/analyst-estimates?symbol=NVDA&apikey=SECRET_abc123"
    assert "SECRET_abc123" not in _redact(leaky)
    assert "***REDACTED***" in _redact(leaky)
    # The realistic leak vector: a requests HTTPError message embeds the full URL,
    # and run_daily logs str(exc) to ingestion_log.
    err = "403 Client Error: Forbidden for url: " + leaky
    assert "SECRET_abc123" not in _redact(err)


def test_fmp_parse_price_handles_stable_list_and_legacy_dict() -> None:
    fmp = _fmp()
    on = date(2025, 6, 16)
    stable = [{"symbol": "NVDA", "date": "2025-06-16", "open": 130.0, "close": 131.25}]
    assert fmp.parse_price(stable, on) == pytest.approx(131.25)  # stable flat list
    legacy = {"symbol": "NVDA", "historical": [{"date": "2025-06-16", "close": 131.25}]}
    assert fmp.parse_price(legacy, on) == pytest.approx(131.25)  # legacy wrapped shape
    assert fmp.parse_price([], on) is None  # no matching bar


def test_edgar_parse_uses_continuing_ops_and_classifies_by_span() -> None:
    payload = load_fixture("edgar_nvda_companyfacts.json")
    recs = EdgarFundamentalsSource("Test test@corridor.local").parse_companyfacts(
        payload, "NVDA", cik="1045810"
    )
    labels = {r.fiscal_period: r for r in recs}
    # 90-day Q1 kept; 364-day FY kept; 181-day 6-month YTD skipped.
    assert set(labels) == {"FY2026Q1", "FY2025"}
    q1 = labels["FY2026Q1"]
    assert q1.value == pytest.approx(0.85)
    assert q1.basis == "gaap_diluted_continuing_ops"
    assert q1.filed_date == date(2025, 5, 28)  # point-in-time filing date retained
    assert_records_shape(recs)


def test_yfinance_parse_attaches_split_ratio_and_keeps_raw_close() -> None:
    rows = [
        {"date": date(2024, 6, 7), "open": 1200.0, "high": 1210.0, "low": 1190.0,
         "close": 1200.0, "adj_close": 120.0, "volume": 1_000_000},
        {"date": date(2024, 6, 10), "open": 120.0, "high": 122.0, "low": 119.0,
         "close": 121.0, "adj_close": 121.0, "volume": 2_000_000},
    ]
    splits = {date(2024, 6, 10): 10.0}  # 10:1 split ex-date
    recs = YFinancePriceSource().parse_history(rows, splits, "NVDA", "USD")
    assert recs[0].split_ratio == 1.0 and recs[0].close == 1200.0  # raw close preserved
    assert recs[1].split_ratio == 10.0
    assert_records_shape(recs)
