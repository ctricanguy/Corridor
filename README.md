# Corridor

A **personal** equity-research engine for a focused universe of large-cap tech/AI
stocks. It screens and monitors names with a three-factor framework —
**Earnings × Valuation (forward-P/E "corridor" + PEG) × Sentiment/base-rates** —
and produces buy/trim signals plus a standardized, graph-rich research memo per
name.

> Decision-support for my own money. **Not investment advice.** Single user, local,
> no auth, no billing. This is a tool I read the code of and trust.

The graphs are half the point: a forward-P/E **corridor** chart, a **True P/E**
chart, a forward **PEG** chart, a per-company dashboard, and a watchlist overview.

---

## Status

| Stage | Scope | State |
|------:|-------|-------|
| **0** | Scaffold: repo layout, deps, config, point-in-time schema, `.env.example` | ✅ **this commit** |
| 1 | Data pipeline: FMP/yfinance/EDGAR adapters, window logic, consistency invariants, sanity gates, actuals loop | ✅ **adapters unvalidated** (see below) |
| 2 | Valuation engine: True P/E series, bands, signals, forward PEG | ⏳ next |
| 3 | Overlay: winning-streak study, RSI/MA, earnings flags | ⏳ |
| 4 | Visualization + memos: the five charts, dashboard, memo generator | ⏳ |
| 5 | Validation harness: walk-forward backtest on point-in-time data | ⏳ |

Built in stages with a review pause between each.

> **⚠️ The three data adapters (FMP, yfinance, EDGAR) are UNVALIDATED against live
> endpoints.** They were built against documented API shapes but not run live (the
> build sandbox blocks outbound network and has no API key). Their JSON parsing is
> unit-tested against hand-built fixtures, and the True P/E math is verified by a
> SYNTHETIC worked example — but before trusting real numbers you must run
> `python scripts/validate_live.py` locally (needs `FMP_API_KEY` + network). It
> re-checks the live data against the same shape contract the fixtures satisfy and
> prints a real, hand-verifiable NVDA True P/E.

---

## The single most important design decision: point-in-time integrity

The whole method depends on an **honest history of *forward* estimates**. Every
time we pull "next-4-quarter EPS," we snapshot it with the **as-of date** and
**never overwrite it**. A naive overwrite would destroy the True P/E history and
silently lookahead-bias every backtest.

So the storage layer is immutable from day one:

- `forward_estimate_snapshots` and `fundamentals` are **append-only**. `init_db`
  installs SQLite triggers that **reject `UPDATE`/`DELETE`** on them — a buggy job
  literally cannot overwrite history. Corrections are new dated rows.
- Every forward estimate row carries its own `as_of_date` and a `source` tag.

### Forward-estimate history is sparse at first — by design

Free sources (yfinance) give you **today's** forward estimates, not what
estimates *were* six months ago. That past series is exactly what the corridor /
True P/E depend on. Consequences this app respects:

- The corridor/True P/E/PEG history will be **thin at launch** and **grows** as
  the daily job snapshots estimates going forward. The app accumulates its own
  point-in-time history from day one.
- We **never backfill** forward estimates by pretending today's number applied in
  the past. (Trailing/as-reported financials from EDGAR *are* backfilled — those
  are honest facts with filing dates.)
- Deep historical forward estimates are a **later paid upgrade** (e.g. a Koyfin
  tier or a one-time dataset). The `datasources` interface lets you swap one in
  and load it into the **same** tables — the engine never knows the difference.
