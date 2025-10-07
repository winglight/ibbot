from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pytest

from services.market_data import main as market_data_main
from src.ibkr_client import IBKRConnectionError


class _StubDataFrame:
    def __init__(self, end_dt: datetime | None):
        self._end_dt = end_dt

    def to_dict(self, orient: str) -> list[dict[str, Any]]:  # pragma: no cover - simple container
        timestamp_ns = 0
        if self._end_dt is not None:
            timestamp_ns = int(self._end_dt.timestamp() * 1_000_000_000)
        return [
            {
                "interval_end_ns": timestamp_ns,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 0,
            }
        ]


class _StubClient:
    connected = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_historical_data(
        self,
        symbol: str,
        *,
        duration: str,
        bar_size: str,
        exchange: str,
        end_datetime: datetime | None,
    ) -> _StubDataFrame:
        self.calls.append(
            {
                "symbol": symbol,
                "duration": duration,
                "bar_size": bar_size,
                "exchange": exchange,
                "end_datetime": end_datetime,
            }
        )
        return _StubDataFrame(end_datetime)


@pytest.mark.asyncio
async def test_fetch_candles_from_ib_splits_large_gap(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    stub_client = _StubClient()

    async def fake_ensure_ib_client(logger: Any) -> _StubClient:  # pragma: no cover - patched in test
        return stub_client

    monkeypatch.setattr(market_data_main, "_ensure_ib_client", fake_ensure_ib_client)
    monkeypatch.setattr(market_data_main, "_bars_to_candles", lambda records, **_: records)
    monkeypatch.setattr(market_data_main, "_normalise_candles", lambda candles: candles)
    monkeypatch.setattr(market_data_main, "IB_FALLBACK_ENABLED", True)

    persisted: list[list[dict[str, Any]]] = []

    async def fake_persist(candles: list[dict[str, Any]]) -> None:
        persisted.append(list(candles))

    query = market_data_main.ChartDataQuery(
        symbol="ES", duration="3 D", bar_size="1 min", exchange="SMART"
    )

    gap_end = datetime.now(tz=market_data_main.NY_TZ)
    gap_start = gap_end - timedelta(hours=48)
    missing_segments = [
        (
            int(gap_start.timestamp() * 1_000_000_000),
            int(gap_end.timestamp() * 1_000_000_000),
        )
    ]

    result = await market_data_main._fetch_candles_from_ib(
        query,
        interval="1m",
        persist=fake_persist,
        logger=logging.getLogger("test_fetch_candles_ib"),
        missing_segments=missing_segments,
    )

    durations = [call["duration"] for call in stub_client.calls]

    assert len(stub_client.calls) == 2
    assert durations == ["86400 S", "86400 S"]
    assert all(int(duration.split()[0]) <= market_data_main.MAX_IB_DURATION_SECONDS for duration in durations)

    end_times = [call["end_datetime"] for call in stub_client.calls]
    assert end_times[0] == gap_end
    assert end_times[1] == gap_end - timedelta(seconds=market_data_main.MAX_IB_DURATION_SECONDS)

    assert len(result) == 2
    assert persisted and len(persisted[0]) == 2

    debug_messages = [record.message for record in caplog.records if record.levelno == logging.DEBUG]
    assert any("IB fallback gap" in message for message in debug_messages)


class _FailingClient:
    connected = True

    def __init__(self) -> None:
        self.calls = 0

    async def get_historical_data(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise IBKRConnectionError("connection lost")


@pytest.mark.asyncio
async def test_fetch_candles_from_ib_aborts_on_connection_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    failing_client = _FailingClient()

    async def fake_ensure_ib_client(logger: Any) -> _FailingClient:  # pragma: no cover - patched
        return failing_client

    monkeypatch.setattr(market_data_main, "_ensure_ib_client", fake_ensure_ib_client)
    monkeypatch.setattr(market_data_main, "IB_FALLBACK_ENABLED", True)

    async def fake_persist(_: list[dict[str, Any]]) -> None:
        raise AssertionError("persist should not be called")

    query = market_data_main.ChartDataQuery(
        symbol="ES",
        duration="1 D",
        bar_size="1 min",
        exchange="SMART",
    )

    missing_segments = [
        (0, 60 * 1_000_000_000),
        (60 * 1_000_000_000, 120 * 1_000_000_000),
    ]

    result = await market_data_main._fetch_candles_from_ib(
        query,
        interval="1m",
        persist=fake_persist,
        logger=logging.getLogger("test_connection_loss_abort"),
        missing_segments=missing_segments,
    )

    assert result == []
    assert failing_client.calls == 1
