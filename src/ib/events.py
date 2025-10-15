from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _ensure_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value):  # NaN check
            return Decimal("NaN")
        return Decimal(str(value))
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _ensure_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


class IBBaseModel(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)


class AccountUpdateEvent(IBBaseModel):
    account: str
    key: str
    value: Decimal = Field(..., allow_inf_nan=True)
    currency: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: Any) -> Decimal:
        decimal_value = _ensure_decimal(value)
        if decimal_value is None:
            return Decimal("0")
        return decimal_value

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class PortfolioPositionEvent(IBBaseModel):
    account: str
    contract_id: int
    symbol: str
    security_type: str
    position: Decimal = Field(..., allow_inf_nan=True)
    average_cost: Decimal | None = Field(default=None, allow_inf_nan=True)
    market_price: Decimal | None = Field(default=None, allow_inf_nan=True)
    market_value: Decimal | None = Field(default=None, allow_inf_nan=True)
    realized_pnl: Decimal | None = Field(default=None, allow_inf_nan=True)
    unrealized_pnl: Decimal | None = Field(default=None, allow_inf_nan=True)
    contract_multiplier: Decimal | None = Field(default=None, allow_inf_nan=True)
    price_scale: Decimal | None = Field(default=None, allow_inf_nan=True)
    price_scale_source: str | None = None
    currency: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator(
        "position",
        "average_cost",
        "market_price",
        "market_value",
        "realized_pnl",
        "unrealized_pnl",
        "contract_multiplier",
        "price_scale",
        mode="before",
    )
    @classmethod
    def _validate_decimal(cls, value: Any) -> Decimal | None:
        return _ensure_decimal(value)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class OrderStatusEvent(IBBaseModel):
    order_id: int
    status: str
    filled: Decimal = Field(..., allow_inf_nan=True)
    remaining: Decimal = Field(..., allow_inf_nan=True)
    avg_fill_price: Decimal | None = Field(default=None, allow_inf_nan=True)
    perm_id: int | None = None
    parent_id: int | None = None
    last_fill_price: Decimal | None = Field(default=None, allow_inf_nan=True)
    client_id: int | None = None
    why_held: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("filled", "remaining", "avg_fill_price", "last_fill_price", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Decimal | None:
        return _ensure_decimal(value)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class ExecutionEvent(IBBaseModel):
    exec_id: str
    order_id: int
    client_id: int | None = None
    perm_id: int | None = None
    symbol: str
    security_type: str
    exchange: str
    side: str
    shares: Decimal = Field(..., allow_inf_nan=True)
    price: Decimal = Field(..., allow_inf_nan=True)
    cumulative_quantity: Decimal = Field(..., allow_inf_nan=True)
    average_price: Decimal = Field(..., allow_inf_nan=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator(
        "shares",
        "price",
        "cumulative_quantity",
        "average_price",
        mode="before",
    )
    @classmethod
    def _coerce_exec_decimal(cls, value: Any) -> Decimal:
        decimal_value = _ensure_decimal(value)
        if decimal_value is None:
            return Decimal("0")
        return decimal_value

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class MarketTickEvent(IBBaseModel):
    contract_id: int
    tick_type: str
    value: Decimal = Field(..., allow_inf_nan=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: Any) -> Decimal:
        decimal_value = _ensure_decimal(value)
        if decimal_value is None:
            return Decimal("0")
        return decimal_value

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class DOMLevelEvent(IBBaseModel):
    contract_id: int
    position: int
    operation: str
    side: str
    price: Decimal = Field(..., allow_inf_nan=True)
    size: Decimal = Field(..., allow_inf_nan=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("price", "size", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Decimal:
        decimal_value = _ensure_decimal(value)
        if decimal_value is None:
            return Decimal("0")
        return decimal_value

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class TickByTickEvent(IBBaseModel):
    contract_id: int
    tick_type: str
    price: Decimal = Field(..., allow_inf_nan=True)
    size: Decimal = Field(..., allow_inf_nan=True)
    exchange: str | None = None
    special_conditions: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("price", "size", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Decimal:
        decimal_value = _ensure_decimal(value)
        if decimal_value is None:
            return Decimal("0")
        return decimal_value

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class RealTimeBarEvent(IBBaseModel):
    contract_id: int
    open: Decimal = Field(..., allow_inf_nan=True)
    high: Decimal = Field(..., allow_inf_nan=True)
    low: Decimal = Field(..., allow_inf_nan=True)
    close: Decimal = Field(..., allow_inf_nan=True)
    volume: Decimal = Field(..., allow_inf_nan=True)
    wap: Decimal = Field(..., allow_inf_nan=True)
    count: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("open", "high", "low", "close", "volume", "wap", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Decimal:
        decimal_value = _ensure_decimal(value)
        if decimal_value is None:
            return Decimal("0")
        return decimal_value

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class ErrorEvent(IBBaseModel):
    request_id: int | None = None
    code: int
    message: str
    advanced_order_reject_json: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime:
        return _ensure_datetime(value)


class RedisChannel(str, Enum):
    ACCOUNT_UPDATES = "ib:account:updates"
    PORTFOLIO = "ib:account:portfolio"
    ORDERS = "ib:orders:status"
    EXECUTIONS = "ib:orders:executions"
    MARKET_TICKS = "ib:market:ticks"
    MARKET_DOM = "ib:market:dom"
    MARKET_TICK_BY_TICK = "ib:market:tick-by-tick"
    MARKET_REAL_TIME_BARS = "ib:market:real-time-bars"
    ERRORS = "ib:errors"


def account_update_from_ib(
    account: str,
    key: str,
    value: Any,
    currency: str | None,
    timestamp: Any = None,
) -> AccountUpdateEvent:
    return AccountUpdateEvent(
        account=account,
        key=key,
        value=value,
        currency=currency,
        timestamp=timestamp,
    )


def portfolio_position_from_ib(
    account: str,
    contract_id: int,
    symbol: str,
    security_type: str,
    position: Any,
    average_cost: Any,
    market_price: Any,
    market_value: Any,
    realized_pnl: Any,
    unrealized_pnl: Any,
    currency: str | None,
    timestamp: Any = None,
    *,
    contract_multiplier: Any | None = None,
    price_scale: Any | None = None,
    price_scale_source: str | None = None,
) -> PortfolioPositionEvent:
    return PortfolioPositionEvent(
        account=account,
        contract_id=contract_id,
        symbol=symbol,
        security_type=security_type,
        position=position,
        average_cost=average_cost,
        market_price=market_price,
        market_value=market_value,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        contract_multiplier=contract_multiplier,
        price_scale=price_scale,
        price_scale_source=price_scale_source,
        currency=currency,
        timestamp=timestamp,
    )


def order_status_from_ib(
    order_id: int,
    status: str,
    filled: Any,
    remaining: Any,
    avg_fill_price: Any,
    perm_id: int | None,
    parent_id: int | None,
    last_fill_price: Any,
    client_id: int | None,
    why_held: str | None,
    timestamp: Any = None,
) -> OrderStatusEvent:
    return OrderStatusEvent(
        order_id=order_id,
        status=status,
        filled=filled,
        remaining=remaining,
        avg_fill_price=avg_fill_price,
        perm_id=perm_id,
        parent_id=parent_id,
        last_fill_price=last_fill_price,
        client_id=client_id,
        why_held=why_held,
        timestamp=timestamp,
    )


def execution_from_ib(
    exec_id: str,
    order_id: int,
    client_id: int | None,
    perm_id: int | None,
    symbol: str,
    security_type: str,
    exchange: str,
    side: str,
    shares: Any,
    price: Any,
    cumulative_quantity: Any,
    average_price: Any,
    timestamp: Any = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        exec_id=exec_id,
        order_id=order_id,
        client_id=client_id,
        perm_id=perm_id,
        symbol=symbol,
        security_type=security_type,
        exchange=exchange,
        side=side,
        shares=shares,
        price=price,
        cumulative_quantity=cumulative_quantity,
        average_price=average_price,
        timestamp=timestamp,
    )


def market_tick_from_ib(
    contract_id: int,
    tick_type: str,
    value: Any,
    timestamp: Any = None,
) -> MarketTickEvent:
    return MarketTickEvent(
        contract_id=contract_id,
        tick_type=tick_type,
        value=value,
        timestamp=timestamp,
    )


def dom_level_from_ib(
    contract_id: int,
    position: int,
    operation: str,
    side: str,
    price: Any,
    size: Any,
    timestamp: Any = None,
) -> DOMLevelEvent:
    return DOMLevelEvent(
        contract_id=contract_id,
        position=position,
        operation=operation,
        side=side,
        price=price,
        size=size,
        timestamp=timestamp,
    )


def tick_by_tick_from_ib(
    contract_id: int,
    tick_type: str,
    price: Any,
    size: Any,
    exchange: str | None,
    special_conditions: str | None,
    timestamp: Any = None,
) -> TickByTickEvent:
    return TickByTickEvent(
        contract_id=contract_id,
        tick_type=tick_type,
        price=price,
        size=size,
        exchange=exchange,
        special_conditions=special_conditions,
        timestamp=timestamp,
    )


def real_time_bar_from_ib(
    contract_id: int,
    time: Any,
    open_: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: Any,
    wap: Any,
    count: int,
) -> RealTimeBarEvent:
    return RealTimeBarEvent(
        contract_id=contract_id,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        wap=wap,
        count=count,
        timestamp=time,
    )


def error_from_ib(
    request_id: int | None,
    code: int,
    message: str,
    advanced_order_reject_json: str | None = None,
    timestamp: Any = None,
) -> ErrorEvent:
    return ErrorEvent(
        request_id=request_id,
        code=code,
        message=message,
        advanced_order_reject_json=advanced_order_reject_json,
        timestamp=timestamp,
    )


def iter_decimal_fields(model: BaseModel) -> Iterable[tuple[str, Decimal]]:
    for field_name, value in model.model_dump().items():
        if isinstance(value, Decimal):
            yield field_name, value
        elif isinstance(value, Mapping):
            for nested_field, nested_value in iter_decimal_fields_dict(value):
                yield f"{field_name}.{nested_field}", nested_value
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for idx, item in enumerate(value):
                if isinstance(item, Decimal):
                    yield f"{field_name}[{idx}]", item


def iter_decimal_fields_dict(data: Mapping[str, Any]) -> Iterable[tuple[str, Decimal]]:
    for key, value in data.items():
        if isinstance(value, Decimal):
            yield key, value
        elif isinstance(value, Mapping):
            for nested_field, nested_value in iter_decimal_fields_dict(value):
                yield f"{key}.{nested_field}", nested_value
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for idx, item in enumerate(value):
                if isinstance(item, Decimal):
                    yield f"{key}[{idx}]", item
