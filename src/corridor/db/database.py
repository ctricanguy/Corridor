"""Database engine, session factory, and schema initialization.

``init_db()`` creates the tables from the ORM models and installs SQLite
triggers that REJECT ``UPDATE``/``DELETE`` on the immutable snapshot tables.
That makes point-in-time integrity a property of the storage layer itself:
forward-estimate history cannot be silently overwritten even by a buggy job.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import IMMUTABLE_TABLES, Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _enable_sqlite_fk(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    """Enforce foreign keys on SQLite (off by default per connection)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine(database_url: str | None = None, echo: bool = False) -> Engine:
    """Return a process-wide singleton engine.

    Args:
        database_url: SQLAlchemy URL. Defaults to the configured CORRIDOR_DATABASE_URL.
        echo: If True, log emitted SQL (useful when debugging the schema).
    """
    global _engine, _SessionFactory
    if _engine is None:
        if database_url is None:
            from ..config import get_settings

            database_url = get_settings().database_url
        _engine = create_engine(database_url, echo=echo, future=True)
        if _engine.dialect.name == "sqlite":
            event.listen(_engine, "connect", _enable_sqlite_fk)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager (commit on success, rollback on error)."""
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _install_immutability_guards(engine: Engine) -> None:
    """Install SQLite triggers that reject UPDATE/DELETE on immutable tables.

    Point-in-time integrity is non-negotiable, so we enforce it in the database
    rather than trusting every caller. New observations are always INSERTs;
    correcting a snapshot means inserting a new dated row, never mutating an old
    one. No-op on non-SQLite backends (a future Postgres swap would use rules).
    """
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        for table_name in IMMUTABLE_TABLES:
            for op in ("UPDATE", "DELETE"):
                trigger = f"trg_{table_name}_no_{op.lower()}"
                conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
                conn.execute(
                    text(
                        f"CREATE TRIGGER {trigger} BEFORE {op} ON {table_name} "
                        f"BEGIN SELECT RAISE(ABORT, "
                        f"'{table_name} is immutable (point-in-time integrity): "
                        f"{op} is not allowed; insert a new dated snapshot instead'); "
                        f"END;"
                    )
                )


def init_db(database_url: str | None = None, echo: bool = False) -> Engine:
    """Create all tables and install immutability guards. Idempotent."""
    engine = get_engine(database_url, echo=echo)
    Base.metadata.create_all(engine)
    _install_immutability_guards(engine)
    return engine


def reset_engine() -> None:
    """Dispose the singleton engine (used by tests to get a clean in-memory DB)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
