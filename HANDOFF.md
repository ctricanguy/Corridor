# Corridor — Session Handoff

> Read this first. It is written for a fresh Claude Code session with **zero memory**
> of how this repo was built. Assume you see only the repository and this document.
> It captures what Corridor is, the non-negotiable invariants, the **hard-won locked
> decisions** (do not relitigate them), current state, and the traps that already bit us.

**Current branch:** `claude/great-volta-w03vwf` (all work lands here; never push elsewhere without asking).
**Status at handoff:** Stages 0–2 + CIK resolver done. Timezone + FY-gate bugs fixed (2026-06-18/19). **97 passed, 1 skipped; ruff + mypy clean.**
Next up: **Stage 4 (visualization/dashboard)**, then **Stage 5 (backtest)**. Do **not** start Stage 4 without the user's go-ahead.

---

## 1. Project

**What Corridor is.** A *personal, single-user* equity-research engine that reverse-engineers an
independent publication's three-factor method — **Earnings × Valuation × Sentiment/base-rates** —
over a focused 10-name large-cap tech/AI watchlist (NVDA, MSFT, AAPL, GOOGL, AMZN, META, TSLA,
AMD, AVGO, MU). It screens/monitors those names, surfaces buy/trim signals, and (eventually)
produces a graph-rich research memo per name. **It is NOT a product** — no auth, no multi-tenant,
no billing, one user (a CS student) running it locally on a laptop and unattended on an always-on
**Raspberry Pi**. Decision-support for the user's own money, **not investment advice**. The
visualization is meant to be first-class (the graphs are half the point) but is not built yet.

### Repo layout

```
Corridor/
├── HANDOFF.md              <- you are here
├── README.md               full project explanation (factors, charts, honesty constraints)
├── config.yaml             watchlist + ALL methodology params (no secrets)
├── .env.example            secrets/paths template (copy to .env; .env is git-ignored)
├── pyproject.toml          deps (uv/venv) + ruff/mypy/pytest config
├── Makefile                install / init-db / schema / test / app
├── src/corridor/
│   ├── config.py           .env (Settings) + config.yaml (Config) loaders; PROJECT_ROOT; sqlite path made ABSOLUTE
│   ├── constants.py        bases / methods / statuses / quarantine reason codes (no magic strings)
│   ├── db/
│   │   ├── models.py       POINT-IN-TIME schema (source of truth). IMMUTABLE_TABLES + ORM models
│   │   └── database.py     engine/session, init_db(), installs immutability TRIGGERS
│   ├── datasources/        swappable adapters (fetch/parse separated; parse is unit-tested)
│   │   ├── base.py         ABCs + record dataclasses (Price/ForwardEstimate/Fundamental)
│   │   ├── fmp_source.py   FMP /stable analyst-estimates (ANNUAL only on Starter) + price cross-check
│   │   ├── yfinance_source.py  prices (raw+adj, splits, retry on empty) + forward-EPS cross-check
│   │   ├── edgar_source.py     EDGAR companyfacts -> diluted EPS (date-derived labels!)
│   │   ├── cik_resolver.py     ticker -> CIK via SEC company_tickers.json (cached)
│   │   └── shape.py        record shape contract (fixtures <-> live; catches renamed fields)
│   ├── ingest/             STAGE 1 PIPELINE (pure, testable) + the daily job
│   │   ├── fiscal.py        FiscalCalendar, label_period (date-derived FY labels), enumerate, bounds
│   │   ├── window.py        next-4-unreported-quarter window (report-date driven)
│   │   ├── forward_sum.py   FLAT (annual - actuals)/unknown-quarters derivation + construction_method + coverage
│   │   ├── consistency.py   currency guard, time-alignment, split-safe True P/E
│   │   ├── gates.py         write-time sanity gates (quarantine)
│   │   ├── reconcile.py     price + NTM + quarterly cross-checks (flag, don't discard)
│   │   ├── actuals.py       beat/miss vs pre-report estimate + per-source accuracy
│   │   ├── records.py       FiscalPeriod, WindowResult, ForwardSumResult, PricePoint, ValuationInput
│   │   └── job.py           assemble_valuation (pure) + run_daily (orchestration) + check_label_alignment
│   ├── engine/             STAGE 2 valuation engine (pure)
│   │   ├── earnings.py      Factor 1 EarningsModel seam (interface only)
│   │   ├── corridor.py      Factor 2: True P/E history -> percentile bands -> price-space -> position
│   │   ├── signals.py       buy/trim from corridor position + earnings trend
│   │   └── peg.py           Factor 2b: forward PEG (explicit growth basis) + LTM-from-EDGAR
│   └── analysis/           ANALYSIS-ONLY (not imported by the pipeline)
│       └── derivation_compare.py   flat vs (a) blend vs (b) seasonality study (kept as evidence)
├── app/dashboard.py        Streamlit entry point — placeholder (Stage 4)
├── scripts/                run from the repo root: `python scripts/<name>.py`
│   ├── init_db.py          create schema + immutability guards + seed watchlist
│   ├── dump_schema.py      print the generated DDL
│   ├── daily_refresh.py    THE cron job: snapshot estimates/price daily (hardened, --dry-run)
│   ├── corridor.py         show the forward-P/E corridor for a ticker (--ticker / --demo)
│   ├── research.py         per-ticker Factor-2 read: corridor SIGNAL + PEG side by side (--ticker / --demo)
│   ├── validate_live.py    one-command LIVE adapter validation (NVDA) — run on the Pi
│   ├── compare_derivation.py  live flat/(a)/(b) study (kept; (b) is dead, see decisions)
│   ├── fmp_probe.py         one-off: is FMP quarterly gated? (already answered: yes, Premium)
│   └── purge_valuations.py  delete biased pre-CIK-fix valuation_snapshots (dry-run default)
└── tests/                  pytest; every calc has hand-checkable numbers; fixtures/ has adversarial JSON
```

