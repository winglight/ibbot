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

__all__ = ["RiskSnapshotRepository", "RiskSnapshotRepositoryError"]


class RiskSnapshotRepositoryError(RepositoryError):
    """Raised when persisting risk snapshots fails."""


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

    async def bulk_upsert_rule_snapshots(
        self,
        *,
        service: str,
        rules: Mapping[str, Mapping[str, Any]],
        updated_at: str | datetime | None = None,
        captured_at: datetime | str | None = None,
    ) -> None:
        if not rules:
            return

        timestamp = _parse_datetime(captured_at) or _utc_now()
        rows: list[dict[str, Any]] = []
        symbols: list[str] = []

        for symbol, payload in rules.items():
            symbol_str = str(symbol)
            if not symbol_str:
                continue
            entry_payload = dict(payload)
            entry_payload.pop("symbol", None)
            entry_updated = entry_payload.get("updated_at", updated_at)
            entry_captured = entry_payload.get("captured_at")
            rows.append(
                {
                    "service": service,
                    "symbol": symbol_str,
                    "payload": _normalise_payload(entry_payload),
                    "updated_at_text": str(entry_updated) if entry_updated is not None else None,
                    "captured_at": _parse_datetime(entry_captured) or timestamp,
                }
            )
            symbols.append(symbol_str)

        if not rows:
            return

        if self._is_disabled():
            return
        try:
            await run_in_thread(self._bulk_upsert_rules, service, rows, symbols)
        except MariaDBConfigurationError as exc:
            self._disable(str(exc))
            return
        except Exception as exc:  # pragma: no cover - log and wrap
            self._logger.exception(
                "Failed to upsert risk rule snapshots for service %s (count=%d)",
                service,
                len(rows),
            )
            raise RiskSnapshotRepositoryError("Failed to upsert risk rule snapshots") from exc

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

    def _bulk_upsert_rules(
        self,
        service: str,
        rows: Sequence[Mapping[str, Any]],
        symbols: Sequence[str],
    ) -> None:
        with use_connection() as connection:
            sqlite = is_sqlite(connection)
            delete_params: dict[str, Any] = {"service": service}
            symbol_placeholders: list[str] = []
            for index, symbol in enumerate(symbols):
                key = f"symbol_{index}"
                delete_params[key] = symbol
                symbol_placeholders.append(f":{key}" if sqlite else f"%({key})s")

            if not symbol_placeholders:
                return

            delete_sql = (
                "DELETE FROM risk_rule_snapshots WHERE service = {service} AND symbol IN ({symbols})"
            ).format(
                service=":service" if sqlite else "%(service)s",
                symbols=", ".join(symbol_placeholders),
            )

            insert_rows = [_prepare_json(row, "payload") for row in rows]
            columns = list(insert_rows[0].keys())
            placeholders = (
                ", ".join(f":{col}" for col in columns)
                if sqlite
                else ", ".join(f"%({col})s" for col in columns)
            )
            insert_sql = "INSERT INTO risk_rule_snapshots ({cols}) VALUES ({values})".format(
                cols=", ".join(columns), values=placeholders
            )

            cursor = connection.cursor()
            try:
                cursor.execute(delete_sql, delete_params)
                cursor.executemany(insert_sql, insert_rows)
            except Exception:
                try:
                    connection.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
                raise
            else:
                connection.commit()
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive
                    pass
