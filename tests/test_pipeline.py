"""End-to-end pipeline: assemble_valuation (pure) + run_daily (with fakes).

Covers the dangerous outcomes wired together: a clean True P/E, an ADR rejected by
the currency guard, time misalignment, an incomplete window, a quarantined True P/E
jump, and idempotent persistence that never duplicates or trips the immutability
triggers.
"""

from __future__ import annotations

from datetime import date

import pytest

from corridor.config import Config, TickerSpec
from corridor.constants import REASON_CURRENCY_MISMATCH, REASON_TRUE_PE_JUMP
from corridor.datasources.base import ForwardEstimateRecord, FundamentalRecord, PriceRecord
from corridor.ingest.job import PriorSnapshot, assemble_valuation, run_daily
from corridor.ingest.records import FiscalPeriod, PricePoint

AS_OF = date(2025, 6, 16)

PERIODS = [
    FiscalPeriod("FY2026Q1", date(2025, 4, 27), date(2025, 5, 28), confirmed=True),
    FiscalPeriod("FY2026Q2", date(2025, 7, 27), date(2025, 8, 27), confirmed=False),
    FiscalPeriod("FY2026Q3", date(2025, 10, 26), date(2025, 11, 19), confirmed=False),
    FiscalPeriod("FY2026Q4", date(2026, 1, 25), date(2026, 2, 25), confirmed=False),
    FiscalPeriod("FY2027Q1", date(2026, 4, 26), date(2026, 5, 27), confirmed=False),
]


def _estimates(currency: str = "USD") -> list[ForwardEstimateRecord]:
    q = [("FY2026Q2", 1.00, date(2025, 7, 27)), ("FY2026Q3", 1.20, date(2025, 10, 26))]
    a = [("FY2026", 4.40), ("FY2027", 6.00)]
    recs = [
        ForwardEstimateRecord("NVDA", AS_OF, "quarter", lbl, end, "eps", v, 40, "fmp",
                              currency=currency)
        for lbl, v, end in q
    ]
    recs += [
        ForwardEstimateRecord("NVDA", AS_OF, "annual", lbl, None, "eps", v, 40, "fmp",
                              currency=currency, construction_method="provider_annual")
        for lbl, v in a
    ]
    return recs


def _point(price_date: date = AS_OF, currency: str = "USD") -> PricePoint:
    return PricePoint(price_date, 120.0, 120.0, 1.0, currency, "yfinance")


# --- assemble_valuation (pure) ----------------------------------------------
def test_assemble_clean_true_pe() -> None:
    r = assemble_valuation(
        ticker="NVDA", as_of=AS_OF, price_point=_point(), report_currency="USD",
        estimate_records=_estimates(), fiscal_periods=PERIODS,
    )
    assert r.status == "ok"
    assert r.valuation is not None
    assert r.valuation.true_pe == pytest.approx(25.0)  # 120 / 4.80
    assert r.valuation.coverage_score == pytest.approx(0.5)
    assert r.valuation.price_basis == "raw"


def test_assemble_annual_only_path_v1() -> None:
    """v1 Starter path: NO quarterly estimates — every quarter derived from the
    multi-year annual curve, with the reported FY2026Q1 actual subtracted."""
    annual = [
        ForwardEstimateRecord("NVDA", AS_OF, "annual", "FY2026", date(2026, 1, 25),
                              "eps", 4.40, 40, "fmp"),
        ForwardEstimateRecord("NVDA", AS_OF, "annual", "FY2027", date(2027, 1, 31),
                              "eps", 6.00, 40, "fmp"),
    ]
    r = assemble_valuation(
        ticker="NVDA", as_of=AS_OF, price_point=_point(), report_currency="USD",
        estimate_records=annual, fiscal_periods=PERIODS,
        reported_actuals={"FY2026Q1": 0.80}, yf_next_quarter_eps=1.10,
    )
    assert r.status == "ok"
    v = r.valuation
    assert v is not None
    # FY2026 Q2/Q3/Q4 each = (4.40 - 0.80)/3 = 1.20 ; FY2027Q1 = 6.00/4 = 1.50.
    assert v.forward_eps_sum == pytest.approx(1.20 * 3 + 1.50)  # 5.10
    assert v.coverage_score == pytest.approx(0.0)  # all derived from annual
    # Quarterly cross-check: derived next-Q 1.20 vs yfinance 1.10 (~9% < 15%) -> no flag.
    assert v.yf_next_q_eps == pytest.approx(1.10)
    assert not v.quarterly_xcheck_flag


