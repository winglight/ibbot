"""Async market data router using :class:`IBAPIController`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from src.ibapi_controller import IBAPIController

market_data_logger = logger.bind(module="market_data_router", request_id="-")


PriceListener = Callable[[str, Mapping[str, Any]], None]
DepthListener = Callable[[str, Sequence[Mapping[str, Any]]], None]
DisconnectListener = Callable[[], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _PriceState:
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last_size: Optional[float] = None
    volume: Optional[float] = None
    market_state: str = "unknown"
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(slots=True)
class _DepthBook:
    bids: Dict[int, Dict[str, float]] = field(default_factory=dict)
    asks: Dict[int, Dict[str, float]] = field(default_factory=dict)


@dataclass(slots=True)
class _Subscription:
    symbol: str
    contract: Any
    price_request_id: int
    depth_request_id: Optional[int]
    dom_rows: int
    is_smart_depth: bool
    price_state: _PriceState = field(default_factory=_PriceState)
    depth_book: _DepthBook = field(default_factory=_DepthBook)


_TICK_PRICE_FIELDS = {
    1: "bid",  # BID
    2: "ask",  # ASK
    4: "last",  # LAST
}

_TICK_SIZE_FIELDS = {
    0: "bid_size",  # BID_SIZE
    3: "ask_size",  # ASK_SIZE
    5: "last_size",  # LAST_SIZE
    8: "volume",  # VOLUME
}

_MARKET_DATA_STATE = {
    1: "real-time",
    2: "frozen",
    3: "delayed",
    4: "delayed-frozen",
}


class MarketDataRouter:
    """Route ibapi market data callbacks to listener callbacks."""

    def __init__(
        self,
        *,
        controller_getter: Callable[[], Awaitable[Optional[IBAPIController]]],
        loop: Optional[asyncio.AbstractEventLoop] = None,
        price_listener: PriceListener,
        depth_listener: DepthListener,
        disconnect_listener: DisconnectListener,
    ) -> None:
        self._controller_getter = controller_getter
        self._loop = loop or asyncio.get_event_loop()
        self._price_listener = price_listener
        self._depth_listener = depth_listener
        self._disconnect_listener = disconnect_listener

        self._controller: Optional[IBAPIController] = None
        self._event_task: Optional[asyncio.Task[None]] = None
        self._subscriptions: Dict[str, _Subscription] = {}
        self._price_requests: Dict[int, str] = {}
        self._depth_requests: Dict[int, str] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def subscribe(
        self,
        *,
        symbol: str,
        contract: Any,
        generic_tick_list: str = "",
        snapshot: bool = False,
        regulatory_snapshot: bool = False,
        options: Optional[Sequence[Any]] = None,
        dom_rows: int = 0,
        is_smart_depth: bool = False,
    ) -> None:
        """Subscribe to real-time price and depth updates for ``symbol``."""

        symbol_key = symbol.upper()
        async with self._lock:
            if symbol_key in self._subscriptions:
                return

            controller = await self._ensure_controller()
            if controller is None:
                raise RuntimeError("IBAPI controller unavailable for market data")

            price_req_id = await controller.request_market_data(
                contract,
                generic_tick_list=generic_tick_list,
                snapshot=snapshot,
                regulatory_snapshot=regulatory_snapshot,
                options=options,
            )

            depth_req_id: Optional[int] = None
            try:
                if dom_rows > 0:
                    depth_req_id = await controller.request_market_depth(
                        contract,
                        num_rows=dom_rows,
                        is_smart_depth=is_smart_depth,
                        options=None,
                    )
            except Exception:
                await controller.cancel_market_data(price_req_id)
                raise

            subscription = _Subscription(
                symbol=symbol_key,
                contract=contract,
                price_request_id=price_req_id,
                depth_request_id=depth_req_id,
                dom_rows=dom_rows,
                is_smart_depth=is_smart_depth,
            )

            self._subscriptions[symbol_key] = subscription
            self._price_requests[price_req_id] = symbol_key
            if depth_req_id is not None:
                self._depth_requests[depth_req_id] = symbol_key

    async def unsubscribe(self, symbol: str) -> None:
        symbol_key = symbol.upper()
        async with self._lock:
            subscription = self._subscriptions.pop(symbol_key, None)

        if subscription is None:
            return

        controller = await self._ensure_controller()
        if controller is not None:
            try:
                await controller.cancel_market_data(subscription.price_request_id)
            except Exception as exc:  # pragma: no cover - defensive logging
                market_data_logger.debug(
                    "Failed to cancel market data", symbol=symbol_key, error=str(exc)
                )
            if subscription.depth_request_id is not None:
                try:
                    await controller.cancel_market_depth(
                        subscription.depth_request_id,
                        is_smart_depth=subscription.is_smart_depth,
                    )
                except Exception as exc:  # pragma: no cover - defensive logging
                    market_data_logger.debug(
                        "Failed to cancel market depth", symbol=symbol_key, error=str(exc)
                    )

        self._price_requests.pop(subscription.price_request_id, None)
        if subscription.depth_request_id is not None:
            self._depth_requests.pop(subscription.depth_request_id, None)

    async def shutdown(self) -> None:
        task = self._event_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:  # pragma: no cover - expected on shutdown
                pass
        self._event_task = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _ensure_controller(self) -> Optional[IBAPIController]:
        controller = await self._controller_getter()
        if controller is None:
            return None

        if controller is not self._controller:
            await self._switch_controller(controller)

        return controller

    async def _switch_controller(self, controller: IBAPIController) -> None:
        previous = self._event_task
        self._controller = controller
        if previous is not None:
            previous.cancel()
            try:
                await previous
            except asyncio.CancelledError:  # pragma: no cover - expected on rotation
                pass

        self._event_task = self._loop.create_task(
            self._event_dispatch_loop(controller),
            name="ibapi-market-data-router",
        )

    async def _event_dispatch_loop(self, controller: IBAPIController) -> None:
        queue = controller.register_event_consumer()
        try:
            while True:
                event = await queue.get()
                self._handle_event(event)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            return
        finally:
            controller.unregister_event_consumer(queue)

    def _handle_event(self, event: Mapping[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "tickPrice":
            self._handle_tick_price(event)
        elif event_type == "tickSize":
            self._handle_tick_size(event)
        elif event_type == "marketDataType":
            self._handle_market_data_type(event)
        elif event_type == "marketDepth":
            self._handle_market_depth(event)
        elif event_type == "marketDataCancelled":
            self._handle_market_data_cancelled(event)
        elif event_type == "marketDepthCancelled":
            self._handle_market_depth_cancelled(event)
        elif event_type == "connectionClosed":
            self._handle_connection_closed()
        elif event_type == "error":
            self._handle_error(event)

    def _handle_tick_price(self, event: Mapping[str, Any]) -> None:
        req_id = int(event.get("request_id", -1))
        symbol = self._price_requests.get(req_id)
        if not symbol:
            return

        subscription = self._subscriptions.get(symbol)
        if subscription is None:
            return

        field = _TICK_PRICE_FIELDS.get(int(event.get("tick_type", -1)))
        if field is None:
            return

        try:
            price = float(event.get("price", 0.0))
        except (TypeError, ValueError):
            price = 0.0

        setattr(subscription.price_state, field, price)
        subscription.price_state.timestamp = _utcnow()
        self._emit_price(subscription)

    def _handle_tick_size(self, event: Mapping[str, Any]) -> None:
        req_id = int(event.get("request_id", -1))
        symbol = self._price_requests.get(req_id)
        if not symbol:
            return

        subscription = self._subscriptions.get(symbol)
        if subscription is None:
            return

        field = _TICK_SIZE_FIELDS.get(int(event.get("tick_type", -1)))
        if field is None:
            return

        try:
            size = float(event.get("size", 0.0))
        except (TypeError, ValueError):
            size = 0.0

        setattr(subscription.price_state, field, size)
        subscription.price_state.timestamp = _utcnow()
        self._emit_price(subscription)

    def _handle_market_data_type(self, event: Mapping[str, Any]) -> None:
        req_id = int(event.get("request_id", -1))
        symbol = self._price_requests.get(req_id)
        if not symbol:
            return

        subscription = self._subscriptions.get(symbol)
        if subscription is None:
            return

        state = _MARKET_DATA_STATE.get(int(event.get("market_data_type", -1)))
        if state is None:
            return

        subscription.price_state.market_state = state
        subscription.price_state.timestamp = _utcnow()
        self._emit_price(subscription)

    def _handle_market_depth(self, event: Mapping[str, Any]) -> None:
        req_id = int(event.get("request_id", -1))
        symbol = self._depth_requests.get(req_id)
        if not symbol:
            return

        subscription = self._subscriptions.get(symbol)
        if subscription is None:
            return

        side_raw = int(event.get("side", -1))
        operation = int(event.get("operation", -1))
        position = int(event.get("position", -1))
        try:
            price = float(event.get("price", 0.0))
            size = float(event.get("size", 0.0))
        except (TypeError, ValueError):
            price = 0.0
            size = 0.0

        book = subscription.depth_book
        levels = book.bids if side_raw == 1 else book.asks

        if operation == 2:  # Remove level
            levels.pop(position, None)
        else:
            levels[position] = {"price": price, "size": size}

        self._emit_depth(subscription)

    def _handle_market_data_cancelled(self, event: Mapping[str, Any]) -> None:
        req_id = int(event.get("request_id", -1))
        symbol = self._price_requests.pop(req_id, None)
        if not symbol:
            return

        subscription = self._subscriptions.get(symbol)
        if subscription is not None:
            subscription.price_request_id = -1

    def _handle_market_depth_cancelled(self, event: Mapping[str, Any]) -> None:
        req_id = int(event.get("request_id", -1))
        symbol = self._depth_requests.pop(req_id, None)
        if not symbol:
            return

        subscription = self._subscriptions.get(symbol)
        if subscription is not None:
            subscription.depth_request_id = None
            subscription.depth_book = _DepthBook()

    def _handle_connection_closed(self) -> None:
        if self._subscriptions:
            self._safe_call(self._disconnect_listener)

    def _handle_error(self, event: Mapping[str, Any]) -> None:
        req_id = int(event.get("request_id", -1))
        if req_id in self._price_requests or req_id in self._depth_requests:
            market_data_logger.debug(
                "Market data request error",
                request_id=req_id,
                code=event.get("code"),
                message=event.get("message"),
            )

    def _emit_price(self, subscription: _Subscription) -> None:
        state = subscription.price_state
        if state.bid is None and state.ask is None and state.last is None:
            return

        payload = {
            "symbol": subscription.symbol,
            "bid": float(state.bid or 0.0),
            "ask": float(state.ask or 0.0),
            "last": float(state.last or 0.0),
            "bid_size": float(state.bid_size or 0.0),
            "ask_size": float(state.ask_size or 0.0),
            "volume": float(state.volume or 0.0),
            "market_state": state.market_state,
            "timestamp": state.timestamp,
        }
        self._safe_call(self._price_listener, subscription.symbol, payload)

    def _emit_depth(self, subscription: _Subscription) -> None:
        book = subscription.depth_book
        now = _utcnow()

        levels: List[Dict[str, Any]] = []

        for level, data in sorted(book.bids.items()):
            levels.append(
                {
                    "symbol": subscription.symbol,
                    "side": "bid",
                    "level": level,
                    "price": float(data.get("price", 0.0)),
                    "size": float(data.get("size", 0.0)),
                    "timestamp": now,
                }
            )

        for level, data in sorted(book.asks.items()):
            levels.append(
                {
                    "symbol": subscription.symbol,
                    "side": "ask",
                    "level": level,
                    "price": float(data.get("price", 0.0)),
                    "size": float(data.get("size", 0.0)),
                    "timestamp": now,
                }
            )

        if levels:
            self._safe_call(self._depth_listener, subscription.symbol, levels)

    def _safe_call(self, callback: Callable[..., Any], *args: Any) -> None:
        try:
            callback(*args)
        except Exception as exc:  # pragma: no cover - defensive logging
            market_data_logger.debug("Listener raised exception", error=str(exc))
