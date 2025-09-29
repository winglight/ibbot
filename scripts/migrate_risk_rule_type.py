"""Backfill missing `rule_type` values in the `risk_rules` table."""

import argparse
import sqlite3
from pathlib import Path

def migrate(db_path: Path) -> None:
    db_path = db_path.expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info('risk_rules')")
        columns = {row[1] for row in cur.fetchall()}
        if "rule_type" not in columns:
            cur.execute("ALTER TABLE risk_rules ADD COLUMN rule_type TEXT DEFAULT 'fixed'")

        cur.execute(
            """
            UPDATE risk_rules
               SET rule_type = 'trailing'
             WHERE (rule_type IS NULL OR rule_type = '')
               AND COALESCE(auto_trailing, 0) = 1
            """
        )
        cur.execute(
            """
            UPDATE risk_rules
               SET rule_type = 'fixed'
             WHERE (rule_type IS NULL OR rule_type = '')
            """
        )
        cur.execute(
            """
            UPDATE risk_rules
               SET auto_trailing = CASE WHEN LOWER(COALESCE(rule_type, '')) = 'trailing' THEN 1 ELSE 0 END
            """
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill risk rule types and sync auto_trailing flags.")
    parser.add_argument(
        "--db",
        dest="db_path",
        default="data/trading_bot.db",
        type=Path,
        help="Path to the SQLite database file.",
    )
    args = parser.parse_args()
    migrate(args.db_path)
    print("Risk rule type migration completed.")
