"""Strategy-level risk controls.

Implements per-strategy risk checks described in the design document. The
manager operates alongside the existing order pipeline and performs three core
checks before orders are queued:

1. Position count limit (`max_positions`).
2. Prevent adding to positions while any tracked position is in floating loss
   (`forbid_add_on_loss`).
3. Timed stop for persistent floating losses (`loss_time_threshold`).

The module keeps lightweight in-memory state to avoid hitting the database for
each signal. Settings are persisted in the shared ``config`` table using the
``strategy_risk_settings`` key.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from .database import DatabaseManager
from .notification_manager import NotificationManager

strategy_risk_logger = logger.bind(module="strategy_risk", request_id="-")


DEFAULT_STRATEGY_SETTINGS = {
    "max_positions": None,
    "forbid_add_on_loss": True,
    "loss_time_threshold": None,
    "loss_pnl_threshold": 0.0,
    "notify_on_trigger": True,
}


@dataclass
class StrategyPosition:
    symbol: str
    quantity: float
    unrealized_pnl: float


class StrategyRiskManager:
    """Run-time manager for per-strategy risk controls."""

    def __init__(
        self,
        *,
        db_manager: DatabaseManager,
        notification_manager: Optional[NotificationManager] = None,
        monitor_interval_seconds: float = 10.0,
    ) -> None:
        self.db_manager = db_manager
        self.notification_manager = notification_manager
        self.monitor_interval_seconds = max(1.0, float(monitor_interval_seconds))

        self.strategy_manager = None
        self.queue_manager = None

        self._settings: Dict[str, Dict[str, Any]] = {}
        self._loss_start_times: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._positions_cache: Dict[str, Any] = {"timestamp": 0.0, "data": []}
        self._positions_cache_ttl = 2.0
        self._pending_closes: set[Tuple[str, str]] = set()
        self._runtime_strategy_symbols: Dict[str, Set[str]] = defaultdict(set)
        self._monitor_scheduler = AsyncIOScheduler()
        self._monitor_job_id = "strategy_risk_monitor"

        self._load_settings_from_db()

    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @staticmethod
    def _coerce_float(value: Any, *, allow_none: bool, default: Optional[float]) -> Optional[float]:
        if value is None:
            return None if allow_none else default
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None if allow_none else default
        try:
            return float(value)
        except (TypeError, ValueError):
            return None if allow_none else default

    @staticmethod
    def _coerce_bool(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"", "none"}:
                return None
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
        return bool(value)

    def _coerce_setting_value(self, key: str, value: Any) -> Any:
        if value is None:
            return None
        if key == "max_positions":
            return self._coerce_int(value)
        if key == "loss_time_threshold":
            return self._coerce_float(value, allow_none=True, default=None)
        if key == "loss_pnl_threshold":
            return self._coerce_float(
                value,
                allow_none=False,
                default=DEFAULT_STRATEGY_SETTINGS["loss_pnl_threshold"],
            )
        if key in {"forbid_add_on_loss", "notify_on_trigger"}:
            return self._coerce_bool(value)
        return value

    def _normalize_settings_map(self, config: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(config)
        normalized["max_positions"] = self._coerce_int(config.get("max_positions"))
        normalized["loss_time_threshold"] = self._coerce_float(
            config.get("loss_time_threshold"), allow_none=True, default=None
        )
        normalized["loss_pnl_threshold"] = self._coerce_float(
            config.get("loss_pnl_threshold"),
            allow_none=False,
            default=DEFAULT_STRATEGY_SETTINGS["loss_pnl_threshold"],
        )
        forbid = self._coerce_bool(config.get("forbid_add_on_loss"))
        notify = self._coerce_bool(config.get("notify_on_trigger"))
        normalized["forbid_add_on_loss"] = (
            forbid
            if forbid is not None
            else DEFAULT_STRATEGY_SETTINGS["forbid_add_on_loss"]
        )
        normalized["notify_on_trigger"] = (
            notify
            if notify is not None
            else DEFAULT_STRATEGY_SETTINGS["notify_on_trigger"]
        )
        return normalized

    # ------------------------------------------------------------------
    def set_dependencies(self, *, strategy_manager=None, queue_manager=None, notification_manager=None) -> None:
        if strategy_manager is not None:
            self.strategy_manager = strategy_manager
        if queue_manager is not None:
            self.queue_manager = queue_manager
        if notification_manager is not None:
            self.notification_manager = notification_manager

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start periodic monitoring for floating-loss timeouts."""

        if not self._monitor_scheduler.running:
            self._monitor_scheduler.start()
        try:
            self._monitor_scheduler.add_job(
                self._monitor_positions,
                "interval",
                seconds=self.monitor_interval_seconds,
                id=self._monitor_job_id,
                coalesce=True,
                max_instances=1,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_risk_logger.warning(f"Failed to schedule monitor job: {exc}")

    # ------------------------------------------------------------------
    def stop(self) -> None:
        if self._monitor_scheduler.running:
            self._monitor_scheduler.remove_all_jobs()
            self._monitor_scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    def get_settings(self, strategy_name: str) -> Dict[str, Any]:
        base = DEFAULT_STRATEGY_SETTINGS.copy()
        custom = self._settings.get(strategy_name) or {}
        base.update({k: v for k, v in custom.items() if v is not None})
        return self._normalize_settings_map(base)

    # ------------------------------------------------------------------
    def update_settings(self, strategy_name: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        if not strategy_name:
            raise ValueError("strategy_name is required")

        current = dict(self._settings.get(strategy_name) or {})
        changed = False
        for key, value in updates.items():
            if key not in DEFAULT_STRATEGY_SETTINGS:
                continue
            if value == "":
                value = None
            coerced = self._coerce_setting_value(key, value)
            if current.get(key) != coerced:
                current[key] = coerced
                changed = True

        if changed:
            self._settings[strategy_name] = current
            self._persist_settings()
        return self.get_settings(strategy_name)

    # ------------------------------------------------------------------
    def _load_settings_from_db(self) -> None:
        try:
            stored = self.db_manager.get_config("strategy_risk_settings", {})
            if isinstance(stored, dict):
                for name, config in stored.items():
                    if isinstance(config, dict):
                        normalized: Dict[str, Any] = {}
                        for key, value in config.items():
                            if key not in DEFAULT_STRATEGY_SETTINGS:
                                continue
                            coerced = self._coerce_setting_value(key, value)
                            if coerced is not None:
                                normalized[key] = coerced
                        if normalized:
                            self._settings[name] = normalized
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_risk_logger.warning(f"Failed to load strategy risk settings: {exc}")

    # ------------------------------------------------------------------
    def _persist_settings(self) -> None:
        try:
            self.db_manager.save_config(
                "strategy_risk_settings",
                self._settings,
                "Per-strategy risk control settings",
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_risk_logger.error(f"Failed to persist strategy risk settings: {exc}")

    # ------------------------------------------------------------------
    async def evaluate_order(
        self,
        *,
        strategy_name: str,
        symbol: str,
        action: str,
        quantity: float,
        current_position: float,
        new_position: float,
    ) -> Tuple[bool, Optional[str]]:
        """Evaluate whether an upcoming order violates risk settings."""

        config = self.get_settings(strategy_name)
        if not any(config.get(key) is not None for key in DEFAULT_STRATEGY_SETTINGS):
            return True, None

        positions = await self._get_strategy_positions(strategy_name)
        symbol_upper = symbol.upper()
        if symbol_upper:
            self._runtime_strategy_symbols[strategy_name].add(symbol_upper)
        if not any(pos.symbol == symbol_upper for pos in positions):
            positions.append(
                StrategyPosition(
                    symbol=symbol_upper,
                    quantity=current_position,
                    unrealized_pnl=None,
                )
            )
        open_positions = [pos for pos in positions if not math.isclose(pos.quantity, 0.0, abs_tol=1e-9)]
        sign_current = math.copysign(1.0, current_position) if current_position else 0.0
        sign_new = math.copysign(1.0, new_position) if new_position else 0.0
        expanding = False
        if abs(new_position) > abs(current_position):
            if sign_current == 0:
                expanding = abs(new_position) > 0
            elif sign_current == sign_new:
                expanding = True

        # 1. Position count limit (only when opening a fresh position)
        max_positions = config.get("max_positions")
        if max_positions:
            currently_open = len([pos for pos in open_positions if pos.symbol != symbol])
            opening_new = math.isclose(current_position, 0.0, abs_tol=1e-9) and not math.isclose(new_position, 0.0, abs_tol=1e-9)
            if opening_new and currently_open >= max_positions:
                reason = f"持仓数量已达上限 {currently_open}/{max_positions}"
                await self._handle_risk_block(strategy_name, symbol, reason, config)
                return False, reason

        # 2. Prevent adding while any tracked position is in loss
        if config.get("forbid_add_on_loss") and expanding:
            loss_threshold = config.get("loss_pnl_threshold")
            if loss_threshold is None:
                loss_threshold = 0.0
            has_loss = any(pos.unrealized_pnl is not None and pos.unrealized_pnl < loss_threshold for pos in open_positions)
            if has_loss:
                reason = "存在浮亏仓位，禁止加仓"
                await self._handle_risk_block(strategy_name, symbol, reason, config)
                return False, reason

        return True, None

    # ------------------------------------------------------------------
    async def record_execution(
        self,
        strategy_name: str,
        symbol: str,
        new_position: Optional[float],
    ) -> None:
        """Update cached position state after an order has been submitted."""

        if not strategy_name or not symbol:
            return
        try:
            await self._refresh_positions_cache(force=True)
        except Exception:
            return
        if math.isclose(new_position or 0.0, 0.0, abs_tol=1e-9):
            symbol_upper = symbol.upper()
            self._loss_start_times.get(strategy_name, {}).pop(symbol_upper, None)
            self._pending_closes.discard((strategy_name, symbol_upper))
            symbols = self._runtime_strategy_symbols.get(strategy_name)
            if symbols is not None:
                symbols.discard(symbol_upper)

    # ------------------------------------------------------------------
    async def _monitor_positions(self) -> None:
        try:
            await self._check_loss_timeouts()
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_risk_logger.error(f"Strategy risk monitor error: {exc}")

    # ------------------------------------------------------------------
    async def _check_loss_timeouts(self) -> None:
        now = time.time()
        positions_by_strategy = await self._get_all_strategy_positions()
        for strategy_name, positions in positions_by_strategy.items():
            config = self.get_settings(strategy_name)
            threshold = config.get("loss_time_threshold")
            if not threshold:
                # 清理状态，避免旧数据残留
                self._loss_start_times.pop(strategy_name, None)
                continue

            loss_threshold = config.get("loss_pnl_threshold")
            if loss_threshold is None:
                loss_threshold = 0.0
            start_times = self._loss_start_times[strategy_name]
            for pos in positions:
                key = pos.symbol.upper()
                if math.isclose(pos.quantity, 0.0, abs_tol=1e-9):
                    start_times.pop(key, None)
                    continue
                pnl = pos.unrealized_pnl
                if pnl is None or pnl >= loss_threshold:
                    start_times.pop(key, None)
                    continue
                if key not in start_times:
                    start_times[key] = now
                    continue
                elapsed = now - start_times[key]
                if elapsed >= threshold:
                    await self._trigger_loss_close(strategy_name, pos, config, elapsed)
                    start_times.pop(key, None)

            # 移除不存在的符号
            to_remove = [sym for sym in start_times if sym not in {p.symbol.upper() for p in positions}]
            for sym in to_remove:
                start_times.pop(sym, None)

    # ------------------------------------------------------------------
    async def _trigger_loss_close(
        self,
        strategy_name: str,
        position: StrategyPosition,
        config: Dict[str, Any],
        elapsed_seconds: float,
    ) -> None:
        key = (strategy_name, position.symbol.upper())
        if key in self._pending_closes:
            return
        if not self.queue_manager:
            strategy_risk_logger.warning(
                "Queue manager unavailable; cannot auto-close position",
                strategy=strategy_name,
                symbol=position.symbol,
            )
            return

        quantity = abs(position.quantity)
        if quantity <= 0:
            return
        action = "SELL" if position.quantity > 0 else "BUY"
        payload = {
            "strategy": strategy_name,
            "symbol": position.symbol,
            "action": action,
            "qty": quantity,
            "type": "MARKET",
        }

        self.queue_manager.add_task("strategy_order", payload)
        self._pending_closes.add(key)

        reason = (
            f"浮亏持续 {round(elapsed_seconds)} 秒触发强制平仓, 当前浮亏: {position.unrealized_pnl:.2f}"
            if position.unrealized_pnl is not None
            else f"浮亏持续 {round(elapsed_seconds)} 秒触发强制平仓"
        )
        strategy_risk_logger.bind(strategy=strategy_name, symbol=position.symbol).warning(
            "Strategy risk triggered timed stop", reason=reason
        )

        if config.get("notify_on_trigger") and self.notification_manager:
            message = (
                f"策略 {strategy_name} 对 {position.symbol} 触发时间止损，"
                f"浮亏已持续 {round(elapsed_seconds)} 秒"
            )
            try:
                await self.notification_manager.send_notification(
                    message=message,
                    event_type="strategy_risk",
                    strategy=strategy_name,
                    symbol=position.symbol,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_risk_logger.warning(f"Failed to send strategy risk notification: {exc}")

    # ------------------------------------------------------------------
    async def _handle_risk_block(
        self,
        strategy_name: str,
        symbol: str,
        reason: str,
        config: Dict[str, Any],
    ) -> None:
        strategy_risk_logger.bind(strategy=strategy_name, symbol=symbol).warning(
            "Strategy order blocked by risk control", reason=reason
        )
        if config.get("notify_on_trigger") and self.notification_manager:
            try:
                await self.notification_manager.send_notification(
                    message=f"策略 {strategy_name} 的指令被风控拒绝：{reason}",
                    event_type="strategy_risk",
                    strategy=strategy_name,
                    symbol=symbol,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                strategy_risk_logger.warning(f"Failed to send risk block notification: {exc}")

    # ------------------------------------------------------------------
    async def _get_strategy_positions(
        self,
        strategy_name: str,
    ) -> List[StrategyPosition]:
        positions_by_strategy = await self._get_all_strategy_positions()
        return positions_by_strategy.get(strategy_name, [])

    # ------------------------------------------------------------------
    async def _get_all_strategy_positions(self) -> Dict[str, List[StrategyPosition]]:
        await self._refresh_positions_cache(force=False)
        raw_positions: Iterable[Dict[str, Any]] = self._positions_cache.get("data", [])
        mapping: Dict[str, List[StrategyPosition]] = defaultdict(list)

        symbol_to_strategy: Dict[str, List[str]] = defaultdict(list)
        if self.strategy_manager:
            for name in getattr(self.strategy_manager, "strategies", {}).keys():
                symbols = self._resolve_strategy_symbols(name)
                for sym in symbols:
                    symbol_to_strategy[sym].append(name)
        for name, symbols in self._runtime_strategy_symbols.items():
            for sym in symbols:
                if name not in symbol_to_strategy[sym]:
                    symbol_to_strategy[sym].append(name)

        for item in raw_positions:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            try:
                quantity = float(item.get("position", 0.0) or 0.0)
            except (TypeError, ValueError):
                quantity = 0.0
            pnl_keys = [
                "unrealizedPnl",
                "unrealized_pnl",
                "unrealizedPNL",
                "unrealizedPnL",
            ]
            pnl_value = None
            for key in pnl_keys:
                if key in item and item[key] is not None:
                    try:
                        pnl_value = float(item[key])
                        break
                    except (TypeError, ValueError):
                        continue
            if math.isclose(quantity, 0.0, abs_tol=1e-9):
                pnl_value = pnl_value if pnl_value is None else 0.0

            strategies = symbol_to_strategy.get(symbol)
            if not strategies:
                continue
            position = StrategyPosition(symbol=symbol, quantity=quantity, unrealized_pnl=pnl_value)
            for strategy_name in strategies:
                mapping[strategy_name].append(position)

        return mapping

    # ------------------------------------------------------------------
    async def _refresh_positions_cache(self, *, force: bool) -> None:
        now = time.time()
        if not force and (now - self._positions_cache.get("timestamp", 0.0)) < self._positions_cache_ttl:
            return
        try:
            import core  # type: ignore

            client = getattr(core, "ibkr_client", None)
            if client is None or not client or not client.is_connected():
                self._positions_cache = {"timestamp": now, "data": []}
                return
            positions = await client.get_positions(fast=True)
            self._positions_cache = {"timestamp": now, "data": positions or []}
        except Exception as exc:  # pragma: no cover - defensive guard
            strategy_risk_logger.debug(f"Failed to refresh position cache: {exc}")
            self._positions_cache = {"timestamp": now, "data": []}

    # ------------------------------------------------------------------
    def _resolve_strategy_symbols(self, strategy_name: str) -> List[str]:
        symbols: List[str] = []
        if not self.strategy_manager:
            return symbols
        record = getattr(self.strategy_manager, "strategies", {}).get(strategy_name)
        if record:
            if getattr(record, "primary_symbol", None):
                symbols.append(str(record.primary_symbol).upper())
            schedule = getattr(record, "schedule", None)
            if isinstance(schedule, list):
                for item in schedule:
                    if isinstance(item, dict) and item.get("symbol"):
                        symbols.append(str(item["symbol"]).upper())
        getter = getattr(self.strategy_manager, "_get_strategy_setting", None)
        if callable(getter):
            saved_symbol = getter(strategy_name, "primary_symbol")
            if saved_symbol:
                symbols.append(str(saved_symbol).upper())
        runtime = getattr(self.strategy_manager, "internal_strategies_runtime", {}).get(strategy_name)
        if runtime is not None:
            runtime_symbol = getattr(runtime, "symbol", None)
            if runtime_symbol:
                symbols.append(str(runtime_symbol).upper())
        cleaned = [sym for sym in (sym.strip().upper() for sym in symbols) if sym]
        return list(dict.fromkeys(cleaned))

