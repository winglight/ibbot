"""DOM-driven automatic trading strategy implementation."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict
from typing import Any, Awaitable, Dict, Optional

from loguru import logger

from .base import BaseStrategy


FUTURES_SYMBOLS = {"MNQ", "NQ", "ES", "MES"}


class DOMStrategy(BaseStrategy):
    """Automatically submits trades when DOM trend score crosses thresholds."""

    def __init__(
        self,
        *,
        name: str = "DOM Strategy",
        description: str = (
            "基于盘口(Depth of Market)特征的自动化策略：结合订单簿不平衡、订单流失衡和"
            "扫单/吸收事件评分，在趋势信号跨过阈值时触发下单，并通过冷却期与断路器控"
            "制噪音交易。"
        ),
        symbol: Optional[str] = None,
        trend_threshold: Optional[float] = None,
        cooldown_seconds: int = 15,
        max_loss_streak: int = 3,
    ) -> None:
        super().__init__(name=name, description=description)
        self.symbol = symbol.upper() if symbol else None
        self.trend_threshold = trend_threshold
        self.cooldown_seconds = max(0, cooldown_seconds)
        self.max_loss_streak = max(1, max_loss_streak)
        self.dom_service: Optional[Any] = None
        self.breaker_tripped: bool = False
        self.loss_streak: int = 0
        self.default_quantity: int = 1
        self._last_signal_direction: int = 0
        self._last_signal_time: float = 0.0
        self._listener_attached: bool = False
        self._processing: bool = False
        self._started_at: Optional[float] = None
        self._last_runtime_duration: float = 0.0
        self._last_data_timestamp: float = 0.0
        self._processed_count: int = 0
        self._threshold_hits: int = 0
        self._buy_signals: int = 0
        self._sell_signals: int = 0
        self.file_path = "src/strategies/dom_strategy.py"
        self.summary_points = [
            "实时监听DOM趋势得分与订单流不平衡，信号跨越阈值时自动开仓",
            "为每次信号提供冷却时间，防止高频噪音连续触发",
            "跟踪连亏次数并触发断路器，由RiskManager接管仓位与风险"
        ]

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
        score = metrics.get("trend_score")
        price = metrics.get("mid_price")
        if score is None or price is None:
            return
        now = time.time()
        self._last_data_timestamp = now
        self._processed_count += 1
        threshold = self._resolve_threshold()
        if threshold <= 0:
            return
        direction = 0
        if score >= threshold:
            direction = 1
        elif score <= -threshold:
            direction = -1
        else:
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
            f"DOM trend trigger: score={score:.2f} direction={'BUY' if direction > 0 else 'SELL'} symbol={symbol_code}"
        )
        self._execute_trade(symbol_code, direction, float(price), metrics)

    # ------------------------------------------------------------------
    def _execute_trade(self, symbol: str, direction: int, price: float, metrics: Dict[str, Any]) -> None:
        if self._processing:
            return
        if not self.queue_manager:
            self.logger.warning("Queue manager not available; skip DOM strategy order")
            return
        if self.breaker_tripped:
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
        finally:
            self._processing = False

    # ------------------------------------------------------------------
    def _resolve_instrument(self, symbol: str) -> tuple[str, str]:
        if symbol.upper() in FUTURES_SYMBOLS:
            return "CME", "FUT"
        return "SMART", "STK"

    # ------------------------------------------------------------------
    def _resolve_threshold(self) -> float:
        if self.trend_threshold is not None:
            try:
                return float(self.trend_threshold)
            except (TypeError, ValueError):
                pass
        if self.dom_service and getattr(self.dom_service, "trend_thresholds", None):
            try:
                thresholds = [float(v) for v in self.dom_service.trend_thresholds]
                if thresholds:
                    return max(thresholds)
            except Exception:
                logger.debug("Failed to parse trend thresholds from DOM service")
        return 0.85

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

        resolved_threshold = self.trend_threshold
        if resolved_threshold is None:
            try:
                resolved_threshold = float(self._resolve_threshold())
            except Exception:
                resolved_threshold = None

        info: Dict[str, Any] = {
            "description": self.description,
            "file_path": self.file_path,
            "risk_parameters": {
                "trend_threshold": float(resolved_threshold) if resolved_threshold is not None else None,
                "cooldown_seconds": self.cooldown_seconds,
                "max_loss_streak": self.max_loss_streak,
                "default_quantity": self.default_quantity,
            },
            "summary_points": list(self.summary_points),
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

