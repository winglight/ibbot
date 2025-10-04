"""DB-API helpers for persisting market price samples."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from src.db.async_support import run_in_thread
from src.db.mariadb import MariaDBConfigurationError
from src.ibbot.logging import get_logger

from .base import RepositoryError, execute_write, is_sqlite, json_dumps, use_connection

__all__ = ["MarketPriceRepository", "MarketPriceRepositoryError"]


class MarketPriceRepositoryError(RepositoryError):
    """Raised when persisting market prices fails."""


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
    if isinstance(value, (int, float)):
        if value > 1_000_000_000_000:
            return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _normalise_extra(extra: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if extra is None:
        return None
    payload: dict[str, Any] = {}
    for key, value in extra.items():
        if isinstance(value, datetime):
            payload[str(key)] = value.isoformat()
        else:
            payload[str(key)] = value
    return payload or None


class MarketPriceRepository:
    """Persist BBO snapshots to ``market_prices``."""

    def __init__(self, *, logger_name: str = "db.market") -> None:
        self._logger = get_logger(logger_name)

    async def insert_price(
        self,
        *,
        symbol: str,
        bid: float | None,
        ask: float | None,
        bid_size: float | None = None,
        ask_size: float | None = None,
        source: str | None = None,
        observed_at: datetime | str | int | float | None = None,
        recorded_at: datetime | str | int | float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        timestamp = _parse_datetime(observed_at) or _parse_datetime(recorded_at) or _utc_now()
        payload = {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "source": source,
            "observed_at": timestamp,
            "extra": _normalise_extra(extra),
        }
        try:
            await run_in_thread(self._write_price, payload)
        except MariaDBConfigurationError as exc:
            raise MarketPriceRepositoryError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - log and wrap
            self._logger.exception("Failed to insert market price for %s", symbol)
            raise MarketPriceRepositoryError("Failed to insert market price") from exc

    def _write_price(self, row: Mapping[str, Any]) -> None:
        with use_connection() as connection:
            prepared = dict(row)
            if prepared.get("extra") is not None:
                prepared["extra"] = json_dumps(prepared["extra"])

            columns = list(prepared.keys())
            placeholders = (
                ", ".join(f":{col}" for col in columns)
                if is_sqlite(connection)
                else ", ".join(f"%({col})s" for col in columns)
            )
            sql = "INSERT INTO market_prices ({cols}) VALUES ({values})".format(
                cols=", ".join(columns), values=placeholders
            )
            execute_write(connection, sql, prepared)
