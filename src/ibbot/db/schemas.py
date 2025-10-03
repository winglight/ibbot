"""SQLAlchemy ORM models for the MariaDB persistence layer."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "metadata",
    "Order",
    "MarketPrice",
    "ServiceMetric",
    "RiskConfigSnapshot",
    "RiskRuleSnapshot",
    "MarketEvent",
]


class Base(DeclarativeBase):
    pass


metadata = Base.metadata


class Order(Base):
    """Normalized representation of order lifecycle events."""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_symbol", "symbol"),
        Index("ix_orders_strategy", "strategy"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trigger: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trigger_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trigger_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    remaining_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sec_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MarketPrice(Base):
    """Last known quote for a traded symbol."""

    __tablename__ = "market_prices"
    __table_args__ = (Index("ix_market_prices_symbol", "symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64))
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ServiceMetric(Base):
    """Metric samples emitted by the platform services."""

    __tablename__ = "service_metrics"
    __table_args__ = (
        Index("ix_service_metrics_metric", "metric"),
        Index("ix_service_metrics_service", "service"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str] = mapped_column(String(128))
    value: Mapped[float] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    host: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class RiskConfigSnapshot(Base):
    """JSON snapshot of the aggregated risk configuration."""

    __tablename__ = "risk_config_snapshots"
    __table_args__ = (Index("ix_risk_config_service", "service"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    rules_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class RiskRuleSnapshot(Base):
    """Snapshot of per-symbol risk rule payloads."""

    __tablename__ = "risk_rule_snapshots"
    __table_args__ = (
        Index("ix_risk_rule_service", "service"),
        Index("ix_risk_rule_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class MarketEvent(Base):
    """Generic market event payloads originally persisted to InfluxDB."""

    __tablename__ = "market_events"
    __table_args__ = (
        Index("ix_market_events_event_type", "event_type"),
        Index("ix_market_events_topic", "topic"),
        Index("ix_market_events_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(128))
    topic: Mapped[str] = mapped_column(String(256))
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
