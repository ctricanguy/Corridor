#!/usr/bin/env python
r"""ONE-OFF DIAGNOSTIC — is FMP period=quarter gated above Starter?

Run LOCALLY (needs FMP_API_KEY + network). This is a throwaway probe (safe to
delete); it changes nothing in the engine. It hits FMP's stable analyst-estimates
endpoint for NVDA TWICE — period=annual and period=quarter — SEPARATELY, captures
each raw HTTP status + body, and classifies the result:

  * annual 200 + quarter 402  -> quarterly is gated above Starter; annual is included
  * annual 402 + quarter 402  -> estimates aren't in your Starter plan at all
  * annual 200 + quarter 200  -> quarterly is NOT gated

If annual succeeds it prints the REAL NVDA annual numbers and the annual-only
next-4-quarter forward sum (every quarter derived from the annual). The api key is
redacted from every printed URL/body.

Usage:
    export FMP_API_KEY=...
    python scripts/fmp_probe.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import load_config  # noqa: E402
from corridor.datasources.fmp_source import FMPForwardEstimateSource, _redact  # noqa: E402
from corridor.ingest.fiscal import enumerate_fiscal_quarters  # noqa: E402
from corridor.ingest.forward_sum import build_forward_eps_sum  # noqa: E402
from corridor.ingest.records import FiscalPeriod  # noqa: E402
from corridor.ingest.window import unreported_window  # noqa: E402

TICKER = "NVDA"


def _probe(base_url: str, api_key: str, period: str) -> tuple[int, str, str, Any]:
    """GET /stable/analyst-estimates for one period; return (status, url, body, json)."""
    import requests

    url = f"{base_url}/stable/analyst-estimates"
    params = {"symbol": TICKER, "period": period, "page": 0, "limit": 100, "apikey": api_key}
    resp = requests.get(url, params=params, timeout=30)
    body = _redact(resp.text[:500])
    parsed: Any = None
    content_type = resp.headers.get("content-type", "")
    if resp.status_code == 200 and content_type.startswith("application/json"):
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None
    return resp.status_code, _redact(resp.url), body, parsed


def main() -> int:
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        print("FMP_API_KEY not set — this probe needs a real key + network.")
        return 2

    config = load_config()
    base = config.data["fmp"]["base_url"]
    cal = config.fiscal_calendars().get(TICKER)
    if cal is None:
        print(f"No fiscal calendar configured for {TICKER}.")
        return 2
    today = datetime.now(UTC).date()

    print(f"Probing FMP /stable/analyst-estimates for {TICKER}  ({today} UTC)\n")
    a_status, a_url, a_body, a_json = _probe(base, api_key, "annual")
    print(f"  annual  : HTTP {a_status}   {a_url}")
    if a_status != 200:
        print(f"            body: {a_body}")
    q_status, q_url, q_body, _q_json = _probe(base, api_key, "quarter")
    print(f"  quarter : HTTP {q_status}   {q_url}")
    if q_status != 200:
        print(f"            body: {q_body}")

    print("\n" + "=" * 70)
    if a_status == 200 and q_status == 402:
        print("VERDICT: quarterly is GATED above Starter; annual IS included.")
    elif a_status == 402 and q_status == 402:
        print("VERDICT: analyst estimates are NOT in your Starter plan (annual also 402).")
    elif a_status == 200 and q_status == 200:
        print("VERDICT: both succeed — quarterly is NOT gated.")
    else:
        print(f"VERDICT: inconclusive (annual={a_status}, quarter={q_status}) — see bodies above.")
    print("=" * 70)

    if a_status == 200 and isinstance(a_json, list):
        _print_annual_breakdown(a_json, api_key, base, cal, today)
    return 0


def _print_annual_breakdown(
    a_json: list[dict[str, Any]], api_key: str, base: str, cal: Any, today: date
) -> None:
    src = FMPForwardEstimateSource(api_key, {TICKER: cal}, base_url=base)
    annual_recs = src.parse_estimates(a_json, TICKER, today, "annual", cal)
    print(f"\n--- {TICKER} annual estimates (REAL, from FMP /stable) ---")
    for r in sorted(annual_recs, key=lambda r: r.period_end_date or date.min):
        print(f"  {r.fiscal_period}  EPS={r.value:.4f}  period_end={r.period_end_date}  "
              f"analysts={r.num_analysts}")

    annual = {r.fiscal_period: r.value for r in annual_recs}
    periods = [
        FiscalPeriod(lbl, end, date.fromordinal(end.toordinal() + 21), confirmed=False)
        for lbl, end in enumerate_fiscal_quarters(cal, today)
    ]
    window = unreported_window(periods, today, n=4)
    fwd = build_forward_eps_sum(window, {}, annual)  # no quarterly -> every quarter derived
    print(f"\n--- {TICKER} annual-only next-4-quarter forward EPS (every quarter DERIVED) ---")
    for c in fwd.components:
        print(f"  {c.fiscal_period}  EPS={c.value:.4f}  [{c.detail}]")
    print(f"  forward EPS sum = {fwd.value:.4f}   coverage={fwd.coverage_score:.2f} "
          f"(0 real / {len(window.periods)} quarters)   complete={fwd.complete}")
    print("  NOTE: with no quarterly estimates every quarter is annual/4 (minus reported")
    print("        actuals once EDGAR is wired in). This is the annual-only path — paste the")
    print("        output back and we decide whether to make annual the v1 source.")


if __name__ == "__main__":
    raise SystemExit(main())
