"""Redis-backed helpers for aggregating account-level PnL statistics."""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any, Dict, Iterable, Mapping, Optional

from loguru import logger

from ..cache import ensure_redis
from ..utils import NY_TZ, ensure_local_timezone, now_local, parse_local_datetime


DAILY_PNL_KEY = "account:daily_pnl"
DAILY_ORDERS_KEY = "account:daily_orders"
ORDER_CONTRIB_KEY = "account:order_contrib"
NET_LIQ_KEY = "account:net_liquidation"
NET_LIQUIDATION_TTL_SECONDS = int(os.getenv("NET_LIQUIDATION_CACHE_TTL_SECONDS", "3600"))

_logger = logger.bind(module="account_pnl_cache", request_id="-")


def _to_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return ensure_local_timezone(value)
    if isinstance(value, str):
        return parse_local_datetime(value)
    return None


def _trade_date(executed_at: Any, created_at: Any) -> Optional[date]:
    executed_dt = _to_datetime(executed_at)
    if executed_dt is None:
        executed_dt = _to_datetime(created_at)
    if executed_dt is None:
        return None
    try:
        return ensure_local_timezone(executed_dt).astimezone(NY_TZ).date()
    except Exception:
        return None


def _load_contrib(raw: Optional[bytes]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)
    except Exception:
        return None


def _serialise_contrib(day: str, pnl: float) -> str:
    return json.dumps({"date": day, "pnl": pnl}, ensure_ascii=False)


async def record_account_fill(order: Any) -> None:
    """Update daily account aggregates for a filled order."""

    if order is None:
        return

    order_id = getattr(order, "order_id", None)
    if not order_id:
        return

    pnl_value = getattr(order, "pnl", None)
    if pnl_value is None:
        return

    try:
        pnl_float = float(pnl_value)
    except (TypeError, ValueError):
        return

    executed_at = getattr(order, "executed_at", None)
    created_at = getattr(order, "created_at", None)
    trade_day = _trade_date(executed_at, created_at)
    if trade_day is None:
        return

    await apply_order_contribution(str(order_id), trade_day, pnl_float)


async def apply_order_contribution(order_id: str, trade_day: date, pnl_value: float) -> None:
    """Apply the given order contribution to the aggregated daily totals."""

    client = await ensure_redis()
    if client is None:
        return

    day_key = trade_day.isoformat()
    existing_raw = await client.hget(ORDER_CONTRIB_KEY, order_id)
    existing = _load_contrib(existing_raw)

    existing_day = existing.get("date") if isinstance(existing, dict) else None
    try:
        existing_pnl = float(existing.get("pnl", 0.0)) if isinstance(existing, dict) else 0.0
    except (TypeError, ValueError):
        existing_pnl = 0.0

    pipe = client.pipeline(transaction=True)

    if existing_day and existing_day != day_key:
        pipe.hincrbyfloat(DAILY_PNL_KEY, existing_day, -existing_pnl)
        pipe.hincrby(DAILY_ORDERS_KEY, existing_day, -1)

    if existing_day == day_key:
        delta_pnl = pnl_value - existing_pnl
        if abs(delta_pnl) > 1e-9:
            pipe.hincrbyfloat(DAILY_PNL_KEY, day_key, delta_pnl)
    else:
        pipe.hincrbyfloat(DAILY_PNL_KEY, day_key, pnl_value)
        pipe.hincrby(DAILY_ORDERS_KEY, day_key, 1)

    pipe.hset(ORDER_CONTRIB_KEY, order_id, _serialise_contrib(day_key, pnl_value))

    try:
        await pipe.execute()
    except Exception as exc:
        _logger.warning("Failed to update account daily cache", error=str(exc))


async def remove_order_contribution(order_id: str) -> None:
    """Remove an order from the aggregated totals."""

    client = await ensure_redis()
    if client is None:
        return

    existing_raw = await client.hget(ORDER_CONTRIB_KEY, order_id)
    existing = _load_contrib(existing_raw)
    if not isinstance(existing, dict):
        await client.hdel(ORDER_CONTRIB_KEY, order_id)
        return

    day_key = existing.get("date")
    try:
        pnl_value = float(existing.get("pnl", 0.0))
    except (TypeError, ValueError):
        pnl_value = 0.0

    pipe = client.pipeline(transaction=True)
    if day_key:
        if pnl_value:
            pipe.hincrbyfloat(DAILY_PNL_KEY, day_key, -pnl_value)
        pipe.hincrby(DAILY_ORDERS_KEY, day_key, -1)
    pipe.hdel(ORDER_CONTRIB_KEY, order_id)

    try:
        await pipe.execute()
    except Exception as exc:
        _logger.warning("Failed to remove order contribution", error=str(exc))


