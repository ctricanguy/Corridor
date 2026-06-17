#!/usr/bin/env python
r"""Show the forward-P/E CORRIDOR for a ticker (Factor 2).

Reads the True P/E history accumulated in valuation_snapshots (run daily_refresh to
build it) and prints the percentile bands, the price-space corridor, the current
position, and — honestly — how many days of real history back it.

    python scripts/corridor.py --ticker NVDA
    python scripts/corridor.py --demo            # synthetic 90-day history (NOT live)

Computes the corridor only; it does NOT emit buy/trim signals (next step).
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import get_settings, load_config  # noqa: E402
from corridor.engine.corridor import Corridor, TruePePoint, build_corridor  # noqa: E402


def _money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "n/a"


def _mult(v: float | None) -> str:
    return f"{v:.2f}x" if v is not None else "n/a"


def _print(c: Corridor) -> None:
    bar = "=" * 70
    print(bar)
    print(f"  {c.ticker} CORRIDOR — as of {c.as_of}")
    print(bar)
    thin = "THIN" if c.is_thin else "ok"
    cov = f"{c.coverage_score:.2f}" if c.coverage_score is not None else "n/a"
    print(f"  history    : {c.history_days} day(s) of snapshots   [{thin}; "
          f"min for confidence = {c.min_history_days}]")
    print(f"  coverage   : {cov}   (0.00 = forward quarters all derived from the annual curve)")
    print(f"  current    : price {_money(c.current_price)}   forward EPS "
          f"{_money(c.current_fwd_eps)}   True P/E {_mult(c.current_true_pe)}")

    if c.bands is None:
        print(f"\n  {c.notes}")
        print(bar)
        return

    b = c.bands
    print("\n  forward-P/E bands (historical distribution of the multiple):")
    print(f"    {b.pctl_low}th pct : {_mult(b.pe_low)}")
    print(f"    median   : {_mult(b.pe_median)}")
    print(f"    {b.pctl_high}th pct : {_mult(b.pe_high)}")
    print(f"\n  price-space corridor (bands x current forward EPS {_money(c.current_fwd_eps)}):")
    print(f"    low  ({b.pctl_low}th)   : {_money(c.corridor_low_price)}")
    print(f"    mid  (median) : {_money(c.corridor_mid_price)}")
    print(f"    high ({b.pctl_high}th)  : {_money(c.corridor_high_price)}")
    pct = f"{c.pe_percentile:.0f}th" if c.pe_percentile is not None else "n/a"
    print(f"\n  position   : price {_money(c.current_price)} is {c.position}")
    print(f"               True P/E {_mult(c.current_true_pe)} sits at the {pct} percentile "
          "of its history")
    if c.notes:
        print(f"\n  [!] {c.notes}")
    print(bar)


def _corridor_params(config) -> dict:  # type: ignore[no-untyped-def]
    cor = (config.valuation or {}).get("corridor", {})
    return {
        "pctl_low": int(cor.get("pctl_low", 20)),
        "pctl_high": int(cor.get("pctl_high", 80)),
        "min_history_days": int(cor.get("min_history_days", 60)),
        "lookback_days": int(cor.get("lookback_days", 504)),
    }


def _from_db(ticker: str, config) -> Corridor:  # type: ignore[no-untyped-def]
    from corridor.db import init_db, session_scope
    from corridor.db.models import ValuationSnapshot

    settings = get_settings()
    init_db(settings.database_url)
    with session_scope() as s:
        rows = (
            s.query(ValuationSnapshot)
            .filter(ValuationSnapshot.ticker == ticker, ValuationSnapshot.true_pe.isnot(None),
                    ValuationSnapshot.engine_version == config.engine_version)  # certified only
            .order_by(ValuationSnapshot.as_of_date)
            .all()
        )
        history = [TruePePoint(r.as_of_date, r.true_pe) for r in rows]
        latest = rows[-1] if rows else None
        as_of = latest.as_of_date if latest else datetime.now(UTC).date()
        return build_corridor(
            ticker, as_of, history,
            current_fwd_eps=latest.forward_eps_ntm if latest else None,
            current_price=latest.price if latest else None,
            coverage_score=latest.coverage_score if latest else None,
            **_corridor_params(config),
        )


def _demo(config) -> Corridor:  # type: ignore[no-untyped-def]
    # SYNTHETIC 90-day True P/E history (NOT live) — oscillates ~18-30x so the bands
    # and price-space corridor are visible end to end.
    start = date(2026, 3, 19)
    history = [
        TruePePoint(start + timedelta(days=i), 24.0 + 6.0 * math.sin(i / 15.0))
        for i in range(90)
    ]
    fwd_eps = 9.66
    current_pe = history[-1].true_pe
    params = _corridor_params(config)
    params["min_history_days"] = 60  # 90 days > 60 -> NOT thin, so bands are trustworthy
    return build_corridor(
        "NVDA (SYNTHETIC)", history[-1].as_of, history,
        current_fwd_eps=fwd_eps, current_price=current_pe * fwd_eps,
        coverage_score=0.0, **params,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Show the forward-P/E corridor for a ticker.")
    ap.add_argument("--ticker", default="NVDA")
    ap.add_argument("--demo", action="store_true", help="synthetic 90-day history (not live)")
    args = ap.parse_args()
    config = load_config()
    if args.demo:
        print("  (SYNTHETIC demo data — not live; for format illustration only)\n")
        _print(_demo(config))
    else:
        _print(_from_db(args.ticker.upper(), config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
