import asyncio
import json
import math
import time
from datetime import timedelta
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
)

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from loguru import logger

import core
from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
)
from src.connection_manager import connection_manager
from src.dom_service import get_dom_service
from src.utils import now_local, parse_local_datetime, to_local_iso
from src.routes.logs import load_logs_data
from src.routes.monitoring import build_influx_write_rate_payload, get_queue_metrics
from src.service_status import build_service_status_payload
from src.ib_command_health import (
    IB_COMMAND_HEALTH_KEY,
    IB_COMMAND_HEALTH_STALE_AFTER_SEC,
    build_ib_connection_health_payload,
    resolve_ib_account_type,
)
from src.routes.service_clients import (
    AccountServiceClient,
    MarketDataServiceClient,
    OrdersServiceClient,
    RiskServiceClient,
    StrategyServiceClient,
    ServiceClientError,
    ServiceUnavailableError,
)

router = APIRouter()

RATE_LIMIT_SECONDS = 15.0
SERVICE_UNAVAILABLE_MESSAGE = "服务不可用"
RISK_RULES_CACHE_KEY = "json:risk:rules"

_ACCOUNT_CLIENT = AccountServiceClient()
_MARKET_CLIENT = MarketDataServiceClient()
_ORDERS_CLIENT = OrdersServiceClient()
_RISK_CLIENT = RiskServiceClient()
_STRATEGY_CLIENT = StrategyServiceClient()


def _services():
    return core.require_container()


def _connection_manager():
    return connection_manager


def _websocket_manager():
    return _services().websocket_manager


def _service_unavailable_payload(payload_type: str, **fields: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "type": payload_type,
        "success": False,
        "error": SERVICE_UNAVAILABLE_MESSAGE,
    }
    payload.update(fields)
    return payload


def _extract_service_unavailable_payload(
    value: Any, payload_type: str
) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None

    try:
        payload = dict(value)
    except Exception:
        return None

    if (
        payload.get("type") == payload_type
        and payload.get("error") == SERVICE_UNAVAILABLE_MESSAGE
        and payload.get("success") is False
    ):
        return payload

    return None


class ConnectionContext(NamedTuple):
    health_payload: Optional[Dict[str, Any]]
    connected: bool
    account_type: str
    last_error: Optional[str]
    redis_connected: Optional[bool]
    redis_fresh: bool
    health_error: Optional[str]
    error_code: Optional[str]


async def _fetch_market_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        payload = await _MARKET_CLIENT.fetch_current_price(symbol)
    except ServiceUnavailableError as exc:
        logger.debug("Market data service unavailable for %s: %s", symbol, exc)
        return None
    except ServiceClientError as exc:
        logger.warning("Market data service error for %s: %s", symbol, exc)
        return None

    if isinstance(payload, Mapping):
        return dict(payload)
    return None


