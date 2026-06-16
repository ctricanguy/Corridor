"""Swappable data-source adapters.

Stage 0 defines the interfaces (base.py). Stage 1 adds concrete adapters:
    yfinance_source.py  -> PriceSource + ForwardEstimateSource (current view only)
    edgar_source.py     -> FundamentalsSource (trailing as-reported)
A paid historical-estimates adapter can later implement ForwardEstimateSource
and load into the same immutable tables without any engine change.
"""

from .base import (
    ForwardEstimateRecord,
    ForwardEstimateSource,
    FundamentalRecord,
    FundamentalsSource,
    PriceRecord,
    PriceSource,
)

__all__ = [
    "PriceRecord",
    "PriceSource",
    "ForwardEstimateRecord",
    "ForwardEstimateSource",
    "FundamentalRecord",
    "FundamentalsSource",
]
