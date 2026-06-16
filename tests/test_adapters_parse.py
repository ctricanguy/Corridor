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


def test_fmp_renamed_field_yields_zero_rows_and_fails_shape() -> None:
    # Simulates FMP's "stable" endpoint shape (epsAvg instead of estimatedEpsAvg):
    # the parser finds no consensus EPS, returns nothing, and the shape guard fires.
    payload = load_fixture("fmp_estimates_renamed_field.json")
    recs = _fmp().parse_estimates(payload, "NVDA", AS_OF, "quarter", NVDA_CAL)
    assert recs == []
    with pytest.raises(ShapeMismatch):
        assert_records_shape(recs)


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
