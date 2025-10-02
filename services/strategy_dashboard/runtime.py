"""Runtime utilities for the strategy dashboard service."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import HTTPException
from loguru import logger

from src.database import DatabaseManager, Order, get_db_manager
from src.models import ChartDataRequest
from src.utils import ensure_local_timezone, now_local, parse_local_datetime
from src.utils.strategy_cache import (
    compute_performance_metrics as cache_compute_performance,
    get_strategy_stats as cache_get_strategy_stats,
    rebuild_strategy_cache as cache_rebuild_strategy_cache,
)

try:  # Optional import: when running tests some modules may not be available
    from src.routes import market_data as market_routes
except Exception:  # pragma: no cover - defensive guard for optional dependency
    market_routes = None  # type: ignore


class StrategyDashboardRuntime:
    """Encapsulates stateful helpers used by the dashboard API."""

    PERFORMANCE_CACHE_TTL = 30.0
    CHART_CACHE_TTL = 60.0

    def __init__(self, *, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db_manager = db_manager or get_db_manager()
        self._performance_cache: Dict[str, Dict[str, Any]] = {}
        self._performance_cache_lock = asyncio.Lock()
        self._strategy_cache_rebuild_locks: Dict[str, asyncio.Lock] = {}
        self._chart_cache: Dict[str, Dict[str, Any]] = {}
        self._chart_cache_lock = asyncio.Lock()

    async def startup(self) -> None:
        """Placeholder for future asynchronous start-up logic."""
        # Nothing to initialise eagerly for now. This hook keeps the interface
        # symmetric with other services and allows future extensions without
        # changing the public API.
        return None

    async def shutdown(self) -> None:
        """Placeholder for graceful shutdown tasks."""
        return None

    # ------------------------------------------------------------------
    # Strategy performance helpers
    # ------------------------------------------------------------------
    async def get_strategy_performance(
        self,
        strategy: str,
        *,
        range_token: str,
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> Dict[str, Any]:
        cache_key = self._make_performance_cache_key(strategy, range_token, start, end)
        now_marker = monotonic()

        async with self._performance_cache_lock:
            entry = self._performance_cache.get(cache_key)
            if entry:
                age = now_marker - entry.get("timestamp", 0.0)
                if age < self.PERFORMANCE_CACHE_TTL:
                    payload = dict(entry.get("data", {}))
                    payload["from_cache"] = True
                    payload["cache_age"] = age
                    return payload

        metrics = await self._compute_strategy_performance(strategy, start=start, end=end)

        payload = {
            "success": True,
            "metrics": metrics or {},
            "range": range_token,
            "from_cache": False,
            "cache_age": 0.0,
        }

        async with self._performance_cache_lock:
            self._performance_cache[cache_key] = {
                "timestamp": now_marker,
                "data": dict(payload),
            }

        return payload

    async def _compute_strategy_performance(
        self,
        strategy: str,
        *,
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> Dict[str, Any]:
        await self._ensure_strategy_cache(strategy)

        metrics: Dict[str, Any] = {}
        try:
            metrics = await cache_compute_performance(strategy, start_date=start, end_date=end)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning(
                "Cached strategy performance computation failed", strategy=strategy, error=str(exc)
            )
            metrics = {}

        if metrics:
            return metrics

        orders = await asyncio.to_thread(
            self.db_manager.load_strategy_orders,
            strategy,
            "strategy",
            None,
        )

        filtered = self._filter_orders_by_range(orders, start, end)
        return self._compute_metrics_from_orders(filtered)

    async def _ensure_strategy_cache(self, strategy: str) -> Optional[Dict[str, Any]]:
        stats = await cache_get_strategy_stats(strategy)
        if stats:
            return stats

        lock = self._strategy_cache_rebuild_locks.setdefault(strategy, asyncio.Lock())
        async with lock:
            stats = await cache_get_strategy_stats(strategy)
            if stats:
                return stats

            orders = await asyncio.to_thread(
                self.db_manager.load_strategy_orders,
                strategy,
                "strategy",
                None,
            )
            serialised = [asdict(order) if isinstance(order, Order) else order for order in orders]

            try:
                await cache_rebuild_strategy_cache(strategy, serialised)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning(
                    "Strategy cache rebuild failed", strategy=strategy, error=str(exc)
                )

            return await cache_get_strategy_stats(strategy)

    @staticmethod
    def _make_performance_cache_key(
        strategy: str,
        range_token: str,
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> str:
        start_token = start.isoformat() if isinstance(start, datetime) else ""
        end_token = end.isoformat() if isinstance(end, datetime) else ""
        return f"{strategy}::{range_token}::{start_token}::{end_token}"

    @staticmethod
    def _filter_orders_by_range(
        orders: Iterable[Order],
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> List[Order]:
        results: List[Order] = []
        for order in orders:
            executed_raw = getattr(order, "executed_at", None)
            created_raw = getattr(order, "timestamp", None)
            executed_at = parse_local_datetime(executed_raw) if executed_raw else None
            created_at = parse_local_datetime(created_raw) if created_raw else None
            anchor = ensure_local_timezone(executed_at or created_at)
            if anchor is None:
                continue
            if start and anchor < start:
                continue
            if end and anchor > end:
                continue
            results.append(order)
        return results

    @staticmethod
    def _compute_metrics_from_orders(orders: Iterable[Order]) -> Dict[str, Any]:
        order_list = list(orders)
        pnl_values: List[float] = []
        commissions: List[float] = []
        durations: List[float] = []

        for order in order_list:
            pnl_raw = getattr(order, "pnl", None)
            if pnl_raw is not None:
                try:
                    pnl_values.append(float(pnl_raw))
                except (TypeError, ValueError):
                    pass
            commission_raw = getattr(order, "commission", None)
            if commission_raw is not None:
                try:
                    commissions.append(float(commission_raw))
                except (TypeError, ValueError):
                    pass
        for order in order_list:
            status = getattr(order, "status", "")
            if isinstance(status, str) and status.lower() != "filled":
                continue
            created_raw = getattr(order, "timestamp", None)
            executed_raw = getattr(order, "executed_at", None)
            created_at = parse_local_datetime(created_raw) if created_raw else None
            executed_at = parse_local_datetime(executed_raw) if executed_raw else None
            if created_at and not executed_at:
                executed_at = created_at
            if not created_at or not executed_at:
                continue
            duration = (executed_at - created_at).total_seconds()
            if duration >= 0:
                durations.append(duration)

        total_pnl = sum(pnl_values)
        avg_pnl = total_pnl / len(pnl_values) if pnl_values else 0.0
        winning_trades = len([p for p in pnl_values if p > 0])
        losing_trades = len([p for p in pnl_values if p < 0])
        win_rate = (winning_trades / len(pnl_values) * 100.0) if pnl_values else 0.0

        cumulative: List[float] = []
        running = 0.0
        for pnl in pnl_values:
            running += pnl
            cumulative.append(running)
        max_drawdown = 0.0
        if cumulative:
            peak = cumulative[0]
            for value in cumulative:
                if value > peak:
                    peak = value
                drawdown = peak - value
                if drawdown > max_drawdown:
                    max_drawdown = drawdown

        sharpe_ratio = 0.0
        if len(pnl_values) > 1:
            import statistics

            try:
                std_dev = statistics.stdev(pnl_values)
                if std_dev != 0:
                    sharpe_ratio = (avg_pnl / std_dev) * (len(pnl_values) ** 0.5)
            except statistics.StatisticsError:
                sharpe_ratio = 0.0

        avg_duration = sum(durations) / len(durations) if durations else 0.0

        metrics = {
            "total_executions": len(order_list),
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": win_rate,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe_ratio,
            "avg_trade_duration": avg_duration,
            "total_commission": sum(commissions) if commissions else 0.0,
        }
        return metrics

    # ------------------------------------------------------------------
    # Chart data helpers
    # ------------------------------------------------------------------
    async def get_chart_data(self, request: ChartDataRequest) -> Dict[str, Any]:
        if not request.symbol:
            raise HTTPException(status_code=400, detail="Symbol is required")

        use_cache = request.use_cache if request.use_cache is not None else True
        force_refresh = bool(request.force_refresh)

        cache_key = self._chart_cache_key(request)
        now_marker = now_local()

        await self._clean_chart_cache(now_marker)

        async with self._chart_cache_lock:
            cached = self._chart_cache.get(cache_key)
            if use_cache and not force_refresh and cached:
                if cached.get("expires_at", now_marker) > now_marker:
                    payload = dict(cached.get("payload", {}))
                    payload.setdefault("success", True)
                    payload.setdefault("from_cache", True)
                    return payload

        interval = self.db_manager._normalize_interval(request.bar_size)
        duration_minutes = self._parse_duration_to_minutes(request.duration)
        start_dt = now_marker - timedelta(minutes=duration_minutes) if duration_minutes > 0 else None
        end_dt = now_marker

        if use_cache and not force_refresh:
            chart = await self._load_chart_from_db(
                request.symbol,
                interval=interval,
                start_dt=start_dt,
                end_dt=end_dt,
            )
            if chart:
                payload = {
                    "success": True,
                    "data": chart,
                    "symbol": request.symbol,
                    "duration": request.duration,
                    "bar_size": request.bar_size,
                    "from_cache": True,
                }
                async with self._chart_cache_lock:
                    self._chart_cache[cache_key] = {
                        "expires_at": now_marker + timedelta(seconds=self.CHART_CACHE_TTL),
                        "payload": dict(payload),
                    }
                return payload

        fresh_chart = await self._fetch_chart_data(request, interval=interval, duration_minutes=duration_minutes)

        if fresh_chart:
            payload = {
                "success": True,
                "data": fresh_chart,
                "symbol": request.symbol,
                "duration": request.duration,
                "bar_size": request.bar_size,
                "from_cache": False,
            }
            async with self._chart_cache_lock:
                self._chart_cache[cache_key] = {
                    "expires_at": now_marker + timedelta(seconds=self.CHART_CACHE_TTL),
                    "payload": dict(payload),
                }
            return payload

        db_chart = await self._load_chart_from_db(
            request.symbol,
            interval=interval,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        if db_chart:
            return {
                "success": True,
                "data": db_chart,
                "symbol": request.symbol,
                "duration": request.duration,
                "bar_size": request.bar_size,
                "from_cache": True,
                "cache_warning": "Fresh chart unavailable; served from historical cache",
            }

        raise HTTPException(status_code=404, detail="No chart data available")

    async def _fetch_chart_data(
        self,
        request: ChartDataRequest,
        *,
        interval: str,
        duration_minutes: int,
    ) -> List[Dict[str, Any]]:
        if market_routes is None:
            return []

        interval_key = market_routes._map_bar_size_to_interval(request.bar_size)
        if interval_key:
            manager = getattr(market_routes, "_get_data_layer_manager", None)
            get_mode = getattr(market_routes, "get_data_layer_mode", None)
            is_legacy = getattr(market_routes, "is_legacy_mode", None)
            data_request_cls = getattr(market_routes, "DataSubscriptionRequest", None)
            data_manager = None
            try:
                mode = get_mode() if callable(get_mode) else None
                data_manager = None if (callable(is_legacy) and is_legacy(mode)) else (
                    manager() if callable(manager) else None
                )
            except Exception:  # pragma: no cover - defensive guard
                data_manager = None

            if data_manager and interval_key and data_request_cls:
                try:
                    candles = await data_manager.history(  # type: ignore[attr-defined]
                        data_request_cls(
                            channel="market.tick",
                            symbol=(request.symbol or "").upper(),
                            interval=interval_key,
                            options={"exchange": request.exchange},
                        )
                    )
                except Exception as exc:  # pragma: no cover - defensive logging
                    logger.debug(
                        "Unified history unavailable", symbol=request.symbol, error=str(exc)
                    )
                    candles = []
                else:
                    candles = market_routes._filter_candles_by_duration(candles, duration_minutes)
                    records = market_routes._candles_to_records(candles)
                    chart = market_routes._records_to_chart(records)
                    if chart:
                        await asyncio.to_thread(
                            self.db_manager.save_historical_records,
                            request.symbol,
                            records,
                            bar_size=request.bar_size,
                        )
                        return chart

        records = await market_routes._fetch_market_history(
            request.symbol,
            interval=interval_key,
            duration_minutes=duration_minutes,
            exchange=request.exchange,
        )

        if not records:
            return []

        chart_data = market_routes._records_to_chart(records)
        if chart_data:
            await asyncio.to_thread(
                self.db_manager.save_historical_records,
                request.symbol,
                records,
                bar_size=request.bar_size,
            )
        return chart_data

    async def _load_chart_from_db(
        self,
        symbol: str,
        *,
        interval: str,
        start_dt: Optional[datetime],
        end_dt: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        if not interval:
            return []
        start_iso = start_dt.isoformat(timespec="seconds") if start_dt else None
        end_iso = end_dt.isoformat(timespec="seconds") if end_dt else None

        df = await asyncio.to_thread(
            self.db_manager.query_historical_cache,
            symbol,
            interval,
            start_iso,
            end_iso,
        )
        if getattr(df, "empty", True):
            return []

        records = market_routes._dataframe_to_records(df) if market_routes else []
        chart = market_routes._records_to_chart(records) if market_routes else []
        return chart

    async def _clean_chart_cache(self, now_marker: datetime) -> None:
        async with self._chart_cache_lock:
            expired = [
                key
                for key, value in self._chart_cache.items()
                if value.get("expires_at", now_marker) <= now_marker
            ]
            for key in expired:
                self._chart_cache.pop(key, None)

    @staticmethod
    def _chart_cache_key(request: ChartDataRequest) -> str:
        return "::".join(
            [
                request.symbol,
                request.duration,
                request.bar_size,
                request.exchange,
            ]
        )

    @staticmethod
    def _parse_duration_to_minutes(duration: str) -> int:
        if market_routes is not None:
            return market_routes._parse_duration_to_minutes(duration)
        if not duration:
            return 0
        s = str(duration).strip().lower()
        num = "";
        unit = ""
        for ch in s:
            if ch.isdigit() or ch == ".":
                num += ch
            else:
                unit += ch
        try:
            value = float(num) if num else 0
        except ValueError:
            return 0
        unit = unit or "min"
        if unit.startswith("d"):
            return int(value * 1440)
        if unit.startswith("h"):
            return int(value * 60)
        if unit.startswith("w"):
            return int(value * 10080)
        return int(value)


def compute_range(
    range_token: str,
    start_text: Optional[str],
    end_text: Optional[str],
) -> Tuple[str, Optional[datetime], Optional[datetime]]:
    """Translate UI range tokens into datetime boundaries."""

    now = now_local()
    token = (range_token or "").lower()

    start: Optional[datetime]
    end: Optional[datetime]
    if token == "1d":
        start = now - timedelta(days=1)
        end = None
    elif token == "1w":
        start = now - timedelta(weeks=1)
        end = None
    elif token == "1m":
        start = now - timedelta(days=30)
        end = None
    elif token == "wtd":
        start = now - timedelta(days=now.weekday())
        end = None
    elif token == "mtd":
        start = now.replace(day=1)
        end = None
    elif token == "ytd":
        start = now.replace(month=1, day=1)
        end = None
    else:
        token = "custom"
        start = parse_local_datetime(start_text) if start_text else None
        end = parse_local_datetime(end_text) if end_text else None

    return token, start, end


__all__ = [
    "StrategyDashboardRuntime",
    "compute_range",
]
