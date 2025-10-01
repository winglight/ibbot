"""Redis pub/sub listener for market data updates."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from loguru import logger

from src.configuration.provider import ConfigProvider, MarketDataServiceConfig


class PubSubListener:
    """Listen to Redis pub/sub channels and maintain in-memory snapshots."""

    def __init__(
        self,
        *,
        config_provider: ConfigProvider[MarketDataServiceConfig],
        redis_client: Any,
        runtime: Any,
        reconnect_delay: float = 1.0,
    ) -> None:
        self._config_provider = config_provider
        self._redis = redis_client
        self._runtime = runtime
        self._reconnect_delay = reconnect_delay

        self._pubsub: Any | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()

        self._symbols: set[str] = set()
        self._desired_channels: set[str] = set()
        self._current_channels: set[str] = set()

    async def start(self) -> None:
        if self._listener_task is not None and not self._listener_task.done():
            return

        self._stop_event.clear()
        self._config_provider.register_callback(self._on_config_update)

        config = await self._config_provider.get_config()
        await self._update_symbols(config.symbols, load_snapshots=True)

        self._listener_task = asyncio.create_task(self._run_listener())

    async def stop(self) -> None:
        self._stop_event.set()
        self._config_provider.unregister_callback(self._on_config_update)

        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        await self._cleanup_pubsub()

    async def _run_listener(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._ensure_pubsub()
                pubsub = self._pubsub
                if pubsub is None:
                    await asyncio.sleep(self._reconnect_delay)
                    continue

                async for message in pubsub.listen():
                    if self._stop_event.is_set():
                        break
                    if not isinstance(message, Mapping):
                        continue
                    try:
                        await self._handle_message(message)
                    except Exception as exc:  # pragma: no cover - defensive log
                        logger.warning("Failed to handle pubsub message", error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("PubSub listener error", error=str(exc))
                await self._cleanup_pubsub()
                await asyncio.sleep(self._reconnect_delay)

        await self._cleanup_pubsub()

    async def _ensure_pubsub(self) -> None:
        if self._pubsub is not None:
            return

        if self._redis is None:
            return

        async with self._lock:
            if self._pubsub is not None:
                return

            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            desired = self._desired_channels.copy()
            if desired:
                await pubsub.subscribe(*desired)
            self._pubsub = pubsub
            self._current_channels = desired

    async def _cleanup_pubsub(self) -> None:
        async with self._lock:
            if self._pubsub is None:
                return
            try:
                await self._pubsub.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
            self._pubsub = None
            self._current_channels.clear()

    async def _on_config_update(self, config: MarketDataServiceConfig) -> None:
        await self._update_symbols(config.symbols, load_snapshots=True)

    async def _update_symbols(self, symbols: Iterable[str], *, load_snapshots: bool) -> None:
        new_symbols = {symbol for symbol in symbols}
        async with self._lock:
            added = new_symbols - self._symbols
            removed = self._symbols - new_symbols
            self._symbols = new_symbols

            desired_channels = self._build_channels(new_symbols)
            pubsub = self._pubsub
            if pubsub is not None:
                subscribe = desired_channels - self._current_channels
                unsubscribe = self._current_channels - desired_channels
                if subscribe:
                    await pubsub.subscribe(*subscribe)
                if unsubscribe:
                    await pubsub.unsubscribe(*unsubscribe)
                self._current_channels -= unsubscribe
                self._current_channels |= subscribe
            self._desired_channels = desired_channels

        if load_snapshots and added:
            for symbol in added:
                await self._load_snapshot(symbol)
        for symbol in new_symbols:
            self._ensure_snapshot(symbol)

    async def _load_snapshot(self, symbol: str) -> None:
        if self._redis is None:
            return

        key = f"hash:market:snapshot:{symbol}"
        try:
            raw = await self._redis.hgetall(key)
        except Exception as exc:  # pragma: no cover - defensive log
            logger.warning("Failed to load snapshot from Redis", symbol=symbol, error=str(exc))
            return

        if not raw:
            return

        data = {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in raw.items()
        }

        snapshot = self._ensure_snapshot(symbol)

        price = self._coerce_float(data.get("price"))
        if price is not None:
            snapshot["price"] = price

        dom_value = data.get("dom")
        if isinstance(dom_value, str):
            try:
                dom_value = json.loads(dom_value)
            except json.JSONDecodeError:
                pass
        if dom_value is not None:
            snapshot["dom"] = dom_value

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            snapshot["updated_at"] = updated_at

        latency = self._coerce_float(data.get("latency_ms"))
        snapshot["latency_ms"] = latency

        stale_value = data.get("stale")
        stale_flag = self._coerce_bool(stale_value)
        if stale_flag is not None:
            snapshot["stale"] = stale_flag

        account_value = data.get("account") or data.get("account_info")
        if account_value is not None:
            snapshot["account"] = account_value

        self._runtime._snapshots[symbol] = snapshot

    async def _handle_message(self, message: Mapping[str, Any]) -> None:
        channel_raw = message.get("channel")
        if channel_raw is None:
            return
        channel = channel_raw.decode() if isinstance(channel_raw, bytes) else channel_raw
        if not isinstance(channel, str):
            return

        data = message.get("data")
        payload = self._normalise_payload(data)

        if channel.startswith("pubsub:ib:price:"):
            symbol = channel.rsplit(":", 1)[-1]
            await self._handle_price(symbol, payload)
        elif channel.startswith("pubsub:ib:dom:"):
            symbol = channel.rsplit(":", 1)[-1]
            await self._handle_dom(symbol, payload)

    async def _handle_price(self, symbol: str, payload: Mapping[str, Any]) -> None:
        price = payload.get("price")
        if price is None:
            price = payload.get("value")
        price_value = self._coerce_float(price)
        if price_value is None:
            return

        timestamp = self._extract_timestamp(payload)
        latency = self._update_snapshot(
            symbol,
            price=price_value,
            message_timestamp=timestamp,
        )
        await self._runtime.publish_stream_event(
            symbol=symbol,
            event_type="price",
            data=payload,
            message_timestamp=timestamp,
            latency_ms=latency,
        )

    async def _handle_dom(self, symbol: str, payload: Mapping[str, Any]) -> None:
        dom_value = payload.get("dom")
        if dom_value is None:
            dom_value = payload.get("value")
        if isinstance(dom_value, str):
            try:
                dom_value = json.loads(dom_value)
            except json.JSONDecodeError:
                pass

        timestamp = self._extract_timestamp(payload)
        latency = self._update_snapshot(
            symbol,
            dom=dom_value,
            message_timestamp=timestamp,
        )
        await self._runtime.publish_stream_event(
            symbol=symbol,
            event_type="dom",
            data=payload,
            message_timestamp=timestamp,
            latency_ms=latency,
        )

    def _update_snapshot(
        self,
        symbol: str,
        *,
        price: float | None = None,
        dom: Any | None = None,
        message_timestamp: datetime | None,
    ) -> float | None:
        snapshot = self._ensure_snapshot(symbol)
        if price is not None:
            snapshot["price"] = price
        if dom is not None:
            snapshot["dom"] = dom

        now = datetime.now(timezone.utc)
        snapshot["updated_at"] = now.isoformat()
        if message_timestamp is not None:
            latency = max(0.0, (now - message_timestamp).total_seconds() * 1000)
        else:
            latency = None
        snapshot["latency_ms"] = latency
        snapshot["stale"] = False
        self._runtime._snapshots[symbol] = snapshot
        return latency

    def _ensure_snapshot(self, symbol: str) -> dict[str, Any]:
        snapshot = self._runtime._snapshots.get(symbol)
        if snapshot is None:
            snapshot = {
                "symbol": symbol,
                "price": None,
                "dom": None,
                "account": None,
                "updated_at": None,
                "latency_ms": None,
                "stale": False,
            }
            snapshot.update(self._runtime._account_snapshot_view())
            self._runtime._snapshots[symbol] = snapshot
        else:
            account_attachment = self._runtime._account_snapshot_view()
            for key, value in account_attachment.items():
                snapshot.setdefault(key, value)
        return snapshot

    def _build_channels(self, symbols: Iterable[str]) -> set[str]:
        channels: set[str] = set()
        for symbol in symbols:
            channels.add(f"pubsub:ib:price:{symbol}")
            channels.add(f"pubsub:ib:dom:{symbol}")
        return channels

    def _normalise_payload(self, data: Any) -> Mapping[str, Any]:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if isinstance(data, str):
            data = data.strip()
            if not data:
                return {}
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                return {"value": data}
            if isinstance(parsed, Mapping):
                return parsed
            return {"value": parsed}
        if isinstance(data, Mapping):
            return data
        return {"value": data}

    def _coerce_float(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            try:
                return float(value)
            except ValueError:
                return None
        return None

    def _coerce_bool(self, value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalised = value.strip().lower()
            if not normalised:
                return None
            if normalised in {"1", "true", "yes", "y"}:
                return True
            if normalised in {"0", "false", "no", "n"}:
                return False
        return None

    def _extract_timestamp(self, payload: Mapping[str, Any]) -> datetime | None:
        for key in ("timestamp", "ts", "time", "updated_at"):
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(value, datetime):
                return value.astimezone(timezone.utc)
            if isinstance(value, (int, float)):
                ts = float(value)
                if ts > 1e12:  # assume milliseconds
                    ts /= 1000
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
                try:
                    numeric = float(value)
                except ValueError:
                    try:
                        normalised = value.replace("Z", "+00:00")
                        return datetime.fromisoformat(normalised)
                    except ValueError:
                        continue
                else:
                    if numeric > 1e12:
                        numeric /= 1000
                    return datetime.fromtimestamp(numeric, tz=timezone.utc)
        return None


__all__ = ["PubSubListener"]
