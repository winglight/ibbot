"""DOM-driven automatic trading strategy implementation."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict
from typing import Any, Awaitable, Dict, Optional, Sequence, Tuple

from loguru import logger

from .base import BaseStrategy


FUTURES_SYMBOLS = {"MNQ", "NQ", "ES", "MES"}


class DOMStrategy(BaseStrategy):
    """DOM结构共振驱动的自动化交易策略。"""

    def __init__(
        self,
        *,
        name: str = "DOM Strategy",
        description: str = (
            "基于盘口(Depth of Market)的结构化策略：聚焦支撑/压力区的多重共振信号，"
            "同时结合盘口堆叠、订单流与扫单/吸收事件，仅在满足足够条件时自动下单，"
            "并借助冷却期与断路器控制噪音风险。"
        ),
        symbol: Optional[str] = None,
        trend_threshold: Optional[float] = None,
        cooldown_seconds: int = 15,
        max_loss_streak: int = 3,
        structure_window_seconds: float = 10.0,
        structure_tolerance_ticks: float = 2.0,
        min_signal_conditions: int = 3,
        signal_frequency_seconds: float = 60.0,
    ) -> None:
        super().__init__(name=name, description=description)
        self.symbol = symbol.upper() if symbol else None
        self.trend_threshold = trend_threshold
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.max_loss_streak = max(1, max_loss_streak)
        self.structure_window_seconds = max(0.0, float(structure_window_seconds))
        self.structure_tolerance_ticks = max(0.0, float(structure_tolerance_ticks))
        self.min_signal_conditions = max(1, int(min_signal_conditions))
        self.signal_frequency_seconds = max(0.0, float(signal_frequency_seconds))
        self.dom_service: Optional[Any] = None
        self.breaker_tripped: bool = False
        self.loss_streak: int = 0
        self.default_quantity: int = 1
        self._last_signal_direction: int = 0
        self._last_signal_time: float = 0.0
        self._last_execution_time: float = 0.0
        self._listener_attached: bool = False
        self._processing: bool = False
        self._started_at: Optional[float] = None
        self._last_runtime_duration: float = 0.0
        self._last_data_timestamp: float = 0.0
        self._processed_count: int = 0
        self._threshold_hits: int = 0
        self._buy_signals: int = 0
        self._sell_signals: int = 0
        self._price_extremes: Dict[str, Dict[str, float]] = {}
        self.file_path = "src/strategies/dom_strategy.py"
        self.summary_points = [
            "仅在中间价触达支撑/压力区并满足≥3条盘口确认条件时入场",
            "综合盘口堆叠、订单流偏向、扫单/吸收与价差变化过滤噪音",
            "配合冷却期与断路器，结合RiskManager执行仓位与风控管理"
        ]
        self.set_parameter_definitions(
            {
                "trend_threshold": {
                    "type": "float",
                    "default": 0.5,
                    "min": 0.0,
                    "max": 5.0,
                    "allow_null": True,
                    "label": "Trend Score Threshold",
                    "description": "最小趋势分数，低于该阈值不触发趋势过滤。",
                    "step": 0.05,
                },
                "cooldown_seconds": {
                    "type": "float",
                    "default": 15.0,
                    "min": 0.0,
                    "max": 600.0,
                    "label": "Signal Cooldown (s)",
                    "description": "同方向信号最短冷却时间，避免噪音重复触发。",
                    "step": 1.0,
                },
                "signal_frequency_seconds": {
                    "type": "float",
                    "default": 60.0,
                    "min": 0.0,
                    "max": 1800.0,
                    "label": "Execution Frequency (s)",
                    "description": "最小成交间隔，限制真实下单频率。",
                    "step": 5.0,
                },
                "max_loss_streak": {
                    "type": "int",
                    "default": 3,
                    "min": 1,
                    "max": 10,
                    "label": "Max Loss Streak",
                    "description": "连续亏损次数达到阈值后触发断路器。",
                },
                "structure_window_seconds": {
                    "type": "float",
                    "default": 10.0,
                    "min": 0.0,
                    "max": 120.0,
                    "label": "Structure Window (s)",
                    "description": "观察近期高低点的窗口长度。",
                    "step": 1.0,
                },
                "structure_tolerance_ticks": {
                    "type": "float",
                    "default": 2.0,
                    "min": 0.0,
                    "max": 10.0,
                    "label": "Zone Tolerance (ticks)",
                    "description": "判定支撑/压力区的允许价差。",
                    "step": 0.25,
                },
                "min_signal_conditions": {
                    "type": "int",
                    "default": 3,
                    "min": 1,
                    "max": 5,
                    "label": "Confirmation Count",
                    "description": "触发信号所需的最少确认条件数量。",
                },
                "default_quantity": {
                    "type": "int",
                    "default": 1,
                    "min": 1,
                    "max": 100,
                    "label": "Fallback Quantity",
                    "description": "风险管理无法计算仓位时使用的默认合约数。",
                },
            }
        )

    # ------------------------------------------------------------------
    def set_dependencies(self, *, dom_service: Any = None, **kwargs: Any) -> None:
        super().set_dependencies(**kwargs)
        if dom_service is not None:
            self.dom_service = dom_service

    # ------------------------------------------------------------------
    def on_registered(self) -> None:
        # Ensure state follows persisted enabled flag
        if self.enabled:
            self.start()

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if not self.enabled:
            self.enabled = True
        if not self.dom_service:
            logger.warning("DOMStrategy cannot start without dom_service dependency")
            return False
        started = super().start()
        if not started:
            return False
        self._started_at = time.time()
        self._last_runtime_duration = 0.0
        self._last_data_timestamp = 0.0
        self._processed_count = 0
        self._threshold_hits = 0
        self._buy_signals = 0
        self._sell_signals = 0
        self._price_extremes.clear()
        if not self._listener_attached:
            self.dom_service.add_listener(self._on_dom_metrics)
            self._listener_attached = True
        return True

    # ------------------------------------------------------------------
    def stop(self) -> bool:
        if not super().stop():
            return False
        if self.dom_service and self._listener_attached:
            self.dom_service.remove_listener(self._on_dom_metrics)
            self._listener_attached = False
        if self._started_at:
            self._last_runtime_duration = time.time() - self._started_at
            self._started_at = None
        return True

    # ------------------------------------------------------------------
    def reset_breaker(self) -> None:
        self.breaker_tripped = False
        self.loss_streak = 0

    # ------------------------------------------------------------------
    def register_trade_result(self, pnl: Optional[float]) -> None:
        """Update loss streak with executed trade result."""
        if pnl is None:
            return
        if pnl < 0:
            self.loss_streak += 1
        else:
            self.loss_streak = 0
        if self.loss_streak >= self.max_loss_streak:
            self._trip_breaker("Loss streak threshold reached")

    # ------------------------------------------------------------------
    def _trip_breaker(self, reason: str) -> None:
        if self.breaker_tripped:
            return
        self.breaker_tripped = True
        self.logger.warning(f"Circuit breaker triggered: {reason}")
        if self.notification_manager:
            coro = self.notification_manager.send_notification(
                message=f"DOM策略断路器触发: {reason}",
                event_type="dom_strategy"
            )
            self._run_coroutine(coro)

    # ------------------------------------------------------------------
    def _on_dom_metrics(self, symbol: Optional[str], metrics: Dict[str, Any], _: Dict[str, Any]) -> None:
        if not self.enabled or not self.active or self.breaker_tripped:
            return
        symbol_code = (symbol or metrics.get("symbol") or "").upper()
        if not symbol_code:
            return
        if self.symbol and symbol_code != self.symbol:
            return
        price = metrics.get("mid_price")
        if price is None:
            return
        previous_timestamp = self._last_data_timestamp
        first_tick = self._processed_count == 0
        now = time.time()
        self._last_data_timestamp = now
        if first_tick:
            self.logger.info("Received first DOM metrics tick", symbol=symbol_code)
        elif previous_timestamp:
            gap = now - previous_timestamp
            if gap >= 10.0:
                self.logger.warning(
                    "DOM metrics gap detected",
                    symbol=symbol_code,
                    seconds_since_last=round(gap, 1),
                    processed=self._processed_count,
                )
        self._processed_count += 1
        tick_size = self._resolve_tick_size(symbol_code, metrics)
        price_float = self._safe_float(price)
        if price_float is None:
            return
        self._update_price_extremes(symbol_code, price_float, now, tick_size)
        direction, satisfied = self._evaluate_signal(symbol_code, metrics, price_float, tick_size, now)
        if direction == 0:
            self._last_signal_direction = 0
            return

        if (
            self._last_signal_direction == direction
            and (now - self._last_signal_time) < self.cooldown_seconds
        ):
            return

        self._last_signal_direction = direction
        self._last_signal_time = now
        self._threshold_hits += 1
        if direction > 0:
            self._buy_signals += 1
        else:
            self._sell_signals += 1
        self.logger.info(
            "DOM结构信号触发: direction=%s satisfied=%s symbol=%s price=%.2f",
            "BUY" if direction > 0 else "SELL",
            ",".join(satisfied) or "-",
            symbol_code,
            price_float,
        )
        self._execute_trade(symbol_code, direction, price_float, metrics)

    # ------------------------------------------------------------------
    def _execute_trade(self, symbol: str, direction: int, price: float, metrics: Dict[str, Any]) -> None:
        if self._processing:
            return
        if not self.queue_manager:
            self.logger.warning("Queue manager not available; skip DOM strategy order")
            return
        if self.breaker_tripped:
            return

        now = time.time()
        if (
            self.signal_frequency_seconds > 0
            and (now - self._last_execution_time) < self.signal_frequency_seconds
        ):
            self.logger.info(
                "Execution frequency limit active: %.2fs remaining",
                self.signal_frequency_seconds - (now - self._last_execution_time),
            )
            return

        self._processing = True
        try:
            if self.risk_manager:
                ok, reason = self.risk_manager.check_circuit_breaker_before_new_order()
                if not ok:
                    self._trip_breaker(reason)
                    return

            quantity = self._determine_quantity(symbol, direction, price)
            if quantity <= 0:
                self.logger.warning("Computed quantity is zero; skip order")
                return

            exchange, sec_type = self._resolve_instrument(symbol)
            payload = {
                "strategy": self.name,
                "symbol": symbol,
                "action": "BUY" if direction > 0 else "SELL",
                "qty": quantity,
                "type": "MARKET",
                "price": float(price),
                "exchange": exchange,
                "sec_type": sec_type,
            }
            self.queue_manager.add_task("strategy_order", payload)
            self._last_execution_time = now
        finally:
            self._processing = False

    # ------------------------------------------------------------------
    def _resolve_instrument(self, symbol: str) -> tuple[str, str]:
        if symbol.upper() in FUTURES_SYMBOLS:
            return "CME", "FUT"
        return "SMART", "STK"

    # ------------------------------------------------------------------
    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    def _resolve_tick_size(self, symbol: str, metrics: Dict[str, Any]) -> float:
        candidates = [
            metrics.get("tick_size"),
            metrics.get("price_tick_size"),
            metrics.get("min_tick"),
            metrics.get("tick"),
        ]
        for container_key in ("instrument", "contract", "meta", "structure"):
            container = metrics.get(container_key)
            if isinstance(container, dict):
                candidates.extend(
                    container.get(key)
                    for key in ("tick_size", "min_tick", "price_tick_size", "tick")
                )
        for candidate in candidates:
            tick = self._safe_float(candidate)
            if tick and tick > 0:
                return tick
        if symbol.upper() in FUTURES_SYMBOLS:
            return 0.25
        return 0.01

    # ------------------------------------------------------------------
    def _update_price_extremes(self, symbol: str, price: float, now: float, tick_size: float) -> None:
        state = self._price_extremes.setdefault(
            symbol,
            {"low": price, "low_time": now, "high": price, "high_time": now},
        )
        tolerance = max(tick_size * 0.5, 0.0)
        low_price = self._safe_float(state.get("low"))
        if low_price is None or price < low_price - tolerance:
            state["low"] = price
            state["low_time"] = now
        high_price = self._safe_float(state.get("high"))
        if high_price is None or price > high_price + tolerance:
            state["high"] = price
            state["high_time"] = now

    # ------------------------------------------------------------------
    def _evaluate_signal(
        self,
        symbol: str,
        metrics: Dict[str, Any],
        price: float,
        tick_size: float,
        now: float,
    ) -> Tuple[int, Sequence[str]]:
        tolerance_support = self._resolve_tolerance_ticks(metrics, "support")
        tolerance_resistance = self._resolve_tolerance_ticks(metrics, "resistance")
        window_seconds = self._resolve_window_seconds(metrics)

        long_conditions = [
            (
                "position",
                self._is_within_zone(
                    "support", metrics, price, tick_size, tolerance_support
                ),
            ),
            ("orderbook", self._orderbook_condition(metrics, bullish=True)),
            ("orderflow", self._orderflow_condition(metrics, bullish=True)),
            (
                "absorption_or_spread",
                self._long_absorption_condition(metrics, tick_size),
            ),
            (
                "no_new_low",
                self._recent_extreme_calm(symbol, "low", now, window_seconds),
            ),
        ]

        short_conditions = [
            (
                "position",
                self._is_within_zone(
                    "resistance", metrics, price, tick_size, tolerance_resistance
                ),
            ),
            ("orderbook", self._orderbook_condition(metrics, bullish=False)),
            ("orderflow", self._orderflow_condition(metrics, bullish=False)),
            (
                "sweep_or_spread",
                self._short_sweep_condition(metrics, tick_size),
            ),
            (
                "no_new_high",
                self._recent_extreme_calm(symbol, "high", now, window_seconds),
            ),
        ]

        long_count = sum(1 for _, ok in long_conditions if ok)
        short_count = sum(1 for _, ok in short_conditions if ok)

        direction = 0
        satisfied: Sequence[str] = []
        if long_count >= self.min_signal_conditions:
            direction = 1
            satisfied = [name for name, ok in long_conditions if ok]
        if short_count >= self.min_signal_conditions:
            short_satisfied = [name for name, ok in short_conditions if ok]
            if direction == 0 or short_count > long_count:
                direction = -1
                satisfied = short_satisfied
            elif short_count == long_count:
                direction = 0
                satisfied = []
        return direction, list(satisfied)

    # ------------------------------------------------------------------
    def _orderbook_condition(self, metrics: Dict[str, Any], *, bullish: bool) -> bool:
        if bullish:
            imbalance = self._get_float(metrics, ["imbalance", "imb", "book_imbalance", "obi5"])
            stacking = self._get_float(
                metrics,
                ["z_stack_bid", "zscore_stack_bid", "stacking_zscore_buy", "stacking_z_buy"],
            )
            if stacking is None and self._has_feature_flag(metrics, "stacking_buy"):
                stacking = 1.0
            return (
                (imbalance is not None and imbalance >= 0.60)
                and (stacking is not None and stacking >= 1.0)
            )
        imbalance = self._get_float(metrics, ["imbalance", "imb", "book_imbalance", "obi5"])
        stacking = self._get_float(
            metrics,
            ["z_stack_ask", "zscore_stack_ask", "stacking_zscore_sell", "stacking_z_sell"],
        )
        if stacking is None and self._has_feature_flag(metrics, "stacking_sell"):
            stacking = 1.0
        return (
            (imbalance is not None and imbalance <= 0.40)
            and (stacking is not None and stacking >= 1.0)
        )

    # ------------------------------------------------------------------
    def _orderflow_condition(self, metrics: Dict[str, Any], *, bullish: bool) -> bool:
        z_ofi = self._get_float(metrics, ["z_ofi", "zscore_ofi"])
        z_cvd = self._get_float(metrics, ["z_cvd", "zscore_cvd"])
        if bullish:
            return (
                (z_ofi is not None and z_ofi >= 1.0)
                or (z_cvd is not None and z_cvd >= 1.0)
            )
        return (
            (z_ofi is not None and z_ofi <= -1.0)
            or (z_cvd is not None and z_cvd <= -1.0)
        )

    # ------------------------------------------------------------------
    def _long_absorption_condition(self, metrics: Dict[str, Any], tick_size: float) -> bool:
        absorb = self._boolean_metric(metrics, ["absorb_up", "absorption_buy", "absorb_buy"])
        if not absorb and self._has_feature_flag(metrics, "absorption_buy"):
            absorb = True
        spread_ok = self._spread_within_limit(metrics, tick_size, 2.0, comparison="lte")
        return absorb or spread_ok

    # ------------------------------------------------------------------
    def _short_sweep_condition(self, metrics: Dict[str, Any], tick_size: float) -> bool:
        sweep = self._boolean_metric(metrics, ["sweep_down", "sweep_sell"])
        if not sweep and self._has_feature_flag(metrics, "sweep_down"):
            sweep = True
        spread_not_tight = self._spread_within_limit(metrics, tick_size, 2.0, comparison="gte")
        return sweep or spread_not_tight

    # ------------------------------------------------------------------
    def _recent_extreme_calm(
        self,
        symbol: str,
        which: str,
        now: float,
        window_seconds: float,
    ) -> bool:
        if window_seconds <= 0:
            return True
        state = self._price_extremes.get(symbol)
        if not state:
            return False
        key = "low_time" if which == "low" else "high_time"
        ts = self._safe_float(state.get(key))
        if ts is None:
            return False
        return (now - ts) >= window_seconds

    # ------------------------------------------------------------------
    def _is_within_zone(
        self,
        zone_type: str,
        metrics: Dict[str, Any],
        price: float,
        tick_size: float,
        tolerance_ticks: float,
    ) -> bool:
        tolerance_ticks = max(tolerance_ticks, 0.0)
        tolerance_price = tolerance_ticks * max(tick_size, 1e-9)
        for key in (
            f"in_{zone_type}_zone",
            f"{zone_type}_zone_active",
            f"{zone_type}_active",
            f"is_{zone_type}",
        ):
            if key in metrics and self._boolean_value(metrics.get(key)):
                return True

        zone_state = metrics.get("structure_zone") or metrics.get("structure_state")
        if isinstance(zone_state, str) and zone_state.lower() == zone_type:
            return True

        for container_key in ("structure", "price_structure", "price_structures", "zones"):
            container = metrics.get(container_key)
            if not isinstance(container, dict):
                continue
            nested_state = container.get("zone") or container.get("state") or container.get("active")
            if isinstance(nested_state, str) and nested_state.lower() == zone_type:
                return True
            direct_flag = container.get(f"in_{zone_type}") or container.get(f"{zone_type}_active")
            if direct_flag is not None and self._boolean_value(direct_flag):
                return True

        band = self._extract_band(metrics, zone_type)
        if band:
            low, high = band
            if low is not None and high is not None:
                return (price >= low - tolerance_price) and (price <= high + tolerance_price)
        return False

    # ------------------------------------------------------------------
    def _extract_band(self, metrics: Dict[str, Any], zone_type: str) -> Optional[Tuple[float, float]]:
        low = self._get_float(
            metrics,
            [
                f"{zone_type}_band_low",
                f"{zone_type}_low",
                f"{zone_type}_min",
                f"{zone_type}_lower",
            ],
        )
        high = self._get_float(
            metrics,
            [
                f"{zone_type}_band_high",
                f"{zone_type}_high",
                f"{zone_type}_max",
                f"{zone_type}_upper",
            ],
        )
        if low is not None and high is not None:
            if low > high:
                low, high = high, low
            return (low, high)

        values: list[Any] = []
        direct_keys = [
            f"{zone_type}_band",
            f"{zone_type}_zone",
            f"{zone_type}_range",
            f"{zone_type}_area",
            f"{zone_type}_levels",
        ]
        for key in direct_keys:
            if key in metrics:
                values.append(metrics.get(key))

        for container_key in ("structure", "price_structure", "price_structures", "zones", "levels"):
            container = metrics.get(container_key)
            if isinstance(container, dict):
                for key in ([zone_type] + direct_keys):
                    if key in container:
                        values.append(container.get(key))

        for value in values:
            band = self._normalise_band_value(value)
            if band is not None:
                return band
        return None

    # ------------------------------------------------------------------
    def _normalise_band_value(self, value: Any) -> Optional[Tuple[float, float]]:
        if value is None:
            return None
        if isinstance(value, dict):
            low = self._safe_float(
                value.get("low")
                or value.get("min")
                or value.get("start")
                or value.get("lower")
            )
            high = self._safe_float(
                value.get("high")
                or value.get("max")
                or value.get("end")
                or value.get("upper")
            )
            if low is not None and high is not None:
                if low > high:
                    low, high = high, low
                return (low, high)
            center = self._safe_float(
                value.get("price")
                or value.get("level")
                or value.get("value")
                or value.get("mid")
            )
            width = self._safe_float(value.get("width") or value.get("radius") or value.get("tolerance"))
            if center is not None and width is not None and width >= 0:
                return (center - width, center + width)
            return None
        if isinstance(value, (list, tuple)):
            if len(value) >= 2:
                low = self._safe_float(value[0])
                high = self._safe_float(value[1])
                if low is None or high is None:
                    return None
                if low > high:
                    low, high = high, low
                return (low, high)
            if len(value) == 1:
                val = self._safe_float(value[0])
                if val is not None:
                    return (val, val)
            return None
        single = self._safe_float(value)
        if single is None:
            return None
        return (single, single)

    # ------------------------------------------------------------------
    def _resolve_tolerance_ticks(self, metrics: Dict[str, Any], zone_type: str) -> float:
        candidate_keys = [
            f"{zone_type}_tolerance_ticks",
            f"{zone_type}_tolerance",
            f"{zone_type}_zone_tolerance",
        ]
        for key in candidate_keys:
            ticks = self._safe_float(metrics.get(key))
            if ticks is not None and ticks >= 0:
                return ticks
        for container_key in ("structure", "price_structure", "price_structures"):
            container = metrics.get(container_key)
            if not isinstance(container, dict):
                continue
            for key in (
                f"{zone_type}_tolerance_ticks",
                f"{zone_type}_tolerance",
                "tolerance_ticks",
            ):
                ticks = self._safe_float(container.get(key))
                if ticks is not None and ticks >= 0:
                    return ticks
        common = self._safe_float(metrics.get("structure_tolerance_ticks") or metrics.get("zone_tolerance_ticks"))
        if common is not None and common >= 0:
            return common
        return self.structure_tolerance_ticks

    # ------------------------------------------------------------------
    def _get_float(
        self,
        metrics: Dict[str, Any],
        keys: Sequence[str],
        containers: Sequence[str] = ("features",),
    ) -> Optional[float]:
        for key in keys:
            if key in metrics:
                value = self._safe_float(metrics.get(key))
                if value is not None:
                    return value
        for container_key in containers:
            container = metrics.get(container_key)
            if isinstance(container, dict):
                for key in keys:
                    if key in container:
                        value = self._safe_float(container.get(key))
                        if value is not None:
                            return value
        return None

    # ------------------------------------------------------------------
    def _boolean_metric(
        self,
        metrics: Dict[str, Any],
        keys: Sequence[str],
        containers: Sequence[str] = (),
    ) -> bool:
        for key in keys:
            if key in metrics and self._boolean_value(metrics.get(key)):
                return True
        for container_key in containers:
            container = metrics.get(container_key)
            if isinstance(container, dict):
                for key in keys:
                    if key in container and self._boolean_value(container.get(key)):
                        return True
        return False

    # ------------------------------------------------------------------
    def _boolean_value(self, value: Any) -> bool:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on", "support", "resistance"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
            return bool(normalized)
        try:
            return bool(value)
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _has_feature_flag(self, metrics: Dict[str, Any], key: str) -> bool:
        flags = metrics.get("feature_flags")
        if isinstance(flags, dict):
            return self._boolean_value(flags.get(key))
        return False

    # ------------------------------------------------------------------
    def _spread_within_limit(
        self,
        metrics: Dict[str, Any],
        tick_size: float,
        threshold_ticks: float,
        *,
        comparison: str = "lte",
    ) -> bool:
        spread = self._get_float(
            metrics,
            ["spread_tick_count", "spread_ticks", "spread", "bid_ask_spread"],
            containers=("depth", "quotes"),
        )
        if spread is None:
            return False
        normalized = self._normalise_spread_to_ticks(spread, tick_size)
        if normalized is None:
            return False
        if comparison == "gte":
            return normalized >= threshold_ticks
        return normalized <= threshold_ticks

    # ------------------------------------------------------------------
    def _normalise_spread_to_ticks(self, spread: float, tick_size: float) -> Optional[float]:
        value = self._safe_float(spread)
        if value is None:
            return None
        value = abs(value)
        tick = tick_size if tick_size and tick_size > 0 else 0.25
        candidates = [value, value / tick, value / (tick * 100.0)]
        positives = [c for c in candidates if math.isfinite(c) and c >= 0]
        if not positives:
            return None

        def _score(candidate: float) -> Tuple[float, float]:
            fractional = abs(candidate - round(candidate))
            return (fractional, candidate)

        best = min(positives, key=_score)
        return best

    # ------------------------------------------------------------------
    def _resolve_window_seconds(self, metrics: Dict[str, Any]) -> float:
        candidate_keys = [
            "structure_window_seconds",
            "structure_window_sec",
            "extreme_window_seconds",
            "extreme_window_sec",
            "recent_extreme_window",
            "signal_window_seconds",
        ]
        for key in candidate_keys:
            seconds = self._safe_float(metrics.get(key))
            if seconds is not None and seconds >= 0:
                return seconds
        for container_key in ("structure", "price_structure", "price_structures"):
            container = metrics.get(container_key)
            if isinstance(container, dict):
                for key in ("window_seconds", "extreme_window_seconds"):
                    seconds = self._safe_float(container.get(key))
                    if seconds is not None and seconds >= 0:
                        return seconds
        return self.structure_window_seconds

    # ------------------------------------------------------------------
    def _determine_quantity(self, symbol: str, direction: int, price: float) -> int:
        rule = None
        if self.risk_manager and hasattr(self.risk_manager, "risk_rules"):
            rule = self.risk_manager.risk_rules.get(symbol)
        stop_price = getattr(rule, "stop_loss_price", None)
        if stop_price is None:
            return self.default_quantity

        stop_distance = price - float(stop_price) if direction > 0 else float(stop_price) - price
        stop_distance = abs(stop_distance)
        if stop_distance <= 0:
            return self.default_quantity

        equity = None
        risk_per_trade = None
        if self.risk_manager:
            equity = getattr(self.risk_manager, "equity_last", None) or getattr(
                self.risk_manager, "sod_equity", None
            )
            risk_per_trade = getattr(self.risk_manager, "risk_per_trade_r", None)
        if not equity or not risk_per_trade:
            return self.default_quantity

        risk_budget = float(equity) * float(risk_per_trade)
        point_value = 1.0
        if self.risk_manager and hasattr(self.risk_manager, "point_value_map"):
            point_value = float(self.risk_manager.point_value_map.get(symbol, 1.0))
        denom = max(1e-9, stop_distance * max(point_value, 1e-9))
        contracts = risk_budget / denom
        if math.isfinite(contracts):
            return max(1, int(contracts))
        return self.default_quantity

    # ------------------------------------------------------------------
    def _run_coroutine(self, coro: Optional[Awaitable[Any]]) -> None:
        if not coro:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(coro)
            except RuntimeError:
                self.logger.exception("Failed to execute coroutine for DOM strategy notification")
        else:
            loop.create_task(coro)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    def get_internal_info(self) -> Dict[str, Any]:
        """Expose summary information for the strategy panel."""

        info: Dict[str, Any] = {
            "description": self.description,
            "file_path": self.file_path,
            "risk_parameters": {
                "trend_threshold": float(self.trend_threshold) if self.trend_threshold is not None else None,
                "cooldown_seconds": self.cooldown_seconds,
                "max_loss_streak": self.max_loss_streak,
                "default_quantity": self.default_quantity,
                "structure_window_seconds": self.structure_window_seconds,
                "structure_tolerance_ticks": self.structure_tolerance_ticks,
                "min_signal_conditions": self.min_signal_conditions,
                "signal_frequency_seconds": self.signal_frequency_seconds,
            },
            "summary_points": list(self.summary_points),
            "configurable_parameters": self.describe_parameters(),
        }

        if self.symbol:
            info["primary_symbol"] = self.symbol

        runtime_rules: Dict[str, Any] = {}
        if self.symbol and self.risk_manager and hasattr(self.risk_manager, "risk_rules"):
            rule = self.risk_manager.risk_rules.get(self.symbol)
            if rule:
                runtime_rules[self.symbol] = asdict(rule)

        if runtime_rules:
            info["risk_rules"] = runtime_rules

        return info

    # ------------------------------------------------------------------
    def get_runtime_status(self) -> Dict[str, Any]:
        """Provide real-time operational statistics for monitoring."""

        now = time.time()
        runtime_seconds = self._last_runtime_duration
        if self.active and self._started_at:
            runtime_seconds = now - self._started_at

        is_receiving = False
        if self.active and self._last_data_timestamp:
            is_receiving = (now - self._last_data_timestamp) <= 5.0

        return {
            "is_receiving_data": bool(is_receiving),
            "runtime_seconds": max(0.0, float(runtime_seconds)),
            "processed_count": self._processed_count,
            "threshold_hits": self._threshold_hits,
            "buy_signals": self._buy_signals,
            "sell_signals": self._sell_signals,
            "last_data_timestamp": float(self._last_data_timestamp) if self._last_data_timestamp else 0.0,
        }

