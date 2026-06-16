"""Shared pytest fixtures.

Each test gets a fresh, isolated, file-backed SQLite database (file-backed rather
than in-memory so multiple sessions/connections see the same data) with the full
schema and immutability guards installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from corridor.db import database, init_db


@pytest.fixture
def db_url(tmp_path: Path) -> Iterator[str]:
    url = f"sqlite:///{tmp_path / 'test_corridor.db'}"
    database.reset_engine()
    init_db(url)
    yield url
    database.reset_engine()
