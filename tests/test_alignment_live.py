"""LIVE-DATA alignment test (the lesson: fixtures passed while live failed).

Skipped by default — it hits the real SEC EDGAR API. Run it where you have network:

    CORRIDOR_RUN_LIVE=1 SEC_EDGAR_USER_AGENT="You <you@email>" \
        .venv/bin/python -m pytest tests/test_alignment_live.py -v

It fetches NVDA's real companyfacts and asserts the FY-label gate reads aligned
(no drift between EDGAR's earliest-filed fy/fp and the date-derived label), and that
the date convention matches NVIDIA's (an April-ending quarter is Q1 of the next-year
FY). If this fails, the window is genuinely misaligned and the derivation is wrong.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("CORRIDOR_RUN_LIVE") != "1",
    reason="live EDGAR test; set CORRIDOR_RUN_LIVE=1 (needs network + SEC_EDGAR_USER_AGENT)",
)

NVDA_CIK = "0001045810"


def test_nvda_fiscal_labels_align_on_live_edgar() -> None:
    from corridor.config import get_settings, load_config
    from corridor.datasources.edgar_source import EdgarFundamentalsSource
    from corridor.ingest.job import check_label_alignment

    settings = get_settings()
    config = load_config()
    cal = config.fiscal_calendars()["NVDA"]
    edgar = EdgarFundamentalsSource(settings.sec_edgar_user_agent, config.fiscal_calendars())
    actuals = edgar.get_fundamentals("NVDA", cik=NVDA_CIK)

    aligns = check_label_alignment(actuals, cal)
    assert aligns, "no EDGAR quarterly actuals parsed for NVDA"
    drift = [(a.period_end, a.edgar_label, a.date_label) for a in aligns if not a.agree]
    assert not drift, f"FY-label drift on LIVE EDGAR (gate would read aligned=False): {drift}"

    # NVIDIA convention: a quarter ending in April of year Y is Q1 of fiscal year Y+1.
    april = [a for a in aligns if a.period_end.month == 4]
    assert april, "expected at least one April (Q1) quarter in NVDA's EDGAR history"
    for a in april:
        assert a.date_label == f"FY{a.period_end.year + 1}Q1"
