from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pytest

from services.market_data import main as market_data_main


class _RecordingRequester:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        symbol: str,
        duration: str,
        bar_size: str,
        exchange: str,
        end_datetime: datetime | str | None,
        redis_client: Any | None,
        logger: Any,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "symbol": symbol,
                "duration": duration,
                "bar_size": bar_size,
                "exchange": exchange,
                "end_datetime": end_datetime,
            }
        )
        ts_ns = 0
        if isinstance(end_datetime, str) and end_datetime:
            try:
                parsed = datetime.fromisoformat(end_datetime)
            except Exception:  # pragma: no cover - defensive guard
                parsed = None
            if parsed is not None:
                ts_ns = int(parsed.timestamp() * 1_000_000_000)
        return [
            {
                "interval_end_ns": ts_ns,
                "open": float(len(self.calls)),
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 0,
            }
        ]


@pytest.mark.asyncio
async def test_fetch_candles_from_ib_splits_large_gap(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    requester = _RecordingRequester()
    monkeypatch.setattr(market_data_main, "_request_ib_historical_records", requester)
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
        redis_client=None,
        persist=fake_persist,
        logger=logging.getLogger("test_fetch_candles_ib"),
        missing_segments=missing_segments,
    )

    durations = [call["duration"] for call in requester.calls]

    assert len(requester.calls) == 2
    assert durations == ["86400 S", "86400 S"]
    assert all(int(duration.split()[0]) <= market_data_main.MAX_IB_DURATION_SECONDS for duration in durations)

    parsed_end_times = []
    for call in requester.calls:
        raw_end = call["end_datetime"]
        if raw_end is None:
            parsed_end_times.append(None)
            continue
        parsed_end_times.append(datetime.fromisoformat(str(raw_end)))

    assert parsed_end_times[0] == gap_end
    assert parsed_end_times[1] == gap_end - timedelta(seconds=market_data_main.MAX_IB_DURATION_SECONDS)

    assert len(result) == 2
    assert persisted and len(persisted[0]) == 2
    assert persisted[0] == result

    debug_messages = [record.message for record in caplog.records if record.levelno == logging.DEBUG]
    assert any("IB fallback gap" in message for message in debug_messages)


@pytest.mark.asyncio
async def test_fetch_candles_from_ib_aborts_on_connection_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def failing_requester(**kwargs: Any) -> list[dict[str, Any]] | None:
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(market_data_main, "_request_ib_historical_records", failing_requester)
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
        redis_client=None,
        persist=fake_persist,
        logger=logging.getLogger("test_connection_loss_abort"),
        missing_segments=missing_segments,
    )

    assert result == []
    assert calls == 1
