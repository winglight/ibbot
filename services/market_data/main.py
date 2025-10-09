from __future__ import annotations

import asyncio
import importlib
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Mapping

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from src.ib.events import (
    DOMLevelEvent,
    ErrorEvent,
    MarketTickEvent,
    RealTimeBarEvent,
    TickByTickEvent,
)
from src.ib.redis_streaming import (
    RedisPublisher,
    publish_dom_event,
    publish_error_event,
    publish_market_tick_event,
    publish_real_time_bar_event,
    publish_tick_by_tick_event,
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
    "ibbot.services.market_data",
    service_name="market-data",
    ib_client_id_env_var="MARKET_DATA_IB_CLIENT_ID",
)
MARKET_DATA_SERVICE_NAME = "market-data-service"
DEFAULT_MAX_SUBSCRIPTIONS = 50


def _ibapi_available() -> bool:
    return importlib.util.find_spec("ibapi") is not None


if _ibapi_available():
    from services.market_data.ib_client import ServiceIBClient  # noqa: F401
else:
    class ServiceIBClient:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - fallback
            raise RuntimeError(
                "Interactive Brokers API support is unavailable; install ibapi"
            )


@dataclass
class SubscriptionMetadata:
    contract_id: int
    symbol: str | None = None


@dataclass
class MarketDataServiceState:
    redis: Any | None = None
    publisher: RedisPublisher | None = None
    ib_client: ServiceIBClient | None = None
    event_task: asyncio.Task | None = None
    shutdown_event: asyncio.Event | None = None
    subscription_lock: asyncio.Lock | None = None
    pending_subscriptions: int = 0
    quote_subscriptions: dict[str, SubscriptionMetadata] = field(default_factory=dict)
    dom_subscriptions: dict[str, SubscriptionMetadata] = field(default_factory=dict)
    tick_by_tick_subscriptions: dict[str, SubscriptionMetadata] = field(default_factory=dict)
    real_time_bar_subscriptions: dict[str, SubscriptionMetadata] = field(default_factory=dict)
    contract_symbols: dict[int, str] = field(default_factory=dict)
    last_error: str | None = None
    max_subscriptions: int = DEFAULT_MAX_SUBSCRIPTIONS
    delayed_fallback_enabled: bool = True
    using_delayed_data: bool = False
    current_market_data_type: int | None = None


_state = MarketDataServiceState()


class ContractPayload(BaseModel):
    con_id: int = Field(validation_alias=AliasChoices("conId", "con_id"))
    symbol: str | None = None
    sec_type: str | None = Field(
        default=None, validation_alias=AliasChoices("secType", "sec_type")
    )
    exchange: str | None = None
    currency: str | None = None
    local_symbol: str | None = Field(
        default=None, validation_alias=AliasChoices("localSymbol", "local_symbol")
    )
    primary_exchange: str | None = Field(
        default=None,
        validation_alias=AliasChoices("primaryExchange", "primary_exchange"),
    )
    trading_class: str | None = Field(
        default=None, validation_alias=AliasChoices("tradingClass", "trading_class")
    )

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class QuoteSubscriptionRequest(BaseModel):
    contract: ContractPayload
    tick_types: list[int] | None = Field(
        default=None, validation_alias=AliasChoices("tickTypes", "tick_types")
    )
    snapshot: bool = False

    model_config = ConfigDict(populate_by_name=True)


class DomSubscriptionRequest(BaseModel):
    contract: ContractPayload
    depth: int = Field(default=5, ge=1, le=40)
    exchange: str | None = None


class TickByTickSubscriptionRequest(BaseModel):
    contract: ContractPayload
    tick_type: str = Field(validation_alias=AliasChoices("tickType", "tick_type"))


class RealTimeBarsSubscriptionRequest(BaseModel):
    contract: ContractPayload
    bar_size: str = Field(validation_alias=AliasChoices("barSize", "bar_size"))
    what_to_show: str = Field(
        default="TRADES",
        validation_alias=AliasChoices("whatToShow", "what_to_show"),
    )



def _normalize_symbol(value: str | None) -> str | None:
    if not value:
        return None
    result = value.strip().upper()
    return result or None


