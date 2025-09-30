from __future__ import annotations

import json
import time
from datetime import datetime, timezone
import inspect
from typing import Any, Iterable, Mapping
from fastapi.testclient import TestClient

from services.market_data import MarketDataRuntime, create_app


class FakeRedis:
    def __init__(self) -> None:
        self.stream_entries: list[dict[str, Any]] = []

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