async def clear_daily_cache() -> None:
    client = await ensure_redis()
    if client is None:
        return
    try:
        await client.delete(DAILY_PNL_KEY, DAILY_ORDERS_KEY, ORDER_CONTRIB_KEY)
    except Exception as exc:
        _logger.warning("Failed to clear account daily cache", error=str(exc))


async def rebuild_account_daily_cache(orders: Iterable[Mapping[str, Any]]) -> None:
    """Rebuild the daily aggregates from database orders."""

    client = await ensure_redis()
    if client is None:
        return

    daily_totals: Dict[str, Dict[str, float]] = {}
    order_contrib: Dict[str, Dict[str, Any]] = {}

    for order in orders:
        order_id = order.get("order_id")
        if order_id is None:
            continue
        order_id = str(order_id)
        pnl_value = order.get("pnl")
        if pnl_value is None:
            continue
        try:
            pnl_float = float(pnl_value)
        except (TypeError, ValueError):
            continue
        trade_day = _trade_date(order.get("executed_at"), order.get("created_at"))
        if trade_day is None:
            continue
        day_key = trade_day.isoformat()
        totals = daily_totals.setdefault(day_key, {"pnl": 0.0, "orders": 0})
        totals["pnl"] += pnl_float
        totals["orders"] += 1
        order_contrib[order_id] = {"date": day_key, "pnl": pnl_float}

    pipe = client.pipeline(transaction=True)
    pipe.delete(DAILY_PNL_KEY, DAILY_ORDERS_KEY, ORDER_CONTRIB_KEY)

    if daily_totals:
        pipe.hset(
            DAILY_PNL_KEY,
            mapping={day: f"{value['pnl']:.6f}" for day, value in daily_totals.items()},
        )
        pipe.hset(
            DAILY_ORDERS_KEY,
            mapping={day: str(int(value["orders"])) for day, value in daily_totals.items()},
        )
    if order_contrib:
        pipe.hset(
            ORDER_CONTRIB_KEY,
            mapping={order_id: json.dumps(payload, ensure_ascii=False) for order_id, payload in order_contrib.items()},
        )
    try:
        await pipe.execute()
    except Exception as exc:
        _logger.warning("Failed to rebuild account daily cache", error=str(exc))


async def load_all_daily_totals() -> Optional[Dict[str, Dict[str, float]]]:
    client = await ensure_redis()
    if client is None:
        return None
    try:
        pnl_map = await client.hgetall(DAILY_PNL_KEY)
        orders_map = await client.hgetall(DAILY_ORDERS_KEY)
    except Exception as exc:
        _logger.warning("Failed to load account daily cache", error=str(exc))
        return None

    if not pnl_map and not orders_map:
        return {}

    results: Dict[str, Dict[str, float]] = {}
    for day_bytes, pnl_raw in pnl_map.items():
        day = day_bytes.decode() if isinstance(day_bytes, bytes) else day_bytes
        try:
            pnl_value = float(pnl_raw)
        except (TypeError, ValueError):
            pnl_value = 0.0
        results.setdefault(day, {})["pnl"] = pnl_value

    for day_bytes, count_raw in orders_map.items():
        day = day_bytes.decode() if isinstance(day_bytes, bytes) else day_bytes
        try:
            count_value = int(float(count_raw))
        except (TypeError, ValueError):
            count_value = 0
        results.setdefault(day, {})["orders"] = count_value

    return results


async def read_net_liquidation_snapshot() -> Optional[Dict[str, Any]]:
    client = await ensure_redis()
    if client is None:
        return None
    try:
        raw = await client.get(NET_LIQ_KEY)
    except Exception as exc:
        _logger.warning("Failed to read net liquidation snapshot", error=str(exc))
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        data = json.loads(raw)
    except Exception:
        return None
    return data


async def write_net_liquidation_snapshot(
    value: float,
    *,
    source: str,
    timestamp: Optional[datetime] = None,
) -> None:
    client = await ensure_redis()
    if client is None:
        return
    ts = ensure_local_timezone(timestamp) if timestamp else now_local()
    payload = {
        "value": float(value),
        "timestamp": ts.isoformat(),
        "source": source,
    }
    try:
        await client.set(
            NET_LIQ_KEY,
            json.dumps(payload, ensure_ascii=False),
            ex=NET_LIQUIDATION_TTL_SECONDS,
        )
    except Exception as exc:
        _logger.warning("Failed to persist net liquidation snapshot", error=str(exc))


async def rebuild_account_daily_cache_from_db(db_manager: Any) -> None:
    try:
        orders = db_manager.fetch_orders_for_pnl()
    except Exception as exc:
        _logger.warning("Failed to fetch orders for cache rebuild", error=str(exc))
        return
    await rebuild_account_daily_cache(orders)