def _get_subscription_lock() -> asyncio.Lock:
    lock = _state.subscription_lock
    if lock is None:
        lock = asyncio.Lock()
        _state.subscription_lock = lock
    return lock


def _current_total_subscriptions() -> int:
    return (
        len(_state.quote_subscriptions)
        + len(_state.dom_subscriptions)
        + len(_state.tick_by_tick_subscriptions)
        + len(_state.real_time_bar_subscriptions)
    )


def _update_subscription_metrics() -> None:
    set_subscription_gauge(
        service="market-data",
        channel="quotes",
        count=len(_state.quote_subscriptions),
    )
    set_subscription_gauge(
        service="market-data",
        channel="dom",
        count=len(_state.dom_subscriptions),
    )
    set_subscription_gauge(
        service="market-data",
        channel="tick-by-tick",
        count=len(_state.tick_by_tick_subscriptions),
    )
    set_subscription_gauge(
        service="market-data",
        channel="real-time-bars",
        count=len(_state.real_time_bar_subscriptions),
    )


async def _reserve_slot() -> None:
    lock = _get_subscription_lock()
    async with lock:
        if (
            _current_total_subscriptions() + _state.pending_subscriptions
            >= _state.max_subscriptions
        ):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "Subscription limit reached"
            )
        _state.pending_subscriptions += 1


async def _release_reservation() -> None:
    lock = _get_subscription_lock()
    async with lock:
        if _state.pending_subscriptions > 0:
            _state.pending_subscriptions -= 1


async def _commit_subscription(
    storage: dict[str, SubscriptionMetadata],
    subscription_id: str,
    metadata: SubscriptionMetadata,
) -> int:
    lock = _get_subscription_lock()
    async with lock:
        if _state.pending_subscriptions > 0:
            _state.pending_subscriptions -= 1
        storage[subscription_id] = metadata
        _record_contract_symbol_locked(metadata.contract_id, metadata.symbol)
        total = _current_total_subscriptions()
        _update_subscription_metrics()
    return total


async def _remove_subscription(
    subscription_id: str,
    storage: dict[str, SubscriptionMetadata],
) -> tuple[SubscriptionMetadata | None, int]:
    lock = _get_subscription_lock()
    async with lock:
        metadata = storage.pop(subscription_id, None)
        if metadata is not None:
            _release_contract_symbol_locked(metadata.contract_id)
        remaining = _current_total_subscriptions()
        _update_subscription_metrics()
    return metadata, remaining


def _record_contract_symbol_locked(contract_id: int, symbol: str | None) -> None:
    normalized = _normalize_symbol(symbol)
    if normalized:
        _state.contract_symbols[contract_id] = normalized


def _release_contract_symbol_locked(contract_id: int) -> None:
    for mapping in (
        _state.quote_subscriptions,
        _state.dom_subscriptions,
        _state.tick_by_tick_subscriptions,
        _state.real_time_bar_subscriptions,
    ):
        if any(entry.contract_id == contract_id for entry in mapping.values()):
            return
    _state.contract_symbols.pop(contract_id, None)


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


