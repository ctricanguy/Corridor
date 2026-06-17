"""Factor 2 — buy/trim signals from the corridor + earnings trend.

The rule (horizon = months, exposure-scaling only — no options, no shorting):

  * HARD BUY : price in the BOTTOM DECILE of its historical forward multiple AND the
               earnings trend is rising.
  * BUY      : in the bottom band (cheap) and earnings rising.
  * WATCH    : cheap but earnings NOT rising (value trap risk) — no buy.
  * TRIM     : near the top band.
  * HARD TRIM: in the top decile.
  * HOLD     : mid-corridor.

The signal is corridor-based; PEG is a SEPARATE view (engine/peg.py) shown alongside,
never blended in. Thin history is honest: with too few snapshots there is no signal,
and a thin corridor downgrades a hard_* call to its soft form with a loud annotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class SignalResult:
    signal: str  # hard_buy|buy|watch_cheap|hold|trim|hard_trim|insufficient_history
    pe_percentile: float | None
    earnings_trend: str  # rising|falling|flat|unknown
    is_thin: bool
    rationale: str


def earnings_trend(
    eps_points: list[tuple[date, float]], lookback_days: int = 63, min_change: float = 0.005
) -> tuple[str, str]:
    """Direction of the forward (NTM) EPS series: rising / falling / flat / unknown.

    Compares the latest NTM EPS to the earliest observation within the trailing
    ``lookback_days``. Needs >=2 points; otherwise 'unknown' (never assume rising).
    """
    pts = sorted(eps_points, key=lambda p: p[0])
    if len(pts) < 2:
        return "unknown", "insufficient EPS history for a trend"
    latest_date, latest = pts[-1]
    cutoff = latest_date - timedelta(days=lookback_days)
    window = [p for p in pts[:-1] if p[0] >= cutoff] or pts[:-1]
    _baseline_date, baseline = window[0]
    if baseline == 0:
        return "unknown", "zero EPS baseline"
    change = (latest - baseline) / abs(baseline)
    if change > min_change:
        return "rising", f"NTM EPS {baseline:.2f} -> {latest:.2f} (+{change:.1%})"
    if change < -min_change:
        return "falling", f"NTM EPS {baseline:.2f} -> {latest:.2f} ({change:.1%})"
    return "flat", f"NTM EPS ~{latest:.2f} (flat)"


_DOWNGRADE = {"hard_buy": "buy", "hard_trim": "trim"}


def classify_signal(
    pe_percentile: float | None,
    trend: str,
    is_thin: bool,
    bands_available: bool,
    *,
    hard_buy_pctl: int = 10,
    buy_pctl: int = 20,
    trim_pctl: int = 80,
    hard_trim_pctl: int = 90,
) -> SignalResult:
    """Classify the buy/trim signal from corridor position + earnings trend."""
    if not bands_available or pe_percentile is None:
        return SignalResult("insufficient_history", pe_percentile, trend, is_thin,
                            "corridor needs more snapshot history before a signal")
    rising = trend == "rising"
    pct = pe_percentile
    if pct <= hard_buy_pctl:
        sig = "hard_buy" if rising else "watch_cheap"
    elif pct <= buy_pctl:
        sig = "buy" if rising else "watch_cheap"
    elif pct >= hard_trim_pctl:
        sig = "hard_trim"
    elif pct >= trim_pctl:
        sig = "trim"
    else:
        sig = "hold"

    note = ""
    if is_thin and sig in _DOWNGRADE:
        sig = _DOWNGRADE[sig]
        note = " [downgraded from hard_* — THIN history, low confidence]"
    return SignalResult(sig, pe_percentile, trend, is_thin, _rationale(sig, pct, trend) + note)


def _rationale(sig: str, pct: float, trend: str) -> str:
    where = f"True P/E at {pct:.0f}th pct of its history"
    if sig == "hard_buy":
        return f"{where} (bottom decile) + earnings {trend} -> hard buy"
    if sig == "buy":
        return f"{where} (bottom band) + earnings {trend} -> buy"
    if sig == "watch_cheap":
        return f"{where} (cheap) but earnings {trend} -> watch, not buy (value-trap risk)"
    if sig == "hard_trim":
        return f"{where} (top decile) -> hard trim"
    if sig == "trim":
        return f"{where} (top band) -> trim"
    return f"{where} (mid-corridor), earnings {trend} -> hold"
