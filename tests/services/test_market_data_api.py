from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
import inspect
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from services.market_data import MarketDataRuntime, create_app
from src.data_layer import bootstrap
from src.data_layer.cache import RedisCacheBackend
from src.data_layer.data_source_manager import DataSubscriptionRequest
from src.data_layer.quote_feed import InMemoryQuoteFeed
from src.ibbot.redis import DEFAULT_REDIS_URL
from src.utils import to_local_iso
import pytest


class FakeRedis:
    def __init__(self) -> None:
        self.stream_entries: list[dict[str, Any]] = []
        self.series: dict[str, list[str]] = {}
        self.fail_lrange = False
        self.hashes: dict[str, dict[str, str]] = {}

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

    async def hset(self, key: str, *, mapping: Mapping[str, Any]) -> int:
        values = self.hashes.setdefault(key, {})
        for field, value in mapping.items():
            values[str(field)] = str(value)
        return len(mapping)

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

        health = redis.hashes.get("hash:service:health:market-data") or {}
        assert health.get("status") == "running"
        assert "price_updates" in health


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


def test_chart_data_endpoint_returns_candles(monkeypatch: pytest.MonkeyPatch) -> None:
    try:  # pragma: no cover - prefer fakeredis when available
        import fakeredis  # type: ignore
    except Exception:  # pragma: no cover - fallback stub when fakeredis missing
        fakeredis = None  # type: ignore

    try:  # pragma: no cover - prefer fakeredis when available
        import fakeredis.aioredis as fakeredis_async  # type: ignore
    except Exception:  # pragma: no cover - fallback stub when fakeredis missing
        fakeredis_async = None  # type: ignore

    if fakeredis is not None and fakeredis_async is not None:
        server = fakeredis.FakeServer()
        sync_client = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
        async_client = fakeredis_async.FakeRedis(server=server, decode_responses=True)
    else:
        class _SharedRedisStore:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}
                self.lists: dict[str, list[str]] = {}
                self.expiry: dict[str, int] = {}

        shared_store = _SharedRedisStore()

        class _SyncRedisPipeline:
            def __init__(self, client: "_SyncRedisClient") -> None:
                self._client = client
                self._ops: list[tuple[str, tuple[Any, ...]]] = []

            def set(self, key: str, value: str) -> "_SyncRedisPipeline":
                self._ops.append(("set", (key, value)))
                return self

            def expire(self, key: str, ttl: int) -> "_SyncRedisPipeline":
                self._ops.append(("expire", (key, ttl)))
                return self

            def rpush(self, key: str, value: str) -> "_SyncRedisPipeline":
                self._ops.append(("rpush", (key, value)))
                return self

            def ltrim(self, key: str, start: int, end: int) -> "_SyncRedisPipeline":
                self._ops.append(("ltrim", (key, start, end)))
                return self

            def execute(self) -> list[Any]:
                results: list[Any] = []
                for name, args in self._ops:
                    method = getattr(self._client, name)
                    results.append(method(*args))
                self._ops.clear()
                return results

        class _SyncRedisClient:
            def __init__(self, store: _SharedRedisStore) -> None:
                self._store = store

            def pipeline(self) -> _SyncRedisPipeline:
                return _SyncRedisPipeline(self)

            def set(self, key: str, value: str) -> bool:
                self._store.values[key] = value
                return True

            def expire(self, key: str, ttl: int) -> bool:
                self._store.expiry[key] = ttl
                return True

            def rpush(self, key: str, value: str) -> int:
                series = self._store.lists.setdefault(key, [])
                series.append(value)
                return len(series)

            def ltrim(self, key: str, start: int, end: int) -> None:
                series = self._store.lists.get(key)
                if series is None:
                    return
                length = len(series)
                if length == 0:
                    return
                if start < 0:
                    start += length
                if end < 0:
                    end += length
                start = max(start, 0)
                end = min(end, length - 1)
                if end < start:
                    self._store.lists[key] = []
                else:
                    self._store.lists[key] = series[start : end + 1]

            def lrange(self, key: str, start: int, end: int) -> list[str]:
                series = list(self._store.lists.get(key, []))
                length = len(series)
                if length == 0:
                    return []
                if start < 0:
                    start += length
                if end < 0:
                    end += length
                start = max(start, 0)
                end = min(end, length - 1)
                if end < start:
                    return []
                return series[start : end + 1]

            def get(self, key: str) -> str | None:
                return self._store.values.get(key)

        class _AsyncRedisClient:
            def __init__(self, store: _SharedRedisStore) -> None:
                self._store = store

            async def lrange(self, key: str, start: int, end: int) -> list[str]:
                sync = _SyncRedisClient(self._store)
                return sync.lrange(key, start, end)

        sync_client = _SyncRedisClient(shared_store)
        async_client = _AsyncRedisClient(shared_store)

    class _FakeRedisCacheBackend(RedisCacheBackend):
        def __init__(self, url: str, *, prefix: str = "market-data", snapshot_ttl: int | None = None) -> None:
            assert url == DEFAULT_REDIS_URL
            self._client = sync_client
            self._prefix = prefix.rstrip(":")
            self._snapshot_ttl = snapshot_ttl

    feed = InMemoryQuoteFeed()

    monkeypatch.setattr(bootstrap, "RedisCacheBackend", _FakeRedisCacheBackend)
    monkeypatch.setattr(bootstrap, "resolve_default_quote_feed", lambda: feed)
    monkeypatch.setattr(bootstrap, "_cache_backend", None)
    monkeypatch.setattr(bootstrap, "_manager", None)
    monkeypatch.setattr(bootstrap, "_quote_feed", None)

    monkeypatch.delenv("DATA_LAYER_REDIS_URL", raising=False)
    monkeypatch.delenv("IBBOT_REDIS_URL", raising=False)
    monkeypatch.setenv("DATA_LAYER_REDIS_PREFIX", "market-data")
    monkeypatch.setenv("DATA_LAYER_CANDLE_INTERVALS", "1s,5m")
    monkeypatch.setenv("DATA_LAYER_CANDLE_HISTORY_LIMIT", "10")

    async def _produce_candles() -> None:
        manager = bootstrap.get_data_source_manager()
        request = DataSubscriptionRequest(channel="market.tick", symbol="AAPL")
        token = await manager.subscribe(request)
        try:
            now_ns = time.time_ns()
            await feed.publish_tick(
                "AAPL",
                {"last": 100.0, "bid": 99.5, "ask": 100.5, "volume": 1.0, "ts_ns": now_ns},
            )
            await asyncio.sleep(0)
            await feed.publish_tick(
                "AAPL",
                {
                    "last": 101.0,
                    "bid": 100.5,
                    "ask": 101.5,
                    "volume": 1.5,
                    "ts_ns": now_ns + 1_000_000_000,
                },
            )
            await asyncio.sleep(0)
        finally:
            await manager.unsubscribe(token)

    asyncio.run(_produce_candles())

    series_key = "market-data:series:market.candle.1s:AAPL"
    stored_entries = sync_client.lrange(series_key, 0, -1)
    assert stored_entries, "Expected candle series to be written to Redis"
    latest_1s = json.loads(stored_entries[-1])
    assert latest_1s["interval"] == "1s"

    series_key_5m = "market-data:series:market.candle.5m:AAPL"
    stored_entries_5m = sync_client.lrange(series_key_5m, 0, -1)
    assert stored_entries_5m, "Expected 5m candle series to be written to Redis"
    latest_5m = json.loads(stored_entries_5m[-1])
    assert latest_5m["interval"] == "5m"

    async def _redis_factory() -> Any:
        return async_client

    runtime = MarketDataRuntime(redis_factory=_redis_factory, pubsub_listener=FakePubSubListener(), subscriptions=[])

    with _start_client(runtime) as client:
        response = client.post(
            "/chart_data",
            json={
                "symbol": "AAPL",
                "duration": "1 D",
                "barSize": "1 sec",
                "exchange": "SMART",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["symbol"] == "AAPL"
        assert payload["data"], "Expected chart data to be returned"

        response_5m = client.post(
            "/chart_data",
            json={
                "symbol": "AAPL",
                "duration": "1 D",
                "barSize": "5 min",
                "exchange": "SMART",
            },
        )
        assert response_5m.status_code == 200
        payload_5m = response_5m.json()
        assert payload_5m["success"] is True
        assert payload_5m["symbol"] == "AAPL"
        assert payload_5m["data"], "Expected 5m chart data to be returned"


def test_unknown_symbol_returns_404() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["CL"])

    with _start_client(runtime) as client:
        response = client.get("/market/NQ")
        assert response.status_code == 404
        assert response.json()["detail"] == "Symbol not found"


def test_current_price_endpoint_returns_snapshot() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["MSFT"])

    with _start_client(runtime) as client:
        _emit_price(runtime, "MSFT", last=330.55, bid=330.4, ask=330.7)
        response = client.get("/market/current_price", params={"symbol": "msft"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["symbol"] == "MSFT"
        assert payload["price"] == 330.55
        assert payload["timestamp"]
        assert payload["source"] == "market_data_service"
        assert payload["data"]["bid"] == 330.4


def test_current_price_endpoint_handles_missing_snapshot() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["MSFT"])

    with _start_client(runtime) as client:
        response = client.get("/market/current_price", params={"symbol": "UNKNOWN"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is False
        assert payload["symbol"] == "UNKNOWN"
        assert payload["price"] is None
        assert payload["error"] == "No market data available"


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


def test_market_historical_returns_series_payload() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["ES"])

    interval = "1m"
    symbol = "ES"
    cache_key = f"market-data:series:market.candle.{interval}:{symbol}"
    redis.set_series(
        cache_key,
        [
            {
                "interval_end_ns": int(datetime.now(timezone.utc).timestamp() * 1_000_000_000),
                "open": 100,
                "high": 110,
                "low": 90,
                "close": 95,
                "volume": 500,
            }
        ],
    )

    with _start_client(runtime) as client:
        response = client.post(
            "/market/historical",
            json={
                "symbol": symbol,
                "duration": "1 D",
                "barSize": "1 min",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["rows"] == 1
        assert payload["symbol"] == symbol
        assert payload["metadata"]["interval"] == interval
        assert payload["data"][0]["open"] == 100.0


def test_market_historical_handles_missing_data() -> None:
    redis = FakeRedis()
    writer = RecordingWriter()
    runtime = _build_runtime(redis, writer, symbols=["ES"])

    with _start_client(runtime) as client:
        response = client.post(
            "/market/historical",
            json={
                "symbol": "ES",
                "duration": "1 D",
                "barSize": "1 min",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is False
        assert payload["data"] == []
        assert payload["rows"] == 0
        assert payload["error"] == "No chart data available"