async def _publish_service_health(
    *, status: str, last_error: str | None = None
) -> None:
    redis_client = await _ensure_redis_client()
    if redis_client is None:
        return
    try:
        await update_service_health(
            redis_client,
            MARKET_DATA_SERVICE_NAME,
            status=status,
            last_error=last_error or _state.last_error or "",
            logger=_LOGGER,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Failed to publish market data service health: %s", exc)


async def _ensure_publisher() -> RedisPublisher | None:
    publisher = _state.publisher
    if publisher is not None:
        return publisher
    redis_client = await _ensure_redis_client()
    if redis_client is None:
        return None
    publisher = RedisPublisher(redis=redis_client, service_name="market-data")
    _state.publisher = publisher
    return publisher


async def _close_publisher() -> None:
    publisher = _state.publisher
    _state.publisher = None
    if publisher is not None:
        await publisher.close()


async def _ensure_ib_client() -> ServiceIBClient:
    client = _state.ib_client
    if client is not None:
        return client
    client = ServiceIBClient()
    await client.connect()
    await client.wait_until_ready()
    record_ib_reconnect(service="market-data")
    _state.ib_client = client
    _refresh_configuration(client)
    return client


async def _disconnect_ib_client() -> None:
    client = _state.ib_client
    if client is None:
        return
    _state.ib_client = None
    try:
        await client.disconnect()
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Error while disconnecting market data client: %s", exc)


async def _start_event_consumer() -> None:
    if _state.event_task is not None:
        return
    if _state.ib_client is None or _state.publisher is None:
        return
    _state.shutdown_event = asyncio.Event()
    _state.event_task = asyncio.create_task(_market_data_event_loop())


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
        try:
            queue = client.callback_queue  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive guard
            queue = None
    if queue is not None:
        try:
            await queue.put(("__shutdown__", None))
        except Exception:  # pragma: no cover - defensive guard
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
            _LOGGER.warning("Market data event loop cancellation raised: %s", exc)
    except asyncio.CancelledError:  # pragma: no cover
        pass
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Market data event loop shutdown error: %s", exc)
    _state.event_task = None
    _state.shutdown_event = None


async def _maybe_teardown_when_idle(remaining: int) -> None:
    if remaining > 0:
        await _publish_service_health(status="running")
        return
    await _stop_event_consumer()
    await _disconnect_ib_client()
    await _close_publisher()
    await _publish_service_health(status="idle")


def _refresh_configuration(client: ServiceIBClient | None) -> None:
    _state.max_subscriptions = _read_max_subscriptions()
    _state.delayed_fallback_enabled = _env_flag(
        "MARKET_DATA_DELAYED_FALLBACK", default=True
    )
    market_data_type = _resolve_market_data_type()
    _state.current_market_data_type = market_data_type
    _state.using_delayed_data = market_data_type in {3, 4}
    if client is None:
        return
    try:
        client.reqMarketDataType(market_data_type)
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Failed to apply market data type %s: %s", market_data_type, exc)
        _state.last_error = str(exc)


def _read_max_subscriptions() -> int:
    raw = os.getenv("MARKET_DATA_MAX_SUBSCRIPTIONS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SUBSCRIPTIONS
    return max(1, value)


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _resolve_market_data_type() -> int:
    raw = os.getenv("MARKET_DATA_TYPE", "")
    value: int | None = None
    if raw:
        key = raw.strip().lower().replace("-", "_")
        mapping = {
            "live": 1,
            "real_time": 1,
            "realtime": 1,
            "frozen": 2,
            "delayed": 3,
            "delayed_frozen": 4,
        }
        value = mapping.get(key)
        if value is None:
            try:
                value = int(key)
            except ValueError:
                value = None
    if _env_flag("MARKET_DATA_REGULATORY_SNAPSHOT", default=False):
        return 4
    return value or 1



def _build_contract(payload: ContractPayload) -> tuple[Any, int, str | None]:
    if _ibapi_available():
        from ibapi.contract import Contract  # type: ignore

        contract: Any = Contract()
    else:  # pragma: no cover - exercised when ibapi missing
        contract = type("Contract", (), {})()
    data = payload.model_dump(mode="python", by_alias=True)
    for key, value in data.items():
        setattr(contract, key, value)
    contract_id = getattr(contract, "conId", None)
    if contract_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "contract.conId is required")
    return contract, int(contract_id), data.get("symbol")


async def _call_with_fallback(
    action: Callable[[], Awaitable[Any]], *, client: ServiceIBClient
) -> Any:
    try:
        return await action()
    except Exception as exc:
        if not _state.delayed_fallback_enabled or _state.using_delayed_data:
            raise
        _LOGGER.warning(
            "Market data request failed; attempting delayed data fallback: %s", exc
        )
        try:
            client.reqMarketDataType(3)
        except Exception as config_exc:  # pragma: no cover - defensive guard
            _LOGGER.warning(
                "Failed to enable delayed data fallback: %s", config_exc
            )
            raise
        _state.using_delayed_data = True
        _state.current_market_data_type = 3
        return await action()


def _dynamic_channel(prefix: str, contract_id: int, symbol: str | None) -> str:
    normalized = _normalize_symbol(symbol)
    if normalized:
        return f"{prefix}:{normalized}"
    return f"{prefix}:conid:{contract_id}"


async def _publish_with_dynamic_channel(
    *,
    payload: MarketTickEvent | DOMLevelEvent | TickByTickEvent | RealTimeBarEvent,
    publisher: RedisPublisher,
    general_publisher: Callable[[RedisPublisher, Any], Awaitable[None]] | None,
    prefix: str,
    contract_id: int,
) -> None:
    if general_publisher is not None:
        try:
            await general_publisher(publisher, payload)
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Failed to publish canonical market data event: %s", exc)
    symbol = _state.contract_symbols.get(contract_id)
    channel = _dynamic_channel(prefix, contract_id, symbol)
    data = payload.model_dump()
    if symbol:
        data.setdefault("symbol", symbol)
    try:
        await publisher.publish(channel, data)
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Failed to publish %s event to channel %s: %s", prefix, channel, exc)


async def _dispatch_market_data_event(event_type: str, payload: Any) -> None:
    publisher = _state.publisher
    if publisher is None:
        return
    if event_type == "market_tick" and isinstance(payload, MarketTickEvent):
        await _publish_with_dynamic_channel(
            payload=payload,
            publisher=publisher,
            general_publisher=publish_market_tick_event,
            prefix="md:quotes",
            contract_id=payload.contract_id,
        )
        return
    if event_type == "dom_update" and isinstance(payload, DOMLevelEvent):
        await _publish_with_dynamic_channel(
            payload=payload,
            publisher=publisher,
            general_publisher=publish_dom_event,
            prefix="md:dom",
            contract_id=payload.contract_id,
        )
        return
    if event_type == "tick_by_tick" and isinstance(payload, TickByTickEvent):
        await _publish_with_dynamic_channel(
            payload=payload,
            publisher=publisher,
            general_publisher=publish_tick_by_tick_event,
            prefix="md:tick-by-tick",
            contract_id=payload.contract_id,
        )
        return
    if event_type == "real_time_bar" and isinstance(payload, RealTimeBarEvent):
        await _publish_with_dynamic_channel(
            payload=payload,
            publisher=publisher,
            general_publisher=publish_real_time_bar_event,
            prefix="md:bars",
            contract_id=payload.contract_id,
        )
        return
    if event_type == "error" and isinstance(payload, ErrorEvent):
        try:
            await publish_error_event(publisher, payload)
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Failed to publish error event: %s", exc)
        _state.last_error = payload.message
        return
    _LOGGER.debug("Ignoring unsupported market data event '%s'", event_type)


async def _market_data_event_loop() -> None:
    client = _state.ib_client
    if client is None:
        return
    try:
        queue = client.callback_queue  # type: ignore[attr-defined]
    except Exception:
        _LOGGER.warning("Market data client callback queue unavailable; stopping event loop")
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
            _LOGGER.warning("Market data event loop read error: %s", exc)
            continue
        if event_type == "__shutdown__":
            break
        await _dispatch_market_data_event(event_type, payload)


async def _get_client_and_publisher() -> ServiceIBClient:
    publisher = await _ensure_publisher()
    if publisher is None:
        await _publish_service_health(status="degraded", last_error="redis-unavailable")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis unavailable")
    try:
        client = await _ensure_ib_client()
    except Exception as exc:
        _LOGGER.warning("Market data client unavailable: %s", exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Market data client unavailable"
        ) from exc
    await _start_event_consumer()
    await _publish_service_health(status="running")
    return client


async def _startup() -> None:
    _refresh_configuration(None)
    await _publish_service_health(status="starting")
    _update_subscription_metrics()


async def _shutdown() -> None:
    await _stop_event_consumer()
    await _disconnect_ib_client()
    await _close_publisher()
    await _close_redis()
    _update_subscription_metrics()


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(title="Market Data Service", lifespan=_lifespan)
app.include_router(build_metrics_router())


@app.post("/subscriptions/quotes")
async def create_quote_subscription(
    request: QuoteSubscriptionRequest,
) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    contract, contract_id, symbol = _build_contract(request.contract)
    tick_types = sorted({int(code) for code in request.tick_types or []})

    await _reserve_slot()
    try:
        subscription_id = await _call_with_fallback(
            lambda: client.subscribe_quotes(
                contract, snapshot=request.snapshot, tick_types=tick_types
            ),
            client=client,
        )
    except Exception as exc:
        await _release_reservation()
        _LOGGER.warning("Failed to create quote subscription: %s", exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Quote subscription failed"
        ) from exc

    metadata = SubscriptionMetadata(
        contract_id=contract_id, symbol=_normalize_symbol(symbol)
    )
    total = await _commit_subscription(
        _state.quote_subscriptions, subscription_id, metadata
    )
    await _publish_service_health(status="running")
    return {
        "subscription_id": subscription_id,
        "contract_id": contract_id,
        "symbol": metadata.symbol,
        "tick_types": tick_types,
        "snapshot": request.snapshot,
        "subscription_count": total,
        "using_delayed_data": _state.using_delayed_data,
    }


@app.delete("/subscriptions/quotes/{subscription_id}")
async def delete_quote_subscription(subscription_id: str) -> Mapping[str, Any]:
    metadata, remaining = await _remove_subscription(
        subscription_id, _state.quote_subscriptions
    )
    client = _state.ib_client
    if metadata is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"success": False, "subscription_count": remaining},
        )
    if client is not None:
        try:
            await client.unsubscribe_quotes(subscription_id)
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Failed to cancel quote subscription %s: %s", subscription_id, exc)
    await _maybe_teardown_when_idle(remaining)
    return {"success": True, "subscription_count": remaining}


