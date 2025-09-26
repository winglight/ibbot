import asyncio
from datetime import datetime, timedelta

import pytest

from src.atr_trailing_service import (
    ATRTrailingService,
    FiveMinuteBarBuilder,
    Bar,
    compute_atr,
)
from src.market_data_stream import PriceVolumeStream, RealTimeBar
from src.risk_manager import RiskRule


def _make_window(start: datetime, open_, high_, low_, close_):
    bars = []
    current = start
    price = open_
    for i in range(12):
        this_open = price if i else open_
        this_close = close_ if i == 11 else this_open
        this_high = max(high_, this_open, this_close)
        this_low = min(low_, this_open, this_close)
        bars.append(
            RealTimeBar(
                time=current,
                open=this_open,
                high=this_high,
                low=this_low,
                close=this_close,
                volume=1.0,
            )
        )
        price = this_close
        current += timedelta(seconds=5)
    return bars


def test_compute_atr_wilder_formula():
    start = datetime(2024, 1, 1, 0, 0)
    bars = [
        Bar(start=start, end=start + timedelta(minutes=5), open=100, high=101, low=99, close=100, volume=10),
        Bar(start=start + timedelta(minutes=5), end=start + timedelta(minutes=10), open=100, high=102, low=98, close=101, volume=10),
        Bar(start=start + timedelta(minutes=10), end=start + timedelta(minutes=15), open=101, high=104, low=100, close=103, volume=10),
    ]

    atr = compute_atr(bars, length=3)
    assert atr is not None
    # Wilder smoothing result ~3.1111
    assert pytest.approx(3.1111, rel=1e-3) == atr


def test_builder_fills_missing_windows():
    builder = FiveMinuteBarBuilder(window_minutes=5, lookback=10)
    start = datetime(2024, 1, 1, 0, 0)
    first_window = _make_window(start, 100, 101, 99, 100)
    second_window_first_bar = RealTimeBar(
        time=start + timedelta(minutes=10),
        open=102,
        high=103,
        low=101,
        close=102,
        volume=1.0,
    )

    for bar in first_window:
        builder.add_bar(bar)

    completed = builder.add_bar(second_window_first_bar)
    assert len(completed) == 2  # first bar + filler for the gap
    filler = completed[1]
    assert filler.volume == 0.0
    assert filler.open == completed[0].close


class _FakeNotificationManager:
    async def send_risk_notification(self, *args, **kwargs):
        return None

    async def send_stop_loss_notification(self, *args, **kwargs):
        return None


class _FakeRiskManager:
    def __init__(self, rule: RiskRule):
        self.risk_rules = {rule.symbol: rule}
        self.updates = []
        self.updated = asyncio.Event()

    def update_risk_rule(self, symbol, **kwargs):
        rule = self.risk_rules[symbol]
        if "stop_loss_price" in kwargs:
            rule.stop_loss_price = kwargs["stop_loss_price"]
        if "take_profit_price" in kwargs:
            rule.take_profit_price = kwargs["take_profit_price"]
        if "enabled" in kwargs and kwargs["enabled"] is not None:
            rule.enabled = kwargs["enabled"]
        self.updates.append(kwargs)
        self.updated.set()
        return True


def test_atr_service_updates_risk_rule():
    async def _run():
        start = datetime(2024, 1, 1, 9, 30)
        bars = []
        bars.extend(_make_window(start, 100, 101, 99, 100))
        bars.extend(_make_window(start + timedelta(minutes=5), 100, 102, 98, 101))
        bars.extend(_make_window(start + timedelta(minutes=10), 101, 104, 100, 103))
        bars.append(
            RealTimeBar(
                time=start + timedelta(minutes=15),
                open=103,
                high=105,
                low=102,
                close=103,
                volume=1.0,
            )
        )

        params = {
            "lookback": 3,
            "stream_interval": 5,
            "update_throttle": 0,
            "multiplier_sl": 1.0,
            "multiplier_tp": 2.0,
        }
        rule = RiskRule(symbol="MNQ", enabled=True, auto_trailing=True, atr_params=params)
        stream = PriceVolumeStream()
        fake_rm = _FakeRiskManager(rule)
        service = ATRTrailingService(price_stream=stream, risk_manager=fake_rm, notification_manager=_FakeNotificationManager())

        position = {"symbol": "MNQ", "position": 1, "current_price": 100.0}
        await service.refresh_for_symbol("MNQ", position, rule)

        for bar in bars:
            await stream.publish_bar("MNQ", bar)

        await asyncio.wait_for(fake_rm.updated.wait(), timeout=1.0)
        last_update = fake_rm.updates[-1]
        assert pytest.approx(99.8889, rel=1e-3) == last_update["stop_loss_price"]
        assert pytest.approx(109.2222, rel=1e-3) == last_update["take_profit_price"]

    asyncio.run(_run())
