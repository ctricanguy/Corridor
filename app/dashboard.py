"""Corridor dashboard — Stage 4 Streamlit entry point.

Run:
    .venv/bin/streamlit run app/dashboard.py

Two views (sidebar nav):
  • Per-company      — corridor / True P/E / PEG charts + signal badge + key stats
                       + forward projection line on the corridor chart
  • Watchlist        — PE-percentile heatmap bar chart + sortable detail table
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd
import streamlit as st

from corridor.viz.charts import (
    corridor_chart, peg_chart, true_pe_chart, watchlist_heatmap,
)
from corridor.viz.data import (
    load_annual_estimates, load_universe,
    load_valuation_history, load_watchlist_latest,
)

# ── page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Corridor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── shared helpers ─────────────────────────────────────────────────────────────

_SIGNAL_COLORS = {
    "hard_buy":            ("#00C864", "⬆⬆ HARD BUY"),
    "buy":                 ("#00A050", "⬆ BUY"),
    "watch_cheap":         ("#60C860", "👀 WATCH CHEAP"),
    "hold":                ("#888888", "— HOLD"),
    "trim":                ("#E07020", "⬇ TRIM"),
    "hard_trim":           ("#CC2020", "⬇⬇ HARD TRIM"),
    "insufficient_history":("#555555", "⏳ THIN HISTORY"),
}


def _signal_badge(signal: str | None) -> None:
    if not signal:
        st.markdown(
            '<span style="background:#333;color:#999;padding:4px 12px;'
            'border-radius:4px;font-weight:bold;font-size:1.1em">— no signal —</span>',
            unsafe_allow_html=True,
        )
        return
    color, label = _SIGNAL_COLORS.get(signal, ("#555555", signal.upper()))
    st.markdown(
        f'<span style="background:{color};color:#fff;padding:4px 14px;'
        f'border-radius:4px;font-weight:bold;font-size:1.15em">{label}</span>',
        unsafe_allow_html=True,
    )


def _fmt_flag(val: bool | None, label: str) -> str:
    return f"⚠ {label}" if val else f"✓ {label}"


def _pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None and pd.notna(v) else "—"

def _f2(v: float | None) -> str:
    return f"{v:.2f}" if v is not None and pd.notna(v) else "—"

def _money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None and pd.notna(v) else "—"


# ── per-company view ───────────────────────────────────────────────────────────

def company_view(ticker: str) -> None:
    df = load_valuation_history(ticker)

    if df.empty:
        st.warning(f"No certified valuation snapshots for **{ticker}**. "
                   "Run `scripts/daily_refresh.py` to populate.")
        return

    latest       = df.iloc[-1]
    as_of        = df.index[-1].date()
    history_days = int(latest.get("history_days") or 0)
    is_thin      = bool(latest.get("is_thin_history", True))

    # ── header ────────────────────────────────────────────────────────────────
    h1, h2, h3 = st.columns([2, 1, 3])
    with h1:
        st.subheader(ticker)
        st.caption(f"As of {as_of}  ·  {history_days}d snapshot history")
    with h2:
        _signal_badge(latest.get("signal"))
    with h3:
        if is_thin:
            st.warning(
                f"⚠ **Thin history** — corridor based on {history_days}d of real snapshots "
                "(target ≥ 60). Bands are unstable; accuracy improves each trading day."
            )
        elif latest.get("notes"):
            st.info(latest["notes"])

    st.divider()

    # ── key stats ─────────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Price", _money(latest.get("price")))
    c2.metric("True P/E", _f2(latest.get("true_pe")))
    c3.metric(
        "P/E percentile",
        f"{latest['pe_percentile']:.0f}th" if pd.notna(latest.get("pe_percentile")) else "—",
    )
    peg_val = latest.get("forward_peg")
    peg_supp = latest.get("peg_suppressed")
    c4.metric(
        "Forward PEG",
        ("suppr." if peg_supp else _f2(peg_val)),
        help="Suppressed when growth rate < 2% (guardrail). Defer to corridor signal.",
    )
    c5.metric("Coverage", _pct(latest.get("coverage_score")),
              help="Fraction of 4Q sum from real quarterly estimates (1.0 = all real).")
    c6.metric("History", f"{history_days}d{'  ⚠' if is_thin else ''}")

    # ── construction detail expander ─────────────────────────────────────────
    with st.expander("Construction detail + data flags", expanded=False):
        st.caption(f"**Method:** {latest.get('construction_method') or '—'}")
        f1, f2, f3 = st.columns(3)
        f1.write(_fmt_flag(latest.get("window_divergence_flag"), "Window divergence"))
        f2.write(_fmt_flag(latest.get("price_disagreement_flag"), "Price disagreement"))
        f3.write(_fmt_flag(latest.get("quarterly_xcheck_flag"), "Quarterly x-check"))

    st.divider()

    # ── charts ────────────────────────────────────────────────────────────────
    # Load annual estimates for the projection line (empty → projection skipped)
    annual_est = load_annual_estimates(ticker)

    st.plotly_chart(
        corridor_chart(df, ticker, annual_estimates=annual_est),
        use_container_width=True,
    )
    st.plotly_chart(true_pe_chart(df, ticker), use_container_width=True)
    st.plotly_chart(peg_chart(df, ticker),     use_container_width=True)

    st.caption(
        "_Decision support for personal use only — not investment advice. "
        "Projection line = historical band multiples × forward annual EPS estimates. "
        "Thin-history corridors are explicitly flagged._"
    )


# ── watchlist overview ─────────────────────────────────────────────────────────

def watchlist_view() -> None:
    st.subheader("Watchlist overview")

    df = load_watchlist_latest()
    if df.empty:
        st.warning("No data yet — run `scripts/daily_refresh.py`.")
        return

    # ── heatmap bar chart (the at-a-glance scan) ──────────────────────────────
    st.plotly_chart(watchlist_heatmap(df), use_container_width=True)

    st.caption(
        "Bar = True P/E percentile vs own snapshot history (all thin today — ≥ 60d needed "
        "for stable corridors). ⚠ = thin history. Green < 20th = buy zone, "
        "Red > 80th = trim zone."
    )
    st.divider()

    # ── detail table ──────────────────────────────────────────────────────────
    st.caption("Detail table — click column headers to sort.")

    disp = df.copy()
    disp["signal"]       = disp["signal"].fillna("—")
    disp["True P/E"]     = disp["true_pe"].map(lambda v: f"{v:.2f}" if pd.notna(v) else "—")
    disp["P/E %ile"]     = disp["pe_percentile"].map(
                               lambda v: f"{v:.0f}th" if pd.notna(v) else "—")
    disp["Price"]        = disp["price"].map(lambda v: f"${v:,.2f}" if pd.notna(v) else "—")
    disp["Corr. low"]    = disp["corridor_low"].map(
                               lambda v: f"${v:,.0f}" if pd.notna(v) else "—")
    disp["Corr. high"]   = disp["corridor_high"].map(
                               lambda v: f"${v:,.0f}" if pd.notna(v) else "—")
    disp["PEG"]          = disp.apply(
        lambda r: "suppr." if r.get("peg_suppressed") else (
            f"{r['forward_peg']:.2f}" if pd.notna(r.get("forward_peg")) else "—"),
        axis=1,
    )
    disp["Coverage"]     = disp["coverage_score"].map(
                               lambda v: f"{v:.0%}" if pd.notna(v) else "—")
    disp["History"]      = disp["history_days"].map(
        lambda v: (f"{int(v)}d ⚠" if pd.notna(v) and v < 60 else
                   f"{int(v)}d"   if pd.notna(v) else "—"))
    disp["Flags"]        = disp.apply(
        lambda r: " ".join(f for f, v in [
            ("W", r.get("window_divergence_flag")),
            ("P", r.get("price_disagreement_flag")),
        ] if v),
        axis=1,
    )

    show_cols = [
        "ticker", "as_of_date", "signal", "Price", "True P/E",
        "P/E %ile", "Corr. low", "Corr. high", "PEG",
        "Coverage", "History", "Flags",
    ]
    disp = disp[show_cols].rename(columns={"ticker": "Ticker", "as_of_date": "As of"})

    def _color_signal(val: str) -> str:
        return {
            "hard_buy":   "background-color:#004020;color:#00E070",
            "buy":        "background-color:#003018;color:#00C864",
            "watch_cheap":"background-color:#003018;color:#60C860",
            "trim":       "background-color:#302000;color:#E07020",
            "hard_trim":  "background-color:#300000;color:#CC2020",
        }.get(val, "")

    st.dataframe(
        disp.style.map(_color_signal, subset=["signal"]),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "Flags: W = window divergence, P = price source disagreement. "
        "Corridor low/high = band multiples × current NTM EPS (in price space)."
    )


# ── sidebar + routing ──────────────────────────────────────────────────────────

def main() -> None:
    with st.sidebar:
        st.title("📈 Corridor")
        st.caption("Personal equity research engine")
        st.divider()

        view = st.radio("View", ["Per-company", "Watchlist overview"], index=0)

        if view == "Per-company":
            universe = load_universe()
            if not universe:
                st.warning("No data yet.")
                selected = None
            else:
                selected = st.selectbox("Ticker", universe, index=0)
        else:
            selected = None

        st.divider()
        st.caption("Not investment advice.")

    if view == "Watchlist overview":
        watchlist_view()
    elif selected:
        company_view(selected)
    else:
        st.info("No tickers in database yet — run `scripts/daily_refresh.py`.")


if __name__ == "__main__":
    main()
