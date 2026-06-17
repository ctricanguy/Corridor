"""Shared string constants — provenance bases, construction methods, statuses,
and quarantine reason codes. Centralized so the engine, the ingest job, and the
tests all speak exactly the same vocabulary (no drifting magic strings).
"""

from __future__ import annotations

# --- EPS bases (documented on every value; never silently mixed) ---
EPS_BASIS_ADJUSTED_DILUTED = "adjusted_diluted"  # non-GAAP consensus estimates (FMP)
EPS_BASIS_GAAP_DILUTED_CONTINUING = "gaap_diluted_continuing_ops"  # EDGAR actuals

# --- Price bases ---
PRICE_BASIS_RAW = "raw"  # unadjusted, contemporaneous — the canonical True P/E basis
PRICE_BASIS_SPLIT_ADJUSTED = "split_adjusted"

# --- Forward-sum construction methods (per quarter component) ---
METHOD_REAL_QUARTERLY = "real_quarterly"  # a genuine provider quarterly estimate
METHOD_DERIVED_FROM_ANNUAL = "derived_from_annual"  # split out of an annual estimate

# --- ingestion_log statuses ---
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_MISSING = "missing"
STATUS_ERROR = "error"
STATUS_QUARANTINE = "quarantine"

# --- Quarantine reason codes (machine-readable; paired with human detail) ---
REASON_NON_POSITIVE_PRICE = "non_positive_price"
REASON_EPS_SIGN_FLIP = "eps_sum_sign_flip_no_earnings"
REASON_TRUE_PE_JUMP = "true_pe_jump_no_split"
REASON_CURRENCY_MISMATCH = "currency_mismatch_unsupported_v1"
REASON_TIME_MISALIGNMENT = "price_estimate_date_mismatch"
REASON_INCOMPLETE_WINDOW = "incomplete_forward_window"
REASON_PRICE_GAP = "price_gap_no_bars"  # yfinance returned zero bars for the date
REASON_NO_ESTIMATES = "no_forward_estimates"

# --- Jobs ---
JOB_DAILY_REFRESH = "daily_refresh"
JOB_BACKFILL_EDGAR = "backfill_edgar"
JOB_ACTUALS = "actuals_loop"

# Until each adapter has been confirmed against its live endpoint via
# scripts/validate_live.py, it is flagged with this marker in code + logs.
UNVALIDATED_MARKER = "UNVALIDATED_AGAINST_LIVE_ENDPOINT"
