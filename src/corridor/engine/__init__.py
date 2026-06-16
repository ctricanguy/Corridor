"""Valuation + overlay engine.

    earnings.py  Factor 1  EarningsModel seam (Stage 0 interface; Stage 2 consensus impl)
    corridor.py  Factor 2  True P/E series, percentile bands, corridor, signals  (Stage 2)
    peg.py       Factor 2b forward PEG with explicit, logged growth basis        (Stage 2)
    overlay.py   Factor 3  winning-streak study, RSI, MA distance, earnings flags (Stage 3)

PEG and the corridor premium-term are computed SEPARATELY and shown side by side;
their disagreement is signal and must never be blended into one opaque score.
"""
