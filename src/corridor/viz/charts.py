"""Pure Plotly chart functions — no I/O, no Streamlit.

Each function takes a DataFrame (from viz.data) and returns a go.Figure.
Color language is consistent throughout:
  green  = buy zone / below low band
  red    = trim zone / above high band
  gray   = corridor band fill / suppressed
  amber  = thin-history warning
  dashed = forward projection (estimates, NOT predictions)

All charts that display thin history attach a loud annotation so the user
never mistakes a handful of days for a deep distribution.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go


# Minimum x-axis window (days). With a single snapshot Plotly zooms to
# microseconds; this pins a sensible date range that expands as history grows.
_MIN_XRANGE_DAYS = 30

# ── shared palette ─────────────────────────────────────────────────────────────
_BUY_FILL   = "rgba(0, 200, 100, 0.10)"
_TRIM_FILL  = "rgba(220, 50, 50, 0.10)"
_BAND_FILL  = "rgba(120, 120, 140, 0.15)"
_BAND_LINE  = "rgba(120, 120, 140, 0.40)"
_PROJ_LOW   = "rgba(0, 200, 100, 0.45)"
_PROJ_MID   = "rgba(180, 180, 200, 0.55)"
_PROJ_HIGH  = "rgba(220, 50, 50, 0.45)"
_PRICE_COLOR  = "#7EB8F7"
_PE_COLOR     = "#C9A84C"
_PEG_COLOR    = "#A37EC9"
_MEDIAN_COLOR = "rgba(200,200,200,0.7)"
_THIN_BG      = "rgba(255,180,0,0.08)"

_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor="#0E1117",
    plot_bgcolor="#0E1117",
    font=dict(family="monospace", size=12, color="#CCCCCC"),
    margin=dict(l=60, r=20, t=50, b=40),
    hovermode="x unified",
)

_SIGNAL_ORDER = [
    "hard_buy", "buy", "watch_cheap", "hold",
    "trim", "hard_trim", "insufficient_history", None,
]
_SIGNAL_BAR_COLOR = {
    "hard_buy":            "#00C864",
    "buy":                 "#00A050",
    "watch_cheap":         "#60C860",
    "hold":                "#888888",
    "trim":                "#E07020",
    "hard_trim":           "#CC2020",
    "insufficient_history":"#555555",
}


def _thin_annotation(history_days: int) -> dict:
    return dict(
        text=(f"⚠ THIN HISTORY — {history_days}d of snapshots. "
              "Bands are unstable; interpret with caution."),
        xref="paper", yref="paper",
        x=0.0, y=1.0, xanchor="left", yanchor="top",
        showarrow=False,
        font=dict(size=11, color="#FFB300"),
        bgcolor=_THIN_BG,
        bordercolor="#FFB300",
        borderwidth=1,
    )


def _xaxis_range(df: pd.DataFrame, pad_right_days: int = 3) -> dict:
    """Pin a minimum 30-day date window so single-point charts show dates."""
    if df.empty:
        return {}
    latest  = df.index[-1]
    earliest = df.index[0]
    if (latest - earliest).days < _MIN_XRANGE_DAYS:
        x_start = latest - pd.Timedelta(days=_MIN_XRANGE_DAYS)
    else:
        x_start = earliest - pd.Timedelta(days=2)
    x_end = latest + pd.Timedelta(days=pad_right_days)
    return {"xaxis": dict(range=[x_start, x_end], type="date")}


def _xaxis_range_proj(df: pd.DataFrame, annual_estimates: pd.DataFrame) -> dict:
    """Like _xaxis_range but extends right to cover projection end date."""
    base = _xaxis_range(df, pad_right_days=30)
    if annual_estimates.empty or not base:
        return base
    proj_end = annual_estimates["period_end_date"].max() + pd.Timedelta(days=60)
    base["xaxis"]["range"][1] = proj_end
    return base


# ── 1. Corridor chart (+ optional forward projection) ─────────────────────────

def _add_projection(
    fig: go.Figure,
    df: pd.DataFrame,
    annual_estimates: pd.DataFrame,
) -> list[dict]:
    """Append dashed projection traces to an existing corridor figure.

    Uses the stored pe_pctl_low / pe_median / pe_pctl_high band multiples
    applied to the forward annual EPS estimates. Requires at least 2 days of
    history so that bands are non-None. Returns extra annotations to add.
    """
    if annual_estimates.empty or df.empty:
        return []

    latest = df.iloc[-1]
    pe_low = latest.get("pe_pctl_low")
    pe_med = latest.get("pe_median")
    pe_high = latest.get("pe_pctl_high")
    ntm_eps = latest.get("forward_eps_ntm")

    if any(v is None or (hasattr(v, "__float__") and pd.isna(float(v)))
           for v in [pe_low, pe_med, pe_high]):
        return []  # no bands yet (< 2 data points); nothing to project

    pe_low, pe_med, pe_high = float(pe_low), float(pe_med), float(pe_high)

    # Anchor: today's corridor prices (connect history → projection seamlessly)
    anchor_date = df.index[-1]
    anchor_low  = float(latest["corridor_low"])  if pd.notna(latest.get("corridor_low"))  else pe_low  * float(ntm_eps or 0)
    anchor_mid  = pe_med * float(ntm_eps or 0)   if ntm_eps and pd.notna(ntm_eps) else None
    anchor_high = float(latest["corridor_high"]) if pd.notna(latest.get("corridor_high")) else pe_high * float(ntm_eps or 0)

    proj_dates = [anchor_date] + list(annual_estimates["period_end_date"])
    eps_vals   = [float(ntm_eps or 0)] + list(annual_estimates["value"].astype(float))

    proj_low  = [pe_low  * e for e in eps_vals]
    proj_mid  = [pe_med  * e for e in eps_vals]
    proj_high = [pe_high * e for e in eps_vals]
    # Override anchor point with stored values for smooth join
    if anchor_low  is not None: proj_low[0]  = anchor_low
    if anchor_mid  is not None: proj_mid[0]  = anchor_mid
    if anchor_high is not None: proj_high[0] = anchor_high

    labels = ["today"] + list(annual_estimates["fiscal_period"])

    common = dict(mode="lines+markers", marker=dict(size=5, symbol="circle-open"))

    fig.add_trace(go.Scatter(
        x=proj_dates, y=proj_low, name="Proj. low band",
        line=dict(color=_PROJ_LOW, width=1.5, dash="dash"),
        customdata=labels,
        hovertemplate="Proj. low (%{customdata}): $%{y:.0f}<extra></extra>",
        **common,
    ))
    fig.add_trace(go.Scatter(
        x=proj_dates, y=proj_mid, name="Proj. median",
        line=dict(color=_PROJ_MID, width=1.5, dash="dash"),
        customdata=labels,
        hovertemplate="Proj. median (%{customdata}): $%{y:.0f}<extra></extra>",
        **common,
    ))
    fig.add_trace(go.Scatter(
        x=proj_dates, y=proj_high, name="Proj. high band",
        line=dict(color=_PROJ_HIGH, width=1.5, dash="dash"),
        customdata=labels,
        hovertemplate="Proj. high (%{customdata}): $%{y:.0f}<extra></extra>",
        **common,
    ))

    # Projection disclaimer annotation anchored to top-right of the chart
    is_thin = bool(latest.get("is_thin_history", True))
    thin_note = " (bands from thin history — treat as very rough)" if is_thin else ""
    disclaimer = dict(
        text=(f"— — PROJECTION: historical band multiples × forward annual EPS estimates"
              f"{thin_note}. NOT a price prediction."),
        xref="paper", yref="paper",
        x=1.0, y=0.0, xanchor="right", yanchor="bottom",
        showarrow=False,
        font=dict(size=9, color="#888888"),
    )
    return [disclaimer]


def corridor_chart(
    df: pd.DataFrame,
    ticker: str,
    annual_estimates: pd.DataFrame | None = None,
) -> go.Figure:
    """Price line + corridor bands + optional forward projection.

    Buy zone (below low band) shaded green; trim zone (above high band) red.
    When annual_estimates is provided and bands exist, dashed projection lines
    extend the corridor forward using forward annual EPS. Clearly labelled as
    a projection of where the band prices would sit, NOT a price target.
    """
    if df.empty:
        fig = go.Figure()
        fig.update_layout(**_LAYOUT, title=f"{ticker} — Corridor (no data)")
        return fig

    dates  = df.index
    price  = df["price"]
    low    = df["corridor_low"]
    high   = df["corridor_high"]

    has_bands    = low.notna().any() and high.notna().any()
    latest       = df.iloc[-1]
    is_thin      = bool(latest.get("is_thin_history", True))
    history_days = int(latest.get("history_days") or 0)

    fig = go.Figure()

    if has_bands:
        low_ff  = low.ffill()
        high_ff = high.ffill()
        price_max = price.max() * 1.3 if price.notna().any() else float(high_ff.max()) * 1.5

        # Buy zone fill (0 → low band)
        fig.add_trace(go.Scatter(
            x=list(dates) + list(dates[::-1]),
            y=list(low_ff) + [0] * len(dates),
            fill="toself", fillcolor=_BUY_FILL,
            line=dict(width=0), showlegend=True, name="Buy zone",
            hoverinfo="skip",
        ))
        # Corridor fill (low → high band)
        fig.add_trace(go.Scatter(
            x=list(dates) + list(dates[::-1]),
            y=list(high_ff) + list(low_ff)[::-1],
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
        # Trim zone fill (high band → ceiling)
        fig.add_trace(go.Scatter(
            x=list(dates) + list(dates[::-1]),
            y=[price_max] * len(dates) + list(high_ff)[::-1],
            fill="toself", fillcolor=_TRIM_FILL,
            line=dict(width=0), showlegend=True, name="Trim zone",
            hoverinfo="skip",
        ))

    # Price line (on top of fills)
    fig.add_trace(go.Scatter(
        x=dates, y=price, mode="lines",
        line=dict(color=_PRICE_COLOR, width=2),
        name="Price (raw close)",
        hovertemplate="Price: $%{y:.2f}<extra></extra>",
    ))

    # Forward projection
    proj_annotations: list[dict] = []
    if annual_estimates is not None and not annual_estimates.empty:
        proj_annotations = _add_projection(fig, df, annual_estimates)

    # Thin-history badge
    chart_annotations = proj_annotations[:]
    if is_thin:
        chart_annotations.append(_thin_annotation(history_days))

    title_suffix = f" — {history_days}d history{'  ⚠ THIN' if is_thin else ''}"
    xr = (_xaxis_range_proj(df, annual_estimates)
          if annual_estimates is not None and not annual_estimates.empty
          else _xaxis_range(df))
    fig.update_layout(
        **_LAYOUT,
        title=f"{ticker} — Corridor Chart{title_suffix}",
        yaxis_title="Price (USD)",
        legend=dict(orientation="h", y=-0.18, font=dict(size=10)),
        annotations=chart_annotations,
        **xr,
    )
    return fig


# ── 2. True P/E history chart ──────────────────────────────────────────────────

def true_pe_chart(df: pd.DataFrame, ticker: str) -> go.Figure:
    """Forward P/E series with median + percentile band lines, current marker."""
    if df.empty:
        fig = go.Figure()
        fig.update_layout(**_LAYOUT, title=f"{ticker} — True P/E (no data)")
        return fig

    dates    = df.index
    true_pe  = df["true_pe"]
    latest   = df.iloc[-1]
    is_thin  = bool(latest.get("is_thin_history", True))
    history_days = int(latest.get("history_days") or 0)

    pe_low  = latest.get("pe_pctl_low")
    pe_med  = latest.get("pe_median")
    pe_high = latest.get("pe_pctl_high")
    current_pe = latest.get("true_pe")
    pe_pct     = latest.get("pe_percentile")

    fig = go.Figure()

    if pd.notna(pe_low) and pd.notna(pe_med) and pd.notna(pe_high):
        pe_floor = max(0.0, float(pe_low) * 0.5)
        pe_ceil  = float(pe_high) * 1.5
        fig.add_hrect(y0=pe_floor,       y1=float(pe_low),
                      fillcolor=_BUY_FILL,  line_width=0,
                      showlegend=True, name="Buy zone")
        fig.add_hrect(y0=float(pe_low),  y1=float(pe_high),
                      fillcolor=_BAND_FILL, line_width=0,
                      showlegend=True, name="Corridor band")
        fig.add_hrect(y0=float(pe_high), y1=pe_ceil,
                      fillcolor=_TRIM_FILL, line_width=0,
                      showlegend=True, name="Trim zone")
        for val, label, dash in [
            (pe_low, f"20th pctl ({pe_low:.1f}x)", "dot"),
            (pe_med, f"Median ({pe_med:.1f}x)",    "solid"),
            (pe_high, f"80th pctl ({pe_high:.1f}x)", "dot"),
        ]:
            fig.add_hline(y=val, line=dict(color=_MEDIAN_COLOR, width=1, dash=dash),
                          annotation_text=label, annotation_position="right",
                          annotation_font_size=10)

    fig.add_trace(go.Scatter(
        x=dates, y=true_pe, mode="lines+markers",
        line=dict(color=_PE_COLOR, width=2),
        marker=dict(size=4),
        name="True P/E",
        hovertemplate="True P/E: %{y:.2f}x<extra></extra>",
    ))

    if current_pe is not None and pd.notna(current_pe):
        pct_label = (f" ({pe_pct:.0f}th pctl)"
                     if pe_pct is not None and pd.notna(pe_pct) else "")
        fig.add_annotation(
            x=dates[-1], y=current_pe,
            text=f"  {current_pe:.2f}x{pct_label}",
            showarrow=False, xanchor="left",
            font=dict(color=_PE_COLOR, size=11),
        )

    annotations = [_thin_annotation(history_days)] if is_thin else []
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
    """Forward PEG over time. Suppressed points shown as grey ✕ markers."""
    if df.empty:
        fig = go.Figure()
        fig.update_layout(**_LAYOUT, title=f"{ticker} — Forward PEG (no data)")
        return fig

    peg_col   = df["forward_peg"]
    suppressed = df["peg_suppressed"].fillna(False)
    active_mask = ~suppressed & peg_col.notna()
    supp_mask   = suppressed  & peg_col.notna()

    fig = go.Figure()

    peg_vals = peg_col.dropna()
    if not peg_vals.empty:
        y_max = max(float(peg_vals.max()) * 1.2, 2.5)
        fig.add_hrect(y0=0,   y1=1.0,   fillcolor=_BUY_FILL,  line_width=0,
                      showlegend=True, name="PEG < 1 (cheap)")
        fig.add_hrect(y0=2.0, y1=y_max, fillcolor=_TRIM_FILL, line_width=0,
                      showlegend=True, name="PEG > 2 (rich)")

    for val, label in [(1.0, "PEG = 1.0"), (2.0, "PEG = 2.0")]:
        fig.add_hline(y=val, line=dict(color=_MEDIAN_COLOR, width=1, dash="dash"),
                      annotation_text=label, annotation_position="right",
                      annotation_font_size=10)

    if supp_mask.any():
        fig.add_trace(go.Scatter(
            x=df.index[supp_mask], y=peg_col[supp_mask],
            mode="markers", marker=dict(color="gray", size=5, symbol="x"),
            name="PEG suppressed (growth guardrail)",
            hovertemplate="PEG (suppressed): %{y:.2f}<extra></extra>",
        ))

    if active_mask.any():
        fig.add_trace(go.Scatter(
            x=df.index[active_mask], y=peg_col[active_mask],
            mode="lines+markers",
            line=dict(color=_PEG_COLOR, width=2),
            marker=dict(size=4),
            name="Forward PEG",
            hovertemplate="PEG: %{y:.2f}<extra></extra>",
        ))

    latest  = df.iloc[-1]
    cur_peg  = latest.get("forward_peg")
    cur_supp = bool(latest.get("peg_suppressed", False))
    if cur_peg is not None and pd.notna(cur_peg):
        suffix = " (suppressed)" if cur_supp else ""
        fig.add_annotation(
            x=df.index[-1], y=cur_peg,
            text=f"  {cur_peg:.2f}{suffix}",
            showarrow=False, xanchor="left",
            font=dict(color="gray" if cur_supp else _PEG_COLOR, size=11),
        )

    is_thin      = bool(latest.get("is_thin_history", True))
    history_days = int(latest.get("history_days") or 0)
    annotations  = [_thin_annotation(history_days)] if is_thin else []
    fig.update_layout(
        **_LAYOUT,
        title=f"{ticker} — Forward PEG  ({history_days}d{'  ⚠ THIN' if is_thin else ''})",
        yaxis_title="Forward PEG",
        legend=dict(orientation="h", y=-0.15),
        annotations=annotations,
        **_xaxis_range(df),
    )
    return fig


# ── 4. Watchlist overview chart ────────────────────────────────────────────────

def watchlist_heatmap(df: pd.DataFrame) -> go.Figure:
    """Horizontal bar chart: PE percentile position for all tickers at a glance.

    Each bar spans 0-100. The filled portion shows where today's True P/E sits
    in its own history. Color = signal. Reference lines at 20 (buy) and 80 (trim).
    Thin-history tickers are marked with ⚠ so the user knows the distribution is
    shallow. Tickers are sorted by signal severity (cheapest first).
    """
    if df.empty:
        fig = go.Figure()
        fig.update_layout(**_LAYOUT, title="Watchlist — no data")
        return fig

    # Sort: signal order, then PE percentile ascending within signal group
    sig_rank = {s: i for i, s in enumerate(_SIGNAL_ORDER)}
    df = df.copy()
    df["_sig_rank"] = df["signal"].map(sig_rank).fillna(len(_SIGNAL_ORDER))
    df["_pct"] = df["pe_percentile"].fillna(50)
    df = df.sort_values(["_sig_rank", "_pct"], ascending=[False, False])

    tickers = []
    pcts    = []
    colors  = []
    texts   = []

    for _, row in df.iterrows():
        ticker = row["ticker"]
        pct    = row.get("pe_percentile")
        sig    = row.get("signal") or "—"
        tpe    = row.get("true_pe")
        thin   = bool(row.get("is_thin_history", True))

        label = ticker + (" ⚠" if thin else "")
        tickers.append(label)
        pcts.append(float(pct) if pd.notna(pct) else 50.0)
        colors.append(_SIGNAL_BAR_COLOR.get(sig, "#555555"))

        pct_str = f"{pct:.0f}th pctl" if pd.notna(pct) else "—"
        pe_str  = f"P/E {tpe:.1f}x" if pd.notna(tpe) else "—"
        thin_str = "  THIN" if thin else ""
        texts.append(f"{pct_str}  {pe_str}  {sig}{thin_str}")

    fig = go.Figure(go.Bar(
        x=pcts,
        y=tickers,
        orientation="h",
        marker_color=colors,
        text=texts,
        textposition="inside",
        insidetextanchor="start",
        textfont=dict(size=11, color="white"),
        hovertemplate="%{y}<br>%{text}<extra></extra>",
        width=0.6,
    ))

    # Reference lines at 20 and 80
    for x, label, color in [
        (20, "buy ≤ 20th", "#00A050"),
        (80, "trim ≥ 80th", "#CC2020"),
    ]:
        fig.add_vline(x=x, line=dict(color=color, width=1, dash="dash"),
                      annotation_text=label, annotation_position="top",
                      annotation_font_size=10, annotation_font_color=color)

    layout = {**_LAYOUT}
    layout["margin"] = dict(l=100, r=20, t=50, b=40)
    fig.update_layout(
        **layout,
        title="Watchlist — True P/E percentile position (⚠ = thin history)",
        xaxis=dict(title="PE percentile (0 = cheapest vs own history)", range=[0, 100]),
        yaxis=dict(title=""),
        height=max(300, 50 * len(tickers) + 80),
        showlegend=False,
    )
    return fig
