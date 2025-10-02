from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger

from src.models import OrderRequest
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command
from src.utils import parse_local_datetime

from .auth import audit_log_failure, audit_log_success, get_current_user, require_roles
from .service_clients import (
    OrdersServiceClient,
    ServiceResponseError,
    ServiceUnavailableError,
)

router = APIRouter()

_ORDERS_SERVICE_CLIENT = OrdersServiceClient()
_WEBSOCKET_BROADCAST_OVERRIDE: Optional[Callable[[Mapping[str, Any]], Awaitable[None]]] = None


def get_orders_service_client() -> OrdersServiceClient:
    """Return the shared orders service client instance."""

    return _ORDERS_SERVICE_CLIENT


async def _resolve_command_queue_key() -> str:
    """Return the Redis list used for IB commands.

    The queue may be overridden by the dependency container at runtime.  We
    consult the container lazily to avoid importing heavy modules during test
    collection.
    """

    try:
        import core  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - optional dependency
        return DEFAULT_COMMAND_QUEUE

    container = getattr(core, "get_container", lambda: None)()
    if not container:
        return DEFAULT_COMMAND_QUEUE

    provider = getattr(container, "config_provider", None)
    if provider is None:
        return DEFAULT_COMMAND_QUEUE

    try:
        queue_key = await provider.get(
            "ib-command",
            "queue_key",
            default=DEFAULT_COMMAND_QUEUE,
        )
    except Exception:  # pragma: no cover - config lookup failure
        return DEFAULT_COMMAND_QUEUE

    queue_key_str = str(queue_key or "").strip()
    return queue_key_str or DEFAULT_COMMAND_QUEUE


def set_websocket_broadcast_override(
    broadcaster: Optional[Callable[[Mapping[str, Any]], Awaitable[None]]]
) -> None:
    """Configure a coroutine used to broadcast websocket events in tests."""

    global _WEBSOCKET_BROADCAST_OVERRIDE
    _WEBSOCKET_BROADCAST_OVERRIDE = broadcaster


async def _broadcast_event(message: Mapping[str, Any]) -> None:
    broadcaster = _WEBSOCKET_BROADCAST_OVERRIDE
    if broadcaster is None:
        return
    try:
        await broadcaster(dict(message))
    except Exception:  # pragma: no cover - defensive logging
        logger.debug("Failed to broadcast orders event", exc_info=True)


def _normalise_detail(detail: Any) -> Any:
    if isinstance(detail, Mapping):
        return detail.get("detail") or detail
    if isinstance(detail, list) and detail:
        return detail[0]
    if detail is None:
        return "Orders service error"
    return detail


async def _call_orders_service(
    func: Callable[..., Awaitable[Mapping[str, Any]]],
    *args: Any,
    **kwargs: Any,
) -> Mapping[str, Any]:
    try:
        return await func(*args, **kwargs)
    except ServiceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orders service unavailable",
        ) from exc
    except ServiceResponseError as exc:
        detail = _normalise_detail(exc.detail)
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc


