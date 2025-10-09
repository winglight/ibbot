from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from datetime import datetime, timezone

from src.ib.base_client import BaseServiceIBClient
from src.ib.events import (
    dom_level_from_ib,
    market_tick_from_ib,
    real_time_bar_from_ib,
    tick_by_tick_from_ib,
)

try:  # pragma: no cover - optional dependency for local development
    from ibapi.contract import Contract  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - fallback used in tests when ibapi missing
    class Contract:  # type: ignore[no-redef]
        ...

try:  # pragma: no cover - optional dependency for translating tick types
    from ibapi.ticktype import TickTypeEnum
except ModuleNotFoundError:  # pragma: no cover - fallback used in tests when ibapi missing
    class TickTypeEnum:  # type: ignore[no-redef]
        @staticmethod
        def to_str(value: int) -> str:
            return str(value)


MARKET_DATA_IB_HOST_ENV = "MARKET_DATA_IB_HOST"
MARKET_DATA_IB_PORT_ENV = "MARKET_DATA_IB_PORT"
MARKET_DATA_IB_CLIENT_ID_ENV = "MARKET_DATA_IB_CLIENT_ID"


QuoteKey = tuple[int, bool, tuple[int, ...]]
DOMKey = tuple[int, int, str | None]
TickByTickKey = tuple[int, str]
RealTimeBarsKey = tuple[int, str, str]


@dataclass(slots=True)
class QuoteSubscription:
    req_id: int
    key: QuoteKey
    contract_id: int
    tick_types: tuple[int, ...]
    snapshot: bool
    subscriber_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class DOMSubscription:
    req_id: int
    key: DOMKey
    contract_id: int
    depth: int
    subscriber_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class TickByTickSubscription:
    req_id: int
    key: TickByTickKey
    contract_id: int
    subscriber_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class RealTimeBarsSubscription:
    req_id: int
    key: RealTimeBarsKey
    contract_id: int
    subscriber_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class HistoricalRequest:
    future: asyncio.Future[list[dict[str, Any]]]
    bars: list[dict[str, Any]] = field(default_factory=list)


