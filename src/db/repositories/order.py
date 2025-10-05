"""DB-API based repository for persisting order lifecycle events."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any, Mapping

from src.db.async_support import run_in_thread
from src.db.mariadb import MariaDBConfigurationError
from src.ibbot.logging import get_logger

from .base import (
    RepositoryError,
    RepositoryValidationError,
    execute_write,
    is_sqlite,
    json_dumps,
    use_connection,
)

__all__ = ["OrderRepository", "OrderRepositoryError"]


class OrderRepositoryError(RepositoryError):
    """Raised when persisting order events fails."""


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
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
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


def _generate_order_id(symbol: Any) -> str:
    base = _coerce_str(symbol) or "order"
    safe = "".join(ch.lower() for ch in base if ch.isalnum()) or "order"
    return f"auto-{safe}-{uuid.uuid4().hex}"


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

    client_identifier = _first_nonempty(
        payload.get("client_order_id"),
        payload.get("command_id"),
        payload.get("id"),
        _lookup("client_order_id", "clientOrderId", "command_id", "commandId", "id"),
    )

    ib_identifier = _first_nonempty(
        payload.get("ib_order_id"),
        payload.get("ibOrderId"),
        payload.get("permid"),
        payload.get("permId"),
        _lookup("ib_order_id", "ibOrderId", "permid", "permId"),
    )

    existing_identifier = _first_nonempty(
        payload.get("order_id"),
        _lookup("order_id", "orderId"),
    )

    order_identifier = client_identifier or existing_identifier or ib_identifier

    symbol_value = _first_nonempty(_lookup("symbol", "ticker"))

    contract_mapping = _coerce_mapping(_lookup("contract"))
    if contract_mapping is not None:
        symbol_value = _first_nonempty(
            symbol_value,
            contract_mapping.get("symbol"),
            contract_mapping.get("localSymbol"),
            contract_mapping.get("ticker"),
        )

    instrument_mapping = _coerce_mapping(_lookup("instrument"))
    if instrument_mapping is not None:
        symbol_value = _first_nonempty(
            symbol_value,
            instrument_mapping.get("symbol"),
            instrument_mapping.get("ticker"),
        )

    symbol_text = _coerce_str(symbol_value) or "UNKNOWN"

    if not order_identifier:
        order_identifier = _generate_order_id(symbol_text)

    if client_identifier is None:
        client_identifier = order_identifier

    order_identifier = str(order_identifier)
    client_identifier = str(client_identifier)
    symbol_value = symbol_text

    side_value = _first_nonempty(_lookup("action", "side"))
    side_text = _coerce_str(side_value)
    side = side_text.upper() if side_text else "UNKNOWN"

    action_value = _first_nonempty(payload.get("action"), _lookup("action"), side_text)
    action_text = _coerce_str(action_value)

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

    order_source_value = _coerce_str(
        _first_nonempty(payload.get("order_source"), _lookup("order_source", "source"))
    )
    strategy_value = _coerce_str(
        _first_nonempty(
            payload.get("strategy_name"),
            payload.get("strategy"),
            _lookup("strategy_name", "strategy"),
        )
    )
    rule_value = _coerce_str(
        _first_nonempty(payload.get("rule_id"), _lookup("rule_id"))
    )
    parent_value = _coerce_str(
        _first_nonempty(payload.get("parent_order_id"), _lookup("parent_order_id"))
    )

    normalised: dict[str, Any] = {
        "order_id": str(order_identifier),
        "client_order_id": client_identifier,
        "symbol": str(symbol_value),
        "side": side,
        "quantity": quantity,
        "price": price,
        "status": status,
        "exchange": exchange,
        "order_timestamp": timestamp_dt,
        "order_source": order_source_value,
        "strategy_name": strategy_value,
        "rule_id": rule_value,
        "parent_order_id": parent_value,
        "raw_payload": _normalise_raw(payload),
    }

    normalised["strategy"] = strategy_value

    if action_text:
        normalised["action"] = action_text.upper()

    if ib_identifier is not None:
        normalised["ib_order_id"] = str(ib_identifier)

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
        "fill_price": _lookup("fill_price", "avg_fill_price", "avg_price", "avgPrice"),
        "pnl": _lookup("pnl", "realized_pnl", "realizedPnl"),
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
        "order_source": order_source_value,
        "strategy_name": strategy_value,
        "strategy": strategy_value,
        "rule_id": rule_value,
        "parent_order_id": parent_value,
    }
    for field_name, raw_value in string_fields.items():
        text = _coerce_str(raw_value)
        if text:
            normalised[field_name] = text

    tag_fields: dict[str, Any] = {
        "account": _lookup("account"),
        "source": _lookup("source"),
        "sec_type": _lookup("sec_type", "security_type", "secType"),
    }
    for field_name, raw_value in tag_fields.items():
        text = _coerce_str(raw_value)
        if text:
            normalised[field_name] = text

    return normalised


class OrderRepository:
    """Persist order updates using DB-API connections."""

    def __init__(self, *, logger_name: str = "db.orders") -> None:
        self._logger = get_logger(logger_name)

    async def upsert_order(self, payload: Mapping[str, Any]) -> None:
        normalised = _normalise_order_payload(payload)

        try:
            await run_in_thread(self._write_order, normalised)
        except RepositoryValidationError:
            raise
        except MariaDBConfigurationError as exc:
            raise OrderRepositoryError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - log and wrap
            order_id = normalised.get("order_id")
            self._logger.exception("Failed to upsert order %s", order_id)
            raise OrderRepositoryError("Failed to upsert order") from exc

    def _write_order(self, row: Mapping[str, Any]) -> None:
        with use_connection() as connection:
            prepared = dict(row)
            if prepared.get("raw_payload") is not None:
                prepared["raw_payload"] = json_dumps(prepared["raw_payload"])

            columns = list(prepared.keys())
            if not columns:
                raise RepositoryValidationError("Normalised order payload is empty")

            if is_sqlite(connection):
                placeholders = ", ".join(f":{col}" for col in columns)
                update_columns = ", ".join(
                    f"{col}=excluded.{col}"
                    for col in columns
                    if col not in {"order_id", "created_at"}
                )
                if update_columns:
                    update_clause = (
                        f"{update_columns}, updated_at=CURRENT_TIMESTAMP"
                    )
                else:
                    update_clause = "updated_at=CURRENT_TIMESTAMP"
                sql = (
                    "INSERT INTO orders ({cols}) VALUES ({values}) "
                    "ON CONFLICT(order_id) DO UPDATE SET {updates}"
                ).format(cols=", ".join(columns), values=placeholders, updates=update_clause)
                execute_write(connection, sql, prepared)
            else:
                placeholders = ", ".join(f"%({col})s" for col in columns)
                update_columns = ", ".join(
                    f"{col}=VALUES({col})"
                    for col in columns
                    if col not in {"order_id", "created_at"}
                )
                if update_columns:
                    update_clause = f"{update_columns}, updated_at=NOW()"
                else:
                    update_clause = "updated_at=NOW()"
                sql = (
                    "INSERT INTO orders ({cols}) VALUES ({values}) "
                    "ON DUPLICATE KEY UPDATE {updates}"
                ).format(cols=", ".join(columns), values=placeholders, updates=update_clause)
                execute_write(connection, sql, prepared)
