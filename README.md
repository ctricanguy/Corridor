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
| 1 | Data pipeline: EDGAR + yfinance ingestion, immutable daily snapshots | ⏳ next |
| 2 | Valuation engine: True P/E series, bands, signals, forward PEG | ⏳ |
| 3 | Overlay: winning-streak study, RSI/MA, earnings flags | ⏳ |
| 4 | Visualization + memos: the five charts, dashboard, memo generator | ⏳ |
| 5 | Validation harness: walk-forward backtest on point-in-time data | ⏳ |

Built in stages with a review pause between each.

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

## Repository layout

```
Corridor/
├── config.yaml              # watchlist + methodology params (no secrets)
├── .env.example             # secrets/paths (SEC user-agent, DB url, snapshot dir)
├── pyproject.toml           # deps (uv/venv), ruff/mypy/pytest config
├── Makefile                 # install / init-db / schema / test / app
├── src/corridor/
│   ├── config.py            # .env + config.yaml loaders (typed)
│   ├── db/
│   │   ├── models.py        # POINT-IN-TIME schema (source of truth)
│   │   └── database.py      # engine, sessions, immutability triggers, init_db
│   ├── datasources/
│   │   └── base.py          # swappable source interfaces (Price/Estimate/Fundamentals)
│   ├── engine/
│   │   └── earnings.py      # Factor 1 EarningsModel seam (corridor/peg/overlay: later)
│   ├── ingest/              # Stage 1 — daily snapshot job
│   ├── viz/                 # Stage 4 — plotly charts
│   ├── memo/                # Stage 4 — memo generator
│   └── backtest/            # Stage 5 — validation harness
├── app/dashboard.py         # Streamlit entry point (Stage 4)
├── scripts/
│   ├── init_db.py           # create schema + seed watchlist
│   ├── dump_schema.py       # print generated DDL for review
│   └── daily_refresh.py     # Stage 1 daily job (stub)
└── tests/                   # pytest: schema immutability, config
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
