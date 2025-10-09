from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import threading
from typing import Any, Mapping

from ibapi.execution import ExecutionFilter
from loguru import logger

from src.ib.base_client import BaseServiceIBClient
from src.ib.events import ExecutionEvent, OrderStatusEvent, execution_from_ib, order_status_from_ib


ORDERS_IB_HOST_ENV = "ORDERS_IB_HOST"
ORDERS_IB_PORT_ENV = "ORDERS_IB_PORT"
ORDERS_IB_CLIENT_ID_ENV = "ORDERS_IB_CLIENT_ID"

ORDER_STATUS_EVENT = "order_status"
OPEN_ORDER_EVENT = "open_order"
COMPLETED_ORDER_EVENT = "completed_order"
EXECUTION_EVENT = "execution"
COMMISSION_EVENT = "commission_report"

SUBMITTED_STATUSES = {"submitted", "presubmitted", "filled", "cancelled", "apicancelled", "inactive", "rejected"}
CANCELLED_STATUSES = {"cancelled", "apicancelled"}


def _safe_getattr(obj: Any, attr: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, attr)
    except AttributeError:
        return default
    return value if value != "" else default


def _contract_to_dict(contract: Any) -> dict[str, Any]:
    fields = (
        "conId",
        "symbol",
        "secType",
        "exchange",
        "currency",
        "localSymbol",
        "tradingClass",
    )
    data: dict[str, Any] = {}
    for field in fields:
        value = _safe_getattr(contract, field)
        if value is not None:
            data[field] = value
    return data


def _order_to_dict(order: Any) -> dict[str, Any]:
    fields = (
        "action",
        "orderType",
        "totalQuantity",
        "lmtPrice",
        "auxPrice",
        "tif",
        "outsideRth",
        "account",
        "permId",
        "transmit",
    )
    data: dict[str, Any] = {}
    for field in fields:
        value = _safe_getattr(order, field)
        if value is not None:
            data[field] = value
    return data


def _normalise_completed_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).isoformat()
    return None


def _order_state_to_dict(order_state: Any) -> dict[str, Any]:
    fields = (
        "status",
        "completedTime",
        "completedStatus",
        "warningText",
    )
    data: dict[str, Any] = {}
    for field in fields:
        value = _safe_getattr(order_state, field)
        if value is not None:
            key = field
            if field == "completedTime":
                key = "completed_time"
                value = _normalise_completed_time(value)
            data[key] = value
    return data


def _commission_to_dict(report: Any) -> dict[str, Any]:
    return {
        "exec_id": _safe_getattr(report, "execId"),
        "commission": _safe_getattr(report, "commission"),
        "currency": _safe_getattr(report, "currency"),
    }


@dataclass
class _OpenOrdersTracker:
    future: asyncio.Future[list[dict[str, Any]]]
    orders: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _CompletedOrdersTracker:
    future: asyncio.Future[list[dict[str, Any]]]
    orders: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _ExecutionTracker:
    future: asyncio.Future[list[dict[str, Any]]]
    events: list[ExecutionEvent] = field(default_factory=list)


