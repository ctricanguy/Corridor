"""Consistency invariants that protect the price<->EPS pairing.

The whole system rests on two numbers per (ticker, date): the forward-EPS sum and
the price paired with it. These functions guard that pairing:

* currency match  — report currency must equal price currency (hard gate; ADRs
  are unsupported in v1 rather than silently mis-priced).
* time alignment  — the paired price's date must equal the estimate's observation
  date (no Tuesday price with a Friday estimate).
* split basis     — True P/E uses the RAW contemporaneous price and RAW
  contemporaneous EPS sum. Both rescale by the same factor on a split's ex-date,
  so the ratio is continuous across splits BY CONSTRUCTION. Mixing a back-adjusted
  price with a raw EPS (or vice versa) is what creates a phantom discontinuity;
  the canonical path never does that.
"""

from __future__ import annotations

from datetime import date

from ..constants import (
    PRICE_BASIS_RAW,
    REASON_CURRENCY_MISMATCH,
    REASON_TIME_MISALIGNMENT,
)
from .records import GateOutcome


def currency_guard(report_currency: str, price_currency: str) -> GateOutcome:
    """Hard gate: refuse to compute when report currency != price currency.

    Returns a failing outcome (reason ``currency_mismatch_unsupported_v1``) for any
    mismatch — e.g. a TWD reporter behind a USD ADR. The caller quarantines to
    ingestion_log and surfaces an explicit 'unsupported' state; it must NEVER fall
    through to a naive mismatched P/E.
    """
    if report_currency.upper() != price_currency.upper():
        return GateOutcome(
            passed=False,
            reason_code=REASON_CURRENCY_MISMATCH,
            detail=(
                f"report currency {report_currency} != price currency {price_currency}; "
                "ADR FX + share-ratio handling is not implemented in v1"
            ),
        )
    return GateOutcome(passed=True)


def time_alignment_guard(price_date: date, estimate_as_of: date) -> GateOutcome:
    """Hard gate: the paired price's date must equal the estimate's observation date."""
    if price_date != estimate_as_of:
        return GateOutcome(
            passed=False,
            reason_code=REASON_TIME_MISALIGNMENT,
            detail=(
                f"price_date {price_date} != estimate as_of {estimate_as_of}; "
                "price and estimate must be observed on the same date"
            ),
        )
    return GateOutcome(passed=True)


def compute_true_pe(price_raw: float, forward_eps_sum: float) -> float:
    """True P/E on the canonical RAW contemporaneous basis = raw price / raw EPS sum.

    Both inputs are the values as they stood on the observation date, so a split
    rescales numerator and denominator identically and the series stays continuous.
    Raises on a non-positive EPS sum — that case is caught upstream by the sanity
    gates and quarantined, never divided through.
    """
    if forward_eps_sum == 0:
        raise ZeroDivisionError("forward EPS sum is zero; should have been gated upstream")
    return price_raw / forward_eps_sum


# The basis label every True P/E in v1 is computed on (stored for transparency).
TRUE_PE_PRICE_BASIS = PRICE_BASIS_RAW
