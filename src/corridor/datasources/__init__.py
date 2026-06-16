"""Swappable data-source adapters.

Stage 0 defined the interfaces (base.py). Stage 1 adds concrete adapters:
    fmp_source.py       -> ForwardEstimateSource (PRIMARY; true per-quarter forward EPS)
    yfinance_source.py  -> PriceSource (raw + adjusted close, split ratios)
    edgar_source.py     -> FundamentalsSource (realized diluted EPS, continuing ops)

ALL THREE ADAPTERS ARE UNVALIDATED AGAINST LIVE ENDPOINTS in this build (no
network / no key). Run scripts/validate_live.py with a key + network to confirm
their parsed output shape before trusting them. A paid historical-estimates
adapter can later implement ForwardEstimateSource and load into the same immutable
tables without any engine change.
"""

from .base import (
    ForwardEstimateRecord,
    ForwardEstimateSource,
    FundamentalRecord,
    FundamentalsSource,
    PriceRecord,
    PriceSource,
)
from .edgar_source import EdgarFundamentalsSource
from .fmp_source import FMPForwardEstimateSource
from .shape import ShapeMismatch, assert_records_shape
from .yfinance_source import YFinancePriceSource

__all__ = [
    "PriceRecord",
    "PriceSource",
    "ForwardEstimateRecord",
    "ForwardEstimateSource",
    "FundamentalRecord",
    "FundamentalsSource",
    "FMPForwardEstimateSource",
    "YFinancePriceSource",
    "EdgarFundamentalsSource",
    "assert_records_shape",
    "ShapeMismatch",
]
