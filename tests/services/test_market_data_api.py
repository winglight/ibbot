from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
import inspect
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from services.market_data import MarketDataRuntime, create_app
from src.utils import to_local_iso


class FakeRedis:
    def __init__(self) -> None:
        self.stream_entries: list[dict[str, Any]] = []
        self.series: dict[str, list[str]] = {}
        self.fail_lrange = False

    async def xadd(
        self,
        key: str,
        fields: Mapping[str, Any],
        *,
        id: str | None = None,
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        entry = {
            "key": key,
            "fields": dict(fields),
            "id": id or "*",
            "maxlen": maxlen,
            "approximate": approximate,
        }
        self.stream_entries.append(entry)
        return "1-0"

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        if self.fail_lrange:
            raise RuntimeError("lrange failure")
        series = self.series.get(key, [])
        length = len(series)
        if length == 0:
            return []

        def _resolve(index: int) -> int:
            if index < 0:
                index += length
            return max(index, 0)

        if end < 0:
            end += length
        start_idx = _resolve(start)
        end_idx = min(max(end, -1), length - 1)
        if end_idx < start_idx:
            return []
        return series[start_idx : end_idx + 1]

    def set_series(self, key: str, values: Iterable[Mapping[str, Any]]) -> None:
        payloads: list[str] = []
        for value in values:
            if isinstance(value, str):
                payloads.append(value)
            else:
                payloads.append(json.dumps(value))
        self.series[key] = payloads


class RecordingWriter:
    def __init__(self) -> None:
        self.points: list[dict[str, Any]] = []

    def __call__(self, points: Iterable[Any]) -> int:
        batch: list[dict[str, Any]] = []
        for point in points:
            if hasattr(point, "as_dict"):
                batch.append(point.as_dict())
            elif isinstance(point, Mapping):
                batch.append(dict(point))
            else:
                raise TypeError(f"Unsupported point type: {type(point)!r}")
        self.points.extend(batch)
        return len(batch)


class FakePubSubListener:
    def __init__(self) -> None:
        self.callbacks: dict[str, list[Any]] = {}
        self.started = False

    def register_callback(self, channel: str, callback: Any) -> None:
        self.callbacks.setdefault(channel, []).append(callback)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def emit(self, channel: str, payload: str) -> None:
        for callback in self.callbacks.get(channel, []):
            result = callback(channel, payload)
            if inspect.isawaitable(result):
                await result


def _build_runtime(redis: FakeRedis, writer: RecordingWriter, *, symbols: list[str]) -> MarketDataRuntime:
    async def factory() -> FakeRedis:
        return redis

    listener = FakePubSubListener()
    runtime = MarketDataRuntime(
        redis_factory=factory,
        write_fn=writer,
        subscriptions=symbols,
        metrics_interval=0.05,
        pubsub_listener=listener,
    )
    return runtime


def _start_client(runtime: MarketDataRuntime) -> TestClient:
    app = create_app(runtime=runtime)
    return TestClient(app)


def _emit_price(runtime: MarketDataRuntime, symbol: str, **fields: Any) -> None:
    timestamp = fields.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    payload = {**fields, "timestamp": timestamp}
    future = runtime.emit_price(symbol, payload)
    future.result(timeout=1.0)


def _emit_dom(runtime: MarketDataRuntime, symbol: str, **fields: Any) -> None:
    timestamp = fields.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    payload = {**fields, "timestamp": timestamp}
    future = runtime.emit_dom(symbol, payload)
    future.result(timeout=1.0)


def test_market_snapshot_available_via_rest() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["AAPL"])

    with _start_client(runtime) as client:
        _emit_price(runtime, "AAPL", bid=199.5, ask=200.5, last=200.1, bid_size=10, ask_size=11)
        response = client.get("/market/AAPL")
        assert response.status_code == 200
        payload = response.json()
        assert payload["symbol"] == "AAPL"
        assert payload["price"]["bid"] == 199.5
        assert payload["updated_at"]

        assert redis.stream_entries
        last_entry = redis.stream_entries[-1]
        assert last_entry["key"] == "stream:market-service"
        fields = last_entry["fields"]
        assert fields["type"] == "price"
        parsed = json.loads(fields["payload"])
        assert parsed["symbol"] == "AAPL"

        time.sleep(0.1)
        metrics = [point for point in writer.points if point.get("measurement") == "service_metrics"]
        assert metrics, "Expected service metrics to be written"


def test_market_websocket_broadcasts_updates() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["ES"])

    with _start_client(runtime) as client:
        with client.websocket_connect("/ws/market") as websocket:
            initial = websocket.receive_json()
            assert initial["type"] == "snapshot"
            assert initial["data"] == []

            _emit_dom(runtime, "ES", bids=[{"level": 0, "price": 4300.25, "size": 5}], asks=[])
            update = websocket.receive_json()
            assert update["symbol"] == "ES"
            assert update["type"] == "dom"
            snapshot = update["data"]
            assert snapshot["symbol"] == "ES"
            assert snapshot["dom"]["bids"][0]["price"] == 4300.25


def test_unknown_symbol_returns_404() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["CL"])

    with _start_client(runtime) as client:
        response = client.get("/market/NQ")
        assert response.status_code == 404
        assert response.json()["detail"] == "Symbol not found"


def test_chart_data_returns_cached_series() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["ES"])

    interval = "1m"
    symbol = "ES"
    cache_key = f"market-data:series:market.candle.{interval}:{symbol}"
    ny_tz = ZoneInfo("America/New_York")
    now_ny = datetime.now(ny_tz)
    base_time = now_ny - timedelta(hours=1)
    recent_ns = int(base_time.timestamp() * 1_000_000_000)
    old_ns = int((now_ny - timedelta(days=2)).timestamp() * 1_000_000_000)
    redis.set_series(
        cache_key,
        [
            {
                "interval_end_ns": old_ns,
                "open": 100,
                "high": 110,
                "low": 90,
                "close": 95,
                "volume": 500,
            },
            {
                "interval_end_ns": recent_ns,
                "open": "200",
                "high": 210,
                "low": 195,
                "close": 205,
                "volume": 1234.5,
            },
        ],
    )

    expected_time = to_local_iso(base_time)

    with _start_client(runtime) as client:
        response = client.post(
            "/chart_data",
            json={
                "symbol": symbol,
                "duration": "1 D",
                "bar_size": "1 min",
                "exchange": "CME",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["symbol"] == symbol
        assert payload["duration"] == "1 D"
        assert payload["bar_size"] == "1 min"
        data = payload["data"]
        assert len(data) == 1
        bar = data[0]
        assert bar["time"] == expected_time
        assert bar["open"] == 200.0
        assert bar["high"] == 210.0
        assert bar["low"] == 195.0
        assert bar["close"] == 205.0
        assert bar["volume"] == 1234


def test_chart_data_returns_404_when_series_missing() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["ES"])

    with _start_client(runtime) as client:
        response = client.post(
            "/chart_data",
            json={
                "symbol": "ES",
                "duration": "1 D",
                "bar_size": "1 min",
            },
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "No chart data available"


def test_chart_data_returns_503_on_backend_error() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["ES"])
    redis.fail_lrange = True

    with _start_client(runtime) as client:
        response = client.post(
            "/chart_data",
            json={
                "symbol": "ES",
                "duration": "1 D",
                "bar_size": "1 min",
            },
        )

        assert response.status_code == 503
        assert response.json()["detail"] == "Market data cache unavailable"
