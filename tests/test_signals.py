"""Buy/trim signals — hand-checkable cases (Factor 2)."""

from __future__ import annotations

from datetime import date

from corridor.engine.signals import classify_signal, earnings_trend


def test_earnings_trend() -> None:
    base = date(2026, 1, 1)
    assert earnings_trend([(base, 5.0), (date(2026, 1, 31), 5.5)])[0] == "rising"
    assert earnings_trend([(base, 5.0), (date(2026, 1, 31), 4.5)])[0] == "falling"
    assert earnings_trend([(base, 5.0), (date(2026, 1, 31), 5.0)])[0] == "flat"
    assert earnings_trend([(base, 5.0)])[0] == "unknown"  # never assume rising


def test_hard_buy_requires_bottom_decile_and_rising() -> None:
    s = classify_signal(7.0, "rising", is_thin=False, bands_available=True)
    assert s.signal == "hard_buy"
    # Same cheap position but earnings NOT rising -> watch, not buy.
    assert classify_signal(7.0, "flat", False, True).signal == "watch_cheap"
    assert classify_signal(7.0, "falling", False, True).signal == "watch_cheap"


def test_buy_band_and_hold_and_trim() -> None:
    assert classify_signal(15.0, "rising", False, True).signal == "buy"
    assert classify_signal(50.0, "rising", False, True).signal == "hold"
    assert classify_signal(85.0, "flat", False, True).signal == "trim"
    assert classify_signal(95.0, "flat", False, True).signal == "hard_trim"


def test_thin_history_downgrades_hard_signals() -> None:
    s = classify_signal(5.0, "rising", is_thin=True, bands_available=True)
    assert s.signal == "buy"  # downgraded from hard_buy
    assert "THIN" in s.rationale


def test_insufficient_history_when_no_bands() -> None:
    s = classify_signal(None, "unknown", is_thin=True, bands_available=False)
    assert s.signal == "insufficient_history"
