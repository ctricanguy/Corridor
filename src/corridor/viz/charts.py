"""Pure Plotly chart functions — no I/O, no Streamlit.

Each function takes a DataFrame (from viz.data) and returns a go.Figure.
Color language is consistent throughout:
  green  = buy zone / below low band
  red    = trim zone / above high band
  gray   = corridor band fill / suppressed
  amber  = thin-history warning

All charts that display thin history attach a loud annotation so the user
never mistakes a handful of days for a deep distribution.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go


# Minimum x-axis window (days). With a single snapshot Plotly zooms to
# microseconds; this gives a sensible date range that expands naturally as
# history accumulates.
_MIN_XRANGE_DAYS = 30


def _xaxis_range(df: pd.DataFrame, pad_right_days: int = 3) -> dict:
    """Return xaxis kwargs that pin a minimum date window."""
    if df.empty:
        return {}
    latest = df.index[-1]
    earliest = df.index[0]
    span = (latest - earliest).days
    if span < _MIN_XRANGE_DAYS:
        x_start = latest - pd.Timedelta(days=_MIN_XRANGE_DAYS)
    else:
        x_start = earliest - pd.Timedelta(days=2)
    x_end = latest + pd.Timedelta(days=pad_right_days)
    return {"xaxis": dict(range=[x_start, x_end], type="date")}


# ── shared palette ─────────────────────────────────────────────────────────────
_BUY_FILL = "rgba(0, 200, 100, 0.10)"
_TRIM_FILL = "rgba(220, 50, 50, 0.10)"
_BAND_FILL = "rgba(120, 120, 140, 0.15)"
_BAND_LINE = "rgba(120, 120, 140, 0.40)"
_PRICE_COLOR = "#7EB8F7"       # steel blue
_PE_COLOR = "#C9A84C"          # amber-gold
_PEG_COLOR = "#A37EC9"         # purple
_MEDIAN_COLOR = "rgba(200,200,200,0.7)"
_THIN_BG = "rgba(255,180,0,0.08)"
_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor="#0E1117",
    plot_bgcolor="#0E1117",
    font=dict(family="monospace", size=12, color="#CCCCCC"),
    margin=dict(l=60, r=20, t=50, b=40),
    hovermode="x unified",
)


def _thin_annotation(history_days: int) -> dict:
    return dict(
        text=f"⚠ THIN HISTORY — {history_days}d of snapshots. Bands are "
             "unstable; interpret with caution.",
        xref="paper", yref="paper",
        x=0.0, y=1.0, xanchor="left", yanchor="top",
        showarrow=False,
        font=dict(size=11, color="#FFB300"),
        bgcolor=_THIN_BG,
        bordercolor="#FFB300",
        borderwidth=1,
    )


# ── 1. Corridor chart ──────────────────────────────────────────────────────────

def corridor_chart(df: pd.DataFrame, ticker: str) -> go.Figure:
    """Price line with forward-P/E percentile bands translated into price space.

    Buy zone (below low band) shaded green; trim zone (above high band) red.
    Corridor fill between the two bands in gray. Thin-history warning when
    is_thin_history is True.
    """
    if df.empty:
        fig = go.Figure()
        fig.update_layout(**_LAYOUT, title=f"{ticker} — Corridor (no data)")
        return fig

    dates = df.index
    price = df["price"]
    low = df["corridor_low"]
    high = df["corridor_high"]

    has_bands = low.notna().any() and high.notna().any()
    latest = df.iloc[-1]
    is_thin = bool(latest.get("is_thin_history", True))
    history_days = int(latest.get("history_days", 0) or 0)

    fig = go.Figure()

    if has_bands:
        # Buy zone: fill from 0 to low band
        fig.add_trace(go.Scatter(
            x=list(dates) + list(dates[::-1]),
            y=list(low.fillna(method="ffill")) + [0] * len(dates),
            fill="toself", fillcolor=_BUY_FILL,
            line=dict(width=0), showlegend=True, name="Buy zone",
            hoverinfo="skip",
        ))
        # Corridor fill between bands
        fig.add_trace(go.Scatter(
            x=list(dates) + list(dates[::-1]),
            y=list(high.fillna(method="ffill")) + list(low.fillna(method="ffill"))[::-1],
            fill="toself", fillcolor=_BAND_FILL,
            line=dict(width=0), showlegend=True, name="Corridor band",
            hoverinfo="skip",
        ))
        # Band edges
        fig.add_trace(go.Scatter(
            x=dates, y=low, mode="lines",
            line=dict(color=_BAND_LINE, width=1, dash="dot"),
            showlegend=False, name="Low band",
            hovertemplate="Low band: $%{y:.2f}<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=dates, y=high, mode="lines",
            line=dict(color=_BAND_LINE, width=1, dash="dot"),
            showlegend=False, name="High band",
            hovertemplate="High band: $%{y:.2f}<extra></extra>",
        ))
        # Trim zone: fill from high band upward (use large ceiling)
        price_max = price.max() * 1.3 if price.notna().any() else (high.max() * 1.5)
        fig.add_trace(go.Scatter(
            x=list(dates) + list(dates[::-1]),
            y=[price_max] * len(dates) + list(high.fillna(method="ffill"))[::-1],
            fill="toself", fillcolor=_TRIM_FILL,
            line=dict(width=0), showlegend=True, name="Trim zone",
            hoverinfo="skip",
        ))

    # Price line (on top)
    fig.add_trace(go.Scatter(
        x=dates, y=price, mode="lines",
        line=dict(color=_PRICE_COLOR, width=2),
        name="Price (raw close)",
        hovertemplate="Price: $%{y:.2f}<extra></extra>",
    ))

    annotations = []
    if is_thin:
        annotations.append(_thin_annotation(history_days))

    title_suffix = f" — {history_days}d history{'  ⚠ THIN' if is_thin else ''}"
    fig.update_layout(
        **_LAYOUT,
        title=f"{ticker} — Corridor Chart{title_suffix}",
        yaxis_title="Price (USD)",
        legend=dict(orientation="h", y=-0.15),
        annotations=annotations,
        **_xaxis_range(df),
    )
    return fig


# ── 2. True P/E history chart ──────────────────────────────────────────────────

def true_pe_chart(df: pd.DataFrame, ticker: str) -> go.Figure:
    """Forward-P/E series with median + percentile band lines, current marker."""
    if df.empty:
        fig = go.Figure()
        fig.update_layout(**_LAYOUT, title=f"{ticker} — True P/E (no data)")
        return fig

    dates = df.index
    true_pe = df["true_pe"]
    latest = df.iloc[-1]
    is_thin = bool(latest.get("is_thin_history", True))
    history_days = int(latest.get("history_days", 0) or 0)

    pe_low = latest.get("pe_pctl_low")
    pe_med = latest.get("pe_median")
    pe_high = latest.get("pe_pctl_high")
    current_pe = latest.get("true_pe")
    pe_pct = latest.get("pe_percentile")

    fig = go.Figure()

    # Buy / trim zone fills (in P/E space, extend across full date range)
    if pd.notna(pe_low) and pd.notna(pe_med) and pd.notna(pe_high):
        pe_floor = max(0, float(pe_low) * 0.5)
        pe_ceil = float(pe_high) * 1.5
        # Buy zone below low band
        fig.add_hrect(y0=pe_floor, y1=float(pe_low),
                      fillcolor=_BUY_FILL, line_width=0, showlegend=True, name="Buy zone")
        # Corridor band
        fig.add_hrect(y0=float(pe_low), y1=float(pe_high),
                      fillcolor=_BAND_FILL, line_width=0, showlegend=True, name="Corridor band")
        # Trim zone
        fig.add_hrect(y0=float(pe_high), y1=pe_ceil,
                      fillcolor=_TRIM_FILL, line_width=0, showlegend=True, name="Trim zone")
        # Band reference lines
        for val, label, dash in [
            (pe_low, f"20th pctl ({pe_low:.1f}x)", "dot"),
            (pe_med, f"Median ({pe_med:.1f}x)", "solid"),
            (pe_high, f"80th pctl ({pe_high:.1f}x)", "dot"),
        ]:
            fig.add_hline(y=val, line=dict(color=_MEDIAN_COLOR, width=1, dash=dash),
                          annotation_text=label,
                          annotation_position="right",
                          annotation_font_size=10)

    # True P/E series
    fig.add_trace(go.Scatter(
        x=dates, y=true_pe, mode="lines+markers",
        line=dict(color=_PE_COLOR, width=2),
        marker=dict(size=4),
        name="True P/E",
        hovertemplate="True P/E: %{y:.2f}x<extra></extra>",
    ))

    # Current marker annotation
    if current_pe is not None and pd.notna(current_pe):
        pct_label = f" ({pe_pct:.0f}th pctl)" if pe_pct is not None and pd.notna(pe_pct) else ""
        fig.add_annotation(
            x=dates[-1], y=current_pe,
            text=f"  {current_pe:.2f}x{pct_label}",
            showarrow=False, xanchor="left",
            font=dict(color=_PE_COLOR, size=11),
        )

    annotations = []
    if is_thin:
        annotations.append(_thin_annotation(history_days))

    fig.update_layout(
        **_LAYOUT,
        title=f"{ticker} — True P/E History  ({history_days}d{'  ⚠ THIN' if is_thin else ''})",
        yaxis_title="Forward P/E (x)",
        legend=dict(orientation="h", y=-0.15),
        annotations=annotations,
        **_xaxis_range(df),
    )
    return fig


# ── 3. PEG chart ───────────────────────────────────────────────────────────────

def peg_chart(df: pd.DataFrame, ticker: str) -> go.Figure:
    """Forward PEG over time, 1.0 and 2.0 reference lines.

    Points where PEG was suppressed are greyed out so the user knows the
    growth guardrail was active and the PEG should not be trusted.
    """
    if df.empty:
        fig = go.Figure()
        fig.update_layout(**_LAYOUT, title=f"{ticker} — Forward PEG (no data)")
        return fig

    peg_col = df["forward_peg"]
    suppressed = df["peg_suppressed"].fillna(False)

    active_mask = ~suppressed & peg_col.notna()
    supp_mask = suppressed & peg_col.notna()

    fig = go.Figure()

    # Shading: PEG < 1 = buy, > 2 = rich
    peg_vals = peg_col.dropna()
    if not peg_vals.empty:
        y_max = max(peg_vals.max() * 1.2, 2.5)
        fig.add_hrect(y0=0, y1=1.0, fillcolor=_BUY_FILL, line_width=0,
                      showlegend=True, name="PEG < 1 (cheap)")
        fig.add_hrect(y0=2.0, y1=y_max, fillcolor=_TRIM_FILL, line_width=0,
                      showlegend=True, name="PEG > 2 (rich)")

    # Reference lines
    for val, label in [(1.0, "PEG = 1.0"), (2.0, "PEG = 2.0")]:
        fig.add_hline(y=val, line=dict(color=_MEDIAN_COLOR, width=1, dash="dash"),
                      annotation_text=label, annotation_position="right",
                      annotation_font_size=10)

    # Suppressed points (greyed)
    if supp_mask.any():
        fig.add_trace(go.Scatter(
            x=df.index[supp_mask], y=peg_col[supp_mask],
            mode="markers", marker=dict(color="gray", size=5, symbol="x"),
            name="PEG suppressed (growth guardrail)",
            hovertemplate="PEG (suppressed): %{y:.2f}<extra></extra>",
        ))

    # Active PEG series
    if active_mask.any():
        fig.add_trace(go.Scatter(
            x=df.index[active_mask], y=peg_col[active_mask],
            mode="lines+markers",
            line=dict(color=_PEG_COLOR, width=2),
            marker=dict(size=4),
            name="Forward PEG",
            hovertemplate="PEG: %{y:.2f}<extra></extra>",
        ))

    # Current value annotation
    latest = df.iloc[-1]
    cur_peg = latest.get("forward_peg")
    cur_supp = bool(latest.get("peg_suppressed", False))
    if cur_peg is not None and pd.notna(cur_peg):
        suffix = " (suppressed)" if cur_supp else ""
        fig.add_annotation(
            x=df.index[-1], y=cur_peg,
            text=f"  {cur_peg:.2f}{suffix}",
            showarrow=False, xanchor="left",
            font=dict(color="gray" if cur_supp else _PEG_COLOR, size=11),
        )

    is_thin = bool(latest.get("is_thin_history", True))
    history_days = int(latest.get("history_days", 0) or 0)
    annotations = [_thin_annotation(history_days)] if is_thin else []

    fig.update_layout(
        **_LAYOUT,
        title=f"{ticker} — Forward PEG  ({history_days}d{'  ⚠ THIN' if is_thin else ''})",
        yaxis_title="Forward PEG",
        legend=dict(orientation="h", y=-0.15),
        annotations=annotations,
        **_xaxis_range(df),
    )
    return fig