- Every chart will **show how much real history backs it** (e.g. "corridor based
  on 47 days of snapshots — thin, interpret with caution").

---

## The three factors (plain English)

**Factor 1 — Earnings (the anchor).** Consensus next-4-quarter EPS/revenue per
ticker. v1 is a pure consensus passthrough behind an `EarningsModel` interface, so
you can later plug in your own per-segment estimates without touching the
valuation math.

**Factor 2 — Valuation: the corridor / "True P/E".** For each ticker, a daily
series of `True_PE_t = Price_t / sum(next 4 unreported-quarter EPS_t)`. Take the
historical distribution of that forward multiple (median + percentile bands,
default 20th/80th); translate the bands back into price space and you get a
**fair-value corridor** the price rides inside or outside. *Hard buy* when price
sits in the bottom decile of its own history **and** earnings are rising; *trim*
near the top band.

**Factor 2b — Growth-adjusted valuation: forward PEG.** The corridor alone
penalizes high-growth names and flatters decliners, so alongside it we compute
`Forward_PEG = Forward_PE / forward_EPS_growth_rate`, using the **same**
next-4-quarter EPS and an **explicit, logged** growth basis (default: NTM vs LTM
EPS; optionally a 2–3yr forward CAGR). PEG and the corridor's premium term are two
views on the same problem — computed **separately and shown side by side**; their
disagreement is signal, never blended into one opaque score. PEG is **suppressed**
when growth is near zero/negative (the ratio blows up or flips sign), falling back
to the corridor.

**Factor 3 — Sentiment / base-rate overlay.** A reproducible winning-streak study
(distribution of consecutive weekly-gain streak lengths and forward returns
conditioned on streak length), plus RSI, distance from 50/200-day MAs, and
pre/post-earnings flags. A clean hook is left for FinBERT sentiment later (not
built yet).

---

## The charts (Stage 4)

1. **Corridor** — price line with forward-P/E percentile bands drawn in price space.
2. **True P/E history** — the forward multiple over time with median/band lines and the current value marked.
3. **PEG** — forward PEG over time with 1.0 and 2.0 reference lines, current value marked, greyed when the growth guardrail suppresses it.
4. **Per-company dashboard** — the three charts + RSI/MA + signal badges + key stats.
5. **Watchlist overview** — a sortable table/heatmap across the universe.

Consistent color language app-wide (**green = buy zone**, **red = trim zone**),
self-explanatory titles/labels/annotations, and thin-history charts marked as such.

---

## The data pipeline (Stage 1)

The whole system's validity rests on two numbers per `(ticker, date)`: the
forward-EPS sum and the price paired with it. The pipeline protects that pairing
with invariants, each backed by an adversarial test:

- **Sourcing & provenance.** Forward estimates from **FMP's current `/stable`
  API** — v1 uses **`period=annual`** (the multi-year forward curve), because on
  the Starter plan `period=quarter` is Premium-gated and the `limit` is capped at
  10 (the deprecated `/api/v3` path 403s). Prices from **yfinance** (price
  cross-checked against FMP; its next-quarter EPS is also used as a **quarterly
  cross-check** the annual curve can't provide). Realized actuals from **EDGAR
  XBRL** (diluted EPS, continuing ops, with `filed_date`). The api key is read from
  `.env` and **redacted** from any logged URL or request error. Every value carries
  `source`, `observation_timestamp` (UTC), a `basis`, and — for the forward sum — a
  `construction_method` string recording *exactly* how it was built.
- **Deriving quarters from the annual curve (the v1 path).** With no per-quarter
  estimates, every forward quarter is split out of its fiscal-year annual
  *correctly* — NOT a naive annual/4:
  `derived = (annual_FY − Σ reported_actuals_in_FY − Σ real_quarterly_ests_in_FY) /
  count(quarters in that FY with neither)`. So the **current FY** subtracts its
  already-reported actuals (EDGAR) and divides the residual across its remaining
  unreported quarters (NVDA mid-FY with one quarter reported → ÷3, not ÷4), while
  quarters **beyond** the current FY use the next FY's annual. `coverage_score` is
  `0.0` on this path (every quarter derived) — surfaced honestly, never dressed up
  as per-quarter precision. (Set `fetch_quarterly: true` on a Premium plan to add
  real per-quarter estimates with no engine change.)
- **Derivation method — v1 decision: FLAT even-spread.** Evaluated flat vs (a)
  yfinance-blended near quarters vs (b) seasonality weighting (`scripts/compare_derivation.py`,
  analysis-only). Adopted **flat**: within a fiscal year the unreported quarters must
  sum to `annual − reported actuals`, so flat and (a) give the *same* forward
  sum/True P/E (the number is anchored by real annual consensus — (a) only zeroes the
  per-quarter cross-check by construction), and (b) moves it only via a far next-FY
  quarter using historical shares distorted by NVDA's ramp. **yfinance stays an
  independent cross-check flag, never blended into the sum** (guarded by a test).
- **Fiscal-year alignment is by DATE, not by label string.** FMP estimates and
  EDGAR actuals are both keyed to a fiscal year by `period_end` + the company's
  `FiscalCalendar` (a single date-derived rule), so an actual is subtracted from the
  *same* fiscal year its date belongs to even if a provider's FY-naming convention
  is off by one. `check_label_alignment` cross-checks EDGAR's own `fy`/`fp` against
  the date-derived label and logs any drift (it also catches a wrong `fy_end_month`
  in config). NVDA's labels agree — verified by test and shown in `validate_live`.
- **The window.** "Next 4 unreported quarters" is keyed off **confirmed report
  dates** and each company's **own fiscal calendar** (NVDA ends late January, AAPL
  September, AVGO November…), never calendar quarters. It rolls the moment a quarter
  reports. Cross-checked against a provider-native NTM; divergence is flagged.
- **Consistency invariants.** True P/E is computed on the **raw contemporaneous
  basis** (raw price / raw EPS), so a split rescales numerator and denominator
  together — no phantom discontinuity. EPS basis is tagged everywhere (estimates are
  non-GAAP `adjusted_diluted`; EDGAR actuals are `gaap_diluted_continuing_ops`) and
  never silently mixed. **Currency is a hard gate**: if report currency ≠ price
  currency (any ADR), Corridor refuses to compute, quarantines to `ingestion_log`,
  and shows an explicit *unsupported* state — never a naive mismatched P/E.
- **Quality & gates.** Each snapshot gets a **coverage score** (real-vs-derived
  fraction of the 4Q sum). Impossible values — non-positive price, an unexplained
  EPS-sum sign flip, a >Nx True P/E jump with no split — are **quarantined** to
  `ingestion_log` with a reason code, never written to the live tables, never
  dropped silently.
- **Actuals feedback loop.** When a quarter reports, the EDGAR actual is compared to
  the estimate that was **live the day before**, accumulating per-source accuracy so
  we can later learn whether a source's consensus is any good (with the GAAP-vs-
  non-GAAP basis mismatch flagged honestly).

