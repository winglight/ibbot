import asyncio
import importlib
import inspect
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping, MutableMapping

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from services.ib_command import CommandConsumer, IBCommandExecutor, ResultPublisher
from services.ib_command.components import ExecutionResult
from services.market_data.main import MarketDataRuntime
from services.optimizer.main import (
    EVENT_STREAM_KEY,
    RESULT_KEY_TEMPLATE,
    OptimizerService,
    create_app as create_optimizer_app,
)
from services.risk import RiskRuntime
from services.strategy import StrategyAdapter, StrategyTaskConsumer

from src.routes import orders as orders_module


class InMemoryRedis:
    """Minimal async Redis clone for end-to-end testing."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._message_counter = 0
        self._groups: dict[tuple[str, str], int] = {}

    async def rpush(self, key: str, value: str) -> int:
        queue = self._queues.setdefault(key, asyncio.Queue())
        await queue.put(value)
        return queue.qsize()

    async def blpop(
        self,
        key: str,
        timeout: float | int | None = 0,
    ) -> tuple[str, str] | None:
        queue = self._queues.setdefault(key, asyncio.Queue())
        if timeout is None:
            value = await queue.get()
            return key, value

        try:
            value = await asyncio.wait_for(queue.get(), timeout=float(timeout))
        except asyncio.TimeoutError:
            return None
        return key, value

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> str:
        self._message_counter += 1
        message_id = f"{self._message_counter}-0"
        stream = self._streams.setdefault(key, [])
        stream.append((message_id, dict(fields)))
        if maxlen is not None and maxlen > 0:
            limit = maxlen if not approximate else maxlen + 1
            if len(stream) > limit:
                del stream[:-maxlen]
        return message_id

    async def xrange(self, key: str) -> list[tuple[str, dict[str, str]]]:
        return list(self._streams.get(key, []))

    async def xgroup_create(
        self,
        stream: str,
        group: str,
        *,
        id: str = "0",
        mkstream: bool = False,
    ) -> None:
        if mkstream:
            self._streams.setdefault(stream, [])
        self._groups[(stream, group)] = len(self._streams.get(stream, []))

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        for stream, _ in streams.items():
            entries = self._streams.get(stream, [])
            index = self._groups.get((stream, groupname), 0)
            if index >= len(entries):
                continue
            batch = entries[index:]
            if count is not None:
                batch = batch[:count]
            if not batch:
                continue
            self._groups[(stream, groupname)] = index + len(batch)
            results.append((stream, [(message_id, dict(payload)) for message_id, payload in batch]))
        if not results and block:
            await asyncio.sleep(block / 1000.0)
        return results

    async def xack(self, stream: str, group: str, *message_ids: str) -> int:
        return len(message_ids)

    async def hset(self, key: str, *, mapping: dict[str, Any]) -> int:
        normalised = {str(k): str(v) for k, v in mapping.items()}
        current = self._hashes.setdefault(key, {})
        current.update(normalised)
        return len(normalised)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))


class DummyWebSocketManager:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


def test_websocket_handshake_with_mock_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.routes import websocket as websocket_module

    app = FastAPI()
    app.include_router(websocket_module.router)

    class _DummyConnectionManager:
        def __init__(self) -> None:
            self.connected = False
            self.config = SimpleNamespace(paper_trading=True)

        def is_connected(self) -> bool:
            return self.connected

        def get_last_connect_error(self) -> str | None:
            return None

    class _DummyWebSocketManager:
        async def connect(self, websocket):
            await websocket.accept()

        def disconnect(self, websocket) -> None:  # pragma: no cover - noop
            return None

    class _DummyIBClient:
        async def get_account_info(self) -> dict[str, Any]:
            return {"account": "test"}

        async def get_positions(self, *, fast: bool = True) -> list[dict[str, Any]]:
            return []

    dummy_conn = _DummyConnectionManager()
    dummy_ws_manager = _DummyWebSocketManager()

    monkeypatch.setattr(websocket_module, "_connection_manager", lambda: dummy_conn)
    monkeypatch.setattr(websocket_module, "_websocket_manager", lambda: dummy_ws_manager)
    monkeypatch.setattr(websocket_module, "get_dom_service", lambda: None)
    monkeypatch.setattr(websocket_module.core, "ibkr_client", _DummyIBClient(), raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            initial = websocket.receive_json()

        assert initial["type"] == "connection_status"
        assert initial["connected"] is False
        assert initial["account_type"] == "Paper"
        assert initial.get("error_code") is None


class RecordingResultPublisher(ResultPublisher):
    def __init__(
        self,
        redis: InMemoryRedis,
        *,
        websocket_manager: DummyWebSocketManager,
        stream_key: str,
        hash_prefix: str,
    ) -> None:
        super().__init__(
            redis,
            stream_key=stream_key,
            hash_prefix=hash_prefix,
            logger_name="tests.ib_command.results",
        )
        self._websocket_manager = websocket_manager
        self.records: list[MutableMapping[str, str]] = []
        self.publications: list[ExecutionResult] = []
        self.broadcasts: list[dict[str, Any]] = []
        self._last_message: MutableMapping[str, str] | None = None

    def _to_message(self, payload: ExecutionResult) -> MutableMapping[str, str]:  # type: ignore[override]
        message = super()._to_message(payload)
        self._last_message = dict(message)
        return message

    async def publish(self, payload: ExecutionResult) -> None:  # type: ignore[override]
        await super().publish(payload)
        if self._last_message is not None:
            self.records.append(dict(self._last_message))
        self.publications.append(payload)
        broadcast_payload = {
            "type": "order_command_result",
            "command_id": payload.command_id,
            "status": payload.status,
            "attempts": payload.attempts,
        }
        self.broadcasts.append(broadcast_payload)
        await self._websocket_manager.broadcast(broadcast_payload)


class FakeIB:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def place_market_order(
        self,
        *,
        symbol: str,
        quantity: int,
        action: str,
        exchange: str,
        sec_type: str,
    ) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "quantity": quantity,
            "action": action,
            "exchange": exchange,
            "sec_type": sec_type,
        }
        self.calls.append(payload)
        return {
            "status": "filled",
            "filled": quantity,
            "symbol": symbol,
        }

    async def submit_risk_close(
        self,
        *,
        symbol: str,
        reason: str,
        price: float,
    ) -> dict[str, Any]:
        payload = {
            "method": "submit_risk_close",
            "symbol": symbol,
            "reason": reason,
            "price": price,
        }
        self.calls.append(payload)
        return {
            "status": "submitted",
            "symbol": symbol,
            "reason": reason,
            "price": price,
        }


class DummyPubSubListener:
    def __init__(self) -> None:
        self._callbacks: dict[str, list[Any]] = {}

    def register_callback(self, channel: str, callback: Any) -> None:
        self._callbacks.setdefault(channel, []).append(callback)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def emit(self, channel: str, payload: str) -> None:
        for callback in self._callbacks.get(channel, []):
            result = callback(channel, payload)
            if inspect.isawaitable(result):
                await result


class RecordingSnapshotRepository:
    def __init__(self) -> None:
        self.config_snapshots: list[dict[str, Any]] = []
        self.rule_snapshots: list[dict[str, Any]] = []

    async def save_config_snapshot(
        self,
        *,
        service: str,
        payload: Mapping[str, Any],
        rules_count: int,
        updated_at: str | None = None,
        captured_at: str | datetime | None = None,
    ) -> None:
        self.config_snapshots.append(
            {
                "service": service,
                "payload": dict(payload),
                "rules_count": rules_count,
                "updated_at": updated_at,
                "captured_at": captured_at,
            }
        )

    async def bulk_upsert_rule_snapshots(
        self,
        *,
        service: str,
        rules: Mapping[str, Mapping[str, Any]],
        updated_at: str | None = None,
        captured_at: str | datetime | None = None,
    ) -> None:
        self.rule_snapshots.append(
            {
                "service": service,
                "rules": {symbol: dict(payload) for symbol, payload in rules.items()},
                "updated_at": updated_at,
                "captured_at": captured_at,
            }
        )


class RecordingMetricsRepository:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.closed = False

    async def record_metrics(self, metrics: Any) -> None:
        if isinstance(metrics, dict):
            batch = [dict(metrics)]
        else:
            batch = [dict(entry) for entry in metrics]
        self.records.extend(batch)

    async def close(self) -> None:
        self.closed = True


async def _wait_for_result(
    redis: InMemoryRedis,
    hash_prefix: str,
    command_id: str,
    *,
    timeout: float = 2.0,
) -> dict[str, str]:
    deadline = asyncio.get_event_loop().time() + timeout
    key = f"{hash_prefix}:{command_id}"
    while True:
        payload = await redis.hgetall(key)
        if payload.get("status"):
            return payload
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"Result for {command_id} not available")
        await asyncio.sleep(0.05)


async def _wait_for_messages(
    manager: DummyWebSocketManager,
    expected_count: int,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if len(manager.messages) >= expected_count:
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError("Websocket messages not emitted in time")
        await asyncio.sleep(0.05)


async def _wait_for_stream_entries(
    redis: InMemoryRedis,
    stream_key: str,
    expected_count: int,
    *,
    timeout: float = 2.0,
) -> list[tuple[str, dict[str, str]]]:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        entries = await redis.xrange(stream_key)
        if len(entries) >= expected_count:
            return entries
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"Stream {stream_key} did not reach {expected_count} entries")
        await asyncio.sleep(0.05)


def test_order_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_run_order_flow(monkeypatch))


async def _run_order_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket_manager = DummyWebSocketManager()

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    async def noop(*_: Any, **__: Any) -> None:
        return None

    class StubOrdersClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(dict(payload))
            return {"command_id": "cmd-test-001", "status": "accepted"}

        async def list_orders(self, **_: Any) -> dict[str, Any]:
            return {"orders": []}

    client_stub = StubOrdersClient()

    app = FastAPI()
    app.include_router(orders_module.router)
    app.dependency_overrides[orders_module.get_current_user] = allow_user
    app.dependency_overrides[orders_module.get_orders_service_client] = lambda: client_stub
    monkeypatch.setattr(orders_module, "audit_log_success", noop)
    monkeypatch.setattr(orders_module, "audit_log_failure", noop)
    monkeypatch.setattr(
        orders_module,
        "enqueue_ib_command",
        lambda payload, queue_key=None: asyncio.sleep(0),
    )
    orders_module.set_websocket_broadcast_override(websocket_manager.broadcast)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/place_order",
            json={
                "symbol": "ES",
                "quantity": 1,
                "action": "BUY",
                "order_type": "market",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"
        assert body["success"] is True
        assert body["command_id"]
    assert body["status"] in {"accepted", "queued"}

    assert websocket_manager.messages[0]["type"] == "order_submitted"
    assert websocket_manager.messages[0]["command_id"] == body["command_id"]


def test_market_strategy_risk_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_run_market_strategy_risk_flow(monkeypatch))


async def _run_market_strategy_risk_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = InMemoryRedis()
    websocket_manager = DummyWebSocketManager()
    fake_ib = FakeIB()
    pubsub = DummyPubSubListener()
    metrics_repo = RecordingMetricsRepository()
    snapshot_repo = RecordingSnapshotRepository()

    queue_key = "queue:ib:commands"
    result_stream = "stream:ib:results"
    result_hash_prefix = "hash:ib:result"
    market_stream = "stream:market-service"
    strategy_queue = "list:queue:strategy"
    strategy_event_stream = "stream:strategy:events"

    async def redis_factory() -> InMemoryRedis:
        return redis

    market_runtime = MarketDataRuntime(
        redis_factory=redis_factory,
        pubsub_listener=pubsub,
        subscriptions=("ES",),
        stream_key=market_stream,
        service_name="market-data-service-test",
        logger_name="tests.market_data.runtime",
        metrics_interval=0.1,
        metrics_repository_factory=lambda: metrics_repo,
    )

    original_append_stream = MarketDataRuntime._append_stream

    async def _patched_append_stream(
        self: MarketDataRuntime,
        symbol: str,
        message_type: str,
        components: Mapping[str, Any],
    ) -> None:
        updated_components = components
        if message_type == "price":
            price_data = components.get("price") if isinstance(components, Mapping) else None
            price_value: float | None = None
            if isinstance(price_data, Mapping):
                raw_value = price_data.get("price") or price_data.get("last")
                if raw_value is not None:
                    try:
                        price_value = float(raw_value)
                    except (TypeError, ValueError):
                        price_value = None
            if price_value is not None:
                mutable = dict(components)
                mutable["last"] = price_value
                updated_components = mutable
        await original_append_stream(self, symbol, message_type, updated_components)

    monkeypatch.setattr(MarketDataRuntime, "_append_stream", _patched_append_stream)

    class ThresholdStrategy:
        def __init__(self, config: dict[str, Any]) -> None:
            self._threshold = float(config.get("threshold", 0.0))
            self._size = int(config.get("size", 1))
            self._action = str(config.get("action", "BUY"))
            self._exchange = str(config.get("exchange", "CME"))
            self._sec_type = str(config.get("sec_type", "FUT"))

        async def run(self, *, context: Any, task: Mapping[str, Any], config: Mapping[str, Any]) -> Mapping[str, Any]:
            symbol = str(task.get("symbol"))
            price = await context.get_price(symbol)
            if price > self._threshold:
                return {}
            context.emit_signal({"type": "entry", "symbol": symbol, "price": price})
            order_payload = {
                "command_id": f"{task['task_id']}-order",
                "method": "place_market_order",
                "kwargs": {
                    "symbol": symbol,
                    "quantity": self._size,
                    "action": self._action,
                    "exchange": self._exchange,
                    "sec_type": self._sec_type,
                },
            }
            return {"orders": [order_payload]}

    strategies = {
        "threshold": {
            "factory": lambda config: ThresholdStrategy(config),
            "params": {
                "threshold": 95.0,
                "size": 1,
                "action": "BUY",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        }
    }

    strategy_adapter = StrategyAdapter(
        redis,
        strategies=strategies,
        command_queue=queue_key,
        event_stream=strategy_event_stream,
        snapshot_prefix="hash:market:snapshot",
    )
    strategy_consumer = StrategyTaskConsumer(
        redis,
        strategy_adapter,
        queue_key=strategy_queue,
        poll_timeout=0.05,
    )

    async def _risk_repository_factory() -> Any:
        return snapshot_repo

    risk_runtime = RiskRuntime(
        redis_factory=redis_factory,
        risk_snapshot_repository_factory=_risk_repository_factory,
        logger_name="tests.risk-service",
        service_name="tests.risk-service",
        poll_interval=0.05,
    )
    risk_config = {
        "rules": json.dumps([{"symbol": "ES", "stop_loss": 95.0}]),
        "market_stream": market_stream,
        "market_group": "group:risk:market",
        "results_stream": result_stream,
        "results_group": "group:risk:results",
        "command_queue": queue_key,
        "batch_size": 5,
    }
    await risk_runtime.update_config(risk_config)

    executor = IBCommandExecutor(fake_ib, logger_name="tests.ib_command.executor")
    publisher = RecordingResultPublisher(
        redis,
        websocket_manager=websocket_manager,
        stream_key=result_stream,
        hash_prefix=result_hash_prefix,
    )
    command_consumer = CommandConsumer(
        redis,
        executor,
        publisher,
        queue_key=queue_key,
        poll_timeout=0.05,
        default_max_retries=0,
        retry_delay=0.01,
        health_key="hash:service:health:e2e",
        logger_name="tests.ib_command.consumer",
    )

    await market_runtime.start()
    consumer_task = asyncio.create_task(command_consumer.consume_forever())
    strategy_task = asyncio.create_task(strategy_consumer.run_forever())
    await risk_runtime.start()

    try:
        await redis.hset("hash:market:snapshot:ES", mapping={"price": "94.5"})

        price_channel = "pubsub:ib:price:ES"
        await pubsub.emit(price_channel, json.dumps({"symbol": "ES", "price": 94.5}))

        task_payload = {"task_id": "job-1", "strategy": "threshold", "symbol": "ES"}
        await redis.rpush(strategy_queue, json.dumps(task_payload))

        market_entries = await _wait_for_stream_entries(redis, market_stream, 1)
        assert market_entries[0][1]["symbol"] == "ES"
        assert market_entries[0][1]["type"] == "price"

        strategy_events = await _wait_for_stream_entries(redis, strategy_event_stream, 2)
        assert [event[1]["status"] for event in strategy_events] == ["started", "completed"]
        signals_payload = json.loads(strategy_events[-1][1]["signals"])
        assert signals_payload and signals_payload[0]["type"] == "entry"

        await _wait_for_result(redis, result_hash_prefix, "job-1-order")
        result_entries = await _wait_for_stream_entries(redis, result_stream, 2)
        result_by_id = {entry[1]["command_id"]: entry[1] for entry in result_entries}
        assert "job-1-order" in result_by_id

        risk_entry = None
        for entry in result_entries:
            payload = entry[1]
            body_text = payload.get("result")
            data = json.loads(body_text) if body_text else {}
            if data.get("reason") == "risk_stop_loss":
                risk_entry = payload
                break

        assert risk_entry is not None, "Risk result was not published"
        risk_command_id = risk_entry["command_id"]
        risk_result_hash = await _wait_for_result(redis, result_hash_prefix, risk_command_id)
        assert risk_result_hash["status"] == "success"

        assert any(call.get("reason") == "risk_stop_loss" for call in fake_ib.calls)

        assert len(result_entries) == 2
        await _wait_for_messages(websocket_manager, 2)
        assert publisher.records
        statuses = {record["status"] for record in publisher.records}
        assert statuses == {"success"}
        published_ids = [record["command_id"] for record in publisher.records]
        assert {"job-1-order", risk_command_id} <= set(published_ids)
        broadcast_ids = {message["command_id"] for message in publisher.broadcasts}
        assert broadcast_ids == set(published_ids)
    finally:
        command_consumer.stop()
        strategy_consumer.stop()
        await asyncio.wait_for(consumer_task, timeout=2.0)
        await asyncio.wait_for(strategy_task, timeout=2.0)
        await risk_runtime.stop()
        await market_runtime.stop()
        assert metrics_repo.closed
        assert metrics_repo.records
        assert publisher.publications


def test_optimizer_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_run_optimizer_flow(monkeypatch))


async def _run_optimizer_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = InMemoryRedis()

    async def fake_db_query(_: str) -> list[dict[str, Any]]:
        return [
            {"timestamp": "2023-01-01T00:00:00Z", "value": 1.2},
            {"timestamp": "2023-01-01T00:01:00Z", "value": 0.8},
            {"timestamp": "2023-01-01T00:02:00Z", "value": 1.0},
        ]

    service = OptimizerService(redis_factory=lambda: redis, db_query=fake_db_query)

    optimizer_module = importlib.import_module("services.optimizer.main")
    monkeypatch.setattr(optimizer_module, "load_project_env", lambda: None)
    monkeypatch.setattr(optimizer_module, "ensure_optimizer_ui_dir", lambda: None)

    strategy_id = "demo-strategy"
    await redis.hset(
        f"hash:optimizer:strategy:{strategy_id}:config",
        mapping={
            "name": json.dumps("demo"),
            "risk": json.dumps("medium"),
            "features": json.dumps({"alpha": 0.2, "beta": 0.8}),
        },
    )
    await redis.hset(
        f"hash:optimizer:strategy:{strategy_id}:params",
        mapping={
            "lookback": json.dumps([60, 120, 240]),
            "threshold": json.dumps([0.1, 0.15, 0.2]),
        },
    )
    await redis.hset(
        f"hash:optimizer:strategy:{strategy_id}:history",
        mapping={
            "sharpe": json.dumps(0.85),
            "runs": json.dumps(12),
        },
    )

    app = create_optimizer_app(service=service)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/optimizer/tasks",
            json={
                "task_id": "opt-task-1",
                "strategy_id": strategy_id,
                "training_query": "SELECT value FROM optimizer_signals",
            },
        )

        assert response.status_code == 200
        assert response.json() == {"task_id": "opt-task-1", "status": "accepted"}

        progress_entries = await redis.xrange(EVENT_STREAM_KEY)
        assert [entry[1]["type"] for entry in progress_entries] == [
            "task_submitted",
            "task_progress",
            "task_completed",
        ]
        assert [entry[1]["progress"] for entry in progress_entries] == ["0", "50", "100"]

        stored_result = await redis.hgetall(RESULT_KEY_TEMPLATE.format(task_id="opt-task-1"))
        assert stored_result["status"] == "completed"
        assert json.loads(stored_result["best_params"]) == {
            "lookback": 60,
            "threshold": 0.1,
        }
        metrics = json.loads(stored_result["metrics"])
        assert metrics["baseline_sharpe"] == pytest.approx(0.85)
        assert metrics["score"] == pytest.approx(1.0)
        assert metrics["config"]["risk"] == "medium"
        assert stored_result["samples"] == "3"

        result_response = await client.get("/api/optimizer/tasks/opt-task-1/result")
        assert result_response.status_code == 200
        result_payload = result_response.json()
        assert result_payload["task_id"] == "opt-task-1"
        assert result_payload["status"] == "completed"
        assert result_payload["best_params"] == {
            "lookback": 60,
            "threshold": 0.1,
        }
        assert result_payload["metrics"]["score"] == pytest.approx(1.0)
        assert result_payload["metrics"]["baseline_sharpe"] == pytest.approx(0.85)
        assert result_payload["metrics"]["config"]["features"] == {"alpha": 0.2, "beta": 0.8}
        assert result_payload["samples"] == 3