@app.post("/subscriptions/dom")
async def create_dom_subscription(
    request: DomSubscriptionRequest,
) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    contract, contract_id, symbol = _build_contract(request.contract)

    await _reserve_slot()
    try:
        subscription_id = await _call_with_fallback(
            lambda: client.subscribe_dom(
                contract, depth=request.depth, exchange=request.exchange
            ),
            client=client,
        )
    except Exception as exc:
        await _release_reservation()
        _LOGGER.warning("Failed to create DOM subscription: %s", exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "DOM subscription failed"
        ) from exc

    metadata = SubscriptionMetadata(
        contract_id=contract_id, symbol=_normalize_symbol(symbol)
    )
    total = await _commit_subscription(
        _state.dom_subscriptions, subscription_id, metadata
    )
    await _publish_service_health(status="running")
    return {
        "subscription_id": subscription_id,
        "contract_id": contract_id,
        "symbol": metadata.symbol,
        "depth": request.depth,
        "subscription_count": total,
        "using_delayed_data": _state.using_delayed_data,
    }


@app.delete("/subscriptions/dom/{subscription_id}")
async def delete_dom_subscription(subscription_id: str) -> Mapping[str, Any]:
    metadata, remaining = await _remove_subscription(
        subscription_id, _state.dom_subscriptions
    )
    client = _state.ib_client
    if metadata is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"success": False, "subscription_count": remaining},
        )
    if client is not None:
        try:
            await client.unsubscribe_dom(subscription_id)
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning("Failed to cancel DOM subscription %s: %s", subscription_id, exc)
    await _maybe_teardown_when_idle(remaining)
    return {"success": True, "subscription_count": remaining}


