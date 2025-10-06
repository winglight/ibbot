"""Repositories for persisting strategy metadata."""

from __future__ import annotations
"""Repositories for persisting strategy metadata."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from src.db.async_support import run_in_thread
from src.ibbot.logging import get_logger

from .base import (
    RepositoryError,
    execute_write,
    is_sqlite,
    json_dumps,
    use_connection,
)

__all__ = ["StrategyRepository", "StrategyRepositoryError", "StrategyRecord"]


class StrategyRepositoryError(RepositoryError):
    """Raised when strategy persistence operations fail."""


@dataclass(slots=True)
class StrategyRecord:
    """Representation of a strategy row stored in the database."""

    name: str
    strategy_type: str | None
    params: dict[str, Any]
    metadata: dict[str, Any]
    description: str | None
    enabled: bool
    created_at: str
    updated_at: str
    enabled_at: str | None
    disabled_at: str | None

    def to_payload(self) -> dict[str, Any]:
        """Return a dictionary payload matching :class:`StrategyDocument`."""

        return {
            "name": self.name,
            "strategy_type": self.strategy_type,
            "params": dict(self.params),
            "metadata": dict(self.metadata),
            "description": self.description,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "enabled_at": self.enabled_at,
            "disabled_at": self.disabled_at,
        }


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialise_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        timestamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return timestamp.isoformat()
    text = str(value)
    return text or None


def _parse_json_column(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return {}
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, Mapping):
            return {str(k): v for k, v in decoded.items()}
    return {}


class StrategyRepository:
    """Persist strategy definitions to the ``strategies`` table."""

    _COLUMNS = (
        "name",
        "strategy_type",
        "params",
        "metadata",
        "description",
        "enabled",
        "created_at",
        "updated_at",
        "enabled_at",
        "disabled_at",
    )

    def __init__(self, *, logger_name: str = "db.strategy") -> None:
        self._logger = get_logger(logger_name)

    async def list_strategies(self) -> list[StrategyRecord]:
        try:
            return await run_in_thread(self._list_strategies)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to list strategies")
            raise StrategyRepositoryError("Failed to list strategies") from exc

    async def get_strategy(self, name: str) -> StrategyRecord | None:
        try:
            return await run_in_thread(self._get_strategy, name)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to load strategy", strategy=name)
            raise StrategyRepositoryError("Failed to load strategy") from exc

    async def upsert_strategy(
        self, payload: Mapping[str, Any]
    ) -> tuple[StrategyRecord, bool]:
        try:
            return await run_in_thread(self._upsert_strategy, dict(payload))
        except StrategyRepositoryError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to upsert strategy", payload=payload)
            raise StrategyRepositoryError("Failed to persist strategy") from exc

    async def update_strategy(
        self,
        name: str,
        updates: Mapping[str, Any],
    ) -> StrategyRecord:
        try:
            return await run_in_thread(self._update_strategy, name, dict(updates))
        except StrategyRepositoryError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to update strategy", strategy=name)
            raise StrategyRepositoryError("Failed to update strategy") from exc

    async def delete_strategy(self, name: str) -> bool:
        try:
            return await run_in_thread(self._delete_strategy, name)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to delete strategy", strategy=name)
            raise StrategyRepositoryError("Failed to delete strategy") from exc

    async def rename_strategy(self, source: str, target: str) -> StrategyRecord:
        try:
            return await run_in_thread(self._rename_strategy, source, target)
        except StrategyRepositoryError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception(
                "Failed to rename strategy", source=source, target=target
            )
            raise StrategyRepositoryError("Failed to rename strategy") from exc

    async def set_strategy_enabled(self, name: str, *, enabled: bool) -> StrategyRecord:
        try:
            return await run_in_thread(self._set_strategy_enabled, name, bool(enabled))
        except StrategyRepositoryError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to toggle strategy", strategy=name)
            raise StrategyRepositoryError("Failed to toggle strategy") from exc

    async def reset_strategy(self, name: str) -> StrategyRecord:
        try:
            return await run_in_thread(self._reset_strategy, name)
        except StrategyRepositoryError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to reset strategy", strategy=name)
            raise StrategyRepositoryError("Failed to reset strategy") from exc

    async def aclose(self) -> None:
        """Compatibility hook for dependency clean-up."""

    # ------------------------------------------------------------------
    # Internal helpers executed within a worker thread
    # ------------------------------------------------------------------
    def _list_strategies(self) -> list[StrategyRecord]:
        with use_connection() as connection:
            cursor = connection.cursor()
            try:
                column_list = ", ".join(self._COLUMNS)
                cursor.execute(f"SELECT {column_list} FROM strategies ORDER BY name")
                rows = cursor.fetchall() or []
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass

        return [self._row_to_record(row) for row in rows]

    def _get_strategy(self, name: str) -> StrategyRecord | None:
        name = str(name).strip()
        if not name:
            raise StrategyRepositoryError("strategy name is required")

        with use_connection() as connection:
            return self._fetch_one(connection, name)

    def _upsert_strategy(
        self, payload: Mapping[str, Any]
    ) -> tuple[StrategyRecord, bool]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise StrategyRepositoryError("strategy name is required")

        with use_connection() as connection:
            existing = self._fetch_one(connection, name)
            created = existing is None
            prepared = self._prepare_payload(payload, existing)

            if created:
                self._insert_row(connection, prepared)
            else:
                self._update_row(connection, name, prepared)

            record = self._fetch_one(connection, name)

        if record is None:  # pragma: no cover - defensive guard
            raise StrategyRepositoryError("Failed to persist strategy")
        return record, created

    def _update_strategy(self, name: str, updates: Mapping[str, Any]) -> StrategyRecord:
        name = str(name).strip()
        if not name:
            raise StrategyRepositoryError("strategy name is required")

        with use_connection() as connection:
            existing = self._fetch_one(connection, name)
            if existing is None:
                raise StrategyRepositoryError(f"unknown strategy: {name}")
            prepared = self._prepare_payload({**existing.to_payload(), **updates}, existing)
            self._update_row(connection, name, prepared)
            record = self._fetch_one(connection, name)

        if record is None:  # pragma: no cover - defensive guard
            raise StrategyRepositoryError("Failed to refresh updated strategy")
        return record

    def _delete_strategy(self, name: str) -> bool:
        name = str(name).strip()
        if not name:
            raise StrategyRepositoryError("strategy name is required")

        with use_connection() as connection:
            placeholder = "?" if is_sqlite(connection) else "%s"
            sql = f"DELETE FROM strategies WHERE name = {placeholder}"
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (name,))
                affected = cursor.rowcount or 0
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
                raise
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
        return affected > 0

    def _rename_strategy(self, source: str, target: str) -> StrategyRecord:
        source = str(source).strip()
        target = str(target).strip()
        if not source or not target:
            raise StrategyRepositoryError("strategy name must not be empty")

        with use_connection() as connection:
            if source == target:
                record = self._fetch_one(connection, source)
                if record is None:
                    raise StrategyRepositoryError(f"unknown strategy: {source}")
                return record

            params = (target, _utc_now_text(), source)
            if is_sqlite(connection):
                sql = "UPDATE strategies SET name = ?, updated_at = ? WHERE name = ?"
            else:
                sql = "UPDATE strategies SET name = %s, updated_at = %s WHERE name = %s"
            execute_write(connection, sql, params)
            record = self._fetch_one(connection, target)

        if record is None:
            raise StrategyRepositoryError("Failed to rename strategy")
        return record

    def _set_strategy_enabled(self, name: str, enabled: bool) -> StrategyRecord:
        name = str(name).strip()
        if not name:
            raise StrategyRepositoryError("strategy name is required")

        with use_connection() as connection:
            existing = self._fetch_one(connection, name)
            if existing is None:
                raise StrategyRepositoryError(f"unknown strategy: {name}")

            now = _utc_now_text()
            updates: dict[str, Any] = {"enabled": bool(enabled)}
            if enabled:
                updates.setdefault(
                    "enabled_at",
                    existing.enabled_at if existing.enabled else now,
                )
                updates.setdefault("disabled_at", None)
            else:
                updates.setdefault("disabled_at", now)
                updates.setdefault("enabled_at", existing.enabled_at)

            prepared = self._prepare_payload({**existing.to_payload(), **updates}, existing)
            self._update_row(connection, name, prepared)
            record = self._fetch_one(connection, name)

        if record is None:
            raise StrategyRepositoryError("Failed to toggle strategy state")
        return record

    def _reset_strategy(self, name: str) -> StrategyRecord:
        name = str(name).strip()
        if not name:
            raise StrategyRepositoryError("strategy name is required")

        with use_connection() as connection:
            existing = self._fetch_one(connection, name)
            if existing is None:
                raise StrategyRepositoryError(f"unknown strategy: {name}")
            now = _utc_now_text()
            updates = {
                "params": {},
                "metadata": {},
                "enabled": True,
                "enabled_at": now,
                "disabled_at": None,
            }
            prepared = self._prepare_payload({**existing.to_payload(), **updates}, existing)
            self._update_row(connection, name, prepared)
            record = self._fetch_one(connection, name)

        if record is None:
            raise StrategyRepositoryError("Failed to reset strategy")
        return record

    # ------------------------------------------------------------------
    # Row handling helpers
    # ------------------------------------------------------------------
    def _fetch_one(self, connection: Any, name: str) -> StrategyRecord | None:
        placeholder = "?" if is_sqlite(connection) else "%s"
        column_list = ", ".join(self._COLUMNS)
        sql = f"SELECT {column_list} FROM strategies WHERE name = {placeholder}"
        cursor = connection.cursor()
        try:
            cursor.execute(sql, (name,))
            row = cursor.fetchone()
        finally:
            try:
                cursor.close()
            except Exception:  # pragma: no cover - defensive cleanup
                pass
        if not row:
            return None
        return self._row_to_record(row)

    def _row_to_record(self, row: Mapping[str, Any] | Sequence[Any]) -> StrategyRecord:
        if isinstance(row, Mapping):
            accessor = row.get
        else:
            accessor = lambda key: row[self._COLUMNS.index(key)]

        params = _parse_json_column(accessor("params"))
        metadata = _parse_json_column(accessor("metadata"))

        enabled_raw = accessor("enabled")
        enabled = bool(int(enabled_raw)) if isinstance(enabled_raw, (int, bool)) else bool(enabled_raw)

        created_at = _serialise_datetime(accessor("created_at")) or _utc_now_text()
        updated_at = _serialise_datetime(accessor("updated_at")) or created_at
        enabled_at = _serialise_datetime(accessor("enabled_at"))
        disabled_at = _serialise_datetime(accessor("disabled_at"))

        strategy_type_raw = accessor("strategy_type")
        description = accessor("description")

        return StrategyRecord(
            name=str(accessor("name")),
            strategy_type=(
                str(strategy_type_raw) if strategy_type_raw not in {None, ""} else None
            ),
            params=params,
            metadata=metadata,
            description=str(description) if description not in {None, ""} else None,
            enabled=enabled,
            created_at=created_at,
            updated_at=updated_at,
            enabled_at=enabled_at,
            disabled_at=disabled_at,
        )

    def _prepare_payload(
        self, payload: Mapping[str, Any], existing: StrategyRecord | None
    ) -> dict[str, Any]:
        now = _utc_now_text()

        name = str(payload.get("name") or "").strip()
        if not name:
            raise StrategyRepositoryError("strategy name is required")

        strategy_type_value = payload.get("strategy_type")
        strategy_type = (
            str(strategy_type_value).strip() if strategy_type_value is not None else None
        )
        if strategy_type == "":
            strategy_type = None

        description_value = payload.get("description")
        description = (
            str(description_value).strip() if isinstance(description_value, str) else None
        )
        if description == "":
            description = None

        params_payload = payload.get("params")
        params = (
            {str(k): v for k, v in params_payload.items()}
            if isinstance(params_payload, Mapping)
            else existing.params if existing is not None else {}
        )

        metadata_payload = payload.get("metadata")
        metadata = (
            {str(k): v for k, v in metadata_payload.items()}
            if isinstance(metadata_payload, Mapping)
            else existing.metadata if existing is not None else {}
        )

        enabled = bool(payload.get("enabled", existing.enabled if existing else True))

        enabled_at_payload = payload.get("enabled_at")
        disabled_at_payload = payload.get("disabled_at")

        enabled_at = (
            _serialise_datetime(enabled_at_payload)
            if enabled_at_payload is not None
            else existing.enabled_at if existing is not None else (now if enabled else None)
        )
        disabled_at = (
            _serialise_datetime(disabled_at_payload)
            if disabled_at_payload is not None
            else existing.disabled_at if existing is not None else (now if not enabled else None)
        )

        if enabled:
            disabled_at = None if disabled_at_payload is None else disabled_at
            enabled_at = enabled_at or now
        else:
            disabled_at = disabled_at or now

        created_at = existing.created_at if existing is not None else now

        return {
            "name": name,
            "strategy_type": strategy_type,
            "params": json_dumps(params),
            "metadata": json_dumps(metadata),
            "description": description,
            "enabled": 1 if enabled else 0,
            "created_at": created_at,
            "updated_at": now,
            "enabled_at": enabled_at,
            "disabled_at": disabled_at,
        }

    def _insert_row(self, connection: Any, payload: Mapping[str, Any]) -> None:
        params = (
            payload["name"],
            payload["strategy_type"],
            payload["params"],
            payload["metadata"],
            payload["description"],
            payload["enabled"],
            payload["enabled_at"],
            payload["disabled_at"],
            payload["created_at"],
            payload["updated_at"],
        )
        if is_sqlite(connection):
            sql = (
                "INSERT INTO strategies (name, strategy_type, params, metadata, description, enabled, "
                "enabled_at, disabled_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
        else:
            sql = (
                "INSERT INTO strategies (name, strategy_type, params, metadata, description, enabled, "
                "enabled_at, disabled_at, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
        execute_write(connection, sql, params)

    def _update_row(self, connection: Any, name: str, payload: Mapping[str, Any]) -> None:
        placeholder = "?" if is_sqlite(connection) else "%s"
        assignments = (
            "strategy_type = {0}, params = {0}, metadata = {0}, description = {0}, "
            "enabled = {0}, enabled_at = {0}, disabled_at = {0}, updated_at = {0}"
        ).format(placeholder)
        sql = f"UPDATE strategies SET {assignments} WHERE name = {placeholder}"
        params = (
            payload["strategy_type"],
            payload["params"],
            payload["metadata"],
            payload["description"],
            payload["enabled"],
            payload["enabled_at"],
            payload["disabled_at"],
            payload["updated_at"],
            name,
        )
        if is_sqlite(connection):
            sql = (
                "UPDATE strategies SET strategy_type = ?, params = ?, metadata = ?, description = ?, "
                "enabled = ?, enabled_at = ?, disabled_at = ?, updated_at = ? WHERE name = ?"
            )
        execute_write(connection, sql, params)