### Foreign / ADR names — excluded from v1 on purpose

None of the 10 watchlist names are ADRs. The currency-match guard is a deliberate
guardrail, not a bug: supporting an ADR (e.g. TSM — TWD reporter, USD ADR, 5:1
share ratio) requires FX + share-ratio handling, to be built and tested against a
real ADR later. See the `TODO(adr)` in `src/corridor/ingest/job.py` and the
`unsupported:` block in `config.yaml`.

### Live validation (do this before trusting real numbers)

```bash
# 1. Hand-verify the MATH (no network needed) — clearly synthetic:
python scripts/nvda_worked_example.py

# 2. Validate the adapters against LIVE data (needs key + network):
export FMP_API_KEY=...                       # your FMP key
export SEC_EDGAR_USER_AGENT="You <you@email>"
python scripts/validate_live.py              # prints a real, hand-verifiable NVDA True P/E

# 3. Once validated, run the daily snapshot job:
python scripts/daily_refresh.py
```

---

## Repository layout

```
Corridor/
├── config.yaml              # watchlist + methodology params (no secrets)
├── .env.example             # secrets/paths (SEC user-agent, DB url, snapshot dir)
├── pyproject.toml           # deps (uv/venv), ruff/mypy/pytest config
├── Makefile                 # install / init-db / schema / test / app
├── src/corridor/
│   ├── config.py            # .env + config.yaml loaders (typed)
│   ├── constants.py         # bases / methods / statuses / quarantine reason codes
│   ├── db/
│   │   ├── models.py        # POINT-IN-TIME schema (source of truth)
│   │   └── database.py      # engine, sessions, immutability triggers, init_db
│   ├── datasources/         # swappable adapters — UNVALIDATED until validate_live.py
│   │   ├── base.py          # interfaces + record dataclasses
│   │   ├── fmp_source.py    # FMP forward estimates (PRIMARY) + price cross-check
│   │   ├── yfinance_source.py  # prices (raw + adjusted, split ratios)
│   │   ├── edgar_source.py  # realized diluted EPS (continuing ops, filed dates)
│   │   └── shape.py         # record shape contract (fixtures <-> live)
│   ├── ingest/              # Stage 1 PIPELINE (pure, testable)
│   │   ├── fiscal.py        # per-company fiscal calendars + quarter enumeration
│   │   ├── window.py        # next-4-unreported-quarter window
│   │   ├── forward_sum.py   # 4Q sum + construction_method + coverage
│   │   ├── consistency.py   # currency guard, time alignment, split-safe True P/E
│   │   ├── gates.py         # write-time sanity gates (quarantine)
│   │   ├── reconcile.py     # multi-source price + NTM cross-checks
│   │   ├── actuals.py       # beat/miss vs pre-report estimate + source accuracy
│   │   └── job.py           # assemble_valuation (pure) + run_daily (injected sources)
│   ├── engine/
│   │   └── earnings.py      # Factor 1 EarningsModel seam (corridor/peg: Stage 2)
│   ├── viz/ memo/ backtest/ # Stage 4 / 5
├── app/dashboard.py         # Streamlit entry point (Stage 4)
├── scripts/
│   ├── init_db.py           # create schema + seed watchlist
│   ├── dump_schema.py       # print generated DDL for review
│   ├── nvda_worked_example.py  # SYNTHETIC True P/E walk-through (validates math)
│   ├── validate_live.py     # LIVE adapter validation (key + network) — real NVDA number
│   └── daily_refresh.py     # Stage 1 daily snapshot job
└── tests/                   # pytest: window, forward sum, consistency, gates,
    └── fixtures/            #   reconcile, actuals, adapter parse, pipeline (45 tests)
```

