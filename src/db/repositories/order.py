"""DB-API based repository for persisting order lifecycle events."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import uuid
from typing import Any, Iterable, Mapping, Sequence

from src.db.async_support import run_in_thread
from src.db.mariadb import MariaDBConfigurationError
from src.ibbot.logging import get_logger

from .base import (
    RepositoryError,
    RepositoryValidationError,
    format_datetime_for_db,
    is_sqlite,
    json_dumps,
    use_connection,
)

__all__ = ["OrderRepository", "OrderRepositoryError", "OrderNotFoundError"]


class OrderRepositoryError(RepositoryError):
    """Raised when persisting order events fails."""


class OrderNotFoundError(OrderRepositoryError):
    """Raised when an update is requested but no existing order matches."""


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


def _iter_identifier_candidates(
    *,
    command_id: Any,
    ib_order_id: Any,
    client_order_id: Any,
    order_id: Any,
) -> tuple[tuple[str, str], ...]:
    """Return unique identifier lookup candidates for repository queries."""

    raw_pairs: tuple[tuple[str, Any], ...] = (
        ("command_id", command_id),
        ("ib_order_id", ib_order_id),
        ("client_order_id", client_order_id),
        ("order_id", order_id),
    )

    seen: set[tuple[str, str]] = set()
    candidates: list[tuple[str, str]] = []

    def _register(column: str, value: Any) -> None:
        text = _coerce_str(value)
        if text is None:
            return
        key = (column, text)
        if key in seen:
            return
        seen.add(key)
        candidates.append((column, text))

    for column, value in raw_pairs:
        _register(column, value)

    # When identifiers are swapped between ``client_order_id`` and ``order_id``
    # (a behaviour observed when reconciling IB-only events), try both values
    # against each column before conceding that no existing row can be found.
    client_text = _coerce_str(client_order_id)
    order_text = _coerce_str(order_id)
    if client_text and order_text and client_text != order_text:
        _register("client_order_id", order_text)
        _register("order_id", client_text)

    return tuple(candidates)


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

    command_identifier = _first_nonempty(
        payload.get("command_id"),
        _lookup("command_id", "commandId"),
    )

    client_identifier = _first_nonempty(
        payload.get("client_order_id"),
        command_identifier,
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
    exchange_text = _coerce_str(exchange_value)

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
        "order_timestamp": timestamp_dt,
        "order_source": order_source_value,
        "strategy_name": strategy_value,
        "rule_id": rule_value,
        "parent_order_id": parent_value,
        "raw_payload": _normalise_raw(payload),
    }

    if exchange_text:
        normalised["exchange"] = exchange_text

    normalised["strategy"] = strategy_value

    if command_identifier is not None:
        normalised["command_id"] = str(command_identifier)

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

    async def insert_order(self, payload: Mapping[str, Any]) -> int:
        normalised = _normalise_order_payload(payload)
        return await self._execute_db_write(
            self._insert_order_sync, normalised, operation="insert"
        )

    async def update_order(self, payload: Mapping[str, Any]) -> None:
        normalised = _normalise_order_payload(payload)
        explicit_identifiers: set[str] = set()
        identifier_aliases: dict[str, tuple[str, ...]] = {
            "command_id": ("command_id", "commandId"),
            "ib_order_id": ("ib_order_id", "ibOrderId", "permid", "permId"),
            "client_order_id": (
                "client_order_id",
                "clientOrderId",
                "command_id",
                "commandId",
                "id",
            ),
            "order_id": ("order_id", "orderId"),
        }
        for column, aliases in identifier_aliases.items():
            for alias in aliases:
                if alias in payload and payload.get(alias) is not None:
                    explicit_identifiers.add(column)
                    break
        await self._execute_db_write(
            self._update_order_sync,
            normalised,
            operation="update",
            explicit_identifiers=explicit_identifiers,
        )

    async def upsert_order(
        self, payload: Mapping[str, Any], *, allow_insert: bool = True
    ) -> int | None:
        try:
            await self.update_order(payload)
        except OrderNotFoundError:
            if not allow_insert:
                return None
            return await self.insert_order(payload)
        return None

    async def _execute_db_write(
        self,
        func: Any,
        normalised: Mapping[str, Any],
        *,
        operation: str,
        **kwargs: Any,
    ) -> Any:
        try:
            return await run_in_thread(func, normalised, **kwargs)
        except (RepositoryValidationError, OrderNotFoundError):
            raise
        except MariaDBConfigurationError as exc:
            raise OrderRepositoryError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - log and wrap
            order_id = normalised.get("order_id")
            self._logger.exception(
                "Failed to %s order %s", operation, order_id
            )
            raise OrderRepositoryError(f"Failed to {operation} order") from exc

    @staticmethod
    def _timestamp_expression() -> str:
        return "COALESCE(updated_at, order_timestamp, created_at)"

    async def fetch_recent_orders(self, *, limit: int) -> list[Mapping[str, Any]]:
        limit_value = int(limit)
        if limit_value <= 0:
            return []
        try:
            return await run_in_thread(self._fetch_recent_orders_sync, limit_value)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to fetch recent orders")
            raise OrderRepositoryError("Failed to fetch recent orders") from exc

    async def paginate_orders(
        self,
        *,
        offset: int,
        limit: int,
        start_date: datetime | None = None,
    ) -> tuple[list[Mapping[str, Any]], int]:
        limit_value = max(int(limit), 0)
        offset_value = max(int(offset), 0)
        try:
            return await run_in_thread(
                self._paginate_orders_sync,
                offset_value,
                limit_value,
                start_date,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to paginate orders")
            raise OrderRepositoryError("Failed to paginate orders") from exc

    async def fetch_order(self, identifier: str) -> Mapping[str, Any] | None:
        try:
            return await run_in_thread(self._fetch_order_sync, identifier)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to fetch order %s", identifier)
            raise OrderRepositoryError("Failed to fetch order") from exc

    def _prepare_order_row(
        self,
        row: Mapping[str, Any],
        *,
        require_primary_identifier: bool,
        explicit_identifiers: set[str] | None = None,
    ) -> tuple[dict[str, Any], str | None, str | None, str | None, str | None]:
        prepared = {key: value for key, value in dict(row).items() if key != "id"}
        if prepared.get("raw_payload") is not None:
            prepared["raw_payload"] = json_dumps(prepared["raw_payload"])

        if not prepared:
            raise RepositoryValidationError("Normalised order payload is empty")

        command_id = prepared.get("command_id")
        ib_order_id = prepared.get("ib_order_id")
        client_order_id = prepared.get("client_order_id")
        order_id = prepared.get("order_id")

        if command_id is not None:
            command_id = str(command_id)
            prepared["command_id"] = command_id
        if ib_order_id is not None:
            ib_order_id = str(ib_order_id)
            prepared["ib_order_id"] = ib_order_id
        if client_order_id is not None:
            client_order_id = str(client_order_id)
            prepared["client_order_id"] = client_order_id
        if order_id is not None:
            order_id = str(order_id)
            prepared["order_id"] = order_id

        explicit_identifier_set = set(explicit_identifiers or ())

        if require_primary_identifier:
            if command_id is None and ib_order_id is None:
                raise RepositoryValidationError(
                    "Normalised order payload requires a command_id or ib_order_id"
                )
        elif (
            command_id is None
            and ib_order_id is None
            and client_order_id is None
            and order_id is None
        ):
            raise RepositoryValidationError(
                "Normalised order update requires an identifying column"
            )

        if not require_primary_identifier and prepared:
            if "command_id" in prepared and "command_id" not in explicit_identifier_set:
                prepared.pop("command_id", None)
            if "ib_order_id" in prepared and "ib_order_id" not in explicit_identifier_set:
                prepared.pop("ib_order_id", None)
            if "order_id" in prepared and "order_id" not in explicit_identifier_set:
                prepared.pop("order_id", None)
            if "client_order_id" in prepared:
                if (
                    "client_order_id" not in explicit_identifier_set
                    and "order_id" not in explicit_identifier_set
                ):
                    prepared.pop("client_order_id", None)

        return prepared, command_id, ib_order_id, client_order_id, order_id

    def _insert_order_sync(self, row: Mapping[str, Any]) -> int:
        prepared, *_ = self._prepare_order_row(
            row, require_primary_identifier=True
        )

        with use_connection() as connection:
            cursor = connection.cursor()
            try:
                if is_sqlite(connection):
                    inserted_id = self._insert_sqlite(cursor, prepared)
                else:
                    inserted_id = self._insert_mariadb(cursor, prepared)
                connection.commit()
                return inserted_id
            except Exception:
                try:
                    connection.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
                raise
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive
                    pass

    def _update_order_sync(
        self,
        row: Mapping[str, Any],
        *,
        explicit_identifiers: set[str] | None = None,
    ) -> None:
        (
            prepared,
            command_id,
            ib_order_id,
            client_order_id,
            order_id,
        ) = self._prepare_order_row(
            row,
            require_primary_identifier=False,
            explicit_identifiers=explicit_identifiers,
        )

        with use_connection() as connection:
            cursor = connection.cursor()
            try:
                if is_sqlite(connection):
                    match_column, match_value = self._find_existing_sqlite(
                        cursor,
                        command_id,
                        ib_order_id,
                        order_id,
                        client_order_id,
                    )
                    self._update_sqlite(cursor, prepared, match_column, match_value)
                else:
                    match_column, match_value = self._find_existing_mariadb(
                        cursor,
                        command_id,
                        ib_order_id,
                        order_id,
                        client_order_id,
                    )
                    self._update_mariadb(cursor, prepared, match_column, match_value)
                connection.commit()
            except OrderNotFoundError:
                raise
            except Exception:
                try:
                    connection.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
                raise
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive
                    pass

    def _find_existing(
        self,
        cursor: Any,
        *,
        placeholder: str,
        command_id: str | None,
        ib_order_id: str | None,
        order_id: str | None,
        client_order_id: str | None,
    ) -> tuple[str, str]:
        for column, value in _iter_identifier_candidates(
            command_id=command_id,
            ib_order_id=ib_order_id,
            client_order_id=client_order_id,
            order_id=order_id,
        ):
            if value is None:
                continue
            cursor.execute(
                f"SELECT id FROM orders WHERE {column} = {placeholder}", (value,)
            )
            row = cursor.fetchone()
            if row:
                return column, value
        raise OrderNotFoundError("Order not found for update")

    def _find_existing_sqlite(
        self,
        cursor: Any,
        command_id: str | None,
        ib_order_id: str | None,
        order_id: str | None,
        client_order_id: str | None,
    ) -> tuple[str, str]:
        return self._find_existing(
            cursor,
            placeholder="?",
            command_id=command_id,
            ib_order_id=ib_order_id,
            order_id=order_id,
            client_order_id=client_order_id,
        )

    def _find_existing_mariadb(
        self,
        cursor: Any,
        command_id: str | None,
        ib_order_id: str | None,
        order_id: str | None,
        client_order_id: str | None,
    ) -> tuple[str, str]:
        return self._find_existing(
            cursor,
            placeholder="%s",
            command_id=command_id,
            ib_order_id=ib_order_id,
            order_id=order_id,
            client_order_id=client_order_id,
        )

    def _insert_sqlite(self, cursor: Any, prepared: dict[str, Any]) -> int:
        columns = list(prepared.keys())
        placeholders = ", ".join(f":{col}" for col in columns)
        sql = "INSERT INTO orders ({cols}) VALUES ({values})".format(
            cols=", ".join(columns), values=placeholders
        )
        cursor.execute(sql, prepared)
        lastrowid = getattr(cursor, "lastrowid", None)
        if lastrowid is None:
            raise OrderRepositoryError("Failed to obtain inserted order id")
        return int(lastrowid)

    def _update_sqlite(
        self,
        cursor: Any,
        prepared: dict[str, Any],
        match_column: str,
        match_value: str,
    ) -> None:
        update_columns = [
            col for col in prepared.keys() if col not in {"created_at", "updated_at"}
        ]
        assignments = ", ".join(f"{col} = :{col}" for col in update_columns)
        params = {col: prepared[col] for col in update_columns}
        if "updated_at" in prepared:
            if assignments:
                assignments += ", updated_at = :updated_at"
            else:
                assignments = "updated_at = :updated_at"
            params["updated_at"] = prepared["updated_at"]
        else:
            if assignments:
                assignments += ", updated_at = CURRENT_TIMESTAMP"
            else:
                assignments = "updated_at = CURRENT_TIMESTAMP"
        if not assignments:
            assignments = "updated_at = CURRENT_TIMESTAMP"
        params[match_column] = match_value
        sql = "UPDATE orders SET {assignments} WHERE {column} = :{column}".format(
            assignments=assignments, column=match_column
        )
        cursor.execute(sql, params)

    def _insert_mariadb(self, cursor: Any, prepared: dict[str, Any]) -> int:
        columns = list(prepared.keys())
        placeholders = ", ".join(f"%({col})s" for col in columns)
        sql = "INSERT INTO orders ({cols}) VALUES ({values})".format(
            cols=", ".join(columns), values=placeholders
        )
        cursor.execute(sql, prepared)
        lastrowid = getattr(cursor, "lastrowid", None)
        if lastrowid is None:
            raise OrderRepositoryError("Failed to obtain inserted order id")
        return int(lastrowid)

    def _update_mariadb(
        self,
        cursor: Any,
        prepared: dict[str, Any],
        match_column: str,
        match_value: str,
    ) -> None:
        update_columns = [
            col for col in prepared.keys() if col not in {"created_at", "updated_at"}
        ]
        assignments = ", ".join(f"{col} = %({col})s" for col in update_columns)
        params = {col: prepared[col] for col in update_columns}
        if "updated_at" in prepared:
            if assignments:
                assignments += ", updated_at = %(updated_at)s"
            else:
                assignments = "updated_at = %(updated_at)s"
            params["updated_at"] = prepared["updated_at"]
        else:
            if assignments:
                assignments += ", updated_at = NOW()"
            else:
                assignments = "updated_at = NOW()"
        if not assignments:
            assignments = "updated_at = NOW()"
        params[match_column] = match_value
        sql = "UPDATE orders SET {assignments} WHERE {column} = %({column})s".format(
            assignments=assignments, column=match_column
        )
        cursor.execute(sql, params)

    def _fetch_recent_orders_sync(self, limit: int) -> list[Mapping[str, Any]]:
        with use_connection() as connection:
            placeholder = "?" if is_sqlite(connection) else "%s"
            timestamp_expr = self._timestamp_expression()
            sql = (
                "SELECT * FROM orders "
                "WHERE is_deleted = 0 "
                f"ORDER BY {timestamp_expr} DESC "
                f"LIMIT {placeholder}"
            )
            cursor = connection.cursor()
            try:
                cursor.execute(sql, (limit,))
                rows = cursor.fetchall()
                columns = [col[0] for col in cursor.description or ()]
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive
                    pass
        return self._rows_to_orders(rows, columns)

    def _paginate_orders_sync(
        self,
        offset: int,
        limit: int,
        start_date: datetime | None,
    ) -> tuple[list[Mapping[str, Any]], int]:
        with use_connection() as connection:
            placeholder = "?" if is_sqlite(connection) else "%s"
            timestamp_expr = self._timestamp_expression()
            where_clauses = ["is_deleted = 0"]
            params: list[Any] = []
            if start_date is not None:
                formatted = format_datetime_for_db(start_date)
                where_clauses.append(f"{timestamp_expr} >= {placeholder}")
                params.append(formatted)
            where_sql = " AND ".join(where_clauses)
            count_sql = f"SELECT COUNT(*) FROM orders WHERE {where_sql}"
            cursor = connection.cursor()
            try:
                cursor.execute(count_sql, tuple(params))
                total_row = cursor.fetchone()
                total = int(total_row[0]) if total_row else 0
                order_sql = (
                    "SELECT * FROM orders "
                    f"WHERE {where_sql} "
                    f"ORDER BY {timestamp_expr} DESC "
                    f"LIMIT {placeholder} OFFSET {placeholder}"
                )
                query_params: list[Any] = list(params)
                query_params.extend([limit, offset])
                cursor.execute(order_sql, tuple(query_params))
                rows = cursor.fetchall()
                columns = [col[0] for col in cursor.description or ()]
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive
                    pass
        return self._rows_to_orders(rows, columns), total

    def _fetch_order_sync(self, identifier: str) -> Mapping[str, Any] | None:
        identifier_text = str(identifier or "").strip()
        if not identifier_text:
            return None
        with use_connection() as connection:
            placeholder = "?" if is_sqlite(connection) else "%s"
            timestamp_expr = self._timestamp_expression()
            where_parts: list[str] = [
                f"command_id = {placeholder}",
                f"order_id = {placeholder}",
                f"client_order_id = {placeholder}",
                f"ib_order_id = {placeholder}",
            ]
            params: list[Any] = [identifier_text] * 4
            try:
                identifier_int = int(identifier_text)
            except ValueError:
                identifier_int = None
            if identifier_int is not None:
                where_parts.append(f"id = {placeholder}")
                params.append(identifier_int)
            where_sql = " OR ".join(where_parts)
            sql = (
                "SELECT * FROM orders "
                "WHERE is_deleted = 0 AND ("
                f"{where_sql}"
                ") "
                f"ORDER BY {timestamp_expr} DESC "
                "LIMIT 1"
            )
            cursor = connection.cursor()
            try:
                cursor.execute(sql, tuple(params))
                row = cursor.fetchone()
                columns = [col[0] for col in cursor.description or ()]
            finally:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover - defensive
                    pass
        if not row:
            return None
        rows = self._rows_to_orders([row], columns)
        return rows[0] if rows else None

    def _rows_to_orders(
        self, rows: Iterable[Any], columns: Sequence[str] | None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                data = dict(row)
            else:
                if columns is None:
                    raise OrderRepositoryError("Unable to map order row")
                data = {
                    columns[index]: row[index]
                    for index in range(min(len(columns), len(row)))
                }
            raw_payload = data.get("raw_payload")
            if isinstance(raw_payload, (bytes, bytearray)):
                try:
                    raw_payload = raw_payload.decode("utf-8")
                except Exception:
                    pass
            if isinstance(raw_payload, str):
                try:
                    data["raw_payload"] = json.loads(raw_payload)
                except Exception:
                    data["raw_payload"] = raw_payload
            for field in ("created_at", "updated_at", "order_timestamp", "executed_at"):
                value = data.get(field)
                parsed = _parse_datetime(value)
                if parsed is not None:
                    data[field] = parsed.isoformat()
            results.append(data)
        return results
