"""Repository for persisting order lifecycle events to MariaDB."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import func
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.ibbot.logging import get_logger

from ..schemas import Order

__all__ = ["MariaDbOrderRepository"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _coerce_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _normalise_raw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalise_raw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalise_raw(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _normalise_order_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    request = _coerce_mapping(payload.get("request")) or {}
    response = _coerce_mapping(payload.get("response")) or {}
    result = _coerce_mapping(payload.get("result")) or {}
    metadata = _coerce_mapping(payload.get("metadata")) or {}

    sources = tuple(
        source
        for source in (payload, result, response, request, metadata)
        if isinstance(source, Mapping)
    )

    def _lookup(*keys: str) -> Any:
        for source in sources:
            for key in keys:
                if key in source:
                    value = source[key]
                    if value is None:
                        continue
                    if isinstance(value, str) and not value.strip():
                        continue
                    return value
        return None

    order_identifier = _first_nonempty(
        _lookup("order_id", "ib_order_id", "permid", "permId"),
        payload.get("id"),
        payload.get("command_id"),
    )
    symbol_value = _first_nonempty(_lookup("symbol", "ticker"))
    if not order_identifier or not symbol_value:
        raise ValueError("Order payload is missing identifying fields")

    side_value = _first_nonempty(_lookup("action", "side"))
    side = str(side_value).upper() if side_value is not None else "UNKNOWN"

    exchange_value = _first_nonempty(_lookup("exchange"))
    exchange = _coerce_str(exchange_value)

    quantity_value = _coerce_float(
        _lookup(
            "quantity",
            "total_quantity",
            "totalQuantity",
            "qty",
            "filled_quantity",
            "filledQuantity",
        )
    )
    quantity = quantity_value if quantity_value is not None else 0.0

    price_value = _coerce_float(
        _lookup(
            "avg_fill_price",
            "avg_price",
            "avgPrice",
            "limit_price",
            "lmt_price",
            "limitPrice",
            "stop_price",
            "aux_price",
            "stopPrice",
            "price",
        )
    )
    price = price_value if price_value is not None else 0.0

    status_value = _first_nonempty(
        payload.get("status"),
        _lookup("status", "order_status", "orderStatus"),
    )
    status = str(status_value) if status_value is not None else "unknown"

    timestamp_dt = _parse_datetime(payload.get("timestamp"))
    if timestamp_dt is None:
        timestamp_dt = _parse_datetime(payload.get("updated_at"))
    if timestamp_dt is None:
        timestamp_dt = _parse_datetime(payload.get("requested_at"))
    if timestamp_dt is None:
        timestamp_dt = _utc_now()

    normalised: dict[str, Any] = {
        "order_id": str(order_identifier),
        "symbol": str(symbol_value),
        "side": side,
        "quantity": quantity,
        "price": price,
        "status": status,
        "exchange": exchange,
        "order_timestamp": timestamp_dt,
        "raw_payload": _normalise_raw(payload),
    }

    float_fields: dict[str, Any] = {
        "filled_quantity": _lookup("filled_quantity", "filled", "filledQuantity"),
        "remaining_quantity": _lookup(
            "remaining_quantity",
            "remaining",
            "remainingQuantity",
            "left",
        ),
        "avg_fill_price": _lookup("avg_fill_price", "avg_price", "avgPrice"),
        "limit_price": _lookup("limit_price", "lmt_price", "limitPrice"),
        "stop_price": _lookup("stop_price", "aux_price", "stopPrice"),
        "commission": _lookup("commission"),
        "realized_pnl": _lookup("realized_pnl", "realizedPnl"),
        "unrealized_pnl": _lookup("unrealized_pnl", "unrealizedPnl"),
        "trigger_price": _lookup("trigger_price"),
    }
    for field_name, raw_value in float_fields.items():
        coerced = _coerce_float(raw_value)
        if coerced is not None:
            normalised[field_name] = coerced

    string_fields: dict[str, Any] = {
        "order_type": _lookup("order_type", "orderType", "type"),
        "trigger": _lookup("trigger"),
        "trigger_type": _lookup("trigger_type"),
        "trigger_status": _lookup("trigger_status", "triggerStatus"),
        "notes": _lookup("notes", "note", "comment"),
    }
    for field_name, raw_value in string_fields.items():
        text = _coerce_str(raw_value)
        if text:
            normalised[field_name] = text

    tag_fields: dict[str, Any] = {
        "account": _lookup("account"),
        "source": _lookup("source"),
        "strategy": _lookup("strategy", "strategy_id"),
        "sec_type": _lookup("sec_type", "security_type", "secType"),
    }
    for field_name, raw_value in tag_fields.items():
        text = _coerce_str(raw_value)
        if text:
            normalised[field_name] = text

    return normalised


class MariaDbOrderRepository:
    """Persist order updates using ``INSERT ... ON DUPLICATE KEY`` semantics."""

    def __init__(self, session: AsyncSession, *, logger_name: str = "db.orders") -> None:
        self._session = session
        self._logger = get_logger(logger_name)

    async def upsert_order(self, payload: Mapping[str, Any]) -> None:
        """Upsert a normalized order payload into the ``orders`` table."""

        normalised = _normalise_order_payload(payload)
        stmt = mysql_insert(Order).values(**normalised)
        update_columns = {
            column: getattr(stmt.inserted, column)
            for column in normalised
            if column not in {"id", "created_at"}
        }
        update_columns["updated_at"] = func.now()
        try:
            await self._session.execute(stmt.on_duplicate_key_update(**update_columns))
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            self._logger.exception(
                "Failed to upsert order", order_id=normalised.get("order_id")
            )
            raise