def test_yfinance_is_flag_only_never_blended_into_the_sum() -> None:
    """v1 DECISION lock: FLAT derivation for the sum; yfinance is an INDEPENDENT
    cross-check flag only. Changing the yfinance next-quarter value must NOT move the
    forward sum or True P/E — only the cross-check fields. This guards against a
    future change silently blending yfinance into the derivation."""
    annual = [
        ForwardEstimateRecord("NVDA", AS_OF, "annual", "FY2026", date(2026, 1, 25),
                              "eps", 4.40, 40, "fmp"),
        ForwardEstimateRecord("NVDA", AS_OF, "annual", "FY2027", date(2027, 1, 31),
                              "eps", 6.00, 40, "fmp"),
    ]
    common = {
        "ticker": "NVDA", "as_of": AS_OF, "price_point": _point(), "report_currency": "USD",
        "estimate_records": annual, "fiscal_periods": PERIODS,
        "reported_actuals": {"FY2026Q1": 0.80},
    }
    # Derived next quarter is flat (4.40-0.80)/3 = 1.20, independent of yfinance.
    no_flag = assemble_valuation(**common, yf_next_quarter_eps=1.15)  # ~4% -> no flag
    flagged = assemble_valuation(**common, yf_next_quarter_eps=2.00)  # ~40% -> flag

    # Sum + True P/E are IDENTICAL regardless of the yfinance value (flat, not blended).
    assert no_flag.valuation.forward_eps_sum == pytest.approx(5.10)
    assert flagged.valuation.forward_eps_sum == pytest.approx(5.10)
    assert no_flag.valuation.true_pe == flagged.valuation.true_pe
    # Only the cross-check reflects yfinance.
    assert no_flag.valuation.yf_next_q_eps == pytest.approx(1.15)
    assert not no_flag.valuation.quarterly_xcheck_flag
    assert flagged.valuation.quarterly_xcheck_flag


def test_assemble_rejects_adr_currency_mismatch() -> None:
    # Report currency TWD vs USD price -> unsupported, never a naive P/E.
    r = assemble_valuation(
        ticker="ADR", as_of=AS_OF, price_point=_point(currency="USD"), report_currency="TWD",
        estimate_records=_estimates(currency="TWD"), fiscal_periods=PERIODS,
    )
    assert r.status == "unsupported"
    assert r.reason_code == REASON_CURRENCY_MISMATCH
    assert r.valuation is None


def test_assemble_quarantines_time_misalignment() -> None:
    r = assemble_valuation(
        ticker="NVDA", as_of=AS_OF, price_point=_point(price_date=date(2025, 6, 12)),
        report_currency="USD", estimate_records=_estimates(), fiscal_periods=PERIODS,
    )
    assert r.status == "quarantine" and r.valuation is None


def test_assemble_incomplete_window_when_quarter_unbuildable() -> None:
    # Only one quarterly estimate, no annuals -> Q3/Q4/Q1 unbuildable -> incomplete.
    recs = [_estimates()[0]]  # just FY2026Q2 quarterly
    r = assemble_valuation(
        ticker="NVDA", as_of=AS_OF, price_point=_point(), report_currency="USD",
        estimate_records=recs, fiscal_periods=PERIODS,
    )
    assert r.status == "incomplete" and r.valuation is None


