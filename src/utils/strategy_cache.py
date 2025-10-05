"""Helpers for maintaining strategy snapshots in Redis."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from src.cache import ensure_redis
from src.utils import ensure_local_timezone, now_local, parse_local_datetime, to_local_iso
from src.routes.service_clients import (
    OrdersServiceClient,
    ServiceClientError,
    ServiceUnavailableError,
)

MAX_RECENT_ORDERS = int(os.getenv("STRATEGY_CACHE_MAX_RECENT", "200"))
STRATEGY_CACHE_TTL_SECONDS = int(os.getenv("STRATEGY_CACHE_TTL_SECONDS", "900"))
STRATEGY_CACHE_REFRESH_PAGE_SIZE = int(os.getenv("STRATEGY_CACHE_REFRESH_PAGE_SIZE", "200"))
STRATEGY_CACHE_REFRESH_MAX_PAGES = int(os.getenv("STRATEGY_CACHE_REFRESH_MAX_PAGES", "10"))

__all__ = [
    "order_to_snapshot",
    "record_filled_order",
    "refresh_strategy_cache",
    "schedule_strategy_cache_refresh",
    "rebuild_strategy_cache",
    "clear_strategy_cache",
    "get_strategy_stats",
    "get_strategy_orders",
    "get_recent_fills",
    "get_daily_pnl",
    "compute_performance_metrics",
    "mark_strategy_cache_stale",
]


_ORDERS_CLIENT: OrdersServiceClient | None = None
_REFRESH_LOCKS: Dict[str, asyncio.Lock] = {}
_REFRESH_LOCKS_GUARD = asyncio.Lock()


async def _get_refresh_lock(strategy: str) -> asyncio.Lock:
    async with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(strategy)
        if lock is None:
            lock = asyncio.Lock()
            _REFRESH_LOCKS[strategy] = lock
        return lock


def _get_orders_client() -> OrdersServiceClient | None:
    global _ORDERS_CLIENT
    if _ORDERS_CLIENT is not None:
        return _ORDERS_CLIENT
    try:
        client = OrdersServiceClient()
    except Exception:
        return None
    _ORDERS_CLIENT = client
    return client


def _base_key(strategy: str) -> str:
    return f"strategy:{strategy}"


def _orders_key(strategy: str) -> str:
    return f"{_base_key(strategy)}:orders"


def _orders_zset_key(strategy: str) -> str:
    return f"{_base_key(strategy)}:orders:zset"


def _recent_key(strategy: str) -> str:
    return f"{_base_key(strategy)}:recent"


def _stats_key(strategy: str) -> str:
    return f"{_base_key(strategy)}:stats"


def _daily_hash_key(strategy: str) -> str:
    return f"{_base_key(strategy)}:daily"


def _daily_zset_key(strategy: str) -> str:
    return f"{_base_key(strategy)}:daily:zset"


def _normalise_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _normalise_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        if isinstance(value, bool):
            return int(value)
        return int(float(value))
    except (TypeError, ValueError):
        return 0


_MISSING = object()


def _extract_from_order(order: Any, *names: str) -> Any:
    if not names:
        return None
    for name in names:
        value = getattr(order, name, _MISSING)
        if value is not _MISSING:
            return value
        if isinstance(order, Mapping):
            if name in order:
                return order[name]
            for sub_key in ("result", "request", "metadata"):
                sub_mapping = order.get(sub_key)
                if isinstance(sub_mapping, Mapping) and name in sub_mapping:
                    return sub_mapping[name]
    return None


def order_to_snapshot(order: Any, *, strategy_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Convert an order object into a serialisable snapshot for Redis."""

    if order is None:
        return None

    strategy = strategy_name or _extract_from_order(order, "strategy_name", "strategy")
    if not strategy:
        return None

    status = _extract_from_order(order, "status", "order_status")
    if not status or str(status).lower() != "filled":
        return None

    order_id = _extract_from_order(order, "order_id", "id", "command_id")
    if not order_id:
        return None

    created_raw = _extract_from_order(
        order,
        "created_at",
        "submitted_at",
        "requested_at",
        "timestamp",
    )
    if isinstance(created_raw, str):
        created_at = parse_local_datetime(created_raw)
    else:
        created_at = ensure_local_timezone(created_raw)

    executed_raw = _extract_from_order(
        order,
        "executed_at",
        "completed_at",
        "filled_at",
        "timestamp",
    )
    if isinstance(executed_raw, str):
        executed_at = parse_local_datetime(executed_raw)
    else:
        executed_at = ensure_local_timezone(executed_raw)

    if executed_at is None:
        executed_at = created_at

    quantity = _normalise_int(_extract_from_order(order, "quantity", "qty", "filled_quantity"))
    pnl = _normalise_float(_extract_from_order(order, "pnl", "realized_pnl"))
    commission = _normalise_float(_extract_from_order(order, "commission", "commission_paid"))
    price = _normalise_float(
        _extract_from_order(order, "price", "limit_price", "requested_price")
    )
    fill_price = _normalise_float(
        _extract_from_order(order, "fill_price", "avg_fill_price", "filled_price")
    )
    if not fill_price:
        fill_price = price

    duration = None
    if created_at and executed_at:
        try:
            delta = executed_at - created_at
            duration = max(delta.total_seconds(), 0.0)
        except Exception:
            duration = None

    snapshot: Dict[str, Any] = {
        "order_id": str(order_id),
        "strategy": strategy,
        "symbol": getattr(order, "symbol", None),
        "action": getattr(order, "action", None),
        "quantity": quantity,
        "order_type": getattr(order, "order_type", None),
        "order_source": getattr(order, "order_source", None),
        "price": price,
        "fill_price": fill_price,
        "pnl": pnl,
        "commission": commission,
        "created_at": to_local_iso(created_at) if created_at else None,
        "executed_at": to_local_iso(executed_at) if executed_at else None,
        "duration": duration,
    }

    return snapshot