async def _enqueue_order_command(
    *,
    request: Request,
    action: str,
    method: str,
    kwargs: Mapping[str, Any],
    audit_success_extra: Mapping[str, Any] | None = None,
    broadcast_payload: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Enqueue an IB command and emit audit/broadcast side-effects."""

    command_id = uuid.uuid4().hex
    payload = {
        "command_id": command_id,
        "method": method,
        "args": [],
        "kwargs": json.loads(json.dumps(dict(kwargs))),
    }

    try:
        queue_key = await _resolve_command_queue_key()
        await enqueue_ib_command(payload, queue_key=queue_key)
    except Exception as exc:
        logger.error(
            "Failed to enqueue order command",
            action=action,
            method=method,
            error=str(exc),
        )
        await audit_log_failure(
            request,
            action=action,
            detail="failed to enqueue order command",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue order command",
        ) from exc

    response: dict[str, Any] = {
        "success": True,
        "command_id": command_id,
        "queue": queue_key,
        "status": "queued",
    }

    extra = dict(audit_success_extra or {})
    extra.setdefault("command_id", command_id)
    await audit_log_success(
        request,
        action=action,
        detail=f"order command {command_id}",
        extra=extra,
    )

    if broadcast_payload:
        payload = dict(broadcast_payload)
        payload.setdefault("command_id", command_id)
        await _broadcast_event(payload)

    return response


@router.post("/api/place_order")
async def place_order(
    order: OrderRequest,
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.place")
    ),
):
    order_payload = order.model_dump(mode="json", exclude_none=True)

    metadata = {
        "symbol": order.symbol,
        "action": order.action,
        "order_type": order.order_type,
        "submitted_by": user or "anonymous",
    }

    return await _enqueue_order_command(
        request=request,
        action="orders.place",
        method="place_order",
        kwargs=order_payload,
        audit_success_extra=metadata,
        broadcast_payload={
            "type": "order_submitted",
            **metadata,
        },
    )


@router.post("/api/cancel_order")
async def cancel_order(
    payload: Mapping[str, Any],
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.cancel")
    ),
):
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    return await _enqueue_order_command(
        request=request,
        action="orders.cancel",
        method="cancel_order",
        kwargs={"order_id": order_id},
        audit_success_extra={
            "order_id": order_id,
            "submitted_by": user or "anonymous",
        },
        broadcast_payload={"type": "order_cancel_requested", "order_id": order_id},
    )


@router.delete("/api/cancel_order/{order_id}")
async def cancel_order_delete(
    order_id: str,
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.cancel")
    ),
):
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    return await _enqueue_order_command(
        request=request,
        action="orders.cancel",
        method="cancel_order",
        kwargs={"order_id": order_id},
        audit_success_extra={
            "order_id": order_id,
            "submitted_by": user or "anonymous",
        },
        broadcast_payload={"type": "order_cancel_requested", "order_id": order_id},
    )


@router.delete("/api/cancel_all_orders")
async def cancel_all_orders(
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.cancel_all")
    ),
):
    return await _enqueue_order_command(
        request=request,
        action="orders.cancel_all",
        method="cancel_all_orders",
        kwargs={},
        audit_success_extra={"submitted_by": user or "anonymous"},
        broadcast_payload={"type": "orders_cancel_all"},
    )


@router.post("/api/close_position")
async def close_position(
    payload: Mapping[str, Any],
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.close_position")
    ),
):
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    return await _enqueue_order_command(
        request=request,
        action="orders.close_position",
        method="close_position",
        kwargs={"symbol": symbol},
        audit_success_extra={"symbol": symbol, "submitted_by": user or "anonymous"},
        broadcast_payload={"type": "position_close_requested", "symbol": symbol},
    )


@router.post("/api/reverse_position")
async def reverse_position(
    payload: Mapping[str, Any],
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.reverse_position")
    ),
):
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    return await _enqueue_order_command(
        request=request,
        action="orders.reverse_position",
        method="reverse_position",
        kwargs={"symbol": symbol},
        audit_success_extra={"symbol": symbol, "submitted_by": user or "anonymous"},
        broadcast_payload={"type": "position_reverse_requested", "symbol": symbol},
    )


@router.get("/api/orders")
async def list_orders(
    limit: int = 100,
    page: int = 1,
    start_date: Optional[str] = None,
    client: OrdersServiceClient = Depends(get_orders_service_client),
    user: Optional[str] = Depends(get_current_user),
):
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be positive")
    if page <= 0:
        raise HTTPException(status_code=400, detail="page must be positive")

    offset = (page - 1) * limit

    start_date_iso: Optional[str] = None
    if isinstance(start_date, str):
        trimmed_start = start_date.strip()
        if trimmed_start:
            parsed = parse_local_datetime(trimmed_start)
            if parsed is not None:
                start_date_iso = parsed.isoformat()
            else:
                start_date_iso = trimmed_start

    payload = await _call_orders_service(
        client.list_orders,
        limit=limit,
        offset=offset,
        start_date=start_date_iso,
    )

    if not isinstance(payload, Mapping):
        return {"success": True, "orders": payload}

    response = dict(payload)
    meta_payload = response.get("meta")
    if isinstance(meta_payload, Mapping):
        meta = dict(meta_payload)
    else:
        meta = {}
    response["meta"] = meta

    effective_start = response.get("start_date")
    if not effective_start and start_date_iso:
        effective_start = start_date_iso
    if effective_start:
        response["start_date"] = effective_start
        meta.setdefault("start_date", effective_start)
    elif isinstance(start_date, str) and start_date.strip():
        trimmed_start = start_date.strip()
        response.setdefault("start_date", trimmed_start)
        meta.setdefault("start_date", trimmed_start)

    response.setdefault("success", True)
    response.setdefault("limit", limit)
    response.setdefault("page", page)
    meta.setdefault("limit", limit)
    meta.setdefault("page", page)
    return response


@router.get("/api/orders/{order_id}")
async def get_order(
    order_id: str,
    client: OrdersServiceClient = Depends(get_orders_service_client),
    user: Optional[str] = Depends(get_current_user),
):
    identifier = str(order_id or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="order_id is required")

    payload = await _call_orders_service(client.get_order, identifier)
    if not isinstance(payload, Mapping):
        return {"success": True, "order": payload}

    response = dict(payload)
    order_payload = response.get("order")
    if isinstance(order_payload, Mapping):
        response["order"] = dict(order_payload)
    response.setdefault("success", True)
    response.setdefault("order_id", identifier)
    return response


@router.get("/api/orders/{order_id}/events")
async def get_order_events(
    order_id: str,
    client: OrdersServiceClient = Depends(get_orders_service_client),
    user: Optional[str] = Depends(get_current_user),
):
    identifier = str(order_id or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="order_id is required")

    payload = await _call_orders_service(client.get_order_events, identifier)
    if not isinstance(payload, Mapping):
        return {"success": True, "order_id": identifier, "events": payload}

    response = dict(payload)
    response.setdefault("success", True)
    response.setdefault("order_id", identifier)
    events = response.get("events")
    if isinstance(events, list):
        response["events"] = [dict(evt) if isinstance(evt, Mapping) else evt for evt in events]
    return response
