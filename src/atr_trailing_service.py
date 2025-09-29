"""ATR-based trailing stop service implementation."""

from __future__ import annotations

import asyncio
import json
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional, Sequence

from loguru import logger

from .market_data_stream import PriceVolumeStream, RealTimeBar


atr_logger = logger.bind(module="atr_trailing", request_id="-")


@dataclass
class Bar:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class ATRTrailingParams:
    lookback: int = 14
    bar_minutes: int = 5
    stream_interval: int = 5
    update_throttle: float = 30.0
    multiplier_sl: float = 1.0
    multiplier_tp: float = 2.0
    delta_threshold: float = 0.25

    @classmethod
    def from_mapping(cls, payload: Optional[Dict]) -> "ATRTrailingParams":
        if not payload:
            return cls()
        base = cls()
        for key, value in payload.items():
            if hasattr(base, key) and value is not None:
                setattr(base, key, float(value) if isinstance(getattr(base, key), float) else int(value))
        return base

    def to_json(self) -> str:
        return json.dumps({
            "lookback": self.lookback,
            "bar_minutes": self.bar_minutes,
            "stream_interval": self.stream_interval,
            "update_throttle": self.update_throttle,
            "multiplier_sl": self.multiplier_sl,
            "multiplier_tp": self.multiplier_tp,
            "delta_threshold": self.delta_threshold,
        })


