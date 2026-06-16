"""Stage 0 config tests — the watchlist and methodology params load as expected."""

from __future__ import annotations

from corridor.config import load_config

SEED_UNIVERSE = {"NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA", "AMD", "AVGO", "MU"}


def test_seed_universe_loads() -> None:
    config = load_config()
    assert set(config.tickers) == SEED_UNIVERSE
    assert len(config.universe) == 10


def test_peg_growth_basis_is_explicit() -> None:
    """PEG's growth input is the whole ballgame — it must be set, not implicit."""
    config = load_config()
    assert config.peg.get("growth_basis") in {"ntm_vs_ltm", "fwd_cagr"}
    assert "min_growth_rate" in config.peg  # the suppression guardrail exists


def test_corridor_bands_present() -> None:
    config = load_config()
    corridor = config.valuation["corridor"]
    assert corridor["pctl_low"] < corridor["pctl_high"]
    assert corridor["min_history_days"] > 0  # thin-history threshold exists