class ServiceIBClient(BaseServiceIBClient):
    """Orders service Interactive Brokers client wrapper."""

    def __init__(self) -> None:
        super().__init__(
            service_name="orders",
            host_env=ORDERS_IB_HOST_ENV,
            port_env=ORDERS_IB_PORT_ENV,
            client_id_env=ORDERS_IB_CLIENT_ID_ENV,
        )
        self._order_status_waiters: dict[int, asyncio.Future[OrderStatusEvent]] = {}
        self._cancel_waiters: dict[int, asyncio.Future[OrderStatusEvent]] = {}
        self._order_waiters_lock = threading.Lock()
        self._open_orders_tracker: _OpenOrdersTracker | None = None
        self._open_orders_lock = threading.Lock()
        self._completed_orders_tracker: _CompletedOrdersTracker | None = None
        self._completed_orders_lock = threading.Lock()
        self._execution_trackers: dict[int, _ExecutionTracker] = {}
        self._execution_lock = threading.Lock()
        self._commission_cache: dict[str, dict[str, Any]] = {}
        self._commission_lock = threading.Lock()

    async def async_disconnect(self) -> None:
        self._cancel_pending()
        await super().async_disconnect()

    async def place_order(self, contract: Any, order: Any) -> OrderStatusEvent:
        if not self.isConnected():
            raise RuntimeError("Orders client is not connected")
        order_id = await self.next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OrderStatusEvent] = loop.create_future()
        with self._order_waiters_lock:
            self._order_status_waiters[order_id] = future
        logger.debug("Placing order %s via IB", order_id)
        self.placeOrder(order_id, contract, order)
        return await asyncio.wait_for(future, self.DEFAULT_TIMEOUT)

    async def cancel_order(self, order_id: int) -> OrderStatusEvent:
        if not self.isConnected():
            raise RuntimeError("Orders client is not connected")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OrderStatusEvent] = loop.create_future()
        with self._order_waiters_lock:
            self._cancel_waiters[order_id] = future
        logger.debug("Cancelling order %s via IB", order_id)
        self.cancelOrder(order_id)
        return await asyncio.wait_for(future, self.DEFAULT_TIMEOUT)

    async def fetch_open_orders(self, *, include_all_accounts: bool = False) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        tracker = _OpenOrdersTracker(future=future)
        with self._open_orders_lock:
            self._open_orders_tracker = tracker
        if include_all_accounts:
            self.reqAllOpenOrders()
        else:
            self.reqOpenOrders()
        orders = await asyncio.wait_for(future, self.DEFAULT_TIMEOUT)
        return orders

    async def fetch_completed_orders(self, days_back: int) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        tracker = _CompletedOrdersTracker(future=future)
        with self._completed_orders_lock:
            self._completed_orders_tracker = tracker
        self.reqCompletedOrders(False)
        orders = await asyncio.wait_for(future, self.DEFAULT_TIMEOUT)
        if days_back <= 0:
            return orders
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        filtered: list[dict[str, Any]] = []
        for order in orders:
            completed_time = order.get("state", {}).get("completed_time")
            if not completed_time:
                filtered.append(order)
                continue
            try:
                completed_dt = datetime.fromisoformat(completed_time)
            except ValueError:
                filtered.append(order)
                continue
            if completed_dt >= cutoff:
                filtered.append(order)
        return filtered

    async def fetch_executions(self, filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        req_id = await self.next_request_id()
        future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        tracker = _ExecutionTracker(future=future)
        with self._execution_lock:
            self._execution_trackers[req_id] = tracker
        filter_obj = ExecutionFilter()
        if filters:
            for key, value in filters.items():
                if hasattr(filter_obj, key):
                    setattr(filter_obj, key, str(value))
        self.reqExecutions(req_id, filter_obj)
        results = await asyncio.wait_for(future, self.DEFAULT_TIMEOUT)
        return results

    async def fetch_open_orders_for_all_accounts(self) -> list[dict[str, Any]]:
        return await self.fetch_open_orders(include_all_accounts=True)

    async def refresh_order_ids(self) -> None:
        self.reqIds(1)

    # --- Callback handlers -------------------------------------------------
    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:  # noqa: N802 - IB API signature
        super().orderStatus(
            orderId,
            status,
            filled,
            remaining,
            avgFillPrice,
            permId,
            parentId,
            lastFillPrice,
            clientId,
            whyHeld,
            mktCapPrice,
        )
        event = order_status_from_ib(
            order_id=orderId,
            status=status,
            filled=filled,
            remaining=remaining,
            avg_fill_price=avgFillPrice,
            perm_id=permId,
            parent_id=parentId,
            last_fill_price=lastFillPrice,
            client_id=clientId,
            why_held=whyHeld,
        )
        self.publish_event(ORDER_STATUS_EVENT, event)
        self._resolve_order_waiter(orderId, event)
        self._resolve_cancel_waiter(orderId, event)

    def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:  # noqa: N802
        payload = {
            "order_id": orderId,
            "contract": _contract_to_dict(contract),
            "order": _order_to_dict(order),
            "state": _order_state_to_dict(orderState),
        }
        self.publish_event(OPEN_ORDER_EVENT, payload)
        with self._open_orders_lock:
            tracker = self._open_orders_tracker
            if tracker is not None:
                tracker.orders.append(payload)

    def openOrderEnd(self) -> None:  # noqa: N802
        with self._open_orders_lock:
            tracker = self._open_orders_tracker
            self._open_orders_tracker = None
        if tracker is not None:
            self._complete_future(tracker.future, tracker.orders)

    def completedOrder(self, contract: Any, order: Any, orderState: Any) -> None:  # noqa: N802
        payload = {
            "contract": _contract_to_dict(contract),
            "order": _order_to_dict(order),
            "state": _order_state_to_dict(orderState),
        }
        self.publish_event(COMPLETED_ORDER_EVENT, payload)
        with self._completed_orders_lock:
            tracker = self._completed_orders_tracker
            if tracker is not None:
                tracker.orders.append(payload)

    def completedOrdersEnd(self) -> None:  # noqa: N802
        with self._completed_orders_lock:
            tracker = self._completed_orders_tracker
            self._completed_orders_tracker = None
        if tracker is not None:
            self._complete_future(tracker.future, tracker.orders)

    def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:  # noqa: N802
        event = execution_from_ib(
            exec_id=_safe_getattr(execution, "execId", ""),
            order_id=int(_safe_getattr(execution, "orderId", 0) or 0),
            client_id=_safe_getattr(execution, "clientId"),
            perm_id=_safe_getattr(execution, "permId"),
            symbol=_safe_getattr(contract, "symbol")
            or _safe_getattr(execution, "symbol", ""),
            security_type=_safe_getattr(contract, "secType")
            or _safe_getattr(execution, "secType", ""),
            exchange=_safe_getattr(execution, "exchange", ""),
            side=_safe_getattr(execution, "side", ""),
            shares=_safe_getattr(execution, "shares", 0),
            price=_safe_getattr(execution, "price", 0),
            cumulative_quantity=_safe_getattr(execution, "cumQty", 0),
            average_price=_safe_getattr(execution, "avgPrice", 0),
        )
        self.publish_event(EXECUTION_EVENT, event)
        with self._execution_lock:
            tracker = self._execution_trackers.get(reqId)
            if tracker is not None:
                tracker.events.append(event)

    def execDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        with self._execution_lock:
            tracker = self._execution_trackers.pop(reqId, None)
        if tracker is None:
            return
        results: list[dict[str, Any]] = []
        for event in tracker.events:
            with self._commission_lock:
                commission = self._commission_cache.pop(event.exec_id, None)
            payload = {
                "execution": event.model_dump(),
            }
            if commission:
                payload["commission"] = commission
            results.append(payload)
        self._complete_future(tracker.future, results)

    def commissionReport(self, commissionReport: Any) -> None:  # noqa: N802
        payload = _commission_to_dict(commissionReport)
        exec_id = payload.get("exec_id")
        if exec_id:
            with self._commission_lock:
                self._commission_cache[exec_id] = payload
        self.publish_event(COMMISSION_EVENT, payload)

    # --- Internal helpers --------------------------------------------------
    def _resolve_order_waiter(self, order_id: int, event: OrderStatusEvent) -> None:
        status = event.status.lower()
        if status not in SUBMITTED_STATUSES:
            return
        with self._order_waiters_lock:
            future = self._order_status_waiters.pop(order_id, None)
        if future is not None:
            self._complete_future(future, event)

    def _resolve_cancel_waiter(self, order_id: int, event: OrderStatusEvent) -> None:
        status = event.status.lower()
        if status not in CANCELLED_STATUSES:
            return
        with self._order_waiters_lock:
            future = self._cancel_waiters.pop(order_id, None)
        if future is not None:
            self._complete_future(future, event)

    def _complete_future(self, future: asyncio.Future[Any], result: Any) -> None:
        loop = self._loop
        if not loop:
            return

        def _set_result() -> None:
            if future.done():
                return
            future.set_result(result)

        loop.call_soon_threadsafe(_set_result)

    def _cancel_future_safe(self, future: asyncio.Future[Any]) -> None:
        loop = self._loop
        if not loop:
            return

        def _cancel() -> None:
            if not future.done():
                future.cancel()

        loop.call_soon_threadsafe(_cancel)

    def _cancel_pending(self) -> None:
        pending: list[asyncio.Future[Any]] = []
        with self._order_waiters_lock:
            pending.extend(self._order_status_waiters.values())
            pending.extend(self._cancel_waiters.values())
            self._order_status_waiters.clear()
            self._cancel_waiters.clear()
        with self._open_orders_lock:
            tracker = self._open_orders_tracker
            if tracker is not None:
                pending.append(tracker.future)
            self._open_orders_tracker = None
        with self._completed_orders_lock:
            tracker = self._completed_orders_tracker
            if tracker is not None:
                pending.append(tracker.future)
            self._completed_orders_tracker = None
        with self._execution_lock:
            for tracker in self._execution_trackers.values():
                pending.append(tracker.future)
            self._execution_trackers.clear()
        with self._commission_lock:
            self._commission_cache.clear()
        for future in pending:
            self._cancel_future_safe(future)


__all__ = ["ServiceIBClient"]