class FiveMinuteBarBuilder:
    """Aggregate 5-second bars into configurable windows."""

    def __init__(self, *, window_minutes: int = 5, lookback: int = 32) -> None:
        self.window = timedelta(minutes=window_minutes)
        self.completed: Deque[Bar] = deque(maxlen=lookback)
        self.active: Optional[Bar] = None
        self._last_close: Optional[float] = None

    def _floor_start(self, when: datetime) -> datetime:
        total_seconds = int(when.timestamp())
        window_seconds = int(self.window.total_seconds())
        floored = total_seconds - (total_seconds % window_seconds)
        return datetime.fromtimestamp(floored, tz=when.tzinfo)

    def add_bar(self, bar: RealTimeBar) -> List[Bar]:
        completed: List[Bar] = []
        start = self._floor_start(bar.time)

        if self.active is None:
            self.active = Bar(
                start=start,
                end=start + self.window,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            self._last_close = bar.close
            return completed

        while bar.time >= self.active.end:
            completed.append(self.active)
            self.completed.append(self.active)
            self._last_close = self.active.close

            gap_start = self.active.end
            if bar.time >= gap_start + self.window:
                # Fill gaps with flat bars using the previous close.
                while bar.time >= gap_start + self.window:
                    filler = Bar(
                        start=gap_start,
                        end=gap_start + self.window,
                        open=self._last_close,
                        high=self._last_close,
                        low=self._last_close,
                        close=self._last_close,
                        volume=0.0,
                    )
                    completed.append(filler)
                    self.completed.append(filler)
                    gap_start = filler.end
            new_start = self._floor_start(bar.time)
            self.active = Bar(
                start=new_start,
                end=new_start + self.window,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            return completed

        # Merge bar into active window
        self.active.high = max(self.active.high, bar.high)
        self.active.low = min(self.active.low, bar.low)
        self.active.close = bar.close
        self.active.volume += bar.volume
        self._last_close = bar.close
        return completed

    def snapshot(self) -> List[Bar]:
        data = list(self.completed)
        if self.active:
            data.append(self.active)
        return data


def compute_atr(bars: Sequence[Bar], length: int = 14) -> Optional[float]:
    if len(bars) < length:
        return None

    window = list(bars)[-length:]
    true_ranges: List[float] = []
    for idx, bar in enumerate(window):
        if idx == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        prev_close = window[idx - 1].close
        tr = max(
            bar.high - bar.low,
            abs(bar.high - prev_close),
            abs(bar.low - prev_close),
        )
        true_ranges.append(tr)

    atr = true_ranges[0]
    for tr in true_ranges[1:]:
        atr = (atr * (length - 1) + tr) / length
    return atr


@dataclass
class _SymbolState:
    params: ATRTrailingParams
    builder: FiveMinuteBarBuilder
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    position: Optional[Dict] = None
    rule: Optional["RiskRule"] = None
    last_atr: Optional[float] = None
    last_price: Optional[float] = None
    last_rule_price: Optional[float] = None
    last_rule_update: Optional[datetime] = None


class ATRTrailingService:
    def __init__(
        self,
        *,
        price_stream: PriceVolumeStream,
        risk_manager,
        notification_manager,
    ) -> None:
        self.price_stream = price_stream
        self.risk_manager = risk_manager
        self.notification_manager = notification_manager
        self._states: Dict[str, _SymbolState] = {}
        self._stream_callback = self._handle_stream_bar

    @staticmethod
    def _is_trailing_rule(rule) -> bool:
        rule_type = getattr(rule, "rule_type", None)
        if not rule_type:
            if getattr(rule, "auto_trailing", False):
                rule_type = "trailing"
        return rule_type == "trailing"

    def _resolve_params(self, rule) -> ATRTrailingParams:
        payload = getattr(rule, "atr_params", None)
        params = ATRTrailingParams.from_mapping(payload)
        return params

    def ensure_subscription(self, symbol: str, rule) -> Optional[_SymbolState]:
        if not self._is_trailing_rule(rule):
            self.disable_symbol(symbol)
            return None

        params = self._resolve_params(rule)
        state = self._states.get(symbol)
        if state is None or state.params.lookback != params.lookback or state.params.bar_minutes != params.bar_minutes:
            builder = FiveMinuteBarBuilder(window_minutes=params.bar_minutes, lookback=max(params.lookback * 2, 32))
            state = _SymbolState(params=params, builder=builder)
            state.rule = rule
            self._states[symbol] = state
            self.price_stream.subscribe(symbol, self._stream_callback, interval_seconds=params.stream_interval)
        else:
            state.params = params
            state.rule = rule
        return state

    async def refresh_for_symbol(self, symbol: str, position: Dict, rule) -> None:
        state = self.ensure_subscription(symbol, rule)
        if state is None:
            return

        async with state.lock:
            state.position = position
            state.rule = rule

        if position.get("position", 0) == 0:
            return

        if state.last_atr and state.last_price is not None:
            await self._maybe_update(symbol, state, state.last_price, reason="position-refresh", event_time=datetime.utcnow())

    async def _handle_stream_bar(self, symbol: str, bar: RealTimeBar) -> None:
        state = self._states.get(symbol)
        if state is None or not self._is_trailing_rule(state.rule):
            return
        async with state.lock:
            completed = state.builder.add_bar(bar)
            state.last_price = bar.close

            if completed:
                atr = compute_atr(state.builder.completed, length=state.params.lookback)
                if atr is not None and not math.isnan(atr):
                    state.last_atr = atr
                    await self._maybe_update(symbol, state, bar.close, reason="bar-close", event_time=bar.time)
                return

            if state.last_atr and state.last_rule_price is not None:
                if abs(bar.close - state.last_rule_price) >= state.params.delta_threshold * state.last_atr:
                    await self._maybe_update(symbol, state, bar.close, reason="price-move", event_time=bar.time)

    async def _maybe_update(
        self,
        symbol: str,
        state: _SymbolState,
        price: float,
        *,
        reason: str,
        event_time: datetime,
    ) -> None:
        if state.position is None or state.rule is None:
            return
        if not self._is_trailing_rule(state.rule):
            return
        qty = state.position.get("position", 0)
        if qty == 0 or not state.rule.enabled:
            return

        if state.last_atr is None or state.last_atr <= 0:
            return

        if state.last_rule_update is not None:
            delta = (event_time - state.last_rule_update).total_seconds()
            if delta < state.params.update_throttle:
                return

        atr_value = state.last_atr
        if qty > 0:
            stop_loss = price - state.params.multiplier_sl * atr_value
            take_profit = price + state.params.multiplier_tp * atr_value
        else:
            stop_loss = price + state.params.multiplier_sl * atr_value
            take_profit = price - state.params.multiplier_tp * atr_value

        updated = False
        if state.rule.stop_loss_price is None or abs(state.rule.stop_loss_price - stop_loss) > 1e-6:
            updated = True
        if state.rule.take_profit_price is None or abs(state.rule.take_profit_price - take_profit) > 1e-6:
            updated = True

        if not updated:
            return

        atr_logger.info(
            f"Updating risk rule for {symbol}: price={price:.2f}, ATR={atr_value:.4f}, reason={reason}"
        )
        self.risk_manager.update_risk_rule(
            symbol,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            enabled=True,
        )
        state.last_rule_price = price
        state.last_rule_update = event_time

    def disable_symbol(self, symbol: str) -> None:
        state = self._states.pop(symbol, None)
        if state is None:
            return
        self.price_stream.unsubscribe(symbol, self._stream_callback)


__all__ = [
    "ATRTrailingService",
    "Bar",
    "FiveMinuteBarBuilder",
    "ATRTrailingParams",
    "compute_atr",
]