class ServiceIBClient(BaseServiceIBClient):
    """Market data service Interactive Brokers client wrapper."""

    def __init__(self) -> None:
        super().__init__(
            service_name="market_data",
            host_env=MARKET_DATA_IB_HOST_ENV,
            port_env=MARKET_DATA_IB_PORT_ENV,
            client_id_env=MARKET_DATA_IB_CLIENT_ID_ENV,
        )
        self._subscription_lock = asyncio.Lock()
        self._quote_by_key: dict[QuoteKey, QuoteSubscription] = {}
        self._quote_by_id: dict[str, QuoteSubscription] = {}
        self._quote_by_req: dict[int, QuoteSubscription] = {}
        self._dom_by_key: dict[DOMKey, DOMSubscription] = {}
        self._dom_by_id: dict[str, DOMSubscription] = {}
        self._dom_by_req: dict[int, DOMSubscription] = {}
        self._tick_by_key: dict[TickByTickKey, TickByTickSubscription] = {}
        self._tick_by_id: dict[str, TickByTickSubscription] = {}
        self._tick_by_req: dict[int, TickByTickSubscription] = {}
        self._rt_by_key: dict[RealTimeBarsKey, RealTimeBarsSubscription] = {}
        self._rt_by_id: dict[str, RealTimeBarsSubscription] = {}
        self._rt_by_req: dict[int, RealTimeBarsSubscription] = {}
        self._historical_requests: dict[int, HistoricalRequest] = {}

    # ---------------------------------------------------------------------
    # Subscription helpers
    # ---------------------------------------------------------------------
    async def subscribe_quotes(
        self,
        contract: Any,
        *,
        snapshot: bool = False,
        tick_types: Sequence[int] | None = None,
    ) -> str:
        await self.wait_until_ready()
        contract_id = self._contract_id(contract)
        tick_tuple = tuple(sorted(int(code) for code in (tick_types or ())))
        key: QuoteKey = (contract_id, bool(snapshot), tick_tuple)

        async with self._subscription_lock:
            subscription_id = self._generate_subscription_id()
            existing = self._quote_by_key.get(key)
            if existing is not None:
                existing.subscriber_ids.add(subscription_id)
                self._quote_by_id[subscription_id] = existing
                return subscription_id

            req_id = await self.next_request_id()
            generic_tick_list = ",".join(str(code) for code in tick_tuple)
            self.reqMktData(req_id, contract, generic_tick_list, snapshot, False, [])

            subscription = QuoteSubscription(
                req_id=req_id,
                key=key,
                contract_id=contract_id,
                tick_types=tick_tuple,
                snapshot=snapshot,
            )
            subscription.subscriber_ids.add(subscription_id)

            self._quote_by_key[key] = subscription
            self._quote_by_id[subscription_id] = subscription
            self._quote_by_req[req_id] = subscription
            return subscription_id

    async def unsubscribe_quotes(self, subscription_id: str) -> bool:
        async with self._subscription_lock:
            subscription = self._quote_by_id.pop(subscription_id, None)
            if subscription is None:
                return False
            subscription.subscriber_ids.discard(subscription_id)
            if subscription.subscriber_ids:
                return True
            self._quote_by_key.pop(subscription.key, None)
            self._quote_by_req.pop(subscription.req_id, None)
        self.cancelMktData(subscription.req_id)
        return True

    async def subscribe_dom(self, contract: Any, *, depth: int, exchange: str | None = None) -> str:
        await self.wait_until_ready()
        contract_id = self._contract_id(contract)
        key: DOMKey = (contract_id, int(depth), exchange)

        async with self._subscription_lock:
            subscription_id = self._generate_subscription_id()
            existing = self._dom_by_key.get(key)
            if existing is not None:
                existing.subscriber_ids.add(subscription_id)
                self._dom_by_id[subscription_id] = existing
                return subscription_id

            req_id = await self.next_request_id()
            self.reqMktDepth(req_id, contract, depth, False, [])

            subscription = DOMSubscription(
                req_id=req_id,
                key=key,
                contract_id=contract_id,
                depth=depth,
            )
            subscription.subscriber_ids.add(subscription_id)

            self._dom_by_key[key] = subscription
            self._dom_by_id[subscription_id] = subscription
            self._dom_by_req[req_id] = subscription
            return subscription_id

    async def unsubscribe_dom(self, subscription_id: str) -> bool:
        async with self._subscription_lock:
            subscription = self._dom_by_id.pop(subscription_id, None)
            if subscription is None:
                return False
            subscription.subscriber_ids.discard(subscription_id)
            if subscription.subscriber_ids:
                return True
            self._dom_by_key.pop(subscription.key, None)
            self._dom_by_req.pop(subscription.req_id, None)
        self.cancelMktDepth(subscription.req_id)
        return True

    async def subscribe_tick_by_tick(self, contract: Any, *, tick_type: str) -> str:
        await self.wait_until_ready()
        contract_id = self._contract_id(contract)
        key: TickByTickKey = (contract_id, tick_type)

        async with self._subscription_lock:
            subscription_id = self._generate_subscription_id()
            existing = self._tick_by_key.get(key)
            if existing is not None:
                existing.subscriber_ids.add(subscription_id)
                self._tick_by_id[subscription_id] = existing
                return subscription_id

            req_id = await self.next_request_id()
            self.reqTickByTickData(req_id, contract, tick_type, 0, False)

            subscription = TickByTickSubscription(
                req_id=req_id,
                key=key,
                contract_id=contract_id,
            )
            subscription.subscriber_ids.add(subscription_id)

            self._tick_by_key[key] = subscription
            self._tick_by_id[subscription_id] = subscription
            self._tick_by_req[req_id] = subscription
            return subscription_id

    async def unsubscribe_tick_by_tick(self, subscription_id: str) -> bool:
        async with self._subscription_lock:
            subscription = self._tick_by_id.pop(subscription_id, None)
            if subscription is None:
                return False
            subscription.subscriber_ids.discard(subscription_id)
            if subscription.subscriber_ids:
                return True
            self._tick_by_key.pop(subscription.key, None)
            self._tick_by_req.pop(subscription.req_id, None)
        self.cancelTickByTickData(subscription.req_id)
        return True

    async def subscribe_real_time_bars(
        self,
        contract: Any,
        *,
        bar_size: str,
        what_to_show: str,
    ) -> str:
        await self.wait_until_ready()
        contract_id = self._contract_id(contract)
        key: RealTimeBarsKey = (contract_id, bar_size, what_to_show)

        async with self._subscription_lock:
            subscription_id = self._generate_subscription_id()
            existing = self._rt_by_key.get(key)
            if existing is not None:
                existing.subscriber_ids.add(subscription_id)
                self._rt_by_id[subscription_id] = existing
                return subscription_id

            req_id = await self.next_request_id()
            bar_size_seconds = self._resolve_bar_size_seconds(bar_size)
            self.reqRealTimeBars(req_id, contract, bar_size_seconds, what_to_show, False, [])

            subscription = RealTimeBarsSubscription(
                req_id=req_id,
                key=key,
                contract_id=contract_id,
            )
            subscription.subscriber_ids.add(subscription_id)

            self._rt_by_key[key] = subscription
            self._rt_by_id[subscription_id] = subscription
            self._rt_by_req[req_id] = subscription
            return subscription_id

    async def unsubscribe_real_time_bars(self, subscription_id: str) -> bool:
        async with self._subscription_lock:
            subscription = self._rt_by_id.pop(subscription_id, None)
            if subscription is None:
                return False
            subscription.subscriber_ids.discard(subscription_id)
            if subscription.subscriber_ids:
                return True
            self._rt_by_key.pop(subscription.key, None)
            self._rt_by_req.pop(subscription.req_id, None)
        self.cancelRealTimeBars(subscription.req_id)
        return True

    async def fetch_historical(
        self,
        contract: Any,
        *,
        duration: str,
        bar_size: str,
        what_to_show: str,
        keep_up_to_date: bool = False,
    ) -> list[dict[str, Any]]:
        await self.wait_until_ready()
        req_id = await self.next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        request = HistoricalRequest(future=future)
        self._historical_requests[req_id] = request

        self.reqHistoricalData(
            req_id,
            contract,
            "",
            duration,
            bar_size,
            what_to_show,
            0,
            1,
            keep_up_to_date,
            [],
        )

        try:
            return await asyncio.wait_for(future, timeout=self.DEFAULT_TIMEOUT)
        finally:
            self._historical_requests.pop(req_id, None)

    # ------------------------------------------------------------------
    # IB API callbacks
    # ------------------------------------------------------------------
    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:  # noqa: N802
        subscription = self._quote_by_req.get(reqId)
        if subscription is None:
            return
        tick_name = self._resolve_tick_type(tickType)
        event = market_tick_from_ib(
            contract_id=subscription.contract_id,
            tick_type=tick_name,
            value=price,
            timestamp=datetime.now(timezone.utc),
        )
        self.publish_event("market_tick", event)

    def tickSize(self, reqId: int, tickType: int, size: int) -> None:  # noqa: N802
        subscription = self._quote_by_req.get(reqId)
        if subscription is None:
            return
        tick_name = self._resolve_tick_type(tickType)
        event = market_tick_from_ib(
            contract_id=subscription.contract_id,
            tick_type=f"{tick_name}_SIZE",
            value=size,
            timestamp=datetime.now(timezone.utc),
        )
        self.publish_event("market_tick", event)

    def tickByTickAllLast(
        self,
        reqId: int,
        tickType: int,
        time: int,
        price: float,
        size: int,
        attribs: Any,
        exchange: str,
        specialConditions: str,
    ) -> None:  # noqa: N802
        subscription = self._tick_by_req.get(reqId)
        if subscription is None:
            return
        tick_name = self._resolve_tick_type(tickType)
        event = tick_by_tick_from_ib(
            contract_id=subscription.contract_id,
            tick_type=tick_name,
            price=price,
            size=size,
            exchange=exchange,
            special_conditions=specialConditions,
            timestamp=datetime.fromtimestamp(time, tz=timezone.utc),
        )
        self.publish_event("tick_by_tick", event)

    def updateMktDepth(  # noqa: N802 - IB API signature
        self,
        reqId: int,
        position: int,
        operation: int,
        side: int,
        price: float,
        size: int,
    ) -> None:
        subscription = self._dom_by_req.get(reqId)
        if subscription is None:
            return
        event = dom_level_from_ib(
            contract_id=subscription.contract_id,
            position=position,
            operation=self._resolve_dom_operation(operation),
            side=self._resolve_dom_side(side),
            price=price,
            size=size,
            timestamp=datetime.now(timezone.utc),
        )
        self.publish_event("dom_update", event)

    def updateMktDepthL2(  # noqa: N802
        self,
        reqId: int,
        position: int,
        marketMaker: str,
        operation: int,
        side: int,
        price: float,
        size: int,
        isSmartDepth: bool,
    ) -> None:
        self.updateMktDepth(reqId, position, operation, side, price, size)

    def realtimeBar(  # noqa: N802
        self,
        reqId: int,
        time: int,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        wap: float,
        count: int,
    ) -> None:
        subscription = self._rt_by_req.get(reqId)
        if subscription is None:
            return
        event = real_time_bar_from_ib(
            contract_id=subscription.contract_id,
            time=datetime.fromtimestamp(time, tz=timezone.utc),
            open_=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            wap=wap,
            count=count,
        )
        self.publish_event("real_time_bar", event)

    def historicalData(self, reqId: int, bar: Any) -> None:  # noqa: N802
        request = self._historical_requests.get(reqId)
        if request is None:
            return

        record = self._serialise_bar(bar)
        loop = self._loop
        if loop is None:
            request.bars.append(record)
        else:
            loop.call_soon_threadsafe(request.bars.append, record)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802
        request = self._historical_requests.get(reqId)
        if request is None:
            return

        future = request.future
        bars = list(request.bars)
        loop = self._loop
        if loop is None:
            if not future.done():
                future.set_result(bars)
        else:
            loop.call_soon_threadsafe(self._set_future_result, future, bars)

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:  # noqa: N802
        request = self._historical_requests.get(reqId)
        if request is not None:
            loop = self._loop
            future = request.future
            if loop is None:
                if not future.done():
                    future.set_exception(RuntimeError(errorString))
            else:
                loop.call_soon_threadsafe(self._set_future_exception, future, errorString)
        super().error(reqId, errorCode, errorString, advancedOrderRejectJson)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_subscription_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _contract_id(contract: Any) -> int:
        if isinstance(contract, Mapping):
            value = contract.get("conId") or contract.get("con_id")
        else:
            value = getattr(contract, "conId", None) or getattr(contract, "con_id", None)
        if value is None:
            raise ValueError("Contract must define conId")
        return int(value)

    @staticmethod
    def _resolve_tick_type(value: int) -> str:
        try:
            name = TickTypeEnum.to_str(value)
        except Exception:  # pragma: no cover - defensive guard
            name = None
        return name or str(value)

    @staticmethod
    def _resolve_dom_operation(value: int) -> str:
        return {0: "insert", 1: "update", 2: "delete"}.get(value, str(value))

    @staticmethod
    def _resolve_dom_side(value: int) -> str:
        return {0: "bid", 1: "ask"}.get(value, str(value))

    @staticmethod
    def _resolve_bar_size_seconds(value: str) -> int:
        try:
            numeric = int(value)
            if numeric > 0:
                return numeric
        except (TypeError, ValueError):
            pass
        mappings = {
            "1": 1,
            "5": 5,
            "10": 10,
            "15": 15,
            "30": 30,
            "60": 60,
            "300": 300,
        }
        cleaned = str(value or "").strip().lower().replace(" secs", "").replace(" sec", "")
        cleaned = cleaned.replace(" seconds", "").replace(" second", "")
        cleaned = cleaned.replace(" s", "")
        return mappings.get(cleaned, 0)

    @staticmethod
    def _serialise_bar(bar: Any) -> dict[str, Any]:
        def _get(attr: str, default: Any = None) -> Any:
            if isinstance(bar, Mapping):
                return bar.get(attr, default)
            return getattr(bar, attr, default)

        timestamp = _get("date", _get("time", ""))
        return {
            "time": timestamp,
            "open": _get("open", _get("open_", 0.0)),
            "high": _get("high", 0.0),
            "low": _get("low", 0.0),
            "close": _get("close", 0.0),
            "volume": _get("volume", 0),
            "wap": _get("wap", 0.0),
            "count": _get("barCount", _get("count", 0)),
        }

    @staticmethod
    def _set_future_result(future: asyncio.Future[list[dict[str, Any]]], value: list[dict[str, Any]]) -> None:
        if not future.done():
            future.set_result(value)

    @staticmethod
    def _set_future_exception(future: asyncio.Future[list[dict[str, Any]]], message: str) -> None:
        if not future.done():
            future.set_exception(RuntimeError(message))


__all__ = ["ServiceIBClient"]
