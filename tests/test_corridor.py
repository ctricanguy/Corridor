"""Corridor math — hand-checkable numbers (Factor 2).

Series [10,20,30,40,50] is used throughout because its percentiles are easy to
verify by hand: 20th = 18, median = 30, 80th = 42 (linear interpolation).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from corridor.engine.corridor import (
    TruePePoint,
    build_corridor,
    compute_bands,
    percentile,
    percentile_rank,
)

SERIES = [10.0, 20.0, 30.0, 40.0, 50.0]


def _history() -> list[TruePePoint]:
    return [TruePePoint(date(2026, 1, 1) + timedelta(days=i), v) for i, v in enumerate(SERIES)]


def test_percentile_hand_values() -> None:
    assert percentile(SERIES, 20) == pytest.approx(18.0)
    assert percentile(SERIES, 50) == pytest.approx(30.0)
    assert percentile(SERIES, 80) == pytest.approx(42.0)
    assert percentile([42.0], 20) == pytest.approx(42.0)  # single point


def test_percentile_rank_hand_values() -> None:
    assert percentile_rank(10.0, SERIES) == pytest.approx(10.0)  # cheapest -> low
    assert percentile_rank(30.0, SERIES) == pytest.approx(50.0)  # median
    assert percentile_rank(50.0, SERIES) == pytest.approx(90.0)  # richest -> high


def test_compute_bands() -> None:
    bands = compute_bands(SERIES, 20, 80)
    assert (bands.pe_low, bands.pe_median, bands.pe_high) == pytest.approx((18.0, 30.0, 42.0))


def test_corridor_price_space_and_position() -> None:
    # current price 300 / fwd EPS 10 -> True P/E 30 (the median).
    c = build_corridor("NVDA", date(2026, 1, 5), _history(), current_fwd_eps=10.0,
                       current_price=300.0, pctl_low=20, pctl_high=80, min_history_days=60)
    assert c.bands is not None
    # bands x current fwd EPS:
    assert c.corridor_low_price == pytest.approx(180.0)
    assert c.corridor_mid_price == pytest.approx(300.0)
    assert c.corridor_high_price == pytest.approx(420.0)
    assert c.current_true_pe == pytest.approx(30.0)
    assert c.pe_percentile == pytest.approx(50.0)
    assert c.position == "lower half of corridor"
    # 5 days < 60 -> thin, annotated.
    assert c.is_thin and "THIN HISTORY" in c.notes and c.history_days == 5


def test_corridor_below_low_band_is_cheap() -> None:
    # price 150 / EPS 10 -> True P/E 15, below the 20th-pct band (18 x 10 = 180).
    c = build_corridor("NVDA", date(2026, 1, 5), _history(), 10.0, 150.0, min_history_days=60)
    assert c.position == "below low band (cheap vs history)"
    assert c.pe_percentile == pytest.approx(20.0)  # cheaper than all but the 10x point


def test_corridor_insufficient_history() -> None:
    c = build_corridor("NVDA", date(2026, 1, 5), [TruePePoint(date(2026, 1, 5), 25.0)],
                       current_fwd_eps=10.0, current_price=250.0)
    assert c.bands is None
    assert c.position == "insufficient history"
    assert "needs" in c.notes and c.history_days == 1


def test_corridor_carries_coverage_and_is_thin() -> None:
    c = build_corridor("NVDA", date(2026, 1, 5), _history(), 10.0, 300.0,
                       min_history_days=60, coverage_score=0.0)
    assert c.coverage_score == 0.0  # annual-only path -> all derived, surfaced honestly
    assert c.is_thin