@app.post("/subscriptions/tick-by-tick")
async def create_tick_by_tick_subscription(
    request: TickByTickSubscriptionRequest,
) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    contract, contract_id, symbol = _build_contract(request.contract)
    tick_type = request.tick_type

    await _reserve_slot()
    try:
        subscription_id = await _call_with_fallback(
            lambda: client.subscribe_tick_by_tick(
                contract, tick_type=tick_type
            ),
            client=client,
        )
    except Exception as exc:
        await _release_reservation()
        _LOGGER.warning("Failed to create tick-by-tick subscription: %s", exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Tick-by-tick subscription failed"
        ) from exc

    metadata = SubscriptionMetadata(
        contract_id=contract_id, symbol=_normalize_symbol(symbol)
    )
    total = await _commit_subscription(
        _state.tick_by_tick_subscriptions, subscription_id, metadata
    )
    await _publish_service_health(status="running")
    return {
        "subscription_id": subscription_id,
        "contract_id": contract_id,
        "symbol": metadata.symbol,
        "tick_type": tick_type,
        "subscription_count": total,
        "using_delayed_data": _state.using_delayed_data,
    }


@app.delete("/subscriptions/tick-by-tick/{subscription_id}")
async def delete_tick_by_tick_subscription(subscription_id: str) -> Mapping[str, Any]:
    metadata, remaining = await _remove_subscription(
        subscription_id, _state.tick_by_tick_subscriptions
    )
    client = _state.ib_client
    if metadata is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"success": False, "subscription_count": remaining},
        )
    if client is not None:
        try:
            await client.unsubscribe_tick_by_tick(subscription_id)
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning(
                "Failed to cancel tick-by-tick subscription %s: %s",
                subscription_id,
                exc,
            )
    await _maybe_teardown_when_idle(remaining)
    return {"success": True, "subscription_count": remaining}


