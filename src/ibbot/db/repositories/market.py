"""Repository helpers for ``market_prices`` records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.ibbot.logging import get_logger

from ..schemas import MarketPrice

__all__ = ["MariaDbMarketPriceRepository"]


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


class MariaDbMarketPriceRepository:
    """Persist best-bid-offer samples to the ``market_prices`` table."""

    def __init__(self, session: AsyncSession, *, logger_name: str = "db.market") -> None:
        self._session = session
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
            await self._session.execute(insert(MarketPrice).values(**payload))
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            self._logger.exception("Failed to insert market price", symbol=symbol)
            raise
