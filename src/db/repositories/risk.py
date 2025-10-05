"""DB-API repositories for risk configuration snapshots."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from src.db.async_support import run_in_thread
from src.db.mariadb import MariaDBConfigurationError
from src.ibbot.logging import get_logger

from .base import (
    RepositoryError,
    RepositoryValidationError,
    executemany_write,
    is_sqlite,
    json_dumps,
    use_connection,
)

__all__ = [
    "RiskSnapshotRepository",
    "RiskSnapshotRepositoryError",
    "RiskRulesRepository",
    "RiskRulesRepositoryError",
]


class RiskSnapshotRepositoryError(RepositoryError):
    """Raised when persisting risk snapshots fails."""


class RiskRulesRepositoryError(RepositoryError):
    """Raised when persisting risk rules fails."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _normalise_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalised: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, datetime):
            normalised[str(key)] = value.isoformat()
        elif isinstance(value, Mapping):
            normalised[str(key)] = _normalise_payload(value)
        elif isinstance(value, (list, tuple, set)):
            normalised[str(key)] = [
                item.isoformat() if isinstance(item, datetime) else item for item in value
            ]
        else:
            normalised[str(key)] = value
    return normalised


def _prepare_json(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    payload = dict(row)
    if payload.get(key) is not None:
        payload[key] = json_dumps(payload[key])
    return payload


class RiskSnapshotRepository:
    """Persist risk configuration and rule snapshots."""

    def __init__(self, *, logger_name: str = "db.risk") -> None:
        self._logger = get_logger(logger_name)
        self._disabled_reason: str | None = None

    def _is_disabled(self) -> bool:
        if self._disabled_reason is None:
            return False
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                "Skipping risk snapshot persistence; disabled: %s",
                self._disabled_reason,
            )
        return True

    def _disable(self, reason: str) -> None:
        if self._disabled_reason is not None:
            return
        self._disabled_reason = reason or "MariaDB configuration error"
        self._logger.warning(
            "Disabling risk snapshot persistence: %s",
            self._disabled_reason,
        )

    async def save_rule_snapshots(
        self,
        *,
        service: str,
        rules: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        updated_at: str | datetime | None = None,
        captured_at: datetime | str | None = None,
    ) -> None:
        timestamp = _parse_datetime(captured_at) or _utc_now()
        rows: list[dict[str, Any]] = []
        if isinstance(rules, Mapping):
            for symbol, payload in rules.items():
                rows.append(
                    {
                        "service": service,
                        "symbol": str(symbol),
                        "payload": _normalise_payload(payload),
                        "updated_at_text": str(updated_at) if updated_at is not None else None,
                        "captured_at": timestamp,
                    }
                )
        else:
            for entry in rules:
                symbol = entry.get("symbol")
                payload = entry.get("payload")
                if symbol is None or not isinstance(payload, Mapping):
                    raise RepositoryValidationError(
                        "Rule entries must include 'symbol' and mapping payload"
                    )
                rows.append(
                    {
                        "service": service,
                        "symbol": str(symbol),
                        "payload": _normalise_payload(payload),
                        "updated_at_text": str(entry.get("updated_at", updated_at))
                        if entry.get("updated_at", updated_at) is not None
                        else None,
                        "captured_at": _parse_datetime(entry.get("captured_at")) or timestamp,
                    }
                )
        if not rows:
            return

        if self._is_disabled():
            return
        try:
            await run_in_thread(self._write_rule_snapshots, rows)
        except RepositoryValidationError:
            raise
        except MariaDBConfigurationError as exc:
            self._disable(str(exc))
            return
        except Exception as exc:  # pragma: no cover - log and wrap
            self._logger.exception(
                "Failed to persist risk rule snapshots for service %s",
                service,
            )
            raise RiskSnapshotRepositoryError("Failed to persist risk rule snapshots") from exc

    def _write_rule_snapshots(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with use_connection() as connection:
            prepared_rows = [_prepare_json(row, "payload") for row in rows]
            columns = list(prepared_rows[0].keys())
            placeholders = (
                ", ".join(f":{col}" for col in columns)
                if is_sqlite(connection)
                else ", ".join(f"%({col})s" for col in columns)
            )
            sql = "INSERT INTO risk_rule_snapshots ({cols}) VALUES ({values})".format(
                cols=", ".join(columns), values=placeholders
            )
            executemany_write(connection, sql, prepared_rows)

class RiskRulesRepository:
    """Persist the active set of risk rules."""

    _COLUMNS = (
        "symbol",
        "stop_loss_price",
        "take_profit_price",
        "max_loss_percent",
        "enabled",
        "max_time_span",
        "timestamp",
        "auto_trailing",
        "atr_params",
        "rule_type",
    )

    def __init__(self, *, logger_name: str = "db.risk_rules") -> None:
        self._logger = get_logger(logger_name)
        self._disabled_reason: str | None = None

    def _is_disabled(self) -> bool:
        if self._disabled_reason is None:
            return False
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                "Skipping risk rules persistence; disabled: %s",
                self._disabled_reason,
            )
        return True

    def _disable(self, reason: str) -> None:
        if self._disabled_reason is not None:
            return
        self._disabled_reason = reason or "MariaDB configuration error"
        self._logger.warning("Disabling risk rules persistence: %s", self._disabled_reason)

    async def replace_rules(
        self,
        rules: Mapping[str, Mapping[str, Any]],
        *,
        timestamp: str | datetime | None = None,
    ) -> None:
        if self._is_disabled():
            return

        prepared = self._prepare_rows(rules, timestamp)

        try:
            await run_in_thread(self._replace_rules, prepared)
        except RepositoryValidationError:
            raise
        except MariaDBConfigurationError as exc:
            self._disable(str(exc))
        except Exception as exc:  # pragma: no cover - log and wrap
            self._logger.exception("Failed to persist risk rules")
            raise RiskRulesRepositoryError("Failed to persist risk rules") from exc

    def _prepare_rows(
        self,
        rules: Mapping[str, Mapping[str, Any]],
        timestamp: str | datetime | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        default_timestamp = _parse_datetime(timestamp) or _utc_now()
        for symbol, payload in rules.items():
            if not isinstance(payload, Mapping):
                raise RepositoryValidationError("risk rule payload must be a mapping")
            symbol_str = str(symbol).strip()
            if not symbol_str:
                raise RepositoryValidationError("risk rule requires a symbol")
            prepared: dict[str, Any] = {
                "symbol": symbol_str,
                "stop_loss_price": _coerce_float(
                    payload.get("stop_loss_price"), payload.get("stop_loss")
                ),
                "take_profit_price": _coerce_float(
                    payload.get("take_profit_price"), payload.get("take_profit")
                ),
                "max_loss_percent": _coerce_float(payload.get("max_loss_percent")),
                "enabled": _coerce_bool(payload.get("enabled"), default=True),
                "max_time_span": _coerce_int(payload.get("max_time_span")),
                "timestamp": _coerce_timestamp(
                    payload.get("updated_at")
                    or payload.get("timestamp")
                    or default_timestamp
                ),
                "auto_trailing": _coerce_bool(payload.get("auto_trailing"), default=False),
                "atr_params": _coerce_atr_params(payload.get("atr_params")),
                "rule_type": str(payload.get("rule_type") or "fixed"),
            }
            rows.append(prepared)
        return rows

    def _replace_rules(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with use_connection() as connection:
            sqlite = is_sqlite(connection)
            cursor = connection.cursor()
            try:
                try:
                    cursor.execute("SELECT symbol FROM risk_rules")
                    existing_symbols = {str(row[0]) for row in cursor.fetchall()}
                except Exception:
                    existing_symbols = set()

                new_symbols = {str(row["symbol"]) for row in rows}
                obsolete = existing_symbols - new_symbols

                if obsolete:
                    delete_sql, delete_params = _build_symbol_delete(obsolete, sqlite)
                    cursor.execute(delete_sql, delete_params)
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive
                    pass

            if rows:
                insert_sql = _build_upsert_sql(sqlite)
                executemany_write(connection, insert_sql, rows)
            else:
                try:
                    connection.commit()
                except Exception:
                    try:
                        connection.rollback()
                    except Exception:  # pragma: no cover - defensive
                        pass
                    raise
        # executemany_write commits on success. If rows is empty we commit manually above.


def _coerce_float(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, *, default: bool) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (None, ""):
        return int(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return 1
    if text in {"0", "false", "f", "no", "n"}:
        return 0
    return int(default)


def _coerce_timestamp(value: Any) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        parsed = _utc_now()
    return parsed.isoformat()


def _coerce_atr_params(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        return value if isinstance(value, str) else value.decode("utf-8", errors="ignore")
    if isinstance(value, Mapping):
        return json_dumps({str(k): v for k, v in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return json_dumps(list(value))
    return json_dumps(value)


def _build_symbol_delete(
    symbols: set[str], sqlite: bool
) -> tuple[str, Sequence[Any]]:
    placeholders: list[str] = []
    params: list[Any] = []
    for symbol in symbols:
        placeholders.append("?" if sqlite else "%s")
        params.append(symbol)
    sql = "DELETE FROM risk_rules WHERE symbol IN ({})".format(", ".join(placeholders))
    return sql, tuple(params)


def _build_upsert_sql(sqlite: bool) -> str:
    columns = list(RiskRulesRepository._COLUMNS)
    column_list = ", ".join(columns)
    if sqlite:
        values = ", ".join(f":{col}" for col in columns)
        updates = ", ".join(f"{col}=excluded.{col}" for col in columns if col != "symbol")
        return (
            "INSERT INTO risk_rules ({cols}) VALUES ({values}) "
            "ON CONFLICT(symbol) DO UPDATE SET {updates}"
        ).format(cols=column_list, values=values, updates=updates)
    values = ", ".join(f"%({col})s" for col in columns)
    updates = ", ".join(f"{col}=VALUES({col})" for col in columns if col != "symbol")
    return (
        "INSERT INTO risk_rules ({cols}) VALUES ({values}) "
        "ON DUPLICATE KEY UPDATE {updates}"
    ).format(cols=column_list, values=values, updates=updates)
