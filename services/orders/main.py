from __future__ import annotations

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from src.ib.events import ExecutionEvent, OrderStatusEvent
from src.ib.redis_streaming import (
    RedisPublisher,
    publish_execution_event,
    publish_order_event,
)
from src.ibbot import update_service_health
from src.ibbot.logging import configure_service_logger
from src.ibbot.redis import get_redis_client
from src.observability.metrics import (
    build_metrics_router,
    record_ib_reconnect,
    set_subscription_gauge,
)


_LOGGER = configure_service_logger(
    "ibbot.services.orders",
    service_name="orders",
    ib_client_id_env_var="ORDERS_IB_CLIENT_ID",
)
ORDERS_SERVICE_NAME = "orders-service"


def _ibapi_available() -> bool:
    return importlib.util.find_spec("ibapi") is not None


if _ibapi_available():
    from services.orders.ib_client import ServiceIBClient  # noqa: F401
else:
    class ServiceIBClient:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - fallback
            raise RuntimeError(
                "Interactive Brokers API support is unavailable; install ibapi"
            )


@dataclass
class OrdersServiceState:
    redis: Any | None = None
    ib_client: ServiceIBClient | None = None
    publisher: RedisPublisher | None = None
    event_task: asyncio.Task | None = None
    shutdown_event: asyncio.Event | None = None
    subscription_lock: asyncio.Lock | None = None
    subscription_count: int = 0
    last_error: str | None = None


_state = OrdersServiceState()


class ContractPayload(BaseModel):
    symbol: str
    sec_type: str | None = Field(default=None, validation_alias=AliasChoices("sec_type", "secType"))
    exchange: str | None = None
    currency: str | None = None
    local_symbol: str | None = Field(
        default=None, validation_alias=AliasChoices("local_symbol", "localSymbol")
    )
    primary_exchange: str | None = Field(
        default=None, validation_alias=AliasChoices("primary_exchange", "primaryExchange")
    )
    trading_class: str | None = Field(
        default=None, validation_alias=AliasChoices("trading_class", "tradingClass")
    )
    con_id: int | None = Field(default=None, validation_alias=AliasChoices("con_id", "conId"))

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OrderPayload(BaseModel):
    action: str
    total_quantity: float | None = Field(
        default=None, validation_alias=AliasChoices("total_quantity", "totalQuantity")
    )
    order_type: str = Field(
        default="MARKET", validation_alias=AliasChoices("order_type", "orderType")
    )
    lmt_price: float | None = Field(
        default=None, validation_alias=AliasChoices("lmt_price", "lmtPrice")
    )
    aux_price: float | None = Field(
        default=None, validation_alias=AliasChoices("aux_price", "auxPrice")
    )
    tif: str | None = None
    transmit: bool | None = None
    account: str | None = None

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OrderSubmissionRequest(BaseModel):
    contract: ContractPayload
    order: OrderPayload


class OrdersSubscriptionRequest(BaseModel):
    notes: str | None = None


async def _startup() -> None:
    await _ensure_redis_client()
    await _publish_service_health(status="starting")
    set_subscription_gauge(service="orders", channel="orders-stream", count=0)


async def _shutdown() -> None:
    await _stop_event_consumer()
    await _disconnect_ib_client()
    await _close_publisher()
    await _close_redis()
    set_subscription_gauge(service="orders", channel="orders-stream", count=0)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(title="Orders Service", lifespan=_lifespan)
app.include_router(build_metrics_router())


async def _ensure_redis_client() -> Any | None:
    if _state.redis is not None:
        return _state.redis
    try:
        client = await get_redis_client()
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Failed to acquire Redis client: %s", exc)
        _state.last_error = str(exc)
        return None
    _state.redis = client
    return client


async def _close_redis() -> None:
    client = _state.redis
    _state.redis = None
    if client is None:
        return
    close = getattr(client, "close", None)
    if close is None:
        return
    if asyncio.iscoroutinefunction(close):
        await close()
    else:  # pragma: no cover - synchronous close
        close()


