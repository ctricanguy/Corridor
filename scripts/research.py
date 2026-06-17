#!/usr/bin/env python
r"""Factor-2 research read for a ticker: corridor SIGNAL + forward PEG, side by side.

Reads the accumulated valuation_snapshots (corridor + earnings trend) and EDGAR
(trailing LTM EPS for PEG). The two views are shown SEPARATELY and never blended —
their disagreement is itself signal.

    python scripts/research.py --ticker NVDA
    python scripts/research.py --demo            # synthetic (NOT live), for the format

Computes/show only; wiring into the dashboard is the next, separate step.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import get_settings, load_config  # noqa: E402
from corridor.engine.corridor import TruePePoint, build_corridor  # noqa: E402
from corridor.engine.peg import forward_peg, ntm_vs_ltm_growth  # noqa: E402
from corridor.engine.signals import classify_signal, earnings_trend  # noqa: E402

CIK = {"NVDA": "0001045810"}  # minimal map; populate ciks in config for the full watchlist


def _m(v):  # money
    return f"${v:,.2f}" if v is not None else "n/a"


def _show(ticker, corridor, trend, trend_detail, signal, peg) -> None:
    bar = "=" * 72
    print(bar)
    print(f"  {ticker} — Factor 2 research read — as of {corridor.as_of}")
    print(bar)
    print("  [corridor]")
    cov = f"{corridor.coverage_score:.2f}" if corridor.coverage_score is not None else "n/a"
    print(f"    history {corridor.history_days}d "
          f"[{'THIN' if corridor.is_thin else 'ok'}]   coverage {cov}")
    print(f"    current: price {_m(corridor.current_price)}  fwd EPS {_m(corridor.current_fwd_eps)}"
          f"  True P/E {corridor.current_true_pe:.2f}x" if corridor.current_true_pe else "")
    if corridor.bands is not None:
        b = corridor.bands
        print(f"    bands: {b.pctl_low}th {b.pe_low:.2f}x | median {b.pe_median:.2f}x"
              f" | {b.pctl_high}th {b.pe_high:.2f}x")
        print(f"    price-space: low {_m(corridor.corridor_low_price)} |"
              f" mid {_m(corridor.corridor_mid_price)} | high {_m(corridor.corridor_high_price)}")
        print(f"    position: {corridor.position}  "
              f"(True P/E at {corridor.pe_percentile:.0f}th pct of history)")
    if corridor.notes:
        print(f"    [!] {corridor.notes}")

    print("\n  [signal]  (corridor + earnings trend — NOT blended with PEG)")
    print(f"    earnings trend: {trend}  ({trend_detail})")
    print(f"    SIGNAL: {signal.signal.upper()}   ({signal.rationale})")

    print("\n  [forward PEG]  (separate growth-adjusted view)")
    print(f"    basis: {peg.growth_basis}")
    if peg.suppressed:
        print(f"    SUPPRESSED — {peg.suppression_reason}  (defer to corridor)")
    else:
        print(f"    NTM EPS {_m(peg.ntm_eps)}  LTM EPS {_m(peg.growth_input)}  "
              f"growth {peg.growth_rate:+.1%}")
        print(f"    forward P/E {peg.forward_pe:.2f} / growth {peg.growth_rate * 100:.1f}"
              f" = PEG {peg.forward_peg:.2f}  -> {peg.context}  [context only]")

    print("\n  two views, side by side (disagreement = signal; act on corridor + trend):")
    peg_view = "suppressed" if peg.suppressed else f"PEG {peg.forward_peg:.2f} ({peg.context})"
    print(f"    corridor signal = {signal.signal.upper()}   |   growth-adjusted = {peg_view}")
    print(bar)


def _params(config):
    cor = (config.valuation or {}).get("corridor", {})
    sig = (config.valuation or {}).get("signals", {})
    return (
        {"pctl_low": int(cor.get("pctl_low", 20)), "pctl_high": int(cor.get("pctl_high", 80)),
         "min_history_days": int(cor.get("min_history_days", 60)),
         "lookback_days": int(cor.get("lookback_days", 504))},
        {"hard_buy_pctl": int(sig.get("hard_buy_pctl", 10)),
         "buy_pctl": int(sig.get("buy_pctl", 20)),
         "trim_pctl": int(sig.get("trim_pctl", 80)),
         "hard_trim_pctl": int(sig.get("hard_trim_pctl", 90))},
        config.peg or {},
    )


def _peg_from(config, ticker, true_pe, ntm_eps, ltm_eps, coverage):
    peg_cfg = config.peg or {}
    rate, reason = ntm_vs_ltm_growth(ntm_eps, ltm_eps)
    return forward_peg(
        true_pe, ntm_eps, rate, "ntm_vs_ltm", growth_input=ltm_eps, growth_reason=reason,
        min_growth_rate=float(peg_cfg.get("min_growth_rate", 0.02)),
        cheap_threshold=float(peg_cfg.get("cheap_threshold", 1.0)),
        rich_threshold=float(peg_cfg.get("rich_threshold", 2.0)),
        coverage_score=coverage, label=ticker,
    )


def _from_db(ticker, config):
    from corridor.datasources.edgar_source import EdgarFundamentalsSource
    from corridor.db import init_db, session_scope
    from corridor.db.models import ValuationSnapshot
    from corridor.engine.peg import quarterly_actuals_from_edgar, trailing_ltm_eps

    settings = get_settings()
    init_db(settings.database_url)
    cor_params, sig_params, _ = _params(config)
    cal = config.fiscal_calendars().get(ticker)
    with session_scope() as s:
        rows = (s.query(ValuationSnapshot)
                .filter(ValuationSnapshot.ticker == ticker, ValuationSnapshot.true_pe.isnot(None))
                .order_by(ValuationSnapshot.as_of_date).all())
        history = [TruePePoint(r.as_of_date, r.true_pe) for r in rows]
        eps_points = [(r.as_of_date, r.forward_eps_ntm) for r in rows if r.forward_eps_ntm]
        latest = rows[-1] if rows else None
    if latest is None:
        print(f"No valuation snapshots for {ticker} yet — run daily_refresh to accumulate.")
        return
    as_of = latest.as_of_date
    corridor = build_corridor(ticker, as_of, history, latest.forward_eps_ntm, latest.price,
                              coverage_score=latest.coverage_score, **cor_params)
    trend, detail = earnings_trend(eps_points)
    signal = classify_signal(corridor.pe_percentile, trend, corridor.is_thin,
                             corridor.bands is not None, **sig_params)
    # LTM from EDGAR for PEG.
    ltm = None
    if cal is not None and ticker in CIK:
        edgar = EdgarFundamentalsSource(settings.sec_edgar_user_agent, config.fiscal_calendars())
        q, a = quarterly_actuals_from_edgar(edgar.get_fundamentals(ticker, cik=CIK[ticker]), cal)
        ltm = trailing_ltm_eps(q, a)
    peg = _peg_from(config, ticker, corridor.current_true_pe, latest.forward_eps_ntm, ltm,
                    latest.coverage_score)
    _show(ticker, corridor, trend, detail, signal, peg)


def _demo(config):
    cor_params, sig_params, _ = _params(config)
    cor_params["min_history_days"] = 60
    start = date(2026, 3, 19)
    # Synthetic 90-day True P/E (~18-30x) + a rising NTM EPS series.
    history = [TruePePoint(start + timedelta(days=i), 24.0 + 6.0 * math.sin(i / 15.0))
               for i in range(90)]
    eps_points = [(start + timedelta(days=i), 9.10 + 0.56 * (i / 89.0)) for i in range(90)]
    ntm_eps, price = 9.66, 211.97
    corridor = build_corridor("NVDA (SYNTHETIC)", history[-1].as_of, history, ntm_eps, price,
                              coverage_score=0.0, **cor_params)
    trend, detail = earnings_trend(eps_points)
    signal = classify_signal(corridor.pe_percentile, trend, corridor.is_thin, True, **sig_params)
    peg = _peg_from(config, "NVDA (SYNTHETIC)", corridor.current_true_pe, ntm_eps, 7.20, 0.0)
    _show("NVDA (SYNTHETIC)", corridor, trend, detail, signal, peg)


def main() -> int:
    ap = argparse.ArgumentParser(description="Factor-2 research read (corridor signal + PEG).")
    ap.add_argument("--ticker", default="NVDA")
    ap.add_argument("--demo", action="store_true", help="synthetic data (not live)")
    args = ap.parse_args()
    config = load_config()
    if args.demo:
        print("  (SYNTHETIC demo — not live)\n")
        _demo(config)
    else:
        _from_db(args.ticker.upper(), config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
