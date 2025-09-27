"""Market quote adapter converting quote feed updates into events."""

from __future__ import annotations

import asyncio
import time
from threading import Lock
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from loguru import logger

from ..cache import CacheBackend
from ..data_source_manager import (
    DataSourceAdapter,
    DataSourceError,
    DataSubscriptionRequest,
)
from ..event_bus import EventEnvelope
from ..intervals import IntervalTransformer
from ..persistence import PersistenceBackend
from ..quote_feed import QuoteFeed

__all__ = ["MarketQuoteAdapter"]


def _normalize_tick_payload(symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    ts_ns = payload.get("ts_ns")
    if not isinstance(ts_ns, int):
        ts_ns = time.time_ns()
    normalized = {
        "symbol": symbol,
        "last": float(payload.get("last", 0.0) or 0.0),
        "bid": float(payload.get("bid", 0.0) or 0.0),
        "ask": float(payload.get("ask", 0.0) or 0.0),
        "volume": float(payload.get("volume", 0.0) or 0.0),
        "market_state": payload.get("market_state", "unknown"),
        "ts_ns": ts_ns,
    }
    return normalized


class MarketQuoteAdapter(DataSourceAdapter):
    """Adapter bridging a quote feed into the unified data layer."""

    channel = "market.tick"

    def __init__(
        self,
        feed: QuoteFeed,
        event_bus,
        cache: CacheBackend,
        persistence: PersistenceBackend,
        *,
        intervals: Iterable[str] = ("1s", "5s", "1m"),
        candle_history: int = 300,
    ) -> None:
        super().__init__(event_bus)
        self._feed = feed
        self._cache = cache
        self._persistence = persistence
        self._transformer = IntervalTransformer(intervals)
        self._candle_history = max(1, int(candle_history))
        self._symbol_cancel: Dict[str, Callable[[], Optional[Awaitable[None]]]] = {}
        self._lock = Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._interval_keys = {interval.strip().lower() for interval in intervals}

    async def subscribe(self, request: DataSubscriptionRequest):  # type: ignore[override]
        loop = asyncio.get_running_loop()
        self._loop = loop
        symbol = (request.symbol or "").strip().upper()
        if not symbol:
            raise DataSourceError("Quote subscription requires symbol")

        def _on_tick(payload: Dict[str, Any], *, sym: str = symbol) -> None:
            self._dispatch_tick(sym, payload)

        cancel_cb = await self._feed.subscribe(symbol, _on_tick, options=request.options)
        with self._lock:
            self._symbol_cancel[symbol] = cancel_cb

        async def _cancel() -> None:
            with self._lock:
                cancel = self._symbol_cancel.pop(symbol, None)
                self._transformer.reset(symbol)
            if cancel is not None:
                maybe = cancel()
                if asyncio.iscoroutine(maybe):
                    await maybe

        return _cancel

    async def snapshot(self, request: DataSubscriptionRequest):  # type: ignore[override]
        symbol = (request.symbol or "").strip().upper()
        if not symbol:
            raise DataSourceError("Quote snapshot requires symbol")
        if request.interval:
            channel = self._candle_channel(request.interval)
            series = self._cache.get_series(channel, symbol, limit=1)
            return series[-1] if series else {}
        return self._cache.get_snapshot(self.channel, symbol)

    async def history(self, request: DataSubscriptionRequest):  # type: ignore[override]
        symbol = (request.symbol or "").strip().upper()
        if not symbol:
            raise DataSourceError("Quote history requires symbol")
        if request.interval:
            channel = self._candle_channel(request.interval)
            return self._cache.get_series(channel, symbol)
        return []

    def _dispatch_tick(self, symbol: str, payload: Dict[str, Any]) -> None:
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._process_tick, symbol, payload)
        else:
            self._process_tick(symbol, payload)

    def _process_tick(self, symbol: str, payload: Dict[str, Any]) -> None:
        normalized = _normalize_tick_payload(symbol, payload)
        ts_ns = normalized["ts_ns"]
        self._cache.set_snapshot(self.channel, symbol, normalized)

        envelope = EventEnvelope(
            event_type="market.tick",
            topic=self.channel,
            symbol=symbol,
            source="ibkr",
            ts_ns=ts_ns,
            payload=dict(normalized),
            meta={"channel": self.channel},
        )
        try:
            self.event_bus.publish_nowait(envelope)
        except RuntimeError:
            asyncio.run(self.event_bus.publish(envelope))
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.exception("Failed to publish market tick: {}", exc)

        try:
            self._persistence.write_event(envelope)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.exception("Failed to persist market tick: {}", exc)

        emissions = self._transformer.update(
            symbol, price=normalized["last"], volume=normalized["volume"], ts_ns=ts_ns
        )
        for interval, candle in emissions:
            channel = self._candle_channel(interval)
            candle_payload = dict(candle)
            candle_payload["symbol"] = symbol
            self._cache.append_series(channel, symbol, candle_payload, limit=self._candle_history)

            candle_envelope = EventEnvelope(
                event_type="market.candle",
                topic=channel,
                symbol=symbol,
                source="ibkr",
                ts_ns=candle_payload["interval_end_ns"],
                payload=dict(candle_payload),
                meta={"interval": interval},
            )
            try:
                self.event_bus.publish_nowait(candle_envelope)
            except RuntimeError:
                asyncio.run(self.event_bus.publish(candle_envelope))
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.exception("Failed to publish candle event: {}", exc)

            try:
                self._persistence.write_event(candle_envelope)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.exception("Failed to persist candle event: {}", exc)

    def _candle_channel(self, interval: str) -> str:
        interval_key = interval.strip().lower()
        if interval_key not in self._interval_keys:
            raise DataSourceError(f"Unsupported interval: {interval}")
        return f"market.candle.{interval_key}"