@app.post("/subscriptions/real-time-bars")
async def create_real_time_bars_subscription(
    request: RealTimeBarsSubscriptionRequest,
) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    contract, contract_id, symbol = _build_contract(request.contract)

    await _reserve_slot()
    try:
        subscription_id = await _call_with_fallback(
            lambda: client.subscribe_real_time_bars(
                contract,
                bar_size=request.bar_size,
                what_to_show=request.what_to_show,
            ),
            client=client,
        )
    except Exception as exc:
        await _release_reservation()
        _LOGGER.warning("Failed to create real-time bars subscription: %s", exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Real-time bars subscription failed"
        ) from exc

    metadata = SubscriptionMetadata(
        contract_id=contract_id, symbol=_normalize_symbol(symbol)
    )
    total = await _commit_subscription(
        _state.real_time_bar_subscriptions, subscription_id, metadata
    )
    await _publish_service_health(status="running")
    return {
        "subscription_id": subscription_id,
        "contract_id": contract_id,
        "symbol": metadata.symbol,
        "bar_size": request.bar_size,
        "what_to_show": request.what_to_show,
        "subscription_count": total,
        "using_delayed_data": _state.using_delayed_data,
    }


@app.delete("/subscriptions/real-time-bars/{subscription_id}")
async def delete_real_time_bars_subscription(
    subscription_id: str,
) -> Mapping[str, Any]:
    metadata, remaining = await _remove_subscription(
        subscription_id, _state.real_time_bar_subscriptions
    )
    client = _state.ib_client
    if metadata is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"success": False, "subscription_count": remaining},
        )
    if client is not None:
        try:
            await client.unsubscribe_real_time_bars(subscription_id)
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.warning(
                "Failed to cancel real-time bars subscription %s: %s",
                subscription_id,
                exc,
            )
    await _maybe_teardown_when_idle(remaining)
    return {"success": True, "subscription_count": remaining}


@app.get("/historical")
async def get_historical_data(
    con_id: int = Query(..., alias="conId"),
    duration: str = Query(...),
    bar_size: str = Query(..., alias="barSize"),
    what_to_show: str = Query("TRADES", alias="whatToShow"),
    keep_up_to_date: bool = Query(False, alias="keepUpToDate"),
    symbol: str | None = Query(default=None),
    exchange: str | None = Query(default=None),
    sec_type: str | None = Query(default=None, alias="secType"),
) -> Mapping[str, Any]:
    client = await _get_client_and_publisher()
    payload = ContractPayload(
        con_id=con_id,
        symbol=symbol,
        exchange=exchange,
        sec_type=sec_type,
    )
    contract, contract_id, symbol_value = _build_contract(payload)

    try:
        bars = await _call_with_fallback(
            lambda: client.fetch_historical(
                contract,
                duration=duration,
                bar_size=bar_size,
                what_to_show=what_to_show,
                keep_up_to_date=keep_up_to_date,
            ),
            client=client,
        )
    except Exception as exc:
        _LOGGER.warning("Historical data request failed: %s", exc)
        await _publish_service_health(status="degraded", last_error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Historical data request failed"
        ) from exc

    await _publish_service_health(status="running")
    return {
        "contract_id": contract_id,
        "symbol": _normalize_symbol(symbol_value),
        "bars": bars,
        "count": len(bars),
        "using_delayed_data": _state.using_delayed_data,
    }


@app.post("/config/reload")
async def reload_configuration() -> Mapping[str, Any]:
    client = _state.ib_client
    _refresh_configuration(client)
    return {
        "success": True,
        "max_subscriptions": _state.max_subscriptions,
        "market_data_type": _state.current_market_data_type,
        "delayed_fallback_enabled": _state.delayed_fallback_enabled,
    }