### How to run (the user does this on the laptop / Pi — see Gotchas about the sandbox)

```bash
# install (uv preferred; falls back to venv+pip)
make install            # or: uv venv && uv pip install -e ".[dev]"

cp .env.example .env    # then edit: FMP_API_KEY, SEC_EDGAR_USER_AGENT (real email)

make init-db            # create schema + immutability guards + seed the 10 securities
make test               # 92 passing, 1 live test skipped (see below)
make schema             # print the DDL any time

# daily snapshot job (needs FMP_API_KEY + network)
python scripts/daily_refresh.py            # real run -> logs/daily_refresh.log
python scripts/daily_refresh.py --dry-run  # fetch + log, write NOTHING (test wiring)

# read the corridor + signal + PEG (after history accumulates)
python scripts/corridor.py --ticker NVDA
python scripts/research.py --ticker NVDA
python scripts/corridor.py --demo          # synthetic, shows the format with no history

# LIVE adapter validation + alignment (Pi/laptop only)
python scripts/validate_live.py
CORRIDOR_RUN_LIVE=1 python -m pytest tests/test_alignment_live.py -v
```

### Pi cron (user is on an always-on Pi; user = `star`, paths = `/home/star/Corridor`)

```cron
# weekdays at 22:00 — snapshot the day's forward estimates + price
0 22 * * 1-5  cd /home/star/Corridor && .venv/bin/python scripts/daily_refresh.py >> logs/cron.out 2>&1
```

