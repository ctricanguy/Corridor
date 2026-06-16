#!/usr/bin/env python
"""Print the generated SQLite DDL for human review.

The ORM models in corridor.db.models are the single source of truth; this script
renders their DDL so the schema can be read without a separate .sql file that
could drift. Also prints the immutability-guard triggers.

Usage:
    python scripts/dump_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_mock_engine  # noqa: E402

from corridor.db.models import IMMUTABLE_TABLES, Base  # noqa: E402


def main() -> None:
    statements: list[str] = []

    def dump(sql, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        statements.append(str(sql.compile(dialect=engine.dialect)).strip())

    engine = create_mock_engine("sqlite://", dump)
    Base.metadata.create_all(engine, checkfirst=False)

    print("-- Corridor schema (generated from corridor.db.models) --\n")
    for stmt in statements:
        print(stmt.rstrip() + ";\n")

    print("-- Point-in-time immutability guards (installed by init_db) --\n")
    for table in IMMUTABLE_TABLES:
        for op in ("UPDATE", "DELETE"):
            print(
                f"CREATE TRIGGER trg_{table}_no_{op.lower()} BEFORE {op} ON {table}\n"
                f"  BEGIN SELECT RAISE(ABORT, '{table} is immutable: {op} not allowed'); END;\n"
            )


if __name__ == "__main__":
    main()
