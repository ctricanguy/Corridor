"""Multi-source reconciliation — store the snapshot, but flag disagreement.

Unlike the sanity gates (which quarantine), reconciliation does NOT discard data:
when two sources disagree beyond a threshold we still store the snapshot, set a
disagreement flag, and log both values so the disagreement is visible and
auditable rather than averaged away.

Two checks:
* price disagreement — yfinance vs FMP price for the same date.
* window divergence  — our strict 4-quarter sum vs the provider's native NTM EPS.
  If these diverge badly, the window logic or the source is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceReconciliation:
    price_primary: float  # the value used downstream (yfinance)
    price_yf: float | None
    price_fmp: float | None
    disagreement_pct: float | None
    disagreement_flag: bool
    detail: str | None


def reconcile_prices(
    price_yf: float | None,
    price_fmp: float | None,
    threshold_pct: float = 0.01,
) -> PriceReconciliation:
    """Cross-check the yfinance and FMP price for a date.

    yfinance is the primary price used downstream; FMP is the cross-check. If both
    are present and differ by more than ``threshold_pct``, the snapshot is flagged
    (not rejected) and both values are retained for the log.
    """
    primary = price_yf if price_yf is not None else price_fmp
    if primary is None:
        return PriceReconciliation(0.0, price_yf, price_fmp, None, False, "no price available")
    if price_yf is None or price_fmp is None:
        return PriceReconciliation(
            primary, price_yf, price_fmp, None, False, "only one price source available"
        )
    disagreement = abs(price_yf - price_fmp) / price_yf if price_yf else None
    flag = disagreement is not None and disagreement > threshold_pct
    detail = (
        f"yfinance {price_yf:.4f} vs FMP {price_fmp:.4f} = {disagreement:.4%} apart"
        if flag
        else None
    )
    return PriceReconciliation(price_yf, price_yf, price_fmp, disagreement, flag, detail)


@dataclass(frozen=True)
class NtmCrossCheck:
    strict_sum: float
    native_ntm: float | None
    divergence_pct: float | None
    divergence_flag: bool
    detail: str | None


def ntm_cross_check(
    strict_sum: float,
    native_ntm: float | None,
    threshold_pct: float = 0.10,
) -> NtmCrossCheck:
    """Compare our strict 4-quarter sum against a provider-native NTM EPS.

    They should be close. A divergence beyond ``threshold_pct`` flags the snapshot
    (window logic or source suspect) without discarding it.
    """
    if native_ntm is None or native_ntm == 0:
        return NtmCrossCheck(strict_sum, native_ntm, None, False, "no native NTM to compare")
    divergence = abs(strict_sum - native_ntm) / abs(native_ntm)
    flag = divergence > threshold_pct
    detail = (
        f"strict 4Q sum {strict_sum:.4f} vs native NTM {native_ntm:.4f} = {divergence:.4%} apart"
        if flag
        else None
    )
    return NtmCrossCheck(strict_sum, native_ntm, divergence, flag, detail)
