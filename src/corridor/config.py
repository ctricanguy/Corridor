"""Configuration loading: `.env` (secrets/paths) + `config.yaml` (methodology).

Two clean layers, by design:

  * ``Settings``  — machine/secret config from environment variables (.env).
  * ``Config``    — the watchlist and methodology parameters from config.yaml.

Nothing about tickers or bands is hardcoded in Python; it all flows from here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Project root = two levels up from this file (src/corridor/config.py -> repo root).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass(frozen=True)
class Settings:
    """Secrets and machine-specific paths, sourced from environment / .env."""

    database_url: str
    snapshot_dir: Path
    sec_edgar_user_agent: str

    @property
    def snapshot_path(self) -> Path:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        return self.snapshot_dir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load environment settings once (cached). Reads .env if present."""
    load_dotenv(PROJECT_ROOT / ".env")
    database_url = os.getenv("CORRIDOR_DATABASE_URL", "sqlite:///data/corridor.db")
    snapshot_dir = Path(os.getenv("CORRIDOR_SNAPSHOT_DIR", "data/snapshots"))
    if not snapshot_dir.is_absolute():
        snapshot_dir = PROJECT_ROOT / snapshot_dir
    user_agent = os.getenv(
        "SEC_EDGAR_USER_AGENT", "Corridor Research personal-use (set-your-email@example.com)"
    )
    return Settings(
        database_url=database_url,
        snapshot_dir=snapshot_dir,
        sec_edgar_user_agent=user_agent,
    )


@dataclass(frozen=True)
class TickerSpec:
    """One watchlist entry."""

    ticker: str
    name: str | None = None
    cik: str | None = None


@dataclass(frozen=True)
class Config:
    """Parsed methodology configuration (the watchlist + all factor parameters)."""

    universe: list[TickerSpec]
    earnings: dict[str, Any] = field(default_factory=dict)
    valuation: dict[str, Any] = field(default_factory=dict)
    peg: dict[str, Any] = field(default_factory=dict)
    overlay: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def tickers(self) -> list[str]:
        """Plain list of ticker symbols in watchlist order."""
        return [t.ticker for t in self.universe]

    @property
    def engine_version(self) -> str:
        return str(self.data.get("engine_version", "0.1.0"))


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate config.yaml into a typed ``Config``.

    Raises:
        FileNotFoundError: if the config file is missing.
        ValueError: if the universe is empty or malformed (fail loud, not silent).
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. Copy the seed config or pass an explicit path."
        )
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}

    universe_raw = raw.get("universe") or []
    if not universe_raw:
        raise ValueError("config.yaml has an empty 'universe' — add at least one ticker.")
    universe = [
        TickerSpec(ticker=str(item["ticker"]).upper(), name=item.get("name"), cik=item.get("cik"))
        for item in universe_raw
    ]

    return Config(
        universe=universe,
        earnings=raw.get("earnings", {}),
        valuation=raw.get("valuation", {}),
        peg=raw.get("peg", {}),
        overlay=raw.get("overlay", {}),
        data=raw.get("data", {}),
    )
