#!/usr/bin/env python3
"""Backfill and synchronise risk rule types in the SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Tuple


def _ensure_columns(cursor: sqlite3.Cursor) -> None:
    """Add the rule_type column if it is missing (legacy deployments)."""

    cursor.execute("PRAGMA table_info('risk_rules')")
    existing = {row[1] for row in cursor.fetchall()}
    if "rule_type" not in existing:
        cursor.execute("ALTER TABLE risk_rules ADD COLUMN rule_type TEXT DEFAULT 'fixed'")
    if "auto_trailing" not in existing:
        cursor.execute("ALTER TABLE risk_rules ADD COLUMN auto_trailing BOOLEAN DEFAULT 0")


def migrate(db_path: Path) -> Tuple[int, int]:
    """Perform the migration and return counts for updated columns."""

    if not db_path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.cursor()
        _ensure_columns(cursor)

        cursor.execute(
            """
            UPDATE risk_rules
            SET rule_type = CASE
                WHEN LOWER(TRIM(COALESCE(rule_type, ''))) IN ('fixed', 'trailing')
                    THEN LOWER(TRIM(rule_type))
                WHEN COALESCE(auto_trailing, 0) = 1 THEN 'trailing'
                ELSE 'fixed'
            END
            WHERE rule_type IS NULL
               OR TRIM(rule_type) = ''
               OR LOWER(TRIM(rule_type)) NOT IN ('fixed', 'trailing')
            """
        )
        rule_type_updates = cursor.rowcount or 0

        cursor.execute(
            """
            UPDATE risk_rules
            SET auto_trailing = CASE
                WHEN LOWER(COALESCE(rule_type, '')) = 'trailing' THEN 1
                ELSE 0
            END
            WHERE auto_trailing IS NULL
               OR auto_trailing NOT IN (0, 1)
               OR (LOWER(COALESCE(rule_type, '')) = 'trailing' AND auto_trailing != 1)
               OR (LOWER(COALESCE(rule_type, '')) != 'trailing' AND auto_trailing != 0)
            """
        )
        auto_trailing_updates = cursor.rowcount or 0

        conn.commit()

    return rule_type_updates, auto_trailing_updates


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill missing risk rule types and align trailing flags."
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default="data/trading_bot.db",
        help="Path to the SQLite database file (default: data/trading_bot.db)",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    rule_updates, auto_updates = migrate(db_path)
    print(
        f"Updated {rule_updates} rows for rule_type and {auto_updates} rows for auto_trailing in {db_path}"
    )


if __name__ == "__main__":
    main()
