"""Multi-source reconciliation: flag disagreement, never silently average."""

from __future__ import annotations

import pytest

from corridor.ingest.reconcile import ntm_cross_check, reconcile_prices


def test_prices_within_threshold_not_flagged() -> None:
    r = reconcile_prices(price_yf=100.0, price_fmp=100.2, threshold_pct=0.01)
    assert not r.disagreement_flag
    assert r.price_primary == 100.0  # yfinance is primary


def test_prices_beyond_threshold_flagged_and_both_retained() -> None:
    r = reconcile_prices(price_yf=100.0, price_fmp=102.5, threshold_pct=0.01)
    assert r.disagreement_flag
    assert r.price_yf == 100.0 and r.price_fmp == 102.5  # both logged
    assert r.disagreement_pct == pytest.approx(0.025)


def test_single_source_does_not_flag() -> None:
    r = reconcile_prices(price_yf=100.0, price_fmp=None, threshold_pct=0.01)
    assert not r.disagreement_flag and r.price_primary == 100.0


def test_ntm_cross_check_flags_window_divergence() -> None:
    ok = ntm_cross_check(strict_sum=4.80, native_ntm=4.85, threshold_pct=0.10)
    assert not ok.divergence_flag
    bad = ntm_cross_check(strict_sum=4.80, native_ntm=6.00, threshold_pct=0.10)
    assert bad.divergence_flag
    assert bad.divergence_pct == pytest.approx(0.20)