### Why Streamlit (not FastAPI + React)

Single user, local, graph-heavy. Streamlit gives interactive plotly dashboards
with almost no plumbing — no separate API, build step, or frontend state. That
matches "a personal app I run daily and extend myself." If this ever needed
multi-user auth or a public API, FastAPI + React would earn its extra moving
parts; for one user it would be pure overhead.

---

## Setup

Requires Python 3.11+.

```bash
# 1. Install (uv preferred; falls back to venv + pip)
make install
#   or: uv venv && uv pip install -e ".[dev]"

# 2. Configure secrets/paths
cp .env.example .env
#   then edit .env — set SEC_EDGAR_USER_AGENT to include your real email
#   (EDGAR's fair-access policy requires it)

# 3. Create the database (schema + immutability guards + seeded watchlist)
make init-db

# 4. Review the generated schema any time
make schema

# 5. Run the tests
make test
```

The watchlist seeds with **NVDA, MSFT, AAPL, GOOGL, AMZN, META, TSLA, AMD, AVGO,
MU** — start narrow, go deep.

---

## Honesty constraints (built in, not bolted on)

- **Decision-support, not advice.** One-line disclaimer in every memo.
- **Never fabricate estimates or fill gaps with guesses** — missing is marked
  missing and logged to `ingestion_log`; no silent failures.
- **Never present a thin-history chart as deep history** — real snapshot depth is
  a first-class column (`history_days`) and is annotated on every chart.
- **PEG is only as honest as its growth input** — the growth basis is explicit,
  configurable, logged on every calc, and suppressed near zero/negative growth.
- **The validation harness is allowed to disappoint.** A factor with no
  out-of-sample edge is labeled context-only, never dressed up as alpha.

---

## License

MIT — personal use.
