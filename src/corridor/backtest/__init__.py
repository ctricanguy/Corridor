"""Validation harness (Stage 5).

Walk-forward backtest of the signals using ONLY point-in-time data (as-of-date
snapshots, filed-date fundamentals). Reports risk-adjusted return net of assumed
costs. Designed to DISAPPOINT when warranted: a factor with no out-of-sample edge
is labeled context-only, not dressed up as alpha. Early runs are explicitly
limited by sparse forward-estimate history and strengthen as snapshots accumulate.
"""
