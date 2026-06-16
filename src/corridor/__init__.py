"""Corridor — a personal equity-research engine.

Three-factor framework over a focused large-cap tech/AI universe:
    Factor 1  Earnings (modeled; v1 = consensus passthrough)        engine/earnings.py
    Factor 2  Valuation: forward-P/E "corridor" / True P/E          engine/corridor.py  (Stage 2)
    Factor 2b Growth-adjusted valuation: forward PEG                engine/peg.py       (Stage 2)
    Factor 3  Sentiment / base-rate overlay                         engine/overlay.py   (Stage 3)

Built on immutable, point-in-time snapshots (db/models.py) so the forward-estimate
history that the corridor depends on is honest and lookahead-bias free.

Decision-support for personal use — not investment advice.
"""

__version__ = "0.1.0"
