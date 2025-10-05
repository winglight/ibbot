"""Utility to migrate legacy SQLite data into the MariaDB schema.

The script copies the order/risk/configuration tables from
``data/trading_bot.db`` (or a custom SQLite database) into the MariaDB
instance configured via ``IBBOT_DB_URL``.  DOM/行情类数据（DOM、K 线）不会迁移。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from src.db.mariadb import MariaDBConfigurationError, connect

_TABLES = ("orders", "risk_rules", "strategies", "config")

_DATETIME_COLUMNS: dict[str, set[str]] = {
    "orders": {"created_at", "updated_at", "executed_at", "order_timestamp"},
    "risk_rules": {"timestamp", "created_at", "updated_at"},
    "strategies": {"created_at", "updated_at"},
    "config": {"created_at", "updated_at"},
}

_JSON_COLUMNS: dict[str, set[str]] = {
    "orders": {"raw_payload"},
    "risk_rules": {"atr_params"},
}

_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "orders": ("id", "order_id", "command_id", "client_order_id", "ib_order_id"),
    "risk_rules": ("id", "symbol"),
    "strategies": ("id", "name"),
    "config": ("id", "config_key"),
}

_SKIP_UPDATE_COLUMNS: dict[str, set[str]] = {
    "orders": {"id", "created_at"},
    "risk_rules": {"created_at"},
    "strategies": {"created_at"},
    "config": {"created_at"},
}

@dataclass
class MigrationStats:
    table: str
    migrated: int = 0
    skipped: int = 0
    missing: bool = False


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)):
        # treat as epoch seconds; heuristically ignore extremely small values
        if value > 10_000_000_000:
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for candidate in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                result = datetime.strptime(text, candidate)
                if result.tzinfo is None:
                    return result.replace(tzinfo=timezone.utc)
                return result.astimezone(timezone.utc)
            except ValueError:
                continue
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
        if result.tzinfo is None:
            return result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    return None


def _normalise_value(table: str, column: str, value: Any) -> Any:
    if column in _DATETIME_COLUMNS.get(table, set()):
        parsed = _parse_datetime(value)
        return parsed.replace(tzinfo=None) if parsed is not None else None
    if column in _JSON_COLUMNS.get(table, set()) and value is not None:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                json.loads(text)
            except json.JSONDecodeError:
                return json.dumps(text, ensure_ascii=False)
            return text
        return json.dumps(value, ensure_ascii=False)
    if column.endswith("_quantity") or column.endswith("_price") or column in {
        "quantity",
        "price",
        "limit_price",
        "stop_price",
        "fill_price",
        "pnl",
        "realized_pnl",
        "unrealized_pnl",
        "commission",
        "risk_percent",
        "position_size",
        "account_value",
        "suggested_quantity",
    }:
        coerced = _coerce_float(value)
        return coerced if coerced is not None else None
    if column == "is_deleted":
        coerced = _coerce_int(value)
        return coerced if coerced is not None else 0
    if column == "enabled":
        coerced = _coerce_int(value)
        if coerced is None:
            return 1
        return 1 if coerced else 0
    if column == "retry_count":
        coerced = _coerce_int(value)
        return coerced if coerced is not None else 0
    if column == "max_time_span":
        return _coerce_int(value)
    return value


def _sqlite_rows(connection: sqlite3.Connection, table: str) -> Iterable[Mapping[str, Any]]:
    cursor = connection.execute(f"SELECT * FROM {table}")
    columns = [col[0] for col in cursor.description]
    for raw in cursor.fetchall():
        yield {columns[idx]: raw[idx] for idx in range(len(columns))}


def _prepare_order_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = dict(row)
    command_identifier = _coerce_str(
        data.get("command_id")
        or data.get("client_order_id")
        or data.get("order_id")
    )
    if command_identifier:
        data["command_id"] = command_identifier
    data.setdefault("client_order_id", row.get("order_id"))
    data.setdefault("strategy_name", row.get("strategy") or row.get("strategy_name"))
    data.setdefault("strategy", row.get("strategy") or row.get("strategy_name"))
    data.setdefault("order_source", row.get("order_source") or "manual")
    data.setdefault("exchange", row.get("exchange") or "SMART")
    data.setdefault("sec_type", row.get("sec_type") or "STK")
    side = data.get("side") or data.get("action")
    if side:
        data["side"] = str(side).upper()
        data.setdefault("action", data["side"])
    action = data.get("action")
    if action:
        data["action"] = str(action).upper()
    if "is_deleted" in data and data["is_deleted"] in {None, "", "FALSE"}:
        data["is_deleted"] = 0
    return data


def _prepare_row(table: str, row: Mapping[str, Any], target_columns: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    source = dict(row)
    if table == "orders":
        source = _prepare_order_row(source)
    for column in target_columns:
        if column in source:
            payload[column] = _normalise_value(table, column, source[column])
        elif table == "orders" and column == "client_order_id":
            payload[column] = _coerce_str(source.get("order_id"))
        elif table == "orders" and column == "strategy_name":
            payload[column] = _coerce_str(source.get("strategy") or source.get("strategy_name"))
        elif table == "orders" and column == "strategy":
            payload[column] = _coerce_str(source.get("strategy") or source.get("strategy_name"))
        elif table == "orders" and column == "order_source":
            payload[column] = source.get("order_source") or "manual"
        elif table == "orders" and column == "command_id":
            payload[column] = _coerce_str(
                source.get("command_id")
                or source.get("client_order_id")
                or source.get("order_id")
            )
    return {key: value for key, value in payload.items() if value is not None}


def _target_columns(connection: Any, table: str) -> list[str]:
    cursor = connection.cursor()
    try:
        try:
            cursor.execute(f"SHOW COLUMNS FROM `{table}`")
            rows = cursor.fetchall()
            if rows:
                return [
                    row[0] if isinstance(row, tuple) else row.get("Field") for row in rows
                ]
        except Exception:
            cursor.close()
            cursor = connection.cursor()
            cursor.execute(f"PRAGMA table_info('{table}')")
            rows = cursor.fetchall()
            if rows:
                return [
                    row[1] if isinstance(row, tuple) else row.get("name") for row in rows
                ]
        raise RuntimeError(f"无法获取 {table} 的列信息")
    finally:
        cursor.close()


def _insert_rows(connection: Any, table: str, columns: list[str], rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    primary_keys = set(_PRIMARY_KEYS.get(table, ()))
    skip_update = _SKIP_UPDATE_COLUMNS.get(table, set())
    placeholders = ", ".join(["%s"] * len(columns))
    columns_sql = ", ".join(f"`{col}`" for col in columns)
    update_candidates = [
        col for col in columns if col not in primary_keys and col not in skip_update
    ]
    if update_candidates:
        update_sql = ", ".join(f"`{col}` = VALUES(`{col}`)" for col in update_candidates)
        statement = (
            f"INSERT INTO `{table}` ({columns_sql}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {update_sql}"
        )
    else:
        statement = f"INSERT INTO `{table}` ({columns_sql}) VALUES ({placeholders})"

    cursor = connection.cursor()
    try:
        cursor.executemany(
            statement,
            [tuple(row.get(col) for col in columns) for row in rows],
        )
    finally:
        cursor.close()
    return len(rows)


def migrate(sqlite_path: Path, tables: Iterable[str]) -> list[MigrationStats]:
    sqlite_connection = sqlite3.connect(str(sqlite_path))
    sqlite_connection.row_factory = sqlite3.Row

    stats: list[MigrationStats] = []
    with connect() as maria:
        for table in tables:
            stat = MigrationStats(table=table)
            try:
                target_columns = _target_columns(maria, table)
            except Exception as exc:  # pragma: no cover - depends on env
                raise RuntimeError(f"无法读取 MariaDB 表结构 `{table}`: {exc}") from exc

            try:
                source_rows = list(_sqlite_rows(sqlite_connection, table))
            except sqlite3.OperationalError:
                stat.missing = True
                stats.append(stat)
                continue

            prepared_rows: list[dict[str, Any]] = []
            for row in source_rows:
                data = _prepare_row(table, row, target_columns)
                if not data:
                    stat.skipped += 1
                    continue
                prepared_rows.append(data)

            inserted = _insert_rows(maria, table, target_columns, prepared_rows)
            stat.migrated = inserted
            stats.append(stat)
    sqlite_connection.close()
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate selected tables from trading_bot SQLite database into MariaDB",
    )
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=Path("data/trading_bot.db"),
        help="Path to the legacy SQLite database (default: data/trading_bot.db)",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        choices=_TABLES,
        help="Optional subset of tables to migrate",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sqlite_path = args.sqlite
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite database not found: {sqlite_path}")

    tables = tuple(args.tables) if args.tables else _TABLES

    try:
        results = migrate(sqlite_path, tables)
    except MariaDBConfigurationError as exc:
        raise SystemExit(f"MariaDB connection failed: {exc}") from exc

    for stat in results:
        if stat.missing:
            print(f"{stat.table}: not found in SQLite, skipped")
        else:
            print(f"{stat.table}: migrated={stat.migrated} skipped={stat.skipped}")


if __name__ == "__main__":
    main()
