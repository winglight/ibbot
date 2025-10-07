"""Quote feed abstractions used by the market quote adapter."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

Callback = Callable[[Dict[str, float]], None]

__all__ = [
    "QuoteFeed",
    "InMemoryQuoteFeed",
    "IBQuoteFeed",
    "RedisQuoteFeed",
    "resolve_default_quote_feed",
]


class QuoteFeed:
    """Abstract feed publishing real-time quote updates."""

    async def subscribe(
        self,
        symbol: str,
        callback: Callback,
        *,
        options: Optional[Dict] = None,
    ) -> Callable[[], Optional[Awaitable[None]]]:
        raise NotImplementedError


class InMemoryQuoteFeed(QuoteFeed):
    """Simple in-memory quote feed useful for tests and development."""

    def __init__(self) -> None:
        self._callbacks: Dict[str, List[Callback]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        symbol: str,
        callback: Callback,
        *,
        options: Optional[Dict] = None,
    ) -> Callable[[], Optional[Awaitable[None]]]:
        async with self._lock:
            callbacks = self._callbacks.setdefault(symbol, [])
            callbacks.append(callback)

        async def _cancel() -> None:
            async with self._lock:
                callbacks = self._callbacks.get(symbol)
                if not callbacks:
                    return
                try:
                    callbacks.remove(callback)
                except ValueError:
                    pass
                if not callbacks:
                    self._callbacks.pop(symbol, None)

        return _cancel

    async def publish_tick(self, symbol: str, payload: Dict[str, float]) -> None:
        async with self._lock:
            callbacks = list(self._callbacks.get(symbol, ()))
        for cb in callbacks:
            try:
                cb(payload)
            except Exception:
                continue


@dataclass
class _Subscription:
    callbacks: List[Callback]
    ticker: any
    contract: any
    handler: Callable[..., None]


class IBQuoteFeed(QuoteFeed):
    """Quote feed backed by a live IBKR connection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: Dict[str, _Subscription] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def subscribe(
        self,
        symbol: str,
        callback: Callback,
        *,
        options: Optional[Dict] = None,
    ) -> Callable[[], Optional[Awaitable[None]]]:
        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            raise ValueError("Symbol is required for IB quote subscription")

        loop = asyncio.get_running_loop()
        self._loop = loop

        with self._lock:
            existing = self._subscriptions.get(symbol_key)
            if existing:
                existing.callbacks.append(callback)

                async def _cancel_existing() -> None:
                    self._remove_callback(symbol_key, callback)

                return _cancel_existing

        client = self._resolve_ibkr_client()
        ensure_fn = getattr(client, "ensure_connection", None)
        if callable(ensure_fn):
            try:
                await ensure_fn(reason="quote-feed")
            except Exception as exc:
                raise self._data_source_error_cls(str(exc)) from exc
        else:
            await asyncio.to_thread(self._ensure_connection_ready)

        try:
            subscription = await asyncio.to_thread(
                self._start_subscription, symbol_key, callback, options or {}
            )
        except RuntimeError as exc:
            raise self._data_source_error_cls(str(exc)) from exc

        if subscription is None:
            raise self._data_source_error_cls(
                f"Failed to start IB quote stream for {symbol_key}"
            )

        with self._lock:
            current = self._subscriptions.get(symbol_key)
            if current:
                current.callbacks.append(callback)
                await asyncio.to_thread(self._stop_subscription, subscription)
            else:
                self._subscriptions[symbol_key] = subscription

        async def _cancel() -> None:
            self._remove_callback(symbol_key, callback)

        return _cancel

    async def publish_tick(self, symbol: str, payload: Dict[str, float]) -> None:  # pragma: no cover - interface parity
        raise NotImplementedError("IBQuoteFeed publishes directly from IB events")

    # ------------------------------------------------------------------
    def _ensure_connection_ready(self) -> None:
        if not self._connection_manager.is_connected():
            raise self._data_source_error_cls("IBKR connection is not active")

    def _start_subscription(
        self,
        symbol: str,
        callback: Callback,
        options: Dict[str, Any],
    ) -> Optional[_Subscription]:
        cm = self._connection_manager

        def _subscribe():
            ib = cm.get_ib_instance()
            if not ib:
                return None
            contract = self._build_contract(symbol, options)
            ticker = ib.reqMktData(contract, "", False, False)
            return ticker, contract

        result = cm.run_in_connection_thread(
            _subscribe,
            timeout=15.0,
        )
        if not result:
            raise RuntimeError(f"reqMktData failed for {symbol}")
        ticker, contract = result

        def _on_update(*_args, sym: str = symbol, tick=ticker) -> None:
            payload = self._extract_payload(sym, tick)
            if payload is None:
                return
            loop = self._loop
            if loop and loop.is_running():
                loop.call_soon_threadsafe(self._notify_callbacks, sym, payload)
            else:
                self._notify_callbacks(sym, payload)

        try:
            ticker.updateEvent += _on_update
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error("Failed to attach update handler for %s: %s", symbol, exc)
            connection_manager.run_in_connection_thread(
                lambda: connection_manager.get_ib_instance().cancelMktData(contract)
            )
            return None

        return _Subscription(callbacks=[callback], ticker=ticker, contract=contract, handler=_on_update)

    def _stop_subscription(self, subscription: _Subscription) -> None:
        ticker = subscription.ticker
        contract = subscription.contract

        cm = self._connection_manager

        def _cancel():
            ib = cm.get_ib_instance()
            if ib:
                try:
                    ib.cancelMktData(contract)
                except Exception as exc:
                    logger.warning("Failed to cancel market data for %s", contract)
            try:
                ticker.updateEvent -= subscription.handler
            except Exception:
                try:
                    ticker.updateEvent.clear()
                except Exception:
                    pass

        cm.run_in_connection_thread(
            _cancel,
            timeout=5.0,
        )

    def _remove_callback(self, symbol: str, callback: Callback) -> None:
        subscription: Optional[_Subscription]
        with self._lock:
            subscription = self._subscriptions.get(symbol)
            if not subscription:
                return
            try:
                subscription.callbacks.remove(callback)
            except ValueError:
                pass
            if subscription.callbacks:
                return
            self._subscriptions.pop(symbol, None)

        self._stop_subscription(subscription)

    def _notify_callbacks(self, symbol: str, payload: Dict[str, float]) -> None:
        callbacks: List[Callback]
        with self._lock:
            subscription = self._subscriptions.get(symbol)
            if not subscription:
                return
            callbacks = list(subscription.callbacks)
        for cb in callbacks:
            try:
                cb(payload)
            except Exception as exc:
                logger.debug("Quote callback failed for %s: %s", symbol, exc)

    def _extract_payload(self, symbol: str, ticker: any) -> Optional[Dict[str, float]]:
        try:
            last = float(getattr(ticker, "last", 0.0) or 0.0)
            bid = float(getattr(ticker, "bid", 0.0) or 0.0)
            ask = float(getattr(ticker, "ask", 0.0) or 0.0)
            volume = float(getattr(ticker, "volume", 0.0) or 0.0)
            market_state = getattr(ticker, "marketState", "unknown") or "unknown"
            ts_ns = getattr(ticker, "time", None)
            if ts_ns is not None:
                try:
                    ts_ns = int(ts_ns.timestamp() * 1_000_000_000)
                except Exception:
                    ts_ns = None
            if ts_ns is None:
                ts_ns = time.time_ns()
            return {
                "symbol": symbol,
                "last": last,
                "bid": bid,
                "ask": ask,
                "volume": volume,
                "market_state": market_state,
                "ts_ns": ts_ns,
            }
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.debug("Failed to extract ticker payload for %s: %s", symbol, exc)
            return None

    def _build_contract(self, symbol: str, options: Dict[str, any]):
        exchange = options.get("exchange", "SMART")
        sec_type = (options.get("sec_type") or "").upper()

        client = self._resolve_ibkr_client()
        if sec_type == "FUT" or symbol.upper() in self._index_future_symbols:
            return client.get_future_contract(symbol, exchange)
        return client.get_stock_contract(symbol, exchange)

    def _resolve_ibkr_client(self):
        try:
            from src.ib_client_registry import get_client_sync  # type: ignore
        except Exception:
            get_client_sync = None  # type: ignore[assignment]
        else:
            try:
                return get_client_sync("secondary")
            except Exception as exc:
                logger.error("Failed to resolve shared IBKR client: %s", exc)

        try:
            import core  # type: ignore
        except ImportError as exc:  # pragma: no cover - defensive guard
            raise RuntimeError("IBKR core module unavailable") from exc

        client = getattr(core, "ibkr_client", None)
        if client is not None:
            return client

        factory = getattr(core, "IBKRClient", None)
        if factory is None:
            raise RuntimeError("IBKRClient factory not available on core module")

        client = factory()
        core.ibkr_client = client
        return client

    @property
    def _connection_manager(self):
        if not hasattr(self, "__connection_manager"):
            self.__connection_manager = import_module("src.connection_manager").connection_manager
        return self.__connection_manager

    @property
    def _index_future_symbols(self):
        if not hasattr(self, "__index_future_symbols"):
            self.__index_future_symbols = import_module("src.ibkr_client").INDEX_FUTURE_SYMBOLS
        return self.__index_future_symbols

    @property
    def _data_source_error_cls(self):
        if not hasattr(self, "__data_source_error_cls"):
            module = import_module("src.data_layer.data_source_manager")
            self.__data_source_error_cls = getattr(module, "DataSourceError")
        return self.__data_source_error_cls