async def _fetch_account_info(force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    try:
        payload = await _ACCOUNT_CLIENT.fetch_info(force_refresh=force_refresh)
    except ServiceUnavailableError as exc:
        logger.debug("Account service unavailable: %s", exc)
        error_payload = _service_unavailable_payload("account_update")
        error_payload["details"] = str(exc)
        return error_payload
    except ServiceClientError as exc:
        logger.warning("Account service error: %s", exc)
        return None

    if isinstance(payload, Mapping):
        if isinstance(payload.get("account_info"), Mapping):
            return dict(payload["account_info"])
        if isinstance(payload.get("data"), Mapping):
            return dict(payload["data"])
        if isinstance(payload.get("result"), Mapping):
            return dict(payload["result"])
        extracted = {
            key: value
            for key, value in payload.items()
            if key not in {"success", "from_cache"}
        }
        return dict(extracted) if extracted else None
    return None


async def _fetch_positions(
    *, force_refresh: bool = False, fast: bool = True
) -> Optional[List[Dict[str, Any]]]:
    try:
        payload = await _ACCOUNT_CLIENT.fetch_positions(
            force_refresh=force_refresh,
            fast=fast,
        )
    except ServiceUnavailableError as exc:
        logger.debug("Account service unavailable for positions: %s", exc)
        error_payload = _service_unavailable_payload("position_update")
        error_payload["details"] = str(exc)
        return error_payload
    except ServiceClientError as exc:
        logger.warning("Account service error fetching positions: %s", exc)
        return None

    if isinstance(payload, list):
        return [dict(item) if isinstance(item, Mapping) else item for item in payload]
    if isinstance(payload, Mapping):
        items = payload.get("positions")
        if items is None:
            items = payload.get("data")
        if isinstance(items, list):
            return [dict(item) if isinstance(item, Mapping) else item for item in items]
    return None


async def _fetch_orders(
    *,
    limit: int,
    offset: int,
    start_date: Optional[str] = None,
) -> Optional[Tuple[List[Dict[str, Any]], Optional[int]]]:
    try:
        payload = await _ORDERS_CLIENT.list_orders(
            limit=limit,
            offset=offset,
            start_date=start_date,
        )
    except ServiceUnavailableError as exc:
        logger.debug("Orders service unavailable for orders: %s", exc)
        error_payload = _service_unavailable_payload("orders_snapshot")
        error_payload["details"] = str(exc)
        return error_payload
    except ServiceClientError as exc:
        logger.warning("Orders service error fetching orders: %s", exc)
        return None

    orders_list: List[Dict[str, Any]] = []
    total_count: Optional[int] = None

    if isinstance(payload, list):
        orders_list = [dict(item) if isinstance(item, Mapping) else {"raw": item} for item in payload]
    elif isinstance(payload, Mapping):
        candidates = None
        for key in ("orders", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if candidates is None and isinstance(payload.get("order"), Mapping):
            candidates = [payload["order"]]
        if candidates is not None:
            orders_list = [dict(item) if isinstance(item, Mapping) else {"raw": item} for item in candidates]

        raw_total = payload.get("total") or payload.get("total_count")
        if raw_total is None:
            meta = payload.get("meta")
            if isinstance(meta, Mapping):
                raw_total = meta.get("total") or meta.get("total_count")
        try:
            if raw_total is not None:
                total_count = int(raw_total)
        except (TypeError, ValueError):
            total_count = None

    if not orders_list and isinstance(payload, Mapping):
        extra_orders = payload.get("orders")
        if isinstance(extra_orders, list):
            orders_list = [
                dict(item) if isinstance(item, Mapping) else {"raw": item}
                for item in extra_orders
            ]

    if not orders_list:
        return None

    if total_count is None:
        total_count = payload.get("total") if isinstance(payload, Mapping) else None
        try:
            if total_count is not None:
                total_count = int(total_count)
        except (TypeError, ValueError):
            total_count = None

    return orders_list, total_count


def _decode_redis_mapping(raw: Dict[Any, Any]) -> Dict[str, str]:
    """Convert Redis hash payload into a plain string dictionary."""

    result: Dict[str, str] = {}
    for key, value in raw.items():
        if key is None:
            continue
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8", errors="ignore")
        else:
            key = str(key)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        elif value is None:
            value = ""
        else:
            value = str(value)
        result[key] = value
    return result


async def _read_json_from_redis(key: str) -> Optional[Dict[str, Any]]:
    container = core.get_container()
    redis_client = getattr(container, "redis_client", None) if container else None
    if redis_client is None:
        return None

    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # pragma: no cover - redis access errors
        logger.debug("Failed to read Redis key %s: %s", key, exc)
        return None

    if not raw:
        return None

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")

    try:
        payload = json.loads(raw)
    except Exception as exc:  # pragma: no cover - invalid json
        logger.debug("Invalid JSON payload in Redis key %s: %s", key, exc)
        return None

    if isinstance(payload, Mapping):
        return dict(payload)
    return None


async def _load_cached_risk_rules() -> Optional[Dict[str, Any]]:
    payload = await _read_json_from_redis(RISK_RULES_CACHE_KEY)
    if payload is None:
        return None
    payload.setdefault("success", True)
    payload.setdefault("source", payload.get("source", "redis"))
    return payload


async def _load_ib_command_health() -> Optional[Dict[str, Any]]:
    """Load ib-command service health details from Redis if available."""

    container = core.get_container()
    redis_client = getattr(container, "redis_client", None) if container else None
    if redis_client is None:
        return None

    try:
        raw = await redis_client.hgetall(IB_COMMAND_HEALTH_KEY)
    except Exception as exc:  # pragma: no cover - redis access errors
        logger.debug("Failed to load ib-command health from Redis: %s", exc)
        return None

    if not raw:
        return None

    stale_after = (
        IB_COMMAND_HEALTH_STALE_AFTER_SEC
        if IB_COMMAND_HEALTH_STALE_AFTER_SEC > 0
        else 30.0
    )

    return build_ib_connection_health_payload(
        _decode_redis_mapping(raw),
        stale_after=stale_after,
    )


async def _send_json(websocket: WebSocket, payload: Dict[str, Any]) -> bool:
    try:
        await websocket.send_text(json.dumps(payload))
        return True
    except WebSocketDisconnect:
        logger.debug("Websocket disconnected during send")
        return False
    except RuntimeError as exc:
        if "Cannot call \"send\" once a close message has been sent" in str(exc):
            logger.debug("Websocket closing; aborting further sends")
            return False
        raise


async def _send_or_disconnect(websocket: WebSocket, payload: Dict[str, Any]) -> None:
    if not await _send_json(websocket, payload):
        raise WebSocketDisconnect()


def _order_value(order: Any, key: str, default: Any = None) -> Any:
    if isinstance(order, Mapping):
        return order.get(key, default)
    return getattr(order, key, default)


def _serialise_order(order: Any) -> Dict[str, Any]:
    raw_type = (_order_value(order, "order_type", "") or "").upper()
    if raw_type in ("MARKET", "MKT"):
        order_type = "MKT"
    elif raw_type in ("LIMIT", "LMT"):
        order_type = "LMT"
    else:
        order_type = raw_type or "MKT"

    price_val = 0.0
    try:
        price = _order_value(order, "price")
        if price is not None:
            price_val = float(price)
    except Exception:
        price_val = 0.0

    trigger_type = None
    try:
        if _order_value(order, "order_source", "manual") == "auto":
            rule_id = _order_value(order, "rule_id")
            symbol = _order_value(order, "symbol")
            if rule_id and rule_id != symbol:
                trigger_type = "strategy"
            else:
                trigger_type = "risk_rule"
    except Exception:
        trigger_type = None

    created_at = _order_value(order, "created_at")
    created_iso = None
    try:
        created_iso = to_local_iso(created_at)
    except Exception:
        if created_at is not None:
            try:
                created_iso = created_at.isoformat()
            except Exception:
                created_iso = None

    avg_fill_price = _order_value(order, "fill_price")
    if avg_fill_price is None and order_type == "MKT" and price_val > 0:
        avg_fill_price = price_val

    return {
        "order_id": _order_value(order, "order_id"),
        "symbol": _order_value(order, "symbol"),
        "action": _order_value(order, "action"),
        "quantity": _order_value(order, "quantity"),
        "order_type": order_type,
        "limit_price": price_val if order_type == "LMT" and price_val > 0 else None,
        "avg_fill_price": avg_fill_price,
        "status": _order_value(order, "status"),
        "filled": _order_value(order, "quantity", 0) if _order_value(order, "status") == "Filled" else 0,
        "remaining": 0 if _order_value(order, "status") == "Filled" else _order_value(order, "quantity", 0),
        "submit_time": created_iso,
        "order_source": _order_value(order, "order_source", "manual"),
        "strategy_name": _order_value(order, "strategy_name"),
        "trigger_type": trigger_type,
        "commission": _order_value(order, "commission"),
        "pnl": _order_value(order, "pnl"),
    }


def _orders_payload(
    orders: List[Any], *, limit: Optional[int] = None, start_date: Optional[str] = None,
    page: Optional[int] = None, total: Optional[int] = None
) -> Dict[str, Any]:
    returned = len(orders)
    meta: Dict[str, Any] = {
        "limit": limit,
        "page_size": limit,
        "start_date": start_date,
        "generated_at": to_local_iso(now_local()),
        "source": "websocket",
        "returned": returned,
    }
    if page is not None:
        meta["page"] = page
        meta["has_prev"] = page > 1
    if total is not None:
        meta["total"] = total
        if limit:
            total_pages = max(1, math.ceil(total / limit))
            meta["total_pages"] = total_pages
            if page is not None:
                meta["has_next"] = page < total_pages
                meta["has_prev"] = page > 1
        elif page is not None and "has_prev" not in meta:
            meta["has_prev"] = page > 1
    elif limit:
        if page is not None and "has_prev" not in meta:
            meta["has_prev"] = page > 1
        meta["has_next"] = returned >= limit

    return {
        "type": "orders_snapshot",
        "success": True,
        "orders": [_serialise_order(order) for order in orders],
        "meta": meta,
    }


def _allow_request(tracker: Dict[str, float], key: str, *, force: bool = False) -> bool:
    now = time.monotonic()
    if force:
        tracker[key] = now
        return True
    last = tracker.get(key, 0.0)
    if now - last >= RATE_LIMIT_SECONDS:
        tracker[key] = now
        return True
    return False


def _enrich_sizing_history_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return sizing history items unchanged.

    The risk microservice now returns fully hydrated entries so the websocket
    layer no longer needs to enrich the payload using the legacy SQLite
    database.  The helper is retained for backwards compatibility with the
    surrounding call sites but simply passes values through.
    """

    return items

async def _realtime_price_sender(
    ws: WebSocket,
    symbol: str,
    interval: float = 2.0,
):
    try:
        while True:
            health = await _load_ib_command_health()
            if not health or health.get("stale"):
                await asyncio.sleep(interval)
                continue
            if not bool(health.get("connected")):
                await asyncio.sleep(interval)
                continue
            try:
                data = await _fetch_market_snapshot(symbol)
                if data is not None:
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "realtime_data",
                                "symbol": symbol,
                                "data": data,
                            }
                        )
                    )
                else:
                    logger.debug(f"Skipping null realtime data for {symbol}")
            except Exception as e:
                logger.debug(f"Realtime data error for {symbol}: {e}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await _websocket_manager().connect(websocket)

    account_service_outage_logged = False
    account_service_readiness: Optional[bool] = None

    def _coerce_readiness(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if not text:
            return None
        if text in {"1", "true", "yes", "ready", "online", "healthy"}:
            return True
        if text in {"0", "false", "no", "offline", "unready", "down"}:
            return False
        return None

    def _update_account_service_readiness(payload: Optional[Mapping[str, Any]]) -> None:
        nonlocal account_service_readiness

        readiness_value: Optional[bool] = None
        if isinstance(payload, Mapping):
            for key in (
                "account_service_ready",
                "account_service_online",
                "account_service_available",
            ):
                if key not in payload:
                    continue
                readiness_value = _coerce_readiness(payload.get(key))
                if readiness_value is not None:
                    break

        account_service_readiness = readiness_value

    def _log_account_service_outage(context: str) -> None:
        nonlocal account_service_outage_logged

        if account_service_outage_logged:
            return

        logger.warning(
            "Account service unavailable during %s (account_service_ready=%s)",
            context,
            account_service_readiness,
        )
        account_service_outage_logged = True

    async def _resolve_connection_context(
        payload: Optional[Mapping[str, Any]] = None,
        *,
        refresh: bool = False,
    ) -> ConnectionContext:
        if refresh or payload is None:
            payload = await _load_ib_command_health()

        data: Dict[str, Any] = {}
        if isinstance(payload, Mapping):
            data = dict(payload)
        elif payload:
            try:
                data = dict(payload)  # type: ignore[arg-type]
            except Exception:
                data = {}

        redis_fresh = bool(data) and not bool(data.get("stale", True))
        redis_connected = bool(data.get("connected")) if redis_fresh else None

        connected_flag = bool(redis_connected)
        account_type_raw = data.get("account_type") if data else None
        account_type = (
            str(account_type_raw)
            if account_type_raw
            else resolve_ib_account_type()
        )
        last_error = data.get("last_error") or None
        error_code_value = data.get("error_code") if data else None
        if isinstance(error_code_value, str) and not error_code_value.strip():
            error_code_value = None

        error_message = None
        if not data:
            error_message = f"{SERVICE_UNAVAILABLE_MESSAGE}：未获取到 ib-command 健康数据"
            last_error = last_error or error_message

        return ConnectionContext(
            data if data else None,
            bool(connected_flag),
            account_type,
            last_error,
            redis_connected,
            redis_fresh,
            error_message,
            error_code_value,
        )

    health_payload: Optional[Dict[str, Any]]
    context = await _resolve_connection_context(refresh=True)
    health_payload = context.health_payload
    connected_flag = context.connected
    account_type_label = context.account_type
    last_error_value = context.last_error
    health_error = context.health_error
    error_code_value = context.error_code

    _update_account_service_readiness(health_payload)

    # 新连接建立后，立即向该客户端单播当前连接状态
    try:
        payload = {
            "type": "connection_status",
            "success": health_payload is not None,
            "connected": bool(connected_flag),
            "account_type": account_type_label,
            "error_code": error_code_value,
        }
        if last_error_value:
            payload["last_error"] = last_error_value
        if health_payload is not None:
            payload["health"] = health_payload
            if health_payload.get("stale"):
                payload["stale"] = True
        if health_error:
            payload["error"] = health_error
        await websocket.send_text(json.dumps(payload))
    except Exception as e:
        logger.debug(f"Failed to send initial connection status: {e}")

    # 初次连接后，向该连接单播账户信息快照
    try:
        snapshot = await read_account_info_snapshot()
        info = await _fetch_account_info()
        unavailable_payload = _extract_service_unavailable_payload(
            info, "account_update"
        )
        info_source = "service"
        snapshot_source = None
        stale_flag = False
        snapshot_updated_at = None
        stale_reason = None
        stale_at = None

        if isinstance(snapshot, dict):
            snapshot_source = snapshot.get("source", "redis") or "redis"
            stale_flag = bool(snapshot.get("stale"))
            snapshot_updated_at = snapshot.get("updated_at")
            stale_reason = snapshot.get("stale_reason")
            stale_at = snapshot.get("stale_at")

        if unavailable_payload is not None:
            account_info_payload: Dict[str, Any] = {}
            if isinstance(unavailable_payload.get("account_info"), Mapping):
                account_info_payload = dict(unavailable_payload["account_info"])
            account_info_payload.setdefault("connected", bool(connected_flag))
            if account_type_label:
                account_info_payload.setdefault("account_type", account_type_label)
            if last_error_value:
                account_info_payload.setdefault("last_error", last_error_value)
            unavailable_payload["account_info"] = account_info_payload
            unavailable_payload.setdefault("source", "service")
            if snapshot_updated_at and "snapshot_updated_at" not in unavailable_payload:
                unavailable_payload["snapshot_updated_at"] = snapshot_updated_at
            if stale_reason and "stale_reason" not in unavailable_payload:
                unavailable_payload["stale_reason"] = stale_reason
            if stale_at and "stale_at" not in unavailable_payload:
                unavailable_payload["stale_at"] = stale_at
            unavailable_payload.setdefault("stale", stale_flag)
            await websocket.send_text(json.dumps(unavailable_payload))
        else:
            if info is None and isinstance(snapshot, dict):
                info = snapshot.get("data")
                info_source = snapshot_source or "redis"

            if info is not None:
                payload_info = info
                if isinstance(info, dict):
                    payload_info = dict(info)
                    payload_info.setdefault("connected", bool(connected_flag))
                    if account_type_label:
                        payload_info.setdefault("account_type", account_type_label)
                response_payload: Dict[str, Any] = {
                    "type": "account_update",
                    "success": True,
                    "account_info": payload_info,
                    "source": info_source,
                }
                if snapshot_updated_at:
                    response_payload["snapshot_updated_at"] = snapshot_updated_at
                response_payload["stale"] = stale_flag
                if stale_reason:
                    response_payload["stale_reason"] = stale_reason
                if stale_at:
                    response_payload["stale_at"] = stale_at
                await websocket.send_text(json.dumps(response_payload))
                if stale_flag:
                    status_payload: Dict[str, Any] = {
                        "type": "account_snapshot_status",
                        "snapshot": "account_info",
                        "stale": True,
                        "refresh_in_progress": True,
                    }
                    if snapshot_updated_at:
                        status_payload["snapshot_updated_at"] = snapshot_updated_at
                    if stale_reason:
                        status_payload["stale_reason"] = stale_reason
                    if stale_at:
                        status_payload["stale_at"] = stale_at
                    await websocket.send_text(json.dumps(status_payload))
            else:
                await websocket.send_text(
                    json.dumps(
                        _service_unavailable_payload(
                            "account_update",
                            account_info={
                                "connected": bool(connected_flag),
                                "account_type": account_type_label,
                                "last_error": last_error_value,
                            },
                            stale=stale_flag,
                            snapshot_updated_at=snapshot_updated_at,
                        )
                    )
                )
    except Exception as e:
        logger.debug(f"Failed to send initial account_update: {e}")

    # 初次连接后，向该连接单播发送订单快照（最多300条）
    try:
        initial_limit = 10
        orders_result = await _fetch_orders(limit=initial_limit, offset=0)
        unavailable_orders = _extract_service_unavailable_payload(
            orders_result, "orders_snapshot"
        )
        if unavailable_orders is not None:
            _log_account_service_outage("initial orders snapshot")
            unavailable_orders.setdefault("meta", {"limit": initial_limit, "page": 1})
            payload = unavailable_orders
        elif orders_result is None:
            _log_account_service_outage("initial orders snapshot")
            payload = _service_unavailable_payload(
                "orders_snapshot",
                meta={"limit": 10, "page": 1},
            )
        else:
            orders, total = orders_result
            payload = _orders_payload(
                orders, limit=initial_limit, page=1, total=total
            )
        await websocket.send_text(json.dumps(payload))
    except Exception as e:
        logger.warning(f"Failed to send initial orders snapshot: {e}")
        await websocket.send_text(
            json.dumps(
                _service_unavailable_payload(
                    "orders_snapshot",
                    meta={"limit": initial_limit, "page": 1},
                )
            )
        )

    # 初次连接后，向该连接单播发送positions快照
    try:
        positions_snapshot = await read_positions_snapshot()
        positions = await _fetch_positions(force_refresh=False, fast=True)
        unavailable_positions = _extract_service_unavailable_payload(
            positions, "position_update"
        )
        positions_source = "service"
        positions_snapshot_source = None
        positions_updated_at = None
        positions_stale = False
        positions_stale_reason = None
        positions_stale_at = None
        if isinstance(positions_snapshot, dict):
            positions_updated_at = positions_snapshot.get("updated_at")
            positions_snapshot_source = (
                positions_snapshot.get("source", "redis") or "redis"
            )
            positions_stale = bool(positions_snapshot.get("stale"))
            positions_stale_reason = positions_snapshot.get("stale_reason")
            positions_stale_at = positions_snapshot.get("stale_at")

        if unavailable_positions is not None:
            unavailable_positions.setdefault("positions", [])
            unavailable_positions.setdefault("source", "service")
            if positions_updated_at and "snapshot_updated_at" not in unavailable_positions:
                unavailable_positions["snapshot_updated_at"] = positions_updated_at
            if positions_stale_reason and "stale_reason" not in unavailable_positions:
                unavailable_positions["stale_reason"] = positions_stale_reason
            if positions_stale_at and "stale_at" not in unavailable_positions:
                unavailable_positions["stale_at"] = positions_stale_at
            unavailable_positions.setdefault("stale", positions_stale)
            await websocket.send_text(json.dumps(unavailable_positions))
        else:
            if positions is None and isinstance(positions_snapshot, dict):
                data = positions_snapshot.get("data")
                if isinstance(data, list):
                    positions = data
                    positions_source = positions_snapshot_source or "redis"

            if positions is not None:
                response_payload = {
                    "type": "position_update",
                    "success": True,
                    "positions": positions,
                    "source": positions_source,
                "stale": positions_stale,
            }
            if positions_updated_at:
                response_payload["snapshot_updated_at"] = positions_updated_at
            if positions_stale_reason:
                response_payload["stale_reason"] = positions_stale_reason
            if positions_stale_at:
                response_payload["stale_at"] = positions_stale_at
            await websocket.send_text(json.dumps(response_payload))
            if positions_stale:
                status_payload = {
                    "type": "account_snapshot_status",
                    "snapshot": "positions",
                    "stale": True,
                    "refresh_in_progress": True,
                }
                if positions_updated_at:
                    status_payload["snapshot_updated_at"] = positions_updated_at
                if positions_stale_reason:
                    status_payload["stale_reason"] = positions_stale_reason
                if positions_stale_at:
                    status_payload["stale_at"] = positions_stale_at
                await websocket.send_text(json.dumps(status_payload))
            else:
                await websocket.send_text(
                    json.dumps(
                        _service_unavailable_payload(
                            "position_update",
                            positions=[],
                            source="service",
                            stale=positions_stale,
                            snapshot_updated_at=positions_updated_at,
                        )
                    )
                )
    except Exception as e:
        logger.debug(f"Failed to send initial positions snapshot: {e}")
    
    realtime_tasks: Dict[str, asyncio.Task] = {}
    request_tracker: Dict[str, float] = {}
    last_responses: Dict[str, Dict[str, Any]] = {}
    dom_service = get_dom_service()
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")
            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif msg_type == "request_connection_status":
                key = "connection_status"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "connection_status",
                                    "success": False,
                                    "error": "Rate limited",
                                }
                            )
                        )
                    continue

                context = await _resolve_connection_context(health_payload, refresh=True)
                health_payload = context.health_payload
                connected_flag = context.connected
                account_type_label = context.account_type
                last_error_value = context.last_error
                redis_connected = context.redis_connected
                redis_fresh = context.redis_fresh
                health_error = context.health_error
                error_code_value = context.error_code

                _update_account_service_readiness(health_payload)

                payload = {
                    "type": "connection_status",
                    "success": health_payload is not None,
                    "connected": bool(connected_flag),
                    "account_type": account_type_label,
                    "source": "websocket",
                }

                if error_code_value:
                    payload["error_code"] = error_code_value
                if last_error_value:
                    payload["last_error"] = last_error_value
                if health_payload is not None:
                    payload["health"] = health_payload
                    if health_payload.get("stale"):
                        payload["stale"] = True
                if health_error:
                    payload["error"] = health_error
                if redis_connected is None:
                    payload["from_cache"] = False
                payload["fresh"] = bool(redis_fresh)

                last_responses[key] = payload
                await websocket.send_text(json.dumps(payload))
            elif msg_type == "request_service_statuses":
                key = "service_statuses"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "service_statuses",
                                "success": False,
                                "error": "Rate limited",
                                "source": "websocket",
                            },
                        )
                    continue

                try:
                    container = core.get_container()
                except Exception:
                    container = None

                redis_client = None
                if container is not None:
                    redis_client = getattr(container, "redis_client", None)

                try:
                    raw_payload = await build_service_status_payload(redis=redis_client)
                    payload = {
                        "type": "service_statuses",
                        "success": True,
                        "source": "websocket",
                    }
                    if isinstance(raw_payload, Mapping):
                        payload.update(dict(raw_payload))
                    else:
                        payload["data"] = raw_payload
                except Exception as exc:
                    logger.warning(f"Failed to build service status payload: {exc}")
                    payload = {
                        "type": "service_statuses",
                        "success": False,
                        "error": str(exc),
                        "source": "websocket",
                    }

                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "subscribe_realtime":
                symbol = message.get("symbol")
                if symbol and symbol not in realtime_tasks:
                    realtime_tasks[symbol] = asyncio.create_task(
                        _realtime_price_sender(websocket, symbol)
                    )
            elif msg_type == "unsubscribe_realtime":
                symbol = message.get("symbol")
                task = realtime_tasks.pop(symbol, None)
                if task:
                    task.cancel()
            elif msg_type == "request_orders_snapshot":
                try:
                    limit = int(message.get("limit", 10))
                except (TypeError, ValueError):
                    limit = 10
                try:
                    page = int(message.get("page", 1))
                except (TypeError, ValueError):
                    page = 1
                if limit <= 0:
                    limit = 10
                if page < 1:
                    page = 1
                offset = (page - 1) * limit

                start_date_raw = message.get("start_date")
                start_dt = parse_local_datetime(start_date_raw)
                key = f"orders_snapshot:{limit}:{start_date_raw}:{page}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "orders_snapshot",
                            "success": False,
                            "error": "Rate limited",
                        }))
                    continue

                try:
                    result = await _fetch_orders(
                        limit=limit,
                        offset=offset,
                        start_date=start_dt.isoformat() if start_dt else None,
                    )
                    unavailable_orders = _extract_service_unavailable_payload(
                        result, "orders_snapshot"
                    )
                    if unavailable_orders is not None:
                        _log_account_service_outage("orders snapshot request")
                        unavailable_orders.setdefault(
                            "meta",
                            {
                                "limit": limit,
                                "page": page,
                                "start_date": start_date_raw,
                            },
                        )
                        payload = unavailable_orders
                    elif result is None:
                        _log_account_service_outage("orders snapshot request")
                        payload = _service_unavailable_payload(
                            "orders_snapshot",
                            meta={
                                "limit": limit,
                                "page": page,
                                "start_date": start_date_raw,
                            },
                        )
                    else:
                        orders, total = result
                        payload = _orders_payload(
                            orders,
                            limit=limit,
                            start_date=start_date_raw,
                            page=page,
                            total=total,
                        )
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to stream orders snapshot: {exc}")
                    error_payload = _service_unavailable_payload(
                        "orders_snapshot",
                        meta={"limit": limit, "page": page, "start_date": start_date_raw},
                    )
                    error_payload["details"] = str(exc)
                    last_responses[key] = error_payload
                    await websocket.send_text(json.dumps(error_payload))
            elif msg_type == "request_logs_snapshot":
                range_value = (message.get("range") or "today").strip() or "today"
                module_value = (message.get("module") or "all").strip() or "all"
                level_value = (message.get("level") or "all").strip() or "all"
                search_value = message.get("search")
                raw_request_filter = message.get("filter_request_id") or message.get("request_id_filter")
                request_filter = None
                if isinstance(raw_request_filter, str):
                    request_filter = raw_request_filter.strip() or None
                request_token = message.get("request_id")

                try:
                    page_size = int(message.get("page_size", 200))
                except (TypeError, ValueError):
                    page_size = 200
                try:
                    cursor = message.get("cursor")
                    cursor_value = int(cursor) if cursor is not None else None
                except (TypeError, ValueError):
                    cursor_value = None

                direction_value = (message.get("direction") or "backward").strip() or "backward"
                direction_value = direction_value if direction_value in {"backward", "forward"} else "backward"

                try:
                    lines_value = int(message.get("lines", 50))
                except (TypeError, ValueError):
                    lines_value = 50

                key = (
                    "logs_snapshot:" +
                    ":".join(
                        str(part)
                        for part in (
                            range_value,
                            module_value,
                            level_value,
                            direction_value,
                            cursor_value if cursor_value is not None else "",
                            page_size,
                            request_filter or "",
                            search_value or "",
                            lines_value,
                        )
                    )
                )
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        if request_token is not None:
                            cached_payload["request_id"] = request_token
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "logs_snapshot",
                                "success": False,
                                "error": "Rate limited",
                                "query": {
                                    "range": range_value,
                                    "module": module_value,
                                    "level": level_value,
                                "direction": direction_value,
                                "cursor": cursor_value,
                                "page_size": page_size,
                                "request_id": request_filter,
                                "search": search_value,
                                "lines": lines_value,
                            },
                        },
                    )
                    continue

                try:
                    result = await load_logs_data(
                        lines=lines_value,
                        level=level_value,
                        module=module_value,
                        range=range_value,
                        cursor=cursor_value,
                        page_size=page_size,
                        direction=direction_value,
                        request_id=request_filter,
                        search=search_value,
                    )
                except Exception as exc:
                    logger.warning(f"Failed to load logs snapshot: {exc}")
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "logs_snapshot",
                            "success": False,
                            "error": str(exc),
                        },
                    )
                    continue

                payload = {
                    "type": "logs_snapshot",
                    "success": bool(result.get("success", True)),
                    "logs": result.get("logs") or [],
                    "pagination": result.get("pagination") or {},
                    "query": {
                        "range": range_value,
                        "module": module_value,
                        "level": level_value,
                        "direction": direction_value,
                        "cursor": cursor_value,
                        "page_size": page_size,
                        "request_id": request_filter,
                        "search": search_value,
                        "lines": lines_value,
                    },
                    "source": "websocket",
                }
                if request_token is not None:
                    payload["request_id"] = request_token
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_positions_snapshot":
                key = "positions_snapshot"
                force = bool(message.get("force"))

                context = await _resolve_connection_context(health_payload, refresh=True)
                health_payload = context.health_payload
                connected_flag = context.connected
                account_type_label = context.account_type
                last_error_value = context.last_error
                redis_connected = context.redis_connected
                redis_fresh = context.redis_fresh
                health_error = context.health_error
                error_code_value = context.error_code

                _update_account_service_readiness(health_payload)

                if health_payload is None:
                    payload = _service_unavailable_payload(
                        "position_update",
                        positions=[],
                        source="websocket",
                    )
                    if health_error:
                        payload["error"] = health_error
                    if last_error_value:
                        payload["last_error"] = last_error_value
                    if error_code_value:
                        payload["error_code"] = error_code_value
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                    continue

                use_cache = message.get("use_cache")
                use_cache = bool(use_cache) if use_cache is not None else False

                cached = last_responses.get(key)
                if use_cache and cached and not force:
                    cached_payload = dict(cached)
                    cached_payload["cached"] = True
                    await websocket.send_text(json.dumps(cached_payload))
                    continue

                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "position_update",
                            "success": False,
                            "error": "Rate limited",
                            "source": "websocket",
                        }))
                    continue

                fast_flag = message.get("fast")
                fast = bool(fast_flag) if fast_flag is not None else False

                try:
                    positions = await _fetch_positions(force_refresh=force, fast=fast)
                    unavailable_positions = _extract_service_unavailable_payload(
                        positions, "position_update"
                    )
                    source = "service"
                    if unavailable_positions is not None:
                        unavailable_positions.setdefault("positions", [])
                        unavailable_positions.setdefault("source", "service")
                        unavailable_positions["fast"] = fast
                        unavailable_positions["connected"] = bool(connected_flag)
                        unavailable_positions["fresh"] = bool(redis_fresh)
                        last_responses[key] = unavailable_positions
                        await websocket.send_text(json.dumps(unavailable_positions))
                        continue
                    if positions is None:
                        snapshot = await read_positions_snapshot()
                        if isinstance(snapshot, dict):
                            data = snapshot.get("data")
                            if isinstance(data, list):
                                positions = data
                                source = snapshot.get("source", "redis") or "redis"
                    if positions is None:
                        unavailable_payload = _service_unavailable_payload(
                            "position_update",
                            positions=[],
                            source=source,
                            fast=fast,
                            connected=bool(connected_flag),
                            fresh=bool(redis_fresh),
                        )
                        unavailable_payload.setdefault(
                            "details",
                            "Positions data unavailable from service or cache",
                        )
                        last_responses[key] = unavailable_payload
                        await websocket.send_text(json.dumps(unavailable_payload))
                        continue
                    payload = {
                        "type": "position_update",
                        "success": True,
                        "positions": positions,
                        "source": source,
                        "fast": fast,
                        "connected": bool(connected_flag),
                        "fresh": bool(redis_fresh),
                    }
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to stream positions snapshot: {exc}")
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        cached_payload["error"] = str(exc)
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        error_payload = _service_unavailable_payload(
                            "position_update",
                            positions=[],
                            source="websocket",
                        )
                        error_payload["details"] = str(exc)
                        await websocket.send_text(json.dumps(error_payload))
            elif msg_type == "request_dom_automation_status":
                await websocket.send_text(json.dumps({
                    "type": "dom_automation_status",
                    "success": False,
                    "error": "DOM automation is disabled",
                }))
                continue
            elif msg_type == "request_dom_stats":
                symbol = (message.get("symbol") or "").strip().upper()
                if not symbol:
                    await websocket.send_text(json.dumps({
                        "type": "dom_stats",
                        "success": False,
                        "error": "Symbol is required",
                    }))
                    continue

                try:
                    lookback_seconds = message.get("lookback_seconds")
                    lookback_val = int(lookback_seconds) if lookback_seconds is not None else None
                except (TypeError, ValueError):
                    lookback_val = None

                try:
                    max_points_raw = message.get("max_points")
                    max_points = int(max_points_raw) if max_points_raw is not None else None
                except (TypeError, ValueError):
                    max_points = None

                start_param = message.get("start")
                end_param = message.get("end")
                key = f"dom_stats:{symbol}:{lookback_val}:{max_points}:{start_param}:{end_param}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "dom_stats",
                            "success": False,
                            "symbol": symbol,
                            "error": "Rate limited",
                        }))
                    continue

                try:
                    result = await dom_service.get_stats_async(
                        symbol,
                        lookback_seconds=lookback_val,
                        start_time=start_param,
                        end_time=end_param,
                        max_points=max_points,
                    )
                    payload = {"type": "dom_stats", "success": True, "symbol": symbol}
                    if isinstance(result, dict):
                        payload.update(result)
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to fetch DOM stats: {exc}")
                    await websocket.send_text(json.dumps({
                        "type": "dom_stats",
                        "success": False,
                        "symbol": symbol,
                        "error": str(exc),
                    }))
            elif msg_type == "request_monitoring_queues":
                raw_queues = message.get("queues")
                if raw_queues is None:
                    raw_queues = message.get("queue")
                raw_streams = message.get("streams")
                if raw_streams is None:
                    raw_streams = message.get("stream")

                def _normalize_sequence(value: Any) -> list[str]:
                    if value is None:
                        return []
                    if isinstance(value, list):
                        return [str(item) for item in value if item not in (None, "")]
                    if isinstance(value, (tuple, set)):
                        return [str(item) for item in value if item not in (None, "")]
                    if isinstance(value, str):
                        token = value.strip()
                        return [token] if token else []
                    return [str(value)]

                queue_tokens = _normalize_sequence(raw_queues)
                stream_tokens = _normalize_sequence(raw_streams)
                key = "monitoring_queues:" + "|".join(
                    [";".join(queue_tokens) or "default", ";".join(stream_tokens) or "default"]
                )
                force = bool(message.get("force"))
                request_token = message.get("request_id")

                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        if request_token is not None:
                            cached_payload["request_id"] = request_token
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "monitoring_queues",
                                "success": False,
                                "error": "Rate limited",
                                "query": {
                                    "queues": queue_tokens,
                                    "streams": stream_tokens,
                                },
                            },
                        )
                    continue

                try:
                    data = await get_queue_metrics(
                        queues=queue_tokens or None,
                        streams=stream_tokens or None,
                    )
                except HTTPException as exc:
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "monitoring_queues",
                            "success": False,
                            "error": getattr(exc, "detail", None) or str(exc),
                            "query": {
                                "queues": queue_tokens,
                                "streams": stream_tokens,
                            },
                        },
                    )
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(f"Failed to load monitoring queues: {exc}")
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "monitoring_queues",
                            "success": False,
                            "error": str(exc),
                            "query": {
                                "queues": queue_tokens,
                                "streams": stream_tokens,
                            },
                        },
                    )
                    continue

                payload = {
                    "type": "monitoring_queues",
                    "success": True,
                    **data,
                }
                if request_token is not None:
                    payload["request_id"] = request_token
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_influx_write_rate":
                window = (message.get("window") or "1h").strip() or "1h"
                service_filter = message.get("service")
                metric_filter = message.get("metric")
                aggregate = (message.get("aggregate") or "sum").strip() or "sum"
                request_token = message.get("request_id")

                key = f"influx_write_rate:{window}:{service_filter or ''}:{metric_filter or ''}:{aggregate}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        if request_token is not None:
                            cached_payload["request_id"] = request_token
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "influx_write_rate",
                                "success": False,
                                "error": "Rate limited",
                                "query": {
                                    "window": window,
                                    "service": service_filter,
                                    "metric": metric_filter,
                                    "aggregate": aggregate,
                                },
                            },
                        )
                    continue

                try:
                    data = build_influx_write_rate_payload(
                        window=window,
                        service=service_filter,
                        metric=metric_filter,
                        aggregate=aggregate,
                    )
                except HTTPException as exc:
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "influx_write_rate",
                            "success": False,
                            "error": getattr(exc, "detail", None) or str(exc),
                            "query": {
                                "window": window,
                                "service": service_filter,
                                "metric": metric_filter,
                                "aggregate": aggregate,
                            },
                        },
                    )
                    continue
                except Exception as exc:
                    logger.warning(f"Failed to load influx metrics: {exc}")
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "influx_write_rate",
                            "success": False,
                            "error": str(exc),
                            "query": {
                                "window": window,
                                "service": service_filter,
                                "metric": metric_filter,
                                "aggregate": aggregate,
                            },
                        },
                    )
                    continue

                payload = {
                    "type": "influx_write_rate",
                    "success": True,
                    "source": "websocket",
                    **data,
                }
                if request_token is not None:
                    payload["request_id"] = request_token
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_risk_rules":
                key = "risk_rules"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "risk_rules",
                                    "success": False,
                                    "error": "Rate limited",
                                }
                            )
                        )
                    continue

                try:
                    service_response = await _RISK_CLIENT.fetch_rules()
                except ServiceUnavailableError as exc:
                    logger.debug(f"Risk service unavailable: {exc}")
                    service_response = None
                except ServiceClientError as exc:
                    logger.warning(f"Risk service error: {exc}")
                    service_response = None

                rules_payload: Any = None
                source = "service"

                if isinstance(service_response, Mapping):
                    rules_payload = service_response.get("rules", service_response.get("data"))
                    if rules_payload is None:
                        rules_payload = service_response
                elif isinstance(service_response, list):
                    rules_payload = service_response

                if rules_payload is None:
                    cached_snapshot = await _load_cached_risk_rules()
                    if cached_snapshot is not None:
                        rules_payload = cached_snapshot.get("rules") or cached_snapshot.get("data")
                        source = cached_snapshot.get("source", "redis") or "redis"

                if rules_payload is None:
                    payload = _service_unavailable_payload("risk_rules")
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                    continue

                payload = {
                    "type": "risk_rules",
                    "success": True,
                    "rules": rules_payload,
                    "source": source,
                }
                last_responses[key] = payload
                await websocket.send_text(json.dumps(payload))
            elif msg_type == "request_sizing_history":
                try:
                    limit = int(message.get("limit", 50))
                except (TypeError, ValueError):
                    limit = 50
                limit = max(1, min(limit, 500))

                symbol = (message.get("symbol") or "").strip() or None
                since_raw = message.get("since")
                context = message.get("context")

                key = f"sizing_history:{symbol or ''}:{since_raw or ''}:{limit}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        if context is not None:
                            cached_payload = dict(cached_payload)
                            cached_payload["context"] = context
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "sizing_history",
                                "success": False,
                                "error": "Rate limited",
                                "context": context,
                            },
                        )
                    continue

                try:
                    response = await _RISK_CLIENT.fetch_sizing_history(
                        {
                            "limit": limit,
                            "symbol": symbol,
                            "since": since_raw,
                        }
                    )
                except ServiceUnavailableError as exc:
                    logger.debug(f"Risk service unavailable for sizing history: {exc}")
                    response = None
                except ServiceClientError as exc:
                    logger.warning(f"Risk service error for sizing history: {exc}")
                    response = None

                base_payload: Dict[str, Any] | None = None
                if isinstance(response, Mapping):
                    base_payload = {"type": "sizing_history", **dict(response)}
                    data = base_payload.get("data")
                    if isinstance(data, list):
                        base_payload["data"] = _enrich_sizing_history_items(
                            [dict(item) if isinstance(item, Mapping) else item for item in data]
                        )
                elif isinstance(response, list):
                    base_payload = {
                        "type": "sizing_history",
                        "success": True,
                        "data": _enrich_sizing_history_items(
                            [dict(item) if isinstance(item, Mapping) else item for item in response]
                        ),
                    }

                if base_payload is None:
                    fallback = _service_unavailable_payload("sizing_history")
                    last_responses[key] = fallback
                    payload = dict(fallback)
                    if context is not None:
                        payload["context"] = context
                    await _send_or_disconnect(websocket, payload)
                    continue

                base_payload.setdefault("success", True)
                base_payload.setdefault("data", [])
                base_payload.setdefault(
                    "meta",
                    {
                        "limit": limit,
                        "symbol": symbol,
                        "since": since_raw,
                    },
                )
                last_responses[key] = base_payload
                payload = dict(base_payload)
                if context is not None:
                    payload["context"] = context
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_strategies":
                key = "strategies_snapshot"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "strategies_snapshot",
                                "success": False,
                                "error": "Rate limited",
                            },
                        )
                    continue

                try:
                    response = await _STRATEGY_CLIENT.list_strategies()
                except ServiceUnavailableError as exc:
                    logger.debug(f"Strategy service unavailable: {exc}")
                    payload = _service_unavailable_payload("strategies_snapshot")
                    payload["detail"] = "Strategy service unavailable"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue
                except ServiceClientError as exc:
                    logger.warning(f"Strategy service error: {exc}")
                    payload = _service_unavailable_payload("strategies_snapshot")
                    payload["detail"] = str(exc)
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                strategies: Any = []
                if isinstance(response, Mapping):
                    strategies = response.get("strategies")
                    if strategies is None:
                        strategies = response.get("data")
                    if strategies is None:
                        strategies = response
                elif isinstance(response, list):
                    strategies = response

                if not isinstance(strategies, list):
                    payload = _service_unavailable_payload("strategies_snapshot")
                    payload["detail"] = "Unexpected strategy payload"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                payload = {
                    "type": "strategies_snapshot",
                    "success": True,
                    "strategies": strategies,
                    "source": "service",
                }
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_strategy_status":
                name = (message.get("name") or "").strip()
                if not name:
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "strategy_status",
                            "success": False,
                            "error": "Strategy name is required",
                        },
                    )
                    continue

                key = f"strategy_status:{name}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "strategy_status",
                                "success": False,
                                "name": name,
                                "error": "Rate limited",
                            },
                        )
                    continue

                try:
                    response = await _STRATEGY_CLIENT.get_strategy_status(name)
                except ServiceUnavailableError as exc:
                    logger.debug(f"Strategy service unavailable for {name}: {exc}")
                    payload = _service_unavailable_payload("strategy_status", name=name)
                    payload["detail"] = "Strategy service unavailable"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue
                except ServiceClientError as exc:
                    logger.warning(f"Strategy service error for {name}: {exc}")
                    payload = _service_unavailable_payload("strategy_status", name=name)
                    payload["detail"] = str(exc)
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                status_payload: Any = None
                if isinstance(response, Mapping):
                    status_payload = response.get("status")
                    if status_payload is None:
                        status_payload = response.get("data")
                    if status_payload is None:
                        status_payload = response

                if status_payload is None:
                    payload = _service_unavailable_payload("strategy_status", name=name)
                    payload["detail"] = "Unexpected strategy status payload"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                payload = {
                    "type": "strategy_status",
                    "success": True,
                    "name": name,
                    "status": status_payload,
                    "source": "service",
                }
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_strategy_performance":
                name = (message.get("name") or "").strip()
                if not name:
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "strategy_performance",
                            "success": False,
                            "error": "Strategy name is required",
                        },
                    )
                    continue

                range_value = (message.get("range") or "1d").strip()
                start_raw = message.get("start")
                end_raw = message.get("end")
                start_date = parse_local_datetime(start_raw)
                end_date = parse_local_datetime(end_raw)

                if range_value:
                    try:
                        now = now_local()
                        if range_value == "1d":
                            start_date = now - timedelta(days=1)
                        elif range_value == "1w":
                            start_date = now - timedelta(weeks=1)
                        elif range_value == "1m":
                            start_date = now - timedelta(days=30)
                        elif range_value == "wtd":
                            start_date = now - timedelta(days=now.weekday())
                        elif range_value == "mtd":
                            start_date = now.replace(day=1)
                        elif range_value == "ytd":
                            start_date = now.replace(month=1, day=1)
                    except Exception:
                        pass

                key = f"strategy_performance:{name}:{range_value}:{start_raw}:{end_raw}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "strategy_performance",
                                "success": False,
                                "name": name,
                                "range": range_value,
                                "error": "Rate limited",
                            },
                        )
                    continue

                try:
                    response = await _STRATEGY_CLIENT.get_strategy_performance(
                        name,
                        range_=range_value,
                        start=start_raw,
                        end=end_raw,
                    )
                except ServiceUnavailableError as exc:
                    logger.debug(f"Strategy service unavailable for {name}: {exc}")
                    payload = _service_unavailable_payload(
                        "strategy_performance",
                        name=name,
                        range=range_value,
                    )
                    payload["detail"] = "Strategy service unavailable"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue
                except ServiceClientError as exc:
                    logger.warning(f"Strategy service error for {name}: {exc}")
                    payload = _service_unavailable_payload(
                        "strategy_performance",
                        name=name,
                        range=range_value,
                    )
                    payload["detail"] = str(exc)
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                metrics: Any = None
                source = "service"
                if isinstance(response, Mapping):
                    metrics = (
                        response.get("metrics")
                        or response.get("performance")
                        or response.get("data")
                    )
                    if metrics is None:
                        nested = response.get("strategy")
                        if isinstance(nested, Mapping):
                            metrics = nested.get("metrics") or nested.get("performance")
                    if isinstance(response.get("source"), str):
                        source = response["source"] or source
                elif isinstance(response, list):
                    metrics = response

                if metrics is None:
                    payload = _service_unavailable_payload(
                        "strategy_performance",
                        name=name,
                        range=range_value,
                    )
                    payload["detail"] = "Unexpected strategy performance payload"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                payload = {
                    "type": "strategy_performance",
                    "success": True,
                    "name": name,
                    "range": range_value,
                    "metrics": metrics,
                    "source": source,
                }
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
    except WebSocketDisconnect:
        for task in realtime_tasks.values():
            task.cancel()
        _websocket_manager().disconnect(websocket)