def test_assemble_quarantines_true_pe_jump() -> None:
    r = assemble_valuation(
        ticker="NVDA", as_of=AS_OF, price_point=_point(), report_currency="USD",
        estimate_records=_estimates(), fiscal_periods=PERIODS,
        prior=PriorSnapshot(forward_eps_sum=4.7, true_pe=5.0),  # 25.0 vs 5.0 = 5x jump
    )
    assert r.status == "quarantine" and r.reason_code == REASON_TRUE_PE_JUMP


# --- run_daily integration (fakes + DB) -------------------------------------
class _FakePrices:
    def get_prices(self, ticker: str, start: date | None = None) -> list[PriceRecord]:
        return [PriceRecord(ticker, AS_OF, 120.0, 121.0, 119.0, 120.0, 120.0, 1_000, "yfinance")]


class _FakeEstimates:
    def get_forward_estimates(self, ticker: str, as_of: date | None = None):
        return _estimates()


class _FakeFundamentals:
    """Returns NVDA's FY2026Q1 as a reported EDGAR actual (drives report date + value)."""

    def get_fundamentals(self, ticker: str, cik: str | None = None) -> list[FundamentalRecord]:
        return [
            FundamentalRecord(
                ticker, cik, "FY2026Q1", date(2025, 4, 27), date(2025, 5, 28),
                "eps_diluted", 0.80, "USD/shares", "10-Q", "edgar",
            )
        ]


def _job_config() -> Config:
    return Config(
        universe=[TickerSpec("NVDA", "NVIDIA", cik="1045810"), TickerSpec("TSM", "TSMC")],
        data={
            "engine_version": "0.1.0",
            "fiscal_calendars": {"NVDA": {"fy_end_month": 1, "fy_end_day": 31}},
        },
        unsupported=[{"ticker": "TSM", "reason": "ADR — TWD reporter vs USD price"}],
    )


def _run(session):  # type: ignore[no-untyped-def]
    # No reported_by_ticker override: report dates AND actuals both come from EDGAR.
    return run_daily(
        _job_config(),
        price_source=_FakePrices(),
        estimate_source=_FakeEstimates(),
        fundamentals_source=_FakeFundamentals(),
        session=session,
        as_of=AS_OF,
    )


def test_run_daily_writes_clean_row_quarantines_adr_and_is_idempotent(db_url: str) -> None:
    from corridor.db import session_scope
    from corridor.db.models import (
        ForwardEstimateSnapshot,
        IngestionLog,
        Security,
        ValuationSnapshot,
    )

    with session_scope() as s:
        s.add(Security(ticker="NVDA", name="NVIDIA"))
        s.add(Security(ticker="TSM", name="TSMC"))

    with session_scope() as s:
        results = _run(s)
    statuses = {r.ticker: r.status for r in results}
    assert statuses["NVDA"] == "ok"
    assert statuses["TSM"] == "unsupported"  # never fetched, hard-rejected

    with session_scope() as s:
        vals = s.query(ValuationSnapshot).all()
        assert len(vals) == 1
        # EDGAR FY2026Q1 actual (0.80) flows through: FY2026Q4 = (4.40-0.80-2.20)/1 = 1.40,
        # FY2027Q1 = 6.00/4 = 1.50, sum = 1.00+1.20+1.40+1.50 = 5.10.
        assert vals[0].forward_eps_ntm == pytest.approx(5.10)
        assert vals[0].true_pe == pytest.approx(120.0 / 5.10)
        assert vals[0].coverage_score == pytest.approx(0.5)
        assert "/1 unknown" in vals[0].construction_method  # divisor shrank via the actual
        assert vals[0].price_basis == "raw" and vals[0].report_currency == "USD"
        assert any(
            log.ticker == "TSM" and log.status == "quarantine" for log in s.query(IngestionLog)
        )
        fwd_count = s.query(ForwardEstimateSnapshot).count()
        assert fwd_count > 0

    # Re-run the same day: idempotent — no duplicate snapshots, no trigger trip.
    with session_scope() as s:
        _run(s)
    with session_scope() as s:
        assert s.query(ForwardEstimateSnapshot).count() == fwd_count
        assert s.query(ValuationSnapshot).count() == 1