def _parse_snapshot_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return parse_local_datetime(value)


def _snapshot_timestamp(snapshot: Mapping[str, Any]) -> Optional[float]:
    executed = _parse_snapshot_datetime(snapshot.get("executed_at"))
    created = _parse_snapshot_datetime(snapshot.get("created_at"))
    anchor = ensure_local_timezone(executed or created)
    if anchor is None:
        return None
    try:
        return float(anchor.timestamp())
    except Exception:
        return None


def _datetime_to_timestamp(value: Optional[datetime]) -> Optional[float]:
    if value is None:
        return None
    anchored = ensure_local_timezone(value)
    if anchored is None:
        return None
    try:
        return float(anchored.timestamp())
    except Exception:
        return None


def _filter_orders_by_range(
    orders: Sequence[Mapping[str, Any]],
    start_bound: Optional[datetime],
    end_bound: Optional[datetime],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for order in orders:
        executed_at = _parse_snapshot_datetime(order.get("executed_at"))
        created_at = _parse_snapshot_datetime(order.get("created_at"))
        anchor = ensure_local_timezone(executed_at or created_at)
        if anchor is None:
            continue
        if start_bound and anchor < start_bound:
            continue
        if end_bound and anchor > end_bound:
            continue
        if isinstance(order, dict):
            result.append(dict(order))
        else:
            result.append(dict(order))
    result.sort(
        key=lambda item: (
            _parse_snapshot_datetime(item.get("executed_at"))
            or _parse_snapshot_datetime(item.get("created_at"))
            or datetime.min.replace(tzinfo=now_local().tzinfo)
        ),
        reverse=True,
    )
    return result


def _prepare_order_map(raw_map: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
    orders: Dict[str, Dict[str, Any]] = {}
    for key, payload in raw_map.items():
        if not payload:
            continue
        try:
            decoded = json.loads(payload)
        except Exception:
            continue
        if not isinstance(decoded, dict):
            continue
        decoded.setdefault("order_id", key)
        orders[key] = decoded
    return orders


def _serialise_order_map(order_map: Mapping[str, Dict[str, Any]]) -> Dict[str, str]:
    return {order_id: json.dumps(snapshot, default=str) for order_id, snapshot in order_map.items()}


def _serialise_recent(sorted_orders: Sequence[Dict[str, Any]]) -> List[str]:
    entries: List[str] = []
    limited = list(
        sorted(
            sorted_orders,
            key=lambda item: (
                _parse_snapshot_datetime(item.get("executed_at"))
                or _parse_snapshot_datetime(item.get("created_at"))
                or datetime.min.replace(tzinfo=now_local().tzinfo)
            ),
            reverse=True,
        )
    )[:MAX_RECENT_ORDERS]
    for item in limited:
        try:
            entries.append(json.dumps(item, default=str))
        except Exception:
            continue
    return entries


def _compute_stats(order_map: Mapping[str, Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, float], List[str]]:
    orders = list(order_map.values())
    if not orders:
        now_iso = to_local_iso(now_local()) or now_local().isoformat()
        return (
            {
                "filled_count": "0",
                "pnl_sum": "0.0",
                "pnl_sumsq": "0.0",
                "commission_sum": "0.0",
                "win_count": "0",
                "loss_count": "0",
                "duration_sum": "0.0",
                "duration_count": "0",
                "avg_duration": "0.0",
                "cumulative_pnl": "0.0",
                "peak_pnl": "0.0",
                "max_drawdown": "0.0",
                "last_trigger_ts": "",
                "updated_at": now_iso,
                "stale": "0",
            },
            {},
            [],
        )

    sorted_orders = sorted(
        orders,
        key=lambda item: (
            _parse_snapshot_datetime(item.get("executed_at"))
            or _parse_snapshot_datetime(item.get("created_at"))
            or datetime.min.replace(tzinfo=now_local().tzinfo)
        ),
    )

    pnl_sum = 0.0
    pnl_sumsq = 0.0
    commission_sum = 0.0
    win_count = 0
    loss_count = 0
    duration_sum = 0.0
    duration_count = 0
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    last_trigger = None
    daily: Dict[str, float] = {}

    for snapshot in sorted_orders:
        pnl = _normalise_float(snapshot.get("pnl"))
        pnl_sum += pnl
        pnl_sumsq += pnl * pnl
        if pnl > 0:
            win_count += 1
        elif pnl < 0:
            loss_count += 1
        commission_sum += _normalise_float(snapshot.get("commission"))

        executed_at = _parse_snapshot_datetime(snapshot.get("executed_at"))
        created_at = _parse_snapshot_datetime(snapshot.get("created_at"))
        anchor_time = executed_at or created_at
        if anchor_time:
            day = anchor_time.strftime("%Y-%m-%d")
            daily[day] = daily.get(day, 0.0) + pnl
            if last_trigger is None or anchor_time > last_trigger:
                last_trigger = anchor_time

        duration = snapshot.get("duration")
        if duration is None and executed_at and created_at:
            try:
                duration = max((executed_at - created_at).total_seconds(), 0.0)
            except Exception:
                duration = None
        if duration is not None:
            try:
                duration_value = float(duration)
            except (TypeError, ValueError):
                duration_value = None
            if duration_value is not None and duration_value >= 0:
                duration_sum += duration_value
                duration_count += 1

        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    avg_duration = duration_sum / duration_count if duration_count else 0.0
    last_trigger_iso = to_local_iso(last_trigger) if last_trigger else ""
    now_iso = to_local_iso(now_local()) or now_local().isoformat()

    stats = {
        "filled_count": str(len(sorted_orders)),
        "pnl_sum": f"{pnl_sum:.6f}",
        "pnl_sumsq": f"{pnl_sumsq:.6f}",
        "commission_sum": f"{commission_sum:.6f}",
        "win_count": str(win_count),
        "loss_count": str(loss_count),
        "duration_sum": f"{duration_sum:.6f}",
        "duration_count": str(duration_count),
        "avg_duration": f"{avg_duration:.6f}",
        "cumulative_pnl": f"{cumulative:.6f}",
        "peak_pnl": f"{peak:.6f}",
        "max_drawdown": f"{max_drawdown:.6f}",
        "last_trigger_ts": last_trigger_iso or "",
        "updated_at": now_iso,
        "stale": "0",
    }

    return stats, daily, _serialise_recent(sorted_orders)


async def _write_strategy_cache(strategy: str, order_map: Mapping[str, Dict[str, Any]]) -> None:
    client = await ensure_redis()
    if client is None:
        return

    stats, daily_map, recent_entries = _compute_stats(order_map)
    orders_serialised = _serialise_order_map(order_map)

    pipe = client.pipeline(transaction=True)
    pipe.delete(
        _orders_key(strategy),
        _orders_zset_key(strategy),
        _stats_key(strategy),
        _recent_key(strategy),
        _daily_hash_key(strategy),
        _daily_zset_key(strategy),
    )

    if orders_serialised:
        pipe.hset(_orders_key(strategy), mapping=orders_serialised)
        pipe.expire(_orders_key(strategy), STRATEGY_CACHE_TTL_SECONDS)

    if order_map:
        zset_mapping: Dict[str, float] = {}
        for order_id, snapshot in order_map.items():
            score = _snapshot_timestamp(snapshot)
            if score is None:
                continue
            zset_mapping[order_id] = score
        if zset_mapping:
            pipe.zadd(_orders_zset_key(strategy), zset_mapping)
            pipe.expire(_orders_zset_key(strategy), STRATEGY_CACHE_TTL_SECONDS)

    if stats:
        pipe.hset(_stats_key(strategy), mapping=stats)
        pipe.expire(_stats_key(strategy), STRATEGY_CACHE_TTL_SECONDS)

    if recent_entries:
        pipe.rpush(_recent_key(strategy), *recent_entries)
        pipe.expire(_recent_key(strategy), STRATEGY_CACHE_TTL_SECONDS)

    if daily_map:
        pipe.hset(
            _daily_hash_key(strategy),
            mapping={day: f"{value:.6f}" for day, value in daily_map.items()},
        )
        pipe.expire(_daily_hash_key(strategy), STRATEGY_CACHE_TTL_SECONDS)
        zset_mapping = {}
        for day, _ in daily_map.items():
            dt = parse_local_datetime(f"{day}T00:00:00")
            if dt is None:
                try:
                    dt = datetime.fromisoformat(f"{day}T00:00:00")
                except ValueError:
                    continue
            dt = ensure_local_timezone(dt)
            zset_mapping[day] = float(dt.timestamp())
        if zset_mapping:
            pipe.zadd(_daily_zset_key(strategy), zset_mapping)
            pipe.expire(_daily_zset_key(strategy), STRATEGY_CACHE_TTL_SECONDS)

    await pipe.execute()


async def _fetch_strategy_orders(strategy: str) -> List[Dict[str, Any]]:
    client = _get_orders_client()
    if client is None:
        return []

    collected: List[Dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    pages = 0

    while pages < STRATEGY_CACHE_REFRESH_MAX_PAGES:
        pages += 1
        try:
            response = await client.list_orders(limit=STRATEGY_CACHE_REFRESH_PAGE_SIZE, offset=offset)
        except (ServiceUnavailableError, ServiceClientError) as exc:
            logger.debug(
                "Failed to fetch strategy orders from service", strategy=strategy, error=str(exc)
            )
            break

        if not isinstance(response, Mapping):
            break

        raw_orders = response.get("orders")
        if not isinstance(raw_orders, list) or not raw_orders:
            break

        matched = 0
        for entry in raw_orders:
            if not isinstance(entry, Mapping):
                continue
            payload = dict(entry)
            if not _order_matches_strategy(payload, strategy):
                continue
            identifier = str(
                payload.get("order_id")
                or payload.get("id")
                or payload.get("command_id")
                or len(collected)
            )
            if identifier in seen:
                continue
            seen.add(identifier)
            collected.append(payload)
            matched += 1

        offset += STRATEGY_CACHE_REFRESH_PAGE_SIZE
        total = response.get("total")
        if isinstance(total, (int, float)) and offset >= int(total):
            break
        if len(raw_orders) < STRATEGY_CACHE_REFRESH_PAGE_SIZE:
            break
        if matched == 0 and len(raw_orders) == 0:
            break

    return collected


def _order_matches_strategy(order: Mapping[str, Any], strategy: str) -> bool:
    target = str(strategy or "").strip().lower()
    if not target:
        return False

    def _candidates(source: Mapping[str, Any]) -> Iterable[str]:
        for key in ("strategy", "strategy_name"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                yield value.strip()
        for sub_key in ("metadata", "request", "result"):
            nested = source.get(sub_key)
            if isinstance(nested, Mapping):
                for key in ("strategy", "strategy_name", "strategy_id"):
                    value = nested.get(key)
                    if isinstance(value, str) and value.strip():
                        yield value.strip()

    for candidate in _candidates(order):
        if candidate.lower() == target:
            return True
    return False


async def _refresh_strategy_cache_from_source(
    strategy: str,
    *,
    base_map: Optional[Mapping[str, Dict[str, Any]]] = None,
    fetch_remote: bool = True,
) -> bool:
    order_map: Dict[str, Dict[str, Any]] = {}
    if base_map:
        order_map.update({str(key): dict(value) for key, value in base_map.items()})

    remote_orders: Iterable[Mapping[str, Any]] = []
    if fetch_remote:
        remote_orders = await _fetch_strategy_orders(strategy)

    for order in remote_orders:
        snapshot = order_to_snapshot(order, strategy_name=strategy)
        if snapshot is None:
            continue
        order_map[snapshot["order_id"]] = snapshot

    if not order_map:
        return False

    await _write_strategy_cache(strategy, order_map)
    return True


async def refresh_strategy_cache(strategy: str, *, fetch_remote: bool = True) -> bool:
    """Rebuild the strategy cache by fetching orders from upstream sources."""

    lock = await _get_refresh_lock(strategy)
    async with lock:
        try:
            return await _refresh_strategy_cache_from_source(strategy, fetch_remote=fetch_remote)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning(
                "Failed to refresh strategy cache", strategy=strategy, error=str(exc)
            )
            return False


def schedule_strategy_cache_refresh(
    strategy: str,
    *,
    base_map: Optional[Mapping[str, Dict[str, Any]]] = None,
    fetch_remote: bool = True,
) -> None:
    """Schedule an asynchronous refresh of the strategy cache."""

    async def _runner() -> None:
        lock = await _get_refresh_lock(strategy)
        async with lock:
            try:
                await _refresh_strategy_cache_from_source(
                    strategy, base_map=base_map, fetch_remote=fetch_remote
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning(
                    "Background refresh failed", strategy=strategy, error=str(exc)
                )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_runner())
        return
    loop.create_task(_runner())


async def record_filled_order(order: Any, *, strategy_name: Optional[str] = None) -> None:
    """Record or update a filled order snapshot in Redis."""

    snapshot = order_to_snapshot(order, strategy_name=strategy_name)
    if not snapshot:
        return

    strategy = snapshot.get("strategy")
    if not strategy:
        return

    client = await ensure_redis()
    if client is None:
        return

    orders_hash = _orders_key(strategy)
    existing = await client.hgetall(orders_hash)
    order_map = _prepare_order_map(existing)
    order_map[snapshot["order_id"]] = snapshot

    try:
        await client.hset(_stats_key(strategy), mapping={"stale": "1"})
    except Exception:
        logger.debug("Failed to mark strategy cache as stale", strategy=strategy, exc_info=True)

    try:
        schedule_strategy_cache_refresh(strategy, base_map=order_map, fetch_remote=False)
    except Exception:
        # Fallback to synchronous refresh if scheduling fails
        try:
            await _refresh_strategy_cache_from_source(
                strategy, base_map=order_map, fetch_remote=False
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning(
                "Failed to update strategy cache", strategy=strategy, error=str(exc)
            )


async def rebuild_strategy_cache(strategy: str, orders: Iterable[Any]) -> None:
    """Rebuild a strategy's cache from iterable orders."""

    order_map: Dict[str, Dict[str, Any]] = {}
    for order in orders:
        snapshot = order_to_snapshot(order, strategy_name=strategy)
        if snapshot is None:
            continue
        order_map[snapshot["order_id"]] = snapshot

    try:
        await _write_strategy_cache(strategy, order_map)
    except Exception as exc:
        logger.warning("Failed to rebuild strategy cache", strategy=strategy, error=str(exc))


async def clear_strategy_cache(strategy: str) -> None:
    client = await ensure_redis()
    if client is None:
        return
    await client.delete(
        _orders_key(strategy),
        _orders_zset_key(strategy),
        _stats_key(strategy),
        _recent_key(strategy),
        _daily_hash_key(strategy),
        _daily_zset_key(strategy),
    )


async def mark_strategy_cache_stale(strategy: str) -> None:
    client = await ensure_redis()
    if client is None:
        return
    try:
        await client.hset(_stats_key(strategy), mapping={"stale": "1"})
    except Exception:
        pass


async def get_strategy_stats(strategy: str) -> Dict[str, Any]:
    client = await ensure_redis()
    if client is None:
        return {}
    raw = await client.hgetall(_stats_key(strategy))
    if not raw or raw.get("stale") == "1":
        refreshed = await refresh_strategy_cache(strategy)
        if refreshed:
            raw = await client.hgetall(_stats_key(strategy))
        if not raw:
            return {}
    result: Dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"filled_count", "win_count", "loss_count", "duration_count"}:
            result[key] = _normalise_int(value)
        elif key in {
            "pnl_sum",
            "pnl_sumsq",
            "commission_sum",
            "duration_sum",
            "avg_duration",
            "cumulative_pnl",
            "peak_pnl",
            "max_drawdown",
        }:
            result[key] = _normalise_float(value)
        elif key == "stale":
            result[key] = value == "1"
        else:
            result[key] = value
    return result


async def get_strategy_orders(strategy: str) -> List[Dict[str, Any]]:
    client = await ensure_redis()
    if client is None:
        return []
    raw = await client.hgetall(_orders_key(strategy))
    if not raw:
        refreshed = await refresh_strategy_cache(strategy)
        if not refreshed:
            return []
        raw = await client.hgetall(_orders_key(strategy))
    if not raw:
        return []
    order_map = _prepare_order_map(raw)
    orders = list(order_map.values())
    orders.sort(
        key=lambda item: (
            _parse_snapshot_datetime(item.get("executed_at"))
            or _parse_snapshot_datetime(item.get("created_at"))
            or datetime.min.replace(tzinfo=now_local().tzinfo)
        ),
        reverse=True,
    )
    return orders


async def get_strategy_orders_in_range(
    strategy: str,
    *,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    if start_date is None and end_date is None:
        return await get_strategy_orders(strategy)

    client = await ensure_redis()
    if client is None:
        orders = await get_strategy_orders(strategy)
        start_bound = ensure_local_timezone(start_date) if start_date else None
        end_bound = ensure_local_timezone(end_date) if end_date else None
        return _filter_orders_by_range(orders, start_bound, end_bound)

    start_score: float | str
    end_score: float | str

    start_ts = _datetime_to_timestamp(start_date)
    end_ts = _datetime_to_timestamp(end_date)
    start_score = start_ts if start_ts is not None else "-inf"
    end_score = end_ts if end_ts is not None else "+inf"

    try:
        order_ids = await client.zrangebyscore(
            _orders_zset_key(strategy), start_score, end_score
        )
    except Exception:
        order_ids = []

    if not order_ids:
        orders = await get_strategy_orders(strategy)
        start_bound = ensure_local_timezone(start_date) if start_date else None
        end_bound = ensure_local_timezone(end_date) if end_date else None
        return _filter_orders_by_range(orders, start_bound, end_bound)

    raw_entries = await client.hmget(_orders_key(strategy), order_ids)
    collected: List[Dict[str, Any]] = []
    for order_id, payload in zip(order_ids, raw_entries):
        if not payload:
            continue
        try:
            decoded = json.loads(payload)
        except Exception:
            continue
        if not isinstance(decoded, dict):
            continue
        decoded.setdefault("order_id", order_id)
        collected.append(decoded)

    start_bound = ensure_local_timezone(start_date) if start_date else None
    end_bound = ensure_local_timezone(end_date) if end_date else None
    return _filter_orders_by_range(collected, start_bound, end_bound)


async def get_recent_fills(strategy: str, limit: int = 20) -> List[Dict[str, Any]]:
    client = await ensure_redis()
    if client is None:
        return []
    entries = await client.lrange(_recent_key(strategy), 0, max(limit - 1, 0))
    if not entries:
        refreshed = await refresh_strategy_cache(strategy)
        if refreshed:
            entries = await client.lrange(_recent_key(strategy), 0, max(limit - 1, 0))
    result: List[Dict[str, Any]] = []
    for entry in entries:
        try:
            payload = json.loads(entry)
        except Exception:
            continue
        if isinstance(payload, dict):
            result.append(payload)
    return result


async def get_daily_pnl(strategy: str) -> List[Dict[str, Any]]:
    client = await ensure_redis()
    if client is None:
        return []
    raw = await client.hgetall(_daily_hash_key(strategy))
    if not raw:
        refreshed = await refresh_strategy_cache(strategy)
        if refreshed:
            raw = await client.hgetall(_daily_hash_key(strategy))
        if not raw:
            return []
    items = []
    for day, pnl_value in raw.items():
        items.append((day, _normalise_float(pnl_value)))
    items.sort(key=lambda item: item[0])
    return [{"date": day, "pnl": pnl} for day, pnl in items]


def _empty_performance() -> Dict[str, Any]:
    return {
        "total_executions": 0,
        "total_pnl": 0.0,
        "avg_pnl": 0.0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
        "sharpe_ratio": 0.0,
        "avg_trade_duration": 0.0,
        "total_commission": 0.0,
    }


async def compute_performance_metrics(
    strategy: str,
    *,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    orders: List[Dict[str, Any]] = []
    if start_date or end_date:
        orders = await get_strategy_orders_in_range(
            strategy, start_date=start_date, end_date=end_date
        )
    if not orders:
        orders = await get_strategy_orders(strategy)
    if not orders:
        return _empty_performance()

    start_bound = ensure_local_timezone(start_date) if start_date else None
    end_bound = ensure_local_timezone(end_date) if end_date else None

    filtered: List[Dict[str, Any]] = []
    for order in orders:
        executed_at = _parse_snapshot_datetime(order.get("executed_at"))
        created_at = _parse_snapshot_datetime(order.get("created_at"))
        anchor = executed_at or created_at
        if anchor is None:
            continue
        if start_bound and anchor < start_bound:
            continue
        if end_bound and anchor > end_bound:
            continue
        filtered.append(order)

    if not filtered:
        return _empty_performance()

    pnl_values = [
        _normalise_float(order.get("pnl"))
        for order in filtered
        if order.get("pnl") is not None
    ]
    commission_values = [
        _normalise_float(order.get("commission"))
        for order in filtered
        if order.get("commission") is not None
    ]

    total_executions = len(filtered)
    total_pnl = sum(pnl_values)
    avg_pnl = total_pnl / len(pnl_values) if pnl_values else 0.0
    winning_trades = len([p for p in pnl_values if p > 0])
    losing_trades = len([p for p in pnl_values if p < 0])
    win_rate = (winning_trades / len(pnl_values) * 100) if pnl_values else 0.0

    cumulative = []
    run_total = 0.0
    for value in pnl_values:
        run_total += value
        cumulative.append(run_total)

    max_drawdown = 0.0
    peak = None
    for value in cumulative:
        if peak is None or value > peak:
            peak = value
        drawdown = (peak or 0.0) - value
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    sharpe_ratio = 0.0
    if len(pnl_values) > 1:
        try:
            import statistics

            std_dev = statistics.stdev(pnl_values)
            if std_dev:
                sharpe_ratio = (avg_pnl / std_dev) * (len(pnl_values) ** 0.5)
        except Exception:
            sharpe_ratio = 0.0

    durations: List[float] = []
    for order in filtered:
        duration = order.get("duration")
        if duration is None:
            executed = _parse_snapshot_datetime(order.get("executed_at"))
            created = _parse_snapshot_datetime(order.get("created_at"))
            if executed and created:
                try:
                    duration = max((executed - created).total_seconds(), 0.0)
                except Exception:
                    duration = None
        if duration is None:
            continue
        try:
            duration_value = float(duration)
        except (TypeError, ValueError):
            continue
        if duration_value >= 0:
            durations.append(duration_value)

    avg_duration = sum(durations) / len(durations) if durations else 0.0

    return {
        "total_executions": total_executions,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": win_rate,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe_ratio,
        "avg_trade_duration": avg_duration,
        "total_commission": sum(commission_values),
    }
