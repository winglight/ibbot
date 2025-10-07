import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

import pytest
from fastapi.testclient import TestClient

from services.market_data import create_app, MarketDataRuntime
from services.market_data import main as market_module
from src.utils import NY_TZ


class DummyRedis:
    def __init__(self) -> None:
        self.series: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.series.get(key, [])
        if not values:
            return []
        length = len(values)

        def _resolve_index(index: int) -> int:
            if index < 0:
                index += length
            return max(min(index, length - 1), 0)

        start_idx = _resolve_index(start)
        end_idx = _resolve_index(end)
        if end_idx < start_idx:
            return []
        return values[start_idx : end_idx + 1]

    async def hset(self, key: str, *, mapping: Mapping[str, Any]) -> int:
        stored = self.hashes.setdefault(key, {})
        for field, value in mapping.items():
            stored[str(field)] = str(value)
        return len(mapping)

    async def xadd(
        self,
        key: str,
        fields: Mapping[str, Any],
        *,
        id: str | None = None,
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        return id or "1-0"

    def set_series(self, key: str, values: Iterable[Mapping[str, Any]]) -> None:
        payloads: list[str] = []
        for value in values:
            if isinstance(value, str):
                payloads.append(value)
            else:
                payloads.append(json.dumps(value))
        self.series[key] = payloads


class DummyListener:
    def register_callback(self, channel: str, callback: Any) -> None:  # pragma: no cover - interface stub
        return None

    async def start(self) -> None:  # pragma: no cover - interface stub
        return None

    async def stop(self) -> None:  # pragma: no cover - interface stub
        return None


class DummyMetricsRepository:
    async def record_metrics(self, metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> None:  # pragma: no cover - noop
        return None

    async def close(self) -> None:  # pragma: no cover - noop
        return None


def test_chart_data_skips_ib_when_no_missing_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    ib_called = False

    async def fake_fetch(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal ib_called
        ib_called = True
        return []

    monkeypatch.setattr(market_module, "_fetch_candles_from_ib", fake_fetch)

    class NullParquetWriter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def read_candles(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(market_module, "MarketDataParquetWriter", NullParquetWriter)

    redis = DummyRedis()
    cache_key = "market-data:series:market.candle.1m:M2K"
    now = datetime.now(NY_TZ).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=5)
    candle = {
        "interval": "1m",
        "interval_start_ns": int(start.timestamp() * 1_000_000_000),
        "interval_end_ns": int(now.timestamp() * 1_000_000_000),
        "open": 100.0,
        "high": 101.0,
        "low": 99.5,
        "close": 100.5,
        "volume": 1,
    }
    redis.set_series(cache_key, [candle])

    async def redis_factory() -> DummyRedis:
        return redis

    runtime = MarketDataRuntime(
        redis_factory=redis_factory,
        subscriptions=(),
        metrics_interval=3600.0,
        pubsub_listener=DummyListener(),
        metrics_repository_factory=lambda: DummyMetricsRepository(),
    )

    app = create_app(runtime=runtime)

    payload = {
        "symbol": "M2K",
        "duration": "5 m",
        "barSize": "1 min",
        "exchange": "SMART",
    }

    with TestClient(app) as client:
        response = client.post("/chart_data", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["symbol"] == "M2K"
        assert isinstance(body["data"], list)

    assert ib_called is False
