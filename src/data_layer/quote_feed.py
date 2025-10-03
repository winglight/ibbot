"""Quote feed abstractions used by the market quote adapter."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import defaultdict
import os
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger
from src.utils import run_in_cpu_executor

Callback = Callable[[Dict[str, float]], None]

__all__ = [
    "QuoteFeed",
    "InMemoryQuoteFeed",
    "IBQuoteFeed",
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
                    await self._remove_callback(symbol_key, callback)

                return _cancel_existing

        await run_in_cpu_executor(self._ensure_connection_ready)

        try:
            subscription = await self._start_subscription(symbol_key, callback, options or {})
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
                await self._stop_subscription(subscription)
            else:
                self._subscriptions[symbol_key] = subscription

        async def _cancel() -> None:
            await self._remove_callback(symbol_key, callback)

        return _cancel

    async def publish_tick(self, symbol: str, payload: Dict[str, float]) -> None:  # pragma: no cover - interface parity
        raise NotImplementedError("IBQuoteFeed publishes directly from IB events")

    # ------------------------------------------------------------------
    def _ensure_connection_ready(self) -> None:
        if not self._connection_manager.is_connected():
            raise self._data_source_error_cls("IBKR connection is not active")

    async def _start_subscription(
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

        result = await cm.run_in_connection_thread(
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
            logger.error("Failed to attach update handler for {}: {}", symbol, exc)
            await cm.run_in_connection_thread(
                lambda: cm.get_ib_instance().cancelMktData(contract)
            )
            return None

        return _Subscription(callbacks=[callback], ticker=ticker, contract=contract, handler=_on_update)

    async def _stop_subscription(self, subscription: _Subscription) -> None:
        ticker = subscription.ticker
        contract = subscription.contract

        cm = self._connection_manager

        def _cancel():
            ib = cm.get_ib_instance()
            if ib:
                try:
                    ib.cancelMktData(contract)
                except Exception as exc:
                    logger.warning("Failed to cancel market data for {}", contract)
            try:
                ticker.updateEvent -= subscription.handler
            except Exception:
                try:
                    ticker.updateEvent.clear()
                except Exception:
                    pass

        await cm.run_in_connection_thread(
            _cancel,
            timeout=5.0,
        )

    async def _remove_callback(self, symbol: str, callback: Callback) -> None:
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

        await self._stop_subscription(subscription)

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
                logger.debug("Quote callback failed for {}: {}", symbol, exc)

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
            logger.debug("Failed to extract ticker payload for {}: {}", symbol, exc)
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


def resolve_default_quote_feed() -> QuoteFeed:
    """Return the default quote feed based on runtime environment."""

    if os.getenv("PYTEST_CURRENT_TEST"):
        logger.debug("Using in-memory quote feed for tests")
        return InMemoryQuoteFeed()

    try:
        return IBQuoteFeed()
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Falling back to in-memory quote feed: {}", exc)
        return InMemoryQuoteFeed()
