"""Adversarial: split basis, currency match (ADR), and time alignment.

* A split must NOT create a day-over-day True P/E discontinuity (raw price and raw
  EPS both rescale, so the ratio is continuous by construction).
* An ADR (TWD reporter + USD price) must be REJECTED, never a naive mismatched P/E.
* A price and estimate observed on different dates must not be paired.
"""

from __future__ import annotations

from datetime import date

import pytest

from corridor.constants import REASON_CURRENCY_MISMATCH, REASON_TIME_MISALIGNMENT
from corridor.ingest.consistency import (
    compute_true_pe,
    currency_guard,
    time_alignment_guard,
)


def test_split_produces_no_true_pe_discontinuity() -> None:
    # Day before a 10:1 split: raw price 1200, raw forward EPS sum 48.00.
    pe_before = compute_true_pe(1200.0, 48.0)
    # Day after the split: BOTH the raw price and the raw EPS rescale by 10.
    pe_after = compute_true_pe(120.0, 4.80)
    assert pe_before == pytest.approx(25.0)
    assert pe_after == pytest.approx(pe_before)  # continuous across the split


def test_mixing_adjusted_price_with_raw_eps_would_create_a_jump() -> None:
    # This is the BUG the consistent basis avoids: a back-adjusted price (120)
    # paired with a pre-split raw EPS (48) fabricates a 10x discontinuity. We assert
    # the basis error is detectable so we never ship it.
    correct = compute_true_pe(1200.0, 48.0)
    mixed_basis = compute_true_pe(120.0, 48.0)
    assert mixed_basis == pytest.approx(2.5)
    assert mixed_basis / correct == pytest.approx(0.1)  # 10x off -> caught by jump gate


def test_currency_guard_rejects_adr_mismatch() -> None:
    # TSM-style: TWD reporter behind a USD-priced ADR.
    outcome = currency_guard(report_currency="TWD", price_currency="USD")
    assert not outcome.passed
    assert outcome.reason_code == REASON_CURRENCY_MISMATCH
    assert "TWD" in outcome.detail and "USD" in outcome.detail


def test_currency_guard_passes_for_matching_usd() -> None:
    assert currency_guard("USD", "USD").passed


def test_time_alignment_rejects_mismatched_dates() -> None:
    bad = time_alignment_guard(price_date=date(2026, 6, 16), estimate_as_of=date(2026, 6, 12))
    assert not bad.passed
    assert bad.reason_code == REASON_TIME_MISALIGNMENT
    good = time_alignment_guard(date(2026, 6, 16), date(2026, 6, 16))
    assert good.passed
