"""Write-time sanity gates.

Impossible or suspicious values are QUARANTINED — written to ingestion_log with a
machine reason code and a human detail, and NOT written to the live tables. They
are never silently dropped and never silently stored.

Gates:
* non-positive price.
* forward-EPS-sum sign flip vs the prior snapshot with no intervening earnings
  event (a real loss reported at earnings is allowed; a phantom flip is not).
* True P/E moving more than ``jump_factor`` x in a day with no split (a split is a
  legitimate rescale; an unexplained jump signals a basis or data error).
"""

from __future__ import annotations

from ..constants import (
    REASON_EPS_SIGN_FLIP,
    REASON_NON_POSITIVE_PRICE,
    REASON_TRUE_PE_JUMP,
)
from .records import GateOutcome

_PASS = GateOutcome(passed=True)


def gate_price_positive(price: float) -> GateOutcome:
    if price <= 0:
        return GateOutcome(
            passed=False,
            reason_code=REASON_NON_POSITIVE_PRICE,
            detail=f"price {price} is not positive",
        )
    return _PASS


def gate_eps_sign_flip(
    current_sum: float, prior_sum: float | None, earnings_event: bool
) -> GateOutcome:
    """Quarantine a forward-EPS-sum sign flip that is not explained by earnings."""
    if prior_sum is None or earnings_event:
        return _PASS
    if current_sum * prior_sum < 0:
        return GateOutcome(
            passed=False,
            reason_code=REASON_EPS_SIGN_FLIP,
            detail=(
                f"forward EPS sum flipped sign {prior_sum:.4f} -> {current_sum:.4f} "
                "with no intervening earnings event"
            ),
        )
    return _PASS


def gate_true_pe_jump(
    current_pe: float,
    prior_pe: float | None,
    split_occurred: bool,
    jump_factor: float = 2.0,
) -> GateOutcome:
    """Quarantine a >jump_factor x day-over-day True P/E move with no split.

    A split rescales price and EPS together, so with the canonical raw basis there
    should be NO jump even across a split; a large move with no split therefore
    points to a data or basis error and is quarantined.
    """
    if prior_pe is None or split_occurred or prior_pe == 0:
        return _PASS
    ratio = current_pe / prior_pe
    if ratio > jump_factor or ratio < (1.0 / jump_factor):
        return GateOutcome(
            passed=False,
            reason_code=REASON_TRUE_PE_JUMP,
            detail=(
                f"True P/E moved {prior_pe:.2f} -> {current_pe:.2f} "
                f"({ratio:.2f}x) in one day with no split"
            ),
        )
    return _PASS


def run_sanity_gates(
    *,
    price: float,
    current_eps_sum: float,
    prior_eps_sum: float | None,
    current_true_pe: float,
    prior_true_pe: float | None,
    earnings_event: bool,
    split_occurred: bool,
    jump_factor: float = 2.0,
) -> GateOutcome:
    """Run all value-anomaly gates in order; return the first failure, else pass."""
    for outcome in (
        gate_price_positive(price),
        gate_eps_sign_flip(current_eps_sum, prior_eps_sum, earnings_event),
        gate_true_pe_jump(current_true_pe, prior_true_pe, split_occurred, jump_factor),
    ):
        if not outcome.passed:
            return outcome
    return _PASS
