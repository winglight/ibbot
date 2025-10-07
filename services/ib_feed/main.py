"""Entry point and runtime for the IB market data feed service."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Mapping, Sequence

from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter, get_market_data_writer

SERVICE_NAME = "ib-feed"
PRICE_CHANNEL_TEMPLATE = "pubsub:ib:price:{symbol}"
SNAPSHOT_KEY_TEMPLATE = "hash:market:snapshot:{symbol}"
DOM_CHANNEL_TEMPLATE = "pubsub:ib:dom:{symbol}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_awaitable(result: Awaitable[Any] | Any) -> Awaitable[Any]:
    if inspect.isawaitable(result):
        return result  # type: ignore[return-value]
    async def _wrap() -> Any:
        return result
    return _wrap()


@dataclass(slots=True)
class MarketDataFeedService:
    """Manage IB market data subscriptions and persist updates."""

    ib_client: Any
    redis: Any
    subscriptions: Sequence[str]
    dom_writer: MarketDataParquetWriter | None = None
    logger_name: str = "ibbot.services.ib_feed"
    reconnect_delay: float = 1.0
    health_service_name: str = SERVICE_NAME
    health_key: str | None = None
    health_interval: float = 5.0

    price_updates: int = 0
    dom_updates: int = 0

    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _stopping: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _reconnect_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _price_cache: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _dom_cache: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _logger: Any = field(default=None, init=False, repr=False)
    _active: bool = field(default=False, init=False, repr=False)
    _stop_health: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _health_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _last_price_updates: int = field(default=0, init=False, repr=False)
    _last_dom_updates: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._logger = get_logger(self.logger_name)
        if self.dom_writer is None:
            try:
                self.dom_writer = get_market_data_writer()
            except Exception:  # pragma: no cover - defensive guard
                if self._logger:
                    self._logger.warning("Falling back to no-op DOM writer", exc_info=True)

    async def start(self) -> None:
        """Start the service by connecting and subscribing to symbols."""

        self._loop = asyncio.get_running_loop()
        self._stopping.clear()
        self._stop_health.clear()
        self._last_price_updates = self.price_updates
        self._last_dom_updates = self.dom_updates
        try:
            await self._publish_health(status="starting", healthy=False)
            self._health_task = asyncio.create_task(
                self._health_loop(), name="ib-feed-health"
            )
            await self._publish_health(status="running", healthy=True)
            if not self.subscriptions:
                self._logger.info(
                    "No market data subscriptions configured; IB feed service running idle",
                )
                return

            self._bind_client_events()
            await self._connect_and_subscribe()
            self._active = True
        except Exception:
            self._active = False
            if self._health_task is not None:
                self._health_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._health_task
                self._health_task = None
            await self._publish_health(status="offline", healthy=False)
            raise

    async def stop(self) -> None:
        """Stop the service and cancel background tasks."""

        self._stopping.set()
        self._stop_health.set()
        if self._health_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_task
            self._health_task = None
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
        if self._active:
            disconnect = getattr(self.ib_client, "disconnect", None)
            if disconnect is not None:
                await _ensure_awaitable(disconnect())
        self._active = False
        await self._publish_health(status="offline", healthy=False)

    def _bind_client_events(self) -> None:
        price_handler = getattr(self.ib_client, "add_price_listener", None)
        dom_handler = getattr(self.ib_client, "add_dom_listener", None)
        disconnect_handler = getattr(self.ib_client, "add_disconnect_listener", None)

        if callable(price_handler):
            price_handler(self._handle_price_event)
        elif hasattr(self.ib_client, "on_price"):
            setattr(self.ib_client, "on_price", self._handle_price_event)
        else:
            raise AttributeError("IB client must expose add_price_listener or on_price callback")

        if callable(dom_handler):
            dom_handler(self._handle_dom_event)
        elif hasattr(self.ib_client, "on_dom"):
            setattr(self.ib_client, "on_dom", self._handle_dom_event)
        else:
            raise AttributeError("IB client must expose add_dom_listener or on_dom callback")

        if callable(disconnect_handler):
            disconnect_handler(self._handle_disconnect_event)
        elif hasattr(self.ib_client, "on_disconnect"):
            setattr(self.ib_client, "on_disconnect", self._handle_disconnect_event)

    async def _connect_and_subscribe(self) -> None:
        connect = getattr(self.ib_client, "connect", None)
        if connect is not None:
            await _ensure_awaitable(connect())
        subscribe = getattr(self.ib_client, "subscribe_market_data", None)
        if subscribe is None:
            raise AttributeError("IB client must implement subscribe_market_data")
        for symbol in self.subscriptions:
            await _ensure_awaitable(subscribe(symbol))
        if self.subscriptions:
            self._logger.info("Subscribed to %s", ", ".join(self.subscriptions))
        else:
            self._logger.info("No market data subscriptions configured")

    def _handle_price_event(self, symbol: str, update: Mapping[str, Any]) -> None:
        if not self._loop:
            raise RuntimeError("Service not started")
        coro = self.process_price_update(symbol, update)
        self._schedule(coro)

    def _handle_dom_event(self, symbol: str, levels: Sequence[Mapping[str, Any]]) -> None:
        if not self._loop:
            raise RuntimeError("Service not started")
        coro = self.process_dom_update(symbol, levels)
        self._schedule(coro)

    def _handle_disconnect_event(self) -> None:
        if not self._loop or self._stopping.is_set() or not self._active:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._logger.warning("IB client disconnected; scheduling reconnect")
        self._reconnect_task = self._loop.create_task(self._reconnect())

    def _schedule(self, coro: Awaitable[Any]) -> None:
        if not self._loop:
            raise RuntimeError("Event loop is not set")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            asyncio.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _reconnect(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self.reconnect_delay)
                await self._connect_and_subscribe()
                self._logger.info("Reconnected to IB and restored subscriptions")
                return
            except Exception:  # pragma: no cover - depends on IB client behaviour
                self._logger.warning("Reconnect attempt failed", exc_info=True)

    async def _health_loop(self) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(self._stop_health.wait(), timeout=self.health_interval)
                    break
                except asyncio.TimeoutError:
                    metrics = self._collect_health_metrics()
                    await self._publish_health(status="running", healthy=True, metrics=metrics)
        finally:
            metrics = self._collect_health_metrics()
            metrics["final"] = True
            await self._publish_health(status="running", healthy=True, metrics=metrics)

    def _collect_health_metrics(self) -> dict[str, Any]:
        metrics = {
            "price_updates_total": int(self.price_updates),
            "dom_updates_total": int(self.dom_updates),
            "price_updates_delta": int(self.price_updates - self._last_price_updates),
            "dom_updates_delta": int(self.dom_updates - self._last_dom_updates),
            "subscriptions": len(self.subscriptions),
            "active": self._active,
        }
        self._last_price_updates = self.price_updates
        self._last_dom_updates = self.dom_updates
        return metrics

    async def _publish_health(
        self,
        *,
        status: str | None = None,
        healthy: bool | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> None:
        redis = getattr(self, "redis", None)
        if redis is None:
            return
        try:
            await update_service_health(
                redis,
                self.health_service_name,
                status=status,
                healthy=healthy,
                metrics=metrics,
                key=self.health_key,
                logger=self._logger,
            )
        except Exception:
            if self._logger:
                self._logger.warning("Failed to publish IB feed health", exc_info=True)

    async def process_price_update(self, symbol: str, update: Mapping[str, Any]) -> None:
        self.price_updates += 1
        snapshot = self._build_price_snapshot(symbol, update)
        self._price_cache[symbol] = snapshot
        await self._publish_price_snapshot(symbol, snapshot)

    async def process_dom_update(self, symbol: str, levels: Sequence[Mapping[str, Any]]) -> None:
        self.dom_updates += 1
        snapshot = self._build_dom_snapshot(symbol, levels)
        self._dom_cache[symbol] = snapshot
        await self._publish_dom_snapshot(symbol, snapshot)
        payload = json.dumps(self._json_ready(snapshot), separators=(",", ":"))
        writer = self.dom_writer
        if writer is not None:
            try:
                writer.append_dom(symbol, snapshot)
            except Exception:  # pragma: no cover - defensive guard
                if self._logger:
                    self._logger.warning("Failed to append DOM snapshot to Parquet", exc_info=True)

    async def _publish_price_snapshot(self, symbol: str, snapshot: Mapping[str, Any]) -> None:
        channel = PRICE_CHANNEL_TEMPLATE.format(symbol=symbol)
        payload = json.dumps(self._json_ready(snapshot), separators=(",", ":"))
        await self.redis.publish(channel, payload)
        await self.redis.hset(
            SNAPSHOT_KEY_TEMPLATE.format(symbol=symbol),
            mapping={k: self._encode_redis_value(v) for k, v in snapshot.items()},
        )

    async def _publish_dom_snapshot(self, symbol: str, snapshot: Mapping[str, Any]) -> None:
        channel = DOM_CHANNEL_TEMPLATE.format(symbol=symbol)
        payload = json.dumps(self._json_ready(snapshot), separators=(",", ":"))
        await self.redis.publish(channel, payload)
        await self.redis.hset(
            SNAPSHOT_KEY_TEMPLATE.format(symbol=symbol),
            mapping={"dom": payload, "dom_updated_at": snapshot["timestamp"].isoformat()},
        )

    def _build_price_snapshot(self, symbol: str, update: Mapping[str, Any]) -> dict[str, Any]:
        now = _now()
        bid = float(update.get("bid", 0.0) or 0.0)
        ask = float(update.get("ask", 0.0) or 0.0)
        last = float(update.get("last", 0.0) or 0.0)
        bid_size = float(update.get("bid_size", 0.0) or 0.0)
        ask_size = float(update.get("ask_size", 0.0) or 0.0)
        volume = float(update.get("volume", 0.0) or 0.0)
        mid = (bid + ask) / 2 if bid and ask else last or (bid or ask)
        snapshot = {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": last,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "volume": volume,
            "mid": mid,
            "timestamp": now,
        }
        return snapshot

    def _build_dom_snapshot(self, symbol: str, levels: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        now = _now()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in levels:
            side = str(entry.get("side", "")).lower()
            if side not in {"bid", "ask"}:
                continue
            level = int(entry.get("level", 0))
            price = float(entry.get("price", 0.0) or 0.0)
            size = float(entry.get("size", 0.0) or 0.0)
            grouped[side].append({"level": level, "price": price, "size": size})
        for side in grouped:
            grouped[side].sort(key=lambda item: item["level"])
        snapshot = {
            "symbol": symbol,
            "timestamp": now,
            "bids": grouped.get("bid", []),
            "asks": grouped.get("ask", []),
        }
        return snapshot

    @staticmethod
    def _encode_redis_value(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (float, int)):
            return str(value)
        if isinstance(value, (list, dict)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    @classmethod
    def _json_ready(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Mapping):
            return {key: cls._json_ready(val) for key, val in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [cls._json_ready(item) for item in value]
        return value


async def run_service() -> None:
    logger = get_logger("ibbot.services.ib_feed.delegator")
    logger.warning(
        "ib-feed runtime has been replaced by ib-gateway; forwarding to unified service",
    )
    from services.ib_gateway import run_service as _run_gateway

    await _run_gateway()


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover
    main()
