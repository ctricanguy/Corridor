#!/usr/bin/env python
r"""Purge biased valuation_snapshots so the corridor history starts clean.

The pre-CIK-fix rows (engine_version 0.1.0) stored an UNCERTIFIED True P/E (annual/4,
no actuals subtracted). This removes them. It touches ONLY the derived
valuation_snapshots — the raw forward_estimate_snapshots / price_snapshots are NOT
biased and are kept untouched.

NOTE: even without purging, the corridor/research scripts already read ONLY rows at
the current engine_version (0.2.0), so the old rows can't blend in. Purge if you want
them physically gone.

    python scripts/purge_valuations.py                 # DRY RUN — counts only
    python scripts/purge_valuations.py --engine 0.1.0 --yes   # delete the old-engine rows
    python scripts/purge_valuations.py --all --yes            # delete ALL valuation_snapshots
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from corridor.config import get_settings  # noqa: E402
from corridor.db import init_db, session_scope  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge biased valuation_snapshots.")
    ap.add_argument("--engine", help="delete only rows with this engine_version (e.g. 0.1.0)")
    ap.add_argument("--all", action="store_true", help="delete ALL valuation_snapshots")
    ap.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()
    if not args.engine and not args.all:
        print("Specify --engine VERSION or --all. (Default is a dry run.)")
        return 2

    from corridor.db.models import ValuationSnapshot

    init_db(get_settings().database_url)
    with session_scope() as s:
        q = s.query(ValuationSnapshot)
        if args.engine:
            q = q.filter(ValuationSnapshot.engine_version == args.engine)
        n = q.count()
        scope = f"engine_version={args.engine}" if args.engine else "ALL versions"
        if not args.yes:
            print(f"DRY RUN: would delete {n} valuation_snapshots ({scope}). "
                  "Re-run with --yes to execute. (Raw estimate/price snapshots are untouched.)")
            return 0
        q.delete(synchronize_session=False)
        print(f"Deleted {n} valuation_snapshots ({scope}). Raw snapshots untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
