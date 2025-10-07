"""买入收盘、卖出开盘的统一K线订阅策略实现。"""

from __future__ import annotations

from datetime import datetime, date
from typing import Any, Dict, Optional, Tuple

import pytz

from src.ibkr_client import INDEX_FUTURE_DETAILS

from .candle_base import CandleSubscriptionStrategy

DEFAULT_FUTURE_TICKS = {
    "ES": 0.25,
    "MES": 0.25,
    "NQ": 0.25,
    "MNQ": 0.25,
    "YM": 1.0,
    "MYM": 1.0,
    "RTY": 0.1,
    "M2K": 0.1,
    "VX": 0.05,
}

EASTERN_TZ = pytz.timezone("US/Eastern")


class BuyDownSellOpenStrategy(CandleSubscriptionStrategy):
    """Buy the futures contract near the equity close and exit near the next open."""

    def __init__(
        self,
        *,
        symbol: str = "MNQ",
        interval: str = "1m",
        history_bars: int = 300,
        name: str = "Buy Down Sell Open",
        description: str = "收盘前买入、开盘后卖出的跨日持仓策略",
        default_quantity: int = 1,
        max_daily_buys: int = 3,
        buy_hour: int = 15,
        buy_minute: int = 59,
        sell_hour: int = 9,
        sell_minute: int = 31,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            symbol=symbol,
            interval=interval,
            history_bars=history_bars,
        )
        self.default_quantity = max(1, int(default_quantity))
        self.max_daily_buys = max(1, int(max_daily_buys))
        self.buy_hour = int(buy_hour)
        self.buy_minute = int(buy_minute)
        self.sell_hour = int(sell_hour)
        self.sell_minute = int(sell_minute)
        self.trades_today: int = 0
        self._last_reset_day: Optional[date] = None
        self._has_position: bool = False
        self._current_position_qty: int = 0
        self._last_entry_ts_ns: Optional[int] = None
        self._last_exit_ts_ns: Optional[int] = None
        self.file_path = "src/strategies/Buy down and Sell open.py"
        self.summary_points = [
            "使用统一数据层订阅1分钟K线，按照美东时间判断收盘/开盘时点",
            "收盘前买入最多3次并记录当日执行次数，次日开盘后平仓",
            "通过队列管理与风险检查生成MARKET订单，与现有策略框架一致对接",
        ]
        self.set_parameter_definitions(
            {
                "symbol": {
                    "type": "str",
                    "default": self.primary_symbol,
                    "label": "Symbol",
                    "description": "订阅与交易的标的代码。",
                },
                "default_quantity": {
                    "type": "int",
                    "default": self.default_quantity,
                    "min": 1,
                    "max": 200,
                    "label": "Order Quantity",
                },
                "max_daily_buys": {
                    "type": "int",
                    "default": self.max_daily_buys,
                    "min": 1,
                    "max": 10,
                    "label": "Max Daily Buys",
                    "description": "每日允许的入场次数上限。",
                },
                "buy_hour": {
                    "type": "int",
                    "default": self.buy_hour,
                    "min": 0,
                    "max": 23,
                    "label": "Buy Hour (EST)",
                },
                "buy_minute": {
                    "type": "int",
                    "default": self.buy_minute,
                    "min": 0,
                    "max": 59,
                    "label": "Buy Minute (EST)",
                },
                "sell_hour": {
                    "type": "int",
                    "default": self.sell_hour,
                    "min": 0,
                    "max": 23,
                    "label": "Sell Hour (EST)",
                },
                "sell_minute": {
                    "type": "int",
                    "default": self.sell_minute,
                    "min": 0,
                    "max": 59,
                    "label": "Sell Minute (EST)",
                },
            }
        )

    # ------------------------------------------------------------------
    def apply_parameter_updates(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        applied = super().apply_parameter_updates(updates)
        if "symbol" in applied:
            new_symbol = str(applied["symbol"]).upper()
            if new_symbol != self.primary_symbol:
                self.primary_symbol = new_symbol
                if self.active:
                    self._teardown_subscription()
                    try:
                        self._ensure_subscription()
                    except Exception as exc:
                        self.logger.error("Failed to resubscribe after symbol change: %s", exc)
        for attr in ("default_quantity", "max_daily_buys", "buy_hour", "buy_minute", "sell_hour", "sell_minute"):
            if attr in applied:
                setattr(self, attr, int(applied[attr]))
        return applied

    # ------------------------------------------------------------------
    def on_candle(self, candle: Dict[str, Any]) -> None:
        ts_ns = candle.get("interval_end_ns")
        if not isinstance(ts_ns, int):
            return
        est_time = self._to_est(ts_ns)
        self._reset_if_new_day(est_time)
        close_price = self._extract_price(candle)
        if close_price <= 0:
            return

        if (
            est_time.hour == self.buy_hour
            and est_time.minute == self.buy_minute
            and self.trades_today < self.max_daily_buys
            and not self._has_position
        ):
            if ts_ns == self._last_entry_ts_ns:
                return
            if self._queue_order("BUY", close_price, candle, est_time, reason="session_close_entry"):
                self.trades_today += 1
                self._has_position = True
                self._current_position_qty = self.default_quantity
                self._last_entry_ts_ns = ts_ns

        if (
            self._has_position
            and est_time.hour == self.sell_hour
            and est_time.minute == self.sell_minute
        ):
            if ts_ns == self._last_exit_ts_ns:
                return
            if self._queue_order("SELL", close_price, candle, est_time, reason="session_open_exit"):
                self._has_position = False
                self._current_position_qty = 0
                self._last_exit_ts_ns = ts_ns

    # ------------------------------------------------------------------
    def _reset_if_new_day(self, est_time: datetime) -> None:
        trade_day = est_time.date()
        if self._last_reset_day != trade_day:
            self.trades_today = 0
            self._last_reset_day = trade_day

    # ------------------------------------------------------------------
    def _queue_order(
        self,
        action: str,
        price: float,
        candle: Dict[str, Any],
        est_time: datetime,
        *,
        reason: str,
    ) -> bool:
        if not self.queue_manager:
            self.logger.warning("Queue manager dependency missing; skip %s order", action)
            return False
        if self.risk_manager:
            ok, risk_reason = self.risk_manager.check_circuit_breaker_before_new_order()
            if not ok:
                self.logger.warning(
                    "Risk breaker blocked %s order", action.lower(), reason=risk_reason
                )
                return False
        quantity = max(1, int(self.default_quantity))
        exchange, sec_type = self._resolve_instrument(self.primary_symbol)
        payload = {
            "strategy": self.name,
            "symbol": self.primary_symbol,
            "action": action,
            "qty": quantity,
            "type": "MARKET",
            "price": float(price) if price > 0 else None,
            "exchange": exchange,
            "sec_type": sec_type,
            "meta": {
                "reason": reason,
                "interval": candle.get("interval"),
                "interval_end_ns": candle.get("interval_end_ns"),
                "est_time": est_time.isoformat(),
                "position_before": self._current_position_qty,
                "tick_size": self._resolve_tick_size(self.primary_symbol),
            },
        }
        try:
            task_id = self.queue_manager.add_task("strategy_order", payload)
        except Exception as exc:
            self.logger.error("Failed to enqueue %s order: %s", action, exc)
            return False
        self.logger.info(
            "Queued %s order", action, task_id=task_id, price=price, quantity=quantity
        )
        return True

    # ------------------------------------------------------------------
    def _resolve_instrument(self, symbol: str) -> Tuple[str, str]:
        sym = symbol.upper()
        details = INDEX_FUTURE_DETAILS.get(sym)
        if details is None:
            for key in sorted(INDEX_FUTURE_DETAILS.keys(), key=len, reverse=True):
                if sym.startswith(key):
                    details = INDEX_FUTURE_DETAILS[key]
                    break
        if details:
            return details.get("exchange", "CME"), "FUT"
        return "SMART", "STK"

    # ------------------------------------------------------------------
    def _extract_price(self, candle: Dict[str, Any]) -> float:
        try:
            return float(candle.get("close") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    def _to_est(self, ts_ns: int) -> datetime:
        utc_time = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=pytz.UTC)
        return utc_time.astimezone(EASTERN_TZ)

    # ------------------------------------------------------------------
    def _resolve_tick_size(self, symbol: str) -> float:
        base = symbol.upper()
        if base in DEFAULT_FUTURE_TICKS:
            return DEFAULT_FUTURE_TICKS[base]
        for key, value in DEFAULT_FUTURE_TICKS.items():
            if base.startswith(key):
                return value
        return 0.01
