"""Ingestion layer (Stage 1).

Maps data-source records onto the immutable snapshot tables and writes Parquet
point-in-time files. Hosts the daily refresh job that ACCUMULATES forward-estimate
history from day one. Every run writes to ingestion_log; data gaps are surfaced,
never silently swallowed. Trailing financials are backfilled from EDGAR; forward
estimates are NEVER synthesized for past dates.
"""
