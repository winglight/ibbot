from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger

from src.models import OrderRequest

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


async def _handle_command_response(
    *,
    request: Request,
    action: str,
    service_call: Callable[[], Awaitable[Mapping[str, Any]]],
    audit_success_extra: Mapping[str, Any] | None = None,
    broadcast_payload: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    try:
        result = await service_call()
    except HTTPException as exc:
        await audit_log_failure(
            request,
            action=action,
            detail=str(exc.detail),
        )
        raise

    command_id = str(result.get("command_id") or "").strip()
    response: dict[str, Any] = {"success": True, "command_id": command_id}
    if "status" in result:
        response["status"] = result["status"]
    if audit_success_extra is not None:
        extra = dict(audit_success_extra)
        if command_id:
            extra.setdefault("command_id", command_id)
    else:
        extra = {"command_id": command_id} if command_id else None
    await audit_log_success(
        request,
        action=action,
        detail=f"orders command {command_id}" if command_id else "orders command",
        extra=extra,
    )

    if broadcast_payload:
        payload = dict(broadcast_payload)
        if command_id:
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
    client: OrdersServiceClient = Depends(get_orders_service_client),
):
    order_payload = order.model_dump(mode="json", exclude_none=True)

    async def _call() -> Mapping[str, Any]:
        return await _call_orders_service(client.place_order, order_payload)

    return await _handle_command_response(
        request=request,
        action="orders.place",
        service_call=_call,
        audit_success_extra={
            "symbol": order.symbol,
            "action": order.action,
            "order_type": order.order_type,
            "submitted_by": user or "anonymous",
        },
        broadcast_payload={
            "type": "order_submitted",
            "symbol": order.symbol,
            "action": order.action,
            "order_type": order.order_type,
        },
    )


@router.post("/api/cancel_order")
async def cancel_order(
    payload: Mapping[str, Any],
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.cancel")
    ),
    client: OrdersServiceClient = Depends(get_orders_service_client),
):
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    async def _call() -> Mapping[str, Any]:
        return await _call_orders_service(client.cancel_order, order_id)

    return await _handle_command_response(
        request=request,
        action="orders.cancel",
        service_call=_call,
        audit_success_extra={"order_id": order_id, "submitted_by": user or "anonymous"},
        broadcast_payload={"type": "order_cancel_requested", "order_id": order_id},
    )


@router.delete("/api/cancel_order/{order_id}")
async def cancel_order_delete(
    order_id: str,
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.cancel")
    ),
    client: OrdersServiceClient = Depends(get_orders_service_client),
):
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    async def _call() -> Mapping[str, Any]:
        return await _call_orders_service(client.cancel_order, order_id)

    return await _handle_command_response(
        request=request,
        action="orders.cancel",
        service_call=_call,
        audit_success_extra={"order_id": order_id, "submitted_by": user or "anonymous"},
        broadcast_payload={"type": "order_cancel_requested", "order_id": order_id},
    )


@router.delete("/api/cancel_all_orders")
async def cancel_all_orders(
    request: Request,
    user: Optional[str] = Depends(
        require_roles("admin", "operator", action="orders.cancel_all")
    ),
    client: OrdersServiceClient = Depends(get_orders_service_client),
):
    async def _call() -> Mapping[str, Any]:
        return await _call_orders_service(client.cancel_all)

    return await _handle_command_response(
        request=request,
        action="orders.cancel_all",
        service_call=_call,
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
    client: OrdersServiceClient = Depends(get_orders_service_client),
):
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    async def _call() -> Mapping[str, Any]:
        return await _call_orders_service(client.close_position, symbol)

    return await _handle_command_response(
        request=request,
        action="orders.close_position",
        service_call=_call,
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
    client: OrdersServiceClient = Depends(get_orders_service_client),
):
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    async def _call() -> Mapping[str, Any]:
        return await _call_orders_service(client.reverse_position, symbol)

    return await _handle_command_response(
        request=request,
        action="orders.reverse_position",
        service_call=_call,
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

    payload = await _call_orders_service(
        client.list_orders,
        limit=limit,
        offset=offset,
        start_date=start_date,
    )

    if not isinstance(payload, Mapping):
        return {"success": True, "orders": payload}

    response = dict(payload)
    response.setdefault("success", True)
    response.setdefault("limit", limit)
    response.setdefault("page", page)
    response.setdefault("start_date", start_date)
    return response
