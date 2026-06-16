"""Adversarial: write-time sanity gates quarantine impossible values.

Non-positive price, an unexplained forward-EPS-sum sign flip, and a >Nx True P/E
move with no split each produce a quarantine outcome (which the job routes to
ingestion_log) rather than a stored row.
"""

from __future__ import annotations

from corridor.constants import (
    REASON_EPS_SIGN_FLIP,
    REASON_NON_POSITIVE_PRICE,
    REASON_TRUE_PE_JUMP,
)
from corridor.ingest.gates import (
    gate_eps_sign_flip,
    gate_price_positive,
    gate_true_pe_jump,
    run_sanity_gates,
)


def test_non_positive_price_quarantined() -> None:
    out = gate_price_positive(-3.0)
    assert not out.passed and out.reason_code == REASON_NON_POSITIVE_PRICE
    assert gate_price_positive(10.0).passed


def test_eps_sign_flip_without_earnings_quarantined() -> None:
    flip = gate_eps_sign_flip(current_sum=-1.2, prior_sum=4.8, earnings_event=False)
    assert not flip.passed and flip.reason_code == REASON_EPS_SIGN_FLIP


def test_eps_sign_flip_allowed_when_earnings_event() -> None:
    # A real loss reported at earnings is a legitimate flip, not an anomaly.
    assert gate_eps_sign_flip(-1.2, 4.8, earnings_event=True).passed
    # No prior to compare against -> pass.
    assert gate_eps_sign_flip(-1.2, None, earnings_event=False).passed


def test_true_pe_jump_without_split_quarantined() -> None:
    jump = gate_true_pe_jump(current_pe=60.0, prior_pe=25.0, split_occurred=False, jump_factor=2.0)
    assert not jump.passed and jump.reason_code == REASON_TRUE_PE_JUMP


def test_true_pe_jump_allowed_on_split_or_within_band() -> None:
    # Even a big nominal move is fine on a split date (legitimate rescale)...
    assert gate_true_pe_jump(2.5, 25.0, split_occurred=True, jump_factor=2.0).passed
    # ...and a modest move is fine with no split.
    assert gate_true_pe_jump(28.0, 25.0, split_occurred=False, jump_factor=2.0).passed


def test_run_sanity_gates_returns_first_failure() -> None:
    out = run_sanity_gates(
        price=-1.0, current_eps_sum=4.8, prior_eps_sum=4.7, current_true_pe=25.0,
        prior_true_pe=25.0, earnings_event=False, split_occurred=False,
    )
    assert not out.passed and out.reason_code == REASON_NON_POSITIVE_PRICE

    clean = run_sanity_gates(
        price=120.0, current_eps_sum=4.8, prior_eps_sum=4.7, current_true_pe=25.0,
        prior_true_pe=24.8, earnings_event=False, split_occurred=False,
    )
    assert clean.passed