@dataclass(slots=True)
class _RedisQuoteStream:
    callbacks: List[Callback]
    thread: threading.Thread
    stop_event: threading.Event
    client: "redis.Redis"
    channel: str


class RedisQuoteFeed(QuoteFeed):
    """Quote feed backed by Redis pub/sub snapshots produced by ib-feed."""

    _CHANNEL_TEMPLATE = os.getenv("IB_FEED_PRICE_CHANNEL", "pubsub:ib:price:{symbol}")
    _SNAPSHOT_TEMPLATE = os.getenv("IB_FEED_SNAPSHOT_KEY", "hash:market:snapshot:{symbol}")

    def __init__(self, redis_url: str | None = None) -> None:
        try:  # lazy import to keep redis optional for some tests
            import redis  # type: ignore
        except Exception as exc:  # pragma: no cover - redis dependency missing
            raise RuntimeError("redis package is required for RedisQuoteFeed") from exc

        self._redis_mod = redis
        self._redis_url = redis_url or (
            os.getenv("DATA_LAYER_REDIS_URL")
            or os.getenv("IB_FEED_REDIS_URL")
            or os.getenv("IBBOT_REDIS_URL")
            or "redis://localhost:6379/0"
        )
        self._streams: Dict[str, _RedisQuoteStream] = {}
        self._lock = threading.Lock()
        logger.debug(
            "RedisQuoteFeed initialised",
            redis_url=self._redis_url,
            channel_template=self._CHANNEL_TEMPLATE,
        )

    async def subscribe(
        self,
        symbol: str,
        callback: Callback,
        *,
        options: Optional[Dict] = None,
    ) -> Callable[[], Optional[Awaitable[None]]]:
        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            raise ValueError("Symbol is required for Redis quote subscription")

        with self._lock:
            existing = self._streams.get(symbol_key)
            if existing:
                if callback not in existing.callbacks:
                    existing.callbacks.append(callback)
                    logger.debug(
                        "Attached additional callback to Redis quote stream",
                        symbol=symbol_key,
                        callback_count=len(existing.callbacks),
                    )

                async def _cancel_existing() -> None:
                    self._remove_callback(symbol_key, callback)

                await self._maybe_emit_snapshot(symbol_key, callback)
                return _cancel_existing

            client = self._redis_mod.Redis.from_url(self._redis_url, decode_responses=True)
            stop_event = threading.Event()
            handle = _RedisQuoteStream(
                callbacks=[callback],
                thread=threading.Thread(
                    target=self._listen,
                    name=f"redis-quote-{symbol_key}",
                    args=(symbol_key, client, stop_event),
                    daemon=True,
                ),
                stop_event=stop_event,
                client=client,
                channel=self._CHANNEL_TEMPLATE.format(symbol=symbol_key),
            )
            self._streams[symbol_key] = handle
            handle.thread.start()

        await self._maybe_emit_snapshot(symbol_key, callback)

        async def _cancel() -> None:
            should_stop = self._remove_callback(symbol_key, callback)
            if should_stop:
                self._stop_stream(symbol_key, handle)

        return _cancel

    async def publish_tick(self, symbol: str, payload: Dict[str, float]) -> None:  # pragma: no cover - interface parity
        raise NotImplementedError("RedisQuoteFeed publishes updates via Redis pubsub")

    # ------------------------------------------------------------------
    async def _maybe_emit_snapshot(
        self,
        symbol: str,
        callback: Callback,
    ) -> None:
        await asyncio.to_thread(self._emit_snapshot, symbol, callback)

    def _emit_snapshot(self, symbol: str, callback: Callback) -> None:
        client = None
        try:
            client = self._redis_mod.Redis.from_url(self._redis_url, decode_responses=True)
            snapshot = client.hgetall(self._SNAPSHOT_TEMPLATE.format(symbol=symbol))
        except Exception as exc:  # pragma: no cover - redis failure
            logger.debug("Failed to read Redis quote snapshot", symbol=symbol, error=str(exc))
            return
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        if not snapshot:
            return
        payload = self._normalise_snapshot(snapshot)
        if not payload:
            return
        logger.debug("Emitting cached Redis quote snapshot", symbol=symbol)
        try:
            callback(payload)
        except Exception as exc:  # pragma: no cover - callback safety
            logger.warning("Quote snapshot callback raised", symbol=symbol, error=str(exc))

    # ------------------------------------------------------------------
    def _listen(
        self,
        symbol: str,
        client: "redis.Redis",
        stop_event: threading.Event,
    ) -> None:
        channel = self._CHANNEL_TEMPLATE.format(symbol=symbol)
        logger.debug("Redis quote listener starting", symbol=symbol, channel=channel)
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(channel)
        except Exception as exc:  # pragma: no cover - connection failures
            logger.error("Redis quote subscribe failed", symbol=symbol, error=str(exc))
            return

        try:
            for message in pubsub.listen():
                if stop_event.is_set():
                    break
                if not message or message.get("type") != "message":
                    continue
                data = message.get("data")
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except Exception as exc:  # pragma: no cover - malformed payload
                    logger.debug("Failed to decode Redis quote payload", symbol=symbol, error=str(exc))
                    continue
                normalized = self._normalise_payload(symbol, payload)
                if not normalized:
                    continue
                logger.debug(
                    "Received Redis quote update",
                    symbol=symbol,
                    bid=normalized.get("bid"),
                    ask=normalized.get("ask"),
                    last=normalized.get("last"),
                )
                callbacks = self._callbacks_for(symbol)
                for cb in callbacks:
                    try:
                        cb(dict(normalized))
                    except Exception as exc:  # pragma: no cover - callback safety
                        logger.warning("Quote callback raised", symbol=symbol, error=str(exc))
        finally:
            try:
                pubsub.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass
            logger.debug("Redis quote listener stopped", symbol=symbol)

    # ------------------------------------------------------------------
    def _remove_callback(self, symbol: str, callback: Callback) -> bool:
        with self._lock:
            handle = self._streams.get(symbol)
            if not handle:
                return False
            try:
                handle.callbacks.remove(callback)
            except ValueError:
                pass
            if handle.callbacks:
                return False
            self._streams.pop(symbol, None)
        return True

    def _stop_stream(self, symbol: str, handle: _RedisQuoteStream) -> None:
        try:
            handle.stop_event.set()
            if handle.thread.is_alive():
                handle.thread.join(timeout=2.0)
        finally:
            try:
                handle.client.close()
            except Exception:
                pass

    def _callbacks_for(self, symbol: str) -> List[Callback]:
        with self._lock:
            handle = self._streams.get(symbol)
            if not handle:
                return []
            return list(handle.callbacks)

    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_payload(symbol: str, payload: Dict[str, Any]) -> Dict[str, float]:
        try:
            ts_raw = payload.get("timestamp")
            if isinstance(ts_raw, str):
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                dt = datetime.now()
        except Exception:
            dt = datetime.now()
        ts_ns = int(dt.timestamp() * 1_000_000_000)
        result = {
            "symbol": symbol,
            "bid": float(payload.get("bid", 0.0) or 0.0),
            "ask": float(payload.get("ask", 0.0) or 0.0),
            "last": float(payload.get("last", 0.0) or 0.0),
            "volume": float(payload.get("volume", 0.0) or 0.0),
            "ts_ns": ts_ns,
        }
        return result

    def _normalise_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, float]:
        try:
            timestamp = snapshot.get("timestamp")
            if isinstance(timestamp, str):
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            else:
                dt = datetime.now()
        except Exception:
            dt = datetime.now()
        ts_ns = int(dt.timestamp() * 1_000_000_000)
        result = {
            "symbol": snapshot.get("symbol"),
            "bid": float(snapshot.get("bid", 0.0) or 0.0),
            "ask": float(snapshot.get("ask", 0.0) or 0.0),
            "last": float(snapshot.get("last", 0.0) or 0.0),
            "volume": float(snapshot.get("volume", 0.0) or 0.0),
            "ts_ns": ts_ns,
        }
        return result


def resolve_default_quote_feed() -> QuoteFeed:
    """Return the default quote feed based on runtime environment."""

    if os.getenv("PYTEST_CURRENT_TEST"):
        logger.debug("Using in-memory quote feed for tests")
        return InMemoryQuoteFeed()

    remote_flag = os.getenv("MARKET_DATA_REMOTE_FEED")
    use_remote = False
    if remote_flag is None:
        use_remote = bool(os.getenv("IB_FEED_REDIS_URL") or os.getenv("DATA_LAYER_REDIS_URL"))
    else:
        use_remote = remote_flag.strip().lower() in {"1", "true", "yes", "on"}

    if use_remote:
        try:
            return RedisQuoteFeed()
        except Exception as exc:
            logger.warning("Failed to initialise Redis quote feed: %s", exc)

    try:
        return IBQuoteFeed()
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Falling back to in-memory quote feed: %s", exc)
        return InMemoryQuoteFeed()
