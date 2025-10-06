"""Repository helpers for the ``config`` key/value table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from src.db.async_support import run_in_thread
from src.db.mariadb import MariaDBConfigurationError
from src.ibbot.logging import get_logger

from .base import RepositoryError, format_datetime_for_db, is_sqlite, use_connection

__all__ = [
    "ConfigRepository",
    "ConfigRepositoryError",
    "ModuleConfigSnapshot",
]


class ConfigRepositoryError(RepositoryError):
    """Raised when configuration persistence fails."""


@dataclass(slots=True)
class ModuleConfigSnapshot:
    """Container describing a module configuration snapshot."""

    module: str
    values: dict[str, str]
    version: str | None


def _utc_now_text() -> str:
    """Return the current UTC timestamp formatted for DATETIME columns."""

    return format_datetime_for_db(datetime.now(timezone.utc))


class ConfigRepository:
    """Persist and load configuration overrides from the database."""

    def __init__(self, *, logger_name: str = "db.config") -> None:
        self._logger = get_logger(logger_name)

    async def load_module(self, module: str) -> ModuleConfigSnapshot:
        """Return the current overrides for *module*."""

        try:
            return await run_in_thread(self._load_module, module)
        except MariaDBConfigurationError as exc:
            raise ConfigRepositoryError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to load configuration", module=module)
            raise ConfigRepositoryError("Failed to load configuration") from exc

    async def save_module(self, module: str, values: Mapping[str, str]) -> ModuleConfigSnapshot:
        """Persist the provided *values* for *module*."""

        try:
            return await run_in_thread(self._write_module, module, dict(values))
        except MariaDBConfigurationError as exc:
            raise ConfigRepositoryError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to persist configuration", module=module)
            raise ConfigRepositoryError("Failed to persist configuration") from exc

    async def load_versions(self) -> dict[str, str]:
        """Return the latest version marker for all modules."""

        try:
            return await run_in_thread(self._load_versions)
        except MariaDBConfigurationError as exc:
            raise ConfigRepositoryError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to load configuration versions")
            raise ConfigRepositoryError("Failed to load configuration versions") from exc

    # ------------------------------------------------------------------
    # internal helpers executed within a thread
    # ------------------------------------------------------------------
    def _load_module(self, module: str) -> ModuleConfigSnapshot:
        module = module.strip()
        if not module:
            raise ConfigRepositoryError("module name is required")

        like_pattern = f"{module}:%"
        values: dict[str, str] = {}
        version: str | None = None

        with use_connection() as connection:
            cursor = connection.cursor()
            try:
                if is_sqlite(connection):
                    cursor.execute(
                        "SELECT config_key, config_value FROM config WHERE config_key LIKE ?",
                        (like_pattern,),
                    )
                else:
                    cursor.execute(
                        "SELECT config_key, config_value FROM config WHERE config_key LIKE %s",
                        (like_pattern,),
                    )
                rows = cursor.fetchall() or []
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass

        for row in rows:
            if isinstance(row, Mapping):
                key = row.get("config_key") or row.get("CONFIG_KEY")
                value = row.get("config_value") or row.get("CONFIG_VALUE")
            else:
                key = row[0]
                value = row[1]
            if key is None:
                continue
            text_key = str(key)
            if ":" not in text_key:
                continue
            module_name, raw_key = text_key.split(":", 1)
            if module_name != module:
                continue
            if raw_key == "__version__":
                version = None if value is None else str(value)
                continue
            if value is None:
                continue
            values[raw_key] = str(value)

        return ModuleConfigSnapshot(module=module, values=values, version=version)

    def _write_module(self, module: str, values: Mapping[str, str]) -> ModuleConfigSnapshot:
        module = module.strip()
        if not module:
            raise ConfigRepositoryError("module name is required")

        timestamp = _utc_now_text()
        prepared: dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key = str(raw_key).strip()
            if not key:
                continue
            prepared[key] = str(raw_value)

        with use_connection() as connection:
            cursor = connection.cursor()
            try:
                like_pattern = f"{module}:%"
                if is_sqlite(connection):
                    cursor.execute(
                        "DELETE FROM config WHERE config_key LIKE ?",
                        (like_pattern,),
                    )
                else:
                    cursor.execute(
                        "DELETE FROM config WHERE config_key LIKE %s",
                        (like_pattern,),
                    )

                rows = [
                    (f"{module}:{key}", value, timestamp, timestamp)
                    for key, value in prepared.items()
                ]
                rows.append((f"{module}:__version__", timestamp, timestamp, timestamp))

                if is_sqlite(connection):
                    insert_sql = (
                        "INSERT INTO config (config_key, config_value, description, created_at, updated_at) "
                        "VALUES (?, ?, NULL, ?, ?)"
                    )
                else:
                    insert_sql = (
                        "INSERT INTO config (config_key, config_value, description, created_at, updated_at) "
                        "VALUES (%s, %s, NULL, %s, %s)"
                    )

                cursor.executemany(insert_sql, rows)
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

        return ModuleConfigSnapshot(module=module, values=prepared, version=timestamp)

    def _load_versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}

        with use_connection() as connection:
            cursor = connection.cursor()
            try:
                if is_sqlite(connection):
                    cursor.execute(
                        "SELECT config_key, config_value FROM config WHERE config_key LIKE ?",
                        ("%:__version__",),
                    )
                else:
                    cursor.execute(
                        "SELECT config_key, config_value FROM config WHERE config_key LIKE %s",
                        ("%:__version__",),
                    )
                rows = cursor.fetchall() or []
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass

        for row in rows:
            if isinstance(row, Mapping):
                key = row.get("config_key") or row.get("CONFIG_KEY")
                value = row.get("config_value") or row.get("CONFIG_VALUE")
            else:
                key = row[0]
                value = row[1]
            if key is None or value is None:
                continue
            text_key = str(key)
            if ":" not in text_key:
                continue
            module, suffix = text_key.split(":", 1)
            if suffix != "__version__":
                continue
            versions[module] = str(value)

        return versions