async def _publish_service_health(*, status: str, last_error: str | None = None) -> None:
    redis_client = await _ensure_redis_client()
    if redis_client is None:
        return
    try:
        await update_service_health(
            redis_client,
            ORDERS_SERVICE_NAME,
            status=status,
            last_error=last_error or "",
            logger=_LOGGER,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Failed to publish orders service health: %s", exc)


async def _ensure_publisher() -> RedisPublisher | None:
    publisher = _state.publisher
    if publisher is not None:
        return publisher
    redis_client = await _ensure_redis_client()
    if redis_client is None:
        return None
    publisher = RedisPublisher(redis=redis_client, service_name="orders")
    _state.publisher = publisher
    return publisher


async def _close_publisher() -> None:
    publisher = _state.publisher
    _state.publisher = None
    if publisher is not None:
        await publisher.close()


def _get_subscription_lock() -> asyncio.Lock:
    lock = _state.subscription_lock
    if lock is None:
        lock = asyncio.Lock()
        _state.subscription_lock = lock
    return lock


async def _ensure_ib_client() -> ServiceIBClient:
    client = _state.ib_client
    if client is not None and client.isConnected():  # type: ignore[attr-defined]
        return client
    client = ServiceIBClient()
    await client.connect()
    await client.wait_until_ready()
    record_ib_reconnect(service="orders")
    _state.ib_client = client
    return client


async def _disconnect_ib_client() -> None:
    client = _state.ib_client
    if client is None:
        return
    _state.ib_client = None
    try:
        await client.disconnect()
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Error while disconnecting orders IB client: %s", exc)


async def _start_event_consumer() -> None:
    if _state.event_task is not None:
        return
    client = _state.ib_client
    publisher = _state.publisher
    if client is None or publisher is None:
        return
    _state.shutdown_event = asyncio.Event()
    _state.event_task = asyncio.create_task(_orders_event_loop())


async def _stop_event_consumer() -> None:
    task = _state.event_task
    if task is None:
        return
    shutdown_event = _state.shutdown_event
    if shutdown_event is not None:
        shutdown_event.set()
    queue = None
    client = _state.ib_client
    if client is not None:
        queue = getattr(client, "callback_queue", None)
    if queue is not None:
        try:
            await queue.put(("__shutdown__", None))
        except Exception:
            pass
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Orders event loop cancellation raised: %s", exc)
    except asyncio.CancelledError:  # pragma: no cover
        pass
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Orders event loop shutdown error: %s", exc)
    _state.event_task = None
    _state.shutdown_event = None


ORDERS_EVENT_DISPATCH: dict[str, Any] = {
    "order_status": publish_order_event,
    "open_order": publish_order_event,
    "completed_order": publish_order_event,
    "execution": publish_execution_event,
    "commission_report": publish_execution_event,
}


async def _dispatch_orders_event(event_type: str, payload: Any) -> None:
    publisher = _state.publisher
    if publisher is None:
        return
    handler = ORDERS_EVENT_DISPATCH.get(event_type)
    if handler is None:
        _LOGGER.debug("Ignoring unsupported orders event '%s'", event_type)
        return
    try:
        await handler(publisher, payload)
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Failed to publish orders event %s: %s", event_type, exc)


async def _orders_event_loop() -> None:
    client = _state.ib_client
    if client is None:
        return
    try:
        queue = client.callback_queue  # type: ignore[attr-defined]
    except Exception:
        _LOGGER.warning("Orders client callback queue unavailable; stopping event loop")
        return
    shutdown_event = _state.shutdown_event
    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            break
        try:
            event_type, payload = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:  # pragma: no cover
            break
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Orders event loop read error: %s", exc)
            continue
        if event_type == "__shutdown__":
            break
        await _dispatch_orders_event(event_type, payload)


def _assign_attributes(target: Any, values: Mapping[str, Any]) -> Any:
    for key, value in values.items():
        setattr(target, key, value)
    return target


def _build_contract(payload: ContractPayload) -> Any:
    if _ibapi_available():
        from ibapi.contract import Contract  # type: ignore

        contract: Any = Contract()
    else:  # pragma: no cover - exercised when ibapi missing
        contract = type("Contract", (), {})()
    data = payload.model_dump(mode="python", by_alias=True)
    return _assign_attributes(contract, data)


def _build_order(payload: OrderPayload) -> Any:
    if _ibapi_available():
        from ibapi.order import Order  # type: ignore

        order: Any = Order()
    else:  # pragma: no cover - exercised when ibapi missing
        order = type("Order", (), {})()
    data = payload.model_dump(mode="python", by_alias=True)
    return _assign_attributes(order, data)


async def _get_client_and_publisher() -> ServiceIBClient:
    publisher = await _ensure_publisher()
    if publisher is None:
        await _publish_service_health(status="degraded", last_error="redis-unavailable")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis unavailable")
    try:
        client = await _ensure_ib_client()
    except Exception as exc:
        _LOGGER.warning("Orders client unavailable: %s", exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Orders client unavailable"
        ) from exc
    await _start_event_consumer()
    await _publish_service_health(status="running")
    return client


@app.post("/subscriptions/orders")
async def create_orders_subscription(
    _: OrdersSubscriptionRequest | None = None,
) -> Mapping[str, Any]:
    await _get_client_and_publisher()
    lock = _get_subscription_lock()
    async with lock:
        _state.subscription_count += 1
        count = _state.subscription_count
    set_subscription_gauge(service="orders", channel="orders-stream", count=count)
    return {"success": True, "subscription_count": count}


@app.delete("/subscriptions/orders")
async def delete_orders_subscription() -> Mapping[str, Any]:
    lock = _get_subscription_lock()
    async with lock:
        if _state.subscription_count > 0:
            _state.subscription_count -= 1
        count = _state.subscription_count
    set_subscription_gauge(service="orders", channel="orders-stream", count=count)
    if count == 0:
        await _stop_event_consumer()
        await _disconnect_ib_client()
        await _close_publisher()
        await _publish_service_health(status="idle")
    return {"success": True, "subscription_count": count}


@app.get("/orders")
async def list_orders(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    try:
        orders = await client.fetch_open_orders(include_all_accounts=True)
    except Exception as exc:
        _LOGGER.warning("Failed to fetch orders list: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Orders unavailable"
        ) from exc

    total = len(orders)
    start = min(max(offset, 0), total)
    end = min(start + limit, total)
    paginated = orders[start:end]
    now_iso = datetime.now(timezone.utc).isoformat()

    metadata = {
        "page": (start // limit) + 1 if limit else 1,
        "limit": limit,
        "total": total,
        "returned": len(paginated),
        "has_more": end < total,
    }

    payload: dict[str, Any] = {
        "success": True,
        "orders": jsonable_encoder(paginated),
        "meta": metadata,
        "from_cache": False,
        "snapshot_updated_at": now_iso,
        "source": ORDERS_SERVICE_NAME,
    }
    return payload


@app.post("/orders", status_code=status.HTTP_201_CREATED)
async def place_order(request: OrderSubmissionRequest) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    contract = _build_contract(request.contract)
    order = _build_order(request.order)
    try:
        status_event = await client.place_order(contract, order)
    except Exception as exc:
        _LOGGER.warning("Failed to place order: %s", exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Order submission failed") from exc
    payload = status_event if isinstance(status_event, OrderStatusEvent) else status_event
    return {"status": jsonable_encoder(payload)}


@app.delete("/orders/{order_id}")
async def cancel_order(order_id: int) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    try:
        status_event = await client.cancel_order(order_id)
    except Exception as exc:
        _LOGGER.warning("Failed to cancel order %s: %s", order_id, exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Order cancellation failed") from exc
    payload = status_event if isinstance(status_event, OrderStatusEvent) else status_event
    return {"status": jsonable_encoder(payload)}


@app.get("/orders/open")
async def get_open_orders(include_all_accounts: bool = Query(default=False)) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    try:
        orders = await client.fetch_open_orders(include_all_accounts=include_all_accounts)
    except Exception as exc:
        _LOGGER.warning("Failed to fetch open orders: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Open orders unavailable") from exc
    return {"orders": jsonable_encoder(orders)}


@app.get("/orders/completed")
async def get_completed_orders(days_back: int = Query(default=7, ge=0)) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    try:
        orders = await client.fetch_completed_orders(days_back)
    except Exception as exc:
        _LOGGER.warning("Failed to fetch completed orders: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Completed orders unavailable") from exc
    return {"orders": jsonable_encoder(orders)}


def _build_execution_filters(
    account: str | None,
    time: str | None,
    symbol: str | None,
    security_type: str | None,
    exchange: str | None,
    side: str | None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if account:
        filters["acctCode"] = account
    if time:
        filters["time"] = time
    if symbol:
        filters["symbol"] = symbol
    if security_type:
        filters["secType"] = security_type
    if exchange:
        filters["exchange"] = exchange
    if side:
        filters["side"] = side
    return filters


@app.get("/orders/executions")
async def get_executions(
    request: Request,
    account: str | None = Query(default=None, alias="acctCode"),
    time: str | None = None,
    symbol: str | None = None,
    security_type: str | None = Query(default=None, alias="secType"),
    exchange: str | None = None,
    side: str | None = None,
) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    filters = _build_execution_filters(account, time, symbol, security_type, exchange, side)
    if not filters and request.query_params:
        # Allow arbitrary filters via query parameters for advanced callers.
        filters = dict(request.query_params)
    try:
        executions = await client.fetch_executions(filters or None)
    except Exception as exc:
        _LOGGER.warning("Failed to fetch executions: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Executions unavailable") from exc
    encoded = []
    for item in executions:
        if isinstance(item, ExecutionEvent):
            encoded.append(jsonable_encoder(item))
        else:
            encoded.append(jsonable_encoder(item))
    return {"executions": encoded}


__all__ = [
    "app",
    "ContractPayload",
    "OrderPayload",
    "OrderSubmissionRequest",
    "OrdersSubscriptionRequest",
]