The job is **idempotent** (safe to re-run / retry the same day), **resilient** (one bad ticker
doesn't abort the run; non-zero exit only on TOTAL failure so cron surfaces real breakage),
**self-contained** (loads `.env` itself; resolves the sqlite path to ABSOLUTE so cron's working
dir doesn't matter), and writes a timestamped run-summary to `logs/daily_refresh.log`.

---

## 2. Architecture & the non-negotiables

### Point-in-time integrity — the single most important thing. WHY: anti-lookahead.

The whole method depends on an **honest history of *forward* estimates**. Every day we snapshot
"next-4-quarter EPS" with the **as-of date** and **never overwrite it**. If you overwrote forward
estimates with today's view, every backtest would silently use future knowledge and look
profitable — lookahead bias that invalidates everything.

Enforcement is structural, not by convention:
- `forward_estimate_snapshots`, `fundamentals`, `realized_actuals` are **append-only**.
  `init_db()` installs SQLite **triggers** that `RAISE(ABORT)` on `UPDATE`/`DELETE` of those tables
  (`IMMUTABLE_TABLES` in `db/models.py`). A correction is a NEW dated row, never a mutation.
- **Do not "optimize" the triggers away.** Tests `tests/test_schema.py` prove they fire. If a
  write needs to UPDATE one of these tables, the design is wrong — insert a new dated snapshot.
- Derived tables (`valuation_snapshots`, `overlay_snapshots`) ARE mutable/recomputable; only the
  raw observations are immutable.

### Swappable data-source interface — WHY: the paid-upgrade path.

Adapters implement ABCs in `datasources/base.py` (`PriceSource`, `ForwardEstimateSource`,
`FundamentalsSource`) and return plain record dataclasses. The engine depends only on the
interface + record shape, never on a provider. Free sources give THIN forward-estimate history
(today's view only); the app accumulates its own history daily. A paid historical-estimates
provider (Koyfin tier / one-time dataset) can later implement `ForwardEstimateSource` and load
into the **same** immutable tables (distinguished by `source`) **without touching the engine**.
Fetch and parse are separated in every adapter so the parse is unit-tested offline and
`scripts/validate_live.py` asserts the live output matches the fixture shape (`shape.py`).

### Honesty constraints baked into the schema — never silently drop these.

- `coverage_score` (valuation_snapshots): fraction of the 4-quarter sum that is REAL quarterly
  estimates vs derived. On the v1 annual path this is **0.0** (all derived) — surfaced, not hidden.
- `is_thin_history` + `history_days`: a corridor on a handful of days is returned WITH a loud
  `THIN HISTORY` annotation; `engine/corridor.py` returns NO bands below 2 days. Never present a
  thin chart as deep history.
- `construction_method` (string): records EXACTLY how the forward sum was built
  (e.g. "2 real quarterly + 2 derived from FY2027 annual"). Never anonymous.
- Cross-check disagreement flags: `quarterly_xcheck_flag` / `quarterly_xcheck_divergence_pct`,
  `price_disagreement_flag`, `window_divergence_flag`. Disagreements are STORED and flagged, never
  averaged away. Quarantined values go to `ingestion_log` with a reason code; nothing is dropped
  silently. "No silent failures on data gaps" is a rule, not a nicety.

---

## 3. Locked decisions (hard-won — DO NOT relitigate)

Each of these cost real debugging against live data. They are correct for v1. Changing them needs
a strong, specific reason.

1. **Data sources / plan reality.**
   - **FMP Starter = ANNUAL estimates only.** `period=quarter` is gated to **Premium** (confirmed
     via `fmp_probe.py`) and `limit` is capped at **10** (higher → HTTP 402). The adapter clamps
     `page_limit<=10` and `get_forward_estimates` fetches annual only (`fetch_quarterly=False`).
     Flip `fetch_quarterly: true` in config on a Premium plan — no engine change.
   - **yfinance = prices + a quarterly cross-check flag ONLY.** Its next-quarter consensus is
     compared to our derived next quarter (a disagreement flag); it is **never blended into the
     forward sum.** Guarded by `test_pipeline.py::test_yfinance_is_flag_only_never_blended_into_the_sum`.
   - **EDGAR = reported actuals**, CIKs resolved automatically via SEC `company_tickers.json`
     (`cik_resolver.py`, cached at `data/cik_map.json`). The watchlist's `cik:` fields are null on
     purpose — the resolver fills them.

2. **Derivation = FLAT.** A forward quarter with no quarterly estimate is
   `(annual_FY − Σ reported_actuals_in_FY − Σ real_quarterly_ests_in_FY) / count(quarters in that FY with neither)`.
   The current FY subtracts EDGAR actuals and divides the residual across its remaining UNKNOWN
   quarters (one quarter reported → ÷3, not ÷4); quarters beyond the current FY use the next FY's
   annual. **NOT a naive annual/4.**
   - **(b) seasonality REJECTED** — NVDA's AI ramp makes historical quarterly shares meaningless
     (live recent-weighted Q4 share was **−26%**; (b) produced a **negative** Q3 EPS). Dead.
   - **(a) yfinance-blend REJECTED** — it corrupts the independence of the yfinance cross-check
     (you'd be checking a quarter you sourced from yfinance), and within a fiscal year the FY total
     is anchored to the annual, so it doesn't even move the True P/E. See `analysis/derivation_compare.py`
     and `scripts/compare_derivation.py` (kept as the evidence).

3. **Fiscal-year alignment = date-derived, gate-scoped.**
   - Labels come from `label_period(period_end, FiscalCalendar)` — the period END date — **NOT**
     EDGAR's `fy`/`fp` fields, which are the FILING's fiscal focus and **drift +1 year on
     comparative periods** (a prior quarter repeated in a later 10-Q). `edgar_source.py` keeps the
     raw `fy`/`fp` only in `source_fiscal_period` for the cross-check.
   - `check_label_alignment` (in `job.py`) compares EDGAR's raw label to the date-derived label,
     **anchored to the earliest-filed (original) entry per period**, and is **scoped to a trailing
     3 fiscal years** (`alignment_gate_trailing_years`). Older quarters are EXEMPT and logged —
     NVDA's pre-2023 52/53-week boundaries can't be labeled by one fixed calendar rule, and the
     forward sum never uses them. This gate is a **CORRECTNESS guard**: `aligned:True` over the
     gate window is REQUIRED before trusting a True P/E. (The display and the verdict must use the
     SAME range — a mismatch there once created a false contradiction.)

4. **Certified NVDA True P/E ≈ 21.47** (flat derivation, gate-aligned, actuals subtracted via the
   CIK path). It's the canary: **if a change moves NVDA's number off ~21.47 without a clear
   reason, something broke.** After the first clean live run (2026-06-18) it read 21.19 — this
   is DATA FRESHNESS (new price + updated estimates), not a calc change. The canary is a
   DIRECTION check (no regression), not a fixed decimal. Accept small daily drift as normal;
   investigate if it moves > ~1-2 points without a known reason.
   (Note: WITHOUT the CIK fix the job stored ~25 = annual/4 — biased; that's why the CIK
   resolver + `engine_version` bump exist, see below.)

5. **FK self-seeding.** `run_daily` UPSERTS the parent `securities` row before any child snapshot
   insert. Do **not** assume `init_db` seeded securities (it doesn't — the seeding lives in
   `scripts/init_db.py`, and cron may hit a fresh DB). Guarded by
   `test_daily_job.py::test_run_daily_self_seeds_securities_on_fresh_db`.

6. **engine_version gates biased history.** `config.yaml` `engine_version: "0.2.0"` marks the
   CIK-fixed (certified) path. `corridor.py`/`research.py` read ONLY rows at the current
   `engine_version`, so pre-fix `0.1.0` (annual/4) rows can never blend into the corridor.
   `scripts/purge_valuations.py --engine 0.1.0 --yes` physically removes them (raw snapshots
   untouched). After this handoff the user is doing a clean re-accumulation from the CIK fix forward.

---

## 4. State: done / not-done

### Done
- **Stage 0** — scaffold, point-in-time schema, immutability triggers, config, `.env.example`.
- **Stage 1** — FMP/yfinance/EDGAR adapters (fetch/parse separated, shape-contract tested), the
  forward window (report-date driven), FLAT derivation with provenance, consistency invariants
  (split-safe True P/E, currency guard, time alignment), write-time sanity gates + quarantine,
  multi-source cross-checks, realized-actuals/beat-miss loop, the **date-derived fiscal-label
  alignment gate (scoped to 3 FY)**, and the **hardened idempotent/resilient/self-contained daily
  job with `--dry-run` + run-summary log + Pi cron**.
- **Stage 2** — `engine/corridor.py` (True P/E history → percentile bands → price-space corridor →
  position), `engine/signals.py` (bottom-decile + rising-earnings = buy; top band = trim;
  cheap-but-not-rising = watch; thin history downgrades hard_* and annotates), `engine/peg.py`
  (forward PEG with EXPLICIT, logged growth basis `ntm_vs_ltm` default / `fwd_cagr`; suppressed
  near zero/negative growth; `<1` cheap `>2` rich are CONTEXT only; LTM reconstructed from EDGAR
  with Q4 = FY − Q1 − Q2 − Q3). PEG is a SEPARATE view from the corridor signal — shown side by
  side, never blended. `scripts/research.py` renders both.
- **CIK resolver** — `cik_resolver.py`; wired into the daily job (actuals fetched watchlist-wide)
  and `research.py` (PEG LTM). `engine_version` bump + purge script for the biased pre-fix rows.

### Not done (next work)
- **Stage 4 — visualization / dashboard.** Build the five plotly charts and the Streamlit app:
  (1) corridor chart (price line + percentile bands in price space), (2) True P/E history
  (multiple over time + median/bands + current marker), (3) PEG chart (over time, 1.0/2.0 ref
  lines, greyed when suppressed), (4) per-company dashboard (the three + RSI/MA + signal badges +
  key stats), (5) watchlist overview (sortable table/heatmap). **Plus a clearly-labeled forward
  PROJECTION line** based on the forward estimates — it is NOT a price prediction; label it
  honestly. Consistent color language (green = buy zone, red = trim zone). Annotate thin-history
  charts. Charts must be self-explanatory (titles, axis labels, current-value annotations).
  **As part of Stage 4: wire the daily job to STORE** signal + forward_peg + growth_rate +
  growth_basis + peg_suppressed into the `valuation_snapshots` columns (they exist, currently
  null) so the dashboard reads stored values.
- **Stage 5 — walk-forward backtest harness** on point-in-time data. Net of assumed costs.
  **Must be able to report that a factor has NO out-of-sample edge** and say so plainly — it is
  there to DISAPPOINT when warranted, not to flatter. Early backtests will be limited by sparse
  forward-estimate history.
- **Factor 3 overlay (Stage 3, partially deferred earlier)** — winning-streak base-rate study,
  RSI, distance from 50/200-day MA, earnings-calendar pre/post flags. `overlay_snapshots` table
  exists. (FinBERT sentiment is a future hook only — do not build.)

### Data reality (important for expectations)
- Forward-estimate history **accumulates daily from the Pi** (started ~2026-06-17). The corridor
  bands need **~60 real days** to be statistically meaningful; until then they return WITH the
  `THIN HISTORY` annotation. This is by design — never fake or backfill forward-estimate history.
- Deep historical forward estimates are a **paid-dataset upgrade** loaded via the swappable
  interface — the corridor math doesn't care whether snapshots came from daily accumulation or a
  purchased history.

---

## 5. Gotchas the next session MUST know

- **The Claude Code sandbox has NO live data access.** Outbound network blocks FMP, EDGAR
  (`data.sec.gov`), and Yahoo with HTTP 403 (only package registries like PyPI are allowed), and
  there is **no API key**. You **cannot** run `daily_refresh.py`, `validate_live.py`,
  `research.py --ticker`, `compare_derivation.py`, or the live alignment test here. **All live
  runs happen on the user's Pi/laptop; the user pastes the output back.** Build + unit-test
  offline (fixtures, `--demo` modes, fake injected sources), and hand the live step to the user.
- **`.env` is git-ignored**, loaded via `python-dotenv` `load_dotenv` inside `config.get_settings()`.
  Scripts read `FMP_API_KEY` / `SEC_EDGAR_USER_AGENT` from it — do not require shell env vars.
- **The Pi user is `star`** → paths are `/home/star/Corridor` (NOT `/home/pi`). Use that in any
  cron/docs you write.
- **Test discipline.** Every calculation has a hand-checkable pytest (e.g. corridor percentiles
  18/30/42; PEG `24/(0.2*100)=1.2`; LTM Q4 inference). **Fixtures must include adversarial cases.**
  The hard lesson of this project: **fixture-only tests passed while live data failed** — the
  fiscal-label `+1yr` drift and the FK-on-fresh-DB bug both hid behind happy-path fixtures.
  For anything data-touching, prefer a **fresh-DB / live-shaped** test (see
  `test_daily_job.py`, `test_fiscal_alignment.py`, the comparative-drift EDGAR fixture, and the
  gated `test_alignment_live.py`).
- **Sandbox model identity / commits.** Never put model identifiers in commits/PRs. Develop on
  `claude/great-volta-w03vwf`. Commit + push when a unit of work is complete; the user reviews on
  the branch. Do NOT open a PR unless asked.
- **Stage discipline.** This project advanced one stage at a time with an explicit review pause at
  the end of each. Show the user the output (for live things: a synthetic `--demo` + the script to
  run), then STOP and wait for go-ahead. Do not one-shot multiple stages.
- **FY-label alignment gate: 52/53-week boundary shifts and old-EDGAR-data false positives (fixed 2026-06-18).**
  Two classes of false in-window drift:
  (a) *52/53-week fiscal year boundary shift*: AMD/AVGO's original 10-Q and comparative entry for the
  same quarter can have period_end dates that differ by 1-2 days. The old gate keyed by raw
  `period_end_date` — it saw two distinct entries and picked the comparative alone → drift fired.
  Fix: gate now keys by DATE-DERIVED label (same as `actuals_from_fundamentals`) so both entries
  collapse to one label and the earliest-filed (original) wins.
  (c) *Comparative-only entry, no original in companyfacts* (AMD live case, fixed 2026-06-19):
  some companies' EDGAR companyfacts entries for certain quarters have no `start` date field.
  `_classify(None, end)` returns None → the original entry is excluded from `fundamentals`
  entirely. The only surviving entry is the COMPARATIVE from the next year's 10-Q (which has
  a proper start/end pair but carries fy=year+1 drift). SEC rules require large accelerated
  filers to file a 10-Q within 40 days of period-end; any entry filed >150 days after period_end
  can ONLY be a comparative. The gate now marks these as benign (agree=True). A genuine
  fy_end_month misconfiguration shows drift on the ORIGINAL filing (filed <40 days) and is
  still surfaced.
  (b) *Old-EDGAR-data-only ticker* (e.g. GOOGL if no recent quarterly EPS tag in companyfacts):
  the old gate anchored its window to `max(EDGAR_entries)`, which could be FY2015, making FY2014
  entries appear "in-window". Fix: gate now anchors window to `fiscal_year_of(as_of, cal)` when
  `as_of` is provided (always the case from `run_daily`). Old entries go to `older` (exempt).
  **The underlying True P/Es were NOT affected** — `actuals_from_fundamentals` uses date-derived
  dedup and was correct throughout. The gate warnings were false alarms only.
  Regression tests: `test_fiscal_alignment.py::test_52_53_week_boundary_shift_...` and
  `test_old_edgar_data_only_does_not_produce_false_in_window_drift`.
- **TSLA triple-digit True P/E is REAL.** Tesla legitimately trades at high forward multiples.
  Do not "fix" it. The pipeline is computing correctly.
- **UTC/Eastern timezone gotcha (price fetch).** The Pi cron fires at 22:00 local; if the Pi is in
  a timezone where 22:00 local is past midnight UTC (e.g. ET = UTC-4, so 22:00 ET = 02:00 UTC),
  then `datetime.now(UTC).date()` returns the NEXT calendar day. yfinance is then given
  `start=June18` while Yahoo's backend end date is still `June17` → "start date cannot be after
  end date" for every ticker. **Fix (applied 2026-06-18):** `run_daily` computes `as_of` via
  `datetime.now(_MARKET_TZ).date()` where `_MARKET_TZ = ZoneInfo("America/New_York")`. Market
  data is always keyed by US/Eastern dates; pin there throughout. Regression test:
  `test_daily_job.py::test_as_of_date_uses_eastern_not_utc`.

---

## 6. Quick orientation for a new session

1. `make test` (offline; 92 pass / 1 live-skipped). Then read `README.md` and this file.
2. Trace one full flow: `scripts/daily_refresh.py` → `ingest/job.py::run_daily` →
   `assemble_valuation` (window → `forward_sum` → consistency/gates → cross-checks) → stored
   `valuation_snapshots`. Then `engine/corridor.py` / `engine/signals.py` / `engine/peg.py` read
   the accumulated snapshots; `scripts/research.py` renders them.
3. The certified canary: NVDA True P/E ≈ **21.47** (flat, gate-aligned, actuals subtracted).
   Daily drift of a fraction is data freshness — only investigate if it moves > ~1-2 points unexplained.
   TSLA triple-digit True P/E is REAL (Tesla legitimately trades at high multiples); not a bug.
4. When you need live numbers, write the code + a `--demo`/fixture test, and ask the user to run
   the live script on the Pi and paste the result. Never assume you can fetch.
