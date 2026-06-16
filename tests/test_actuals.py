"""Realized-actuals feedback loop: beat/miss vs the pre-report estimate.

Asserts we compare the actual to the estimate that was LIVE the day before the
report, flag the GAAP-vs-non-GAAP basis mismatch, and roll up per-source accuracy.
"""

from __future__ import annotations

from datetime import date

import pytest

from corridor.constants import (
    EPS_BASIS_ADJUSTED_DILUTED,
    EPS_BASIS_GAAP_DILUTED_CONTINUING,
)
from corridor.ingest.actuals import (
    EstimateObservation,
    aggregate_source_accuracy,
    compute_beat_miss,
    select_pre_report_estimate,
)


def _obs(day: int, value: float) -> EstimateObservation:
    return EstimateObservation(
        as_of_date=date(2025, 8, day), value=value, basis=EPS_BASIS_ADJUSTED_DILUTED, source="fmp"
    )


def test_selects_estimate_live_day_before_report() -> None:
    estimates = [_obs(20, 1.00), _obs(26, 1.05), _obs(27, 9.99)]  # 27th = report day
    chosen = select_pre_report_estimate(estimates, report_date=date(2025, 8, 27))
    assert chosen is not None
    assert chosen.as_of_date == date(2025, 8, 26)  # strictly before the report
    assert chosen.value == pytest.approx(1.05)


def test_no_pre_report_estimate_returns_none() -> None:
    estimates = [_obs(27, 1.0), _obs(28, 1.0)]
    assert select_pre_report_estimate(estimates, date(2025, 8, 27)) is None


def test_beat_miss_flags_basis_mismatch() -> None:
    estimate = _obs(26, 0.90)  # non-GAAP adjusted estimate
    bm = compute_beat_miss(
        "FY2026Q2", actual_eps=1.00, actual_basis=EPS_BASIS_GAAP_DILUTED_CONTINUING,
        estimate=estimate,
    )
    assert bm.beat is True
    assert bm.surprise_abs == pytest.approx(0.10)
    assert bm.surprise_pct == pytest.approx(0.10 / 0.90)
    assert bm.basis_mismatch  # GAAP actual vs non-GAAP estimate -> not like-for-like


def test_source_accuracy_rollup() -> None:
    estimate = _obs(26, 1.00)
    beats = [
        compute_beat_miss("FY2026Q2", 1.10, EPS_BASIS_ADJUSTED_DILUTED, estimate),  # +10%
        compute_beat_miss("FY2026Q3", 0.90, EPS_BASIS_ADJUSTED_DILUTED, estimate),  # -10%
    ]
    stats = aggregate_source_accuracy(beats, source="fmp")
    assert stats.n_observations == 2
    assert stats.hit_rate == pytest.approx(0.5)  # one beat, one miss
    assert stats.mean_abs_pct_error == pytest.approx(0.10)
