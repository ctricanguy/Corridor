"""Streamlit dashboard — STAGE 4 entry point (placeholder).

Why Streamlit (vs FastAPI + React/Vite): this is a single-user, local,
graph-heavy data app. Streamlit gives interactive plotly dashboards with almost
no plumbing — no separate API, build step, or frontend state to maintain — which
matches "personal app I can run daily, read the code of, and extend." If this
ever needed multi-user auth or a public API, FastAPI + React would earn its
extra moving parts; for one user it would be overhead. plotly handles the
interactive corridor/PEG visuals either way.

Run (once Stage 4 lands):
    streamlit run app/dashboard.py
"""

from __future__ import annotations


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Corridor", layout="wide")
    st.title("Corridor — personal equity research")
    st.caption("Decision-support for personal use. Not investment advice.")
    st.info(
        "Scaffold only (Stage 0). The watchlist dashboard, corridor / True P/E / "
        "PEG charts, and memos arrive in Stage 4. Initialize the database with "
        "`python scripts/init_db.py` to begin."
    )


if __name__ == "__main__":
    main()
