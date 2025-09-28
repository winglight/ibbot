"""Momentum-support buy-the-dip strategy using unified candle subscriptions."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

import pandas as pd

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


class BuyTheDipStrategy(CandleSubscriptionStrategy):
    """Buy the dip when momentum resumes after a pullback."""

    def __init__(
        self,
        *,
        symbol: str = "ES",
        interval: str = "1m",
        history_bars: int = 240,
        name: str = "Buy The Dip",
        description: str = (
            "当价格短期回调且动量指标恢复向上时入场，结合成交量差确认买盘优势，"
            "并通过冷却期及风险控制避免过度交易。"
        ),
        default_quantity: int = 1,
        cooldown_seconds: float = 120.0,
        rsi_period: int = 14,
        rsi_oversold: float = 35.0,
        ma_fast_period: int = 20,
        ma_slow_period: int = 50,
        dip_threshold: float = 0.004,
        cvd_window: int = 50,
        cvd_z_entry: float = 0.5,
        stop_loss_ticks: float = 10.0,
        take_profit_ticks: float = 16.0,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            symbol=symbol,
            interval=interval,
            history_bars=history_bars,
        )
        self.default_quantity = max(1, int(default_quantity))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.rsi_period = max(2, int(rsi_period))
        self.rsi_oversold = float(rsi_oversold)
        self.ma_fast_period = max(2, int(ma_fast_period))
        self.ma_slow_period = max(self.ma_fast_period + 1, int(ma_slow_period))
        self.dip_threshold = max(0.0, float(dip_threshold))
        self.cvd_window = max(5, int(cvd_window))
        self.cvd_z_entry = float(cvd_z_entry)
        self.stop_loss_ticks = max(0.0, float(stop_loss_ticks))
        self.take_profit_ticks = max(0.0, float(take_profit_ticks))
        self._last_signal_time: float = 0.0
        self._last_order_payload: Optional[Dict[str, Any]] = None
        self.file_path = "src/strategies/buy_the_dip_strategy.py"
        self.summary_points = [
            "订阅统一K线数据，实时计算RSI、均线乖离与成交量差指标",
            "价格跌破短期均线且RSI超卖，同时CVD恢复为正时触发买入",
            "通过冷却期和风险参数控制连续信号，适配RiskManager与队列执行"
        ]
        self.set_parameter_definitions(
            {
                "symbol": {
                    "type": "str",
                    "default": symbol.upper(),
                    "label": "Symbol",
                    "description": "订阅及交易的标的代码。",
                },
                "default_quantity": {
                    "type": "int",
                    "default": self.default_quantity,
                    "min": 1,
                    "max": 200,
                    "label": "Order Quantity",
                    "description": "默认下单数量，风险管理无法计算仓位时使用。",
                },
                "cooldown_seconds": {
                    "type": "float",
                    "default": self.cooldown_seconds,
                    "min": 0.0,
                    "max": 1800.0,
                    "label": "Signal Cooldown (s)",
                    "step": 5.0,
                },
                "rsi_period": {
                    "type": "int",
                    "default": self.rsi_period,
                    "min": 5,
                    "max": 50,
                    "label": "RSI Period",
                },
                "rsi_oversold": {
                    "type": "float",
                    "default": self.rsi_oversold,
                    "min": 5.0,
                    "max": 60.0,
                    "label": "RSI Oversold Threshold",
                },
                "ma_fast_period": {
                    "type": "int",
                    "default": self.ma_fast_period,
                    "min": 5,
                    "max": 100,
                    "label": "Fast MA Period",
                },
                "ma_slow_period": {
                    "type": "int",
                    "default": self.ma_slow_period,
                    "min": 10,
                    "max": 200,
                    "label": "Slow MA Period",
                },
                "dip_threshold": {
                    "type": "float",
                    "default": self.dip_threshold,
                    "min": 0.0005,
                    "max": 0.05,
                    "label": "MA Pullback Threshold",
                    "description": "价格低于快线的最小回调比例 (eg. 0.004 = 0.4%)。",
                    "step": 0.0005,
                },
                "cvd_window": {
                    "type": "int",
                    "default": self.cvd_window,
                    "min": 10,
                    "max": 200,
                    "label": "CVD Window",
                },
                "cvd_z_entry": {
                    "type": "float",
                    "default": self.cvd_z_entry,
                    "min": -5.0,
                    "max": 5.0,
                    "label": "CVD Z-score Threshold",
                },
                "stop_loss_ticks": {
                    "type": "float",
                    "default": self.stop_loss_ticks,
                    "min": 0.0,
                    "max": 100.0,
                    "label": "Stop Loss (ticks)",
                    "step": 0.25,
                },
                "take_profit_ticks": {
                    "type": "float",
                    "default": self.take_profit_ticks,
                    "min": 0.0,
                    "max": 200.0,
                    "label": "Take Profit (ticks)",
                    "step": 0.25,
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
                        self.logger.error("Failed to resubscribe after symbol change: {}", exc)
        return applied

    # ------------------------------------------------------------------
    def on_candle(self, candle: Dict[str, Any]) -> None:
        candles = self.get_candles()
        if len(candles) < max(self.ma_slow_period, self.cvd_window) + 5:
            return
        df = pd.DataFrame(candles)
        df = df[df.get("is_closed", True)].copy()
        if df.empty:
            return
        for column in ("open", "high", "low", "close", "volume"):
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        df.dropna(subset=["close"], inplace=True)
        if df.empty:
            return
        df["ma_fast"] = df["close"].rolling(self.ma_fast_period).mean()
        df["ma_slow"] = df["close"].rolling(self.ma_slow_period).mean()
        df["rsi"] = self._compute_rsi(df["close"], self.rsi_period)
        df["direction"] = 0.0
        df.loc[df["close"] > df["open"], "direction"] = 1.0
        df.loc[df["close"] < df["open"], "direction"] = -1.0
        df["delta_volume"] = df["direction"] * df["volume"].fillna(0.0)
        df["cvd"] = df["delta_volume"].cumsum()
        rolling = df["cvd"].rolling(self.cvd_window)
        df["z_cvd"] = (df["cvd"] - rolling.mean()) / rolling.std(ddof=0)
        df.replace({"z_cvd": {pd.NA: 0, math.inf: 0, -math.inf: 0}}, inplace=True)
        df["z_cvd"].fillna(0.0, inplace=True)
        latest = df.iloc[-1]
        if latest["ma_fast"] is None or latest["ma_slow"] is None:
            return
        if latest["ma_fast"] <= 0 or latest["ma_slow"] <= 0:
            return
        ma_gap = (latest["close"] - latest["ma_fast"]) / latest["ma_fast"]
        now = time.time()
        if self.cooldown_seconds > 0 and (now - self._last_signal_time) < self.cooldown_seconds:
            return
        if latest["rsi"] is None or math.isnan(latest["rsi"]):
            return
        conditions = [
            ma_gap <= -self.dip_threshold,
            latest["rsi"] <= self.rsi_oversold,
            latest["z_cvd"] >= self.cvd_z_entry,
            latest["ma_fast"] > latest["ma_slow"],
        ]
        if not all(conditions):
            return
        price = float(latest["close"])
        if price <= 0:
            return
        self._trigger_entry(price, latest, candle)

    # ------------------------------------------------------------------
    def _trigger_entry(self, price: float, latest_row: pd.Series, candle: Dict[str, Any]) -> None:
        if not self.queue_manager:
            self.logger.warning("Queue manager dependency missing; skip entry")
            return
        if self.risk_manager:
            ok, reason = self.risk_manager.check_circuit_breaker_before_new_order()
            if not ok:
                self.logger.warning("Risk breaker blocked order", reason=reason)
                return
        quantity = self._determine_quantity(price)
        if quantity <= 0:
            return
        exchange, sec_type = self._resolve_instrument(self.primary_symbol)
        tick_size = self._resolve_tick_size(self.primary_symbol)
        stop_price = price - self.stop_loss_ticks * tick_size if self.stop_loss_ticks > 0 else None
        take_profit = price + self.take_profit_ticks * tick_size if self.take_profit_ticks > 0 else None
        payload = {
            "strategy": self.name,
            "symbol": self.primary_symbol,
            "action": "BUY",
            "qty": quantity,
            "type": "MARKET",
            "price": float(price),
            "exchange": exchange,
            "sec_type": sec_type,
            "meta": {
                "stop_loss": stop_price,
                "take_profit": take_profit,
                "rsi": float(latest_row["rsi"]),
                "z_cvd": float(latest_row["z_cvd"]),
                "ma_fast": float(latest_row["ma_fast"]),
                "ma_slow": float(latest_row["ma_slow"]),
                "interval_end_ns": candle.get("interval_end_ns"),
            },
        }
        try:
            task_id = self.queue_manager.add_task("strategy_order", payload)
        except Exception as exc:
            self.logger.error("Failed to enqueue order: {}", exc)
            return
        self._last_signal_time = time.time()
        self._last_order_payload = payload
        self.logger.info(
            "Buy-the-dip order queued",
            task_id=task_id,
            price=price,
            qty=quantity,
            rsi=latest_row["rsi"],
            z_cvd=latest_row["z_cvd"],
        )

    # ------------------------------------------------------------------
    def _determine_quantity(self, price: float) -> int:
        return self.default_quantity

    # ------------------------------------------------------------------
    def _resolve_instrument(self, symbol: str) -> tuple[str, str]:
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
    def _resolve_tick_size(self, symbol: str) -> float:
        base = symbol.upper()
        if base in DEFAULT_FUTURE_TICKS:
            return DEFAULT_FUTURE_TICKS[base]
        for key, value in DEFAULT_FUTURE_TICKS.items():
            if base.startswith(key):
                return value
        return 0.01

    # ------------------------------------------------------------------
    @staticmethod
    def _compute_rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.fillna(50.0)
        return rsi.clip(0.0, 100.0)
