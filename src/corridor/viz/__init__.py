"""Visualization layer (Stage 4) — first-class, the graphs are half the point.

Reusable plotly figures shared by the dashboard and the memos:
    1. corridor chart      price vs forward-P/E percentile bands in price space
    2. true_pe chart       forward P/E series with median + bands + current marker
    3. peg chart           forward PEG over time, 1.0/2.0 ref lines, greyed when suppressed
    4. company dashboard    the three charts + RSI/MA + signal badges + key stats
    5. watchlist overview   sortable table/heatmap across the universe

Consistent color language app-wide (green = buy zone, red = trim zone) and every
thin-history chart is annotated with its real snapshot-history depth.
"""
