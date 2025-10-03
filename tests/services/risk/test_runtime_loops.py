from __future__ import annotations

import json
from typing import Any

import pytest

from services.risk.engine import RiskEvent, RiskRule
from services.risk.runtime import RiskRuntime
from src.ibbot.redis import StreamMessage
from tests.services.risk.helpers import CapturingLogger, FakeRedis


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _create_runtime(redis: FakeRedis) -> RiskRuntime:
    async def factory() -> FakeRedis:
        return redis

    runtime = RiskRuntime(redis_factory=factory)
    runtime._redis = redis  # type: ignore[assignment]
    runtime._logger = CapturingLogger()
    await runtime.update_config({"rules": []})
    return runtime


async def test_market_iteration_updates_health_and_acknowledges() -> None:
    redis = FakeRedis()
    runtime = await _create_runtime(redis)

    payload = {"symbol": "ES", "payload": json.dumps({"last": 4500.25})}
    message = StreamMessage(stream=runtime.market_stream_key, message_id="1-0", data=payload)

    await runtime._process_stream_messages(  # type: ignore[attr-defined]
        loop_name="market",
        stream_key=runtime.market_stream_key,
        group=runtime.market_group,
        messages=[message],
        handler=runtime._handle_market_message,
        on_processed=runtime._set_market_last_processed,
    )

    assert redis.acked == [(runtime.market_stream_key, "1-0")]

    health_key = f"hash:service:health:{runtime.service_name}"
    metrics = redis.hashes[health_key]
    assert metrics["loop"] == "market"
    assert metrics["messages"] == "1"
    assert metrics["acknowledged"] == "1"
    assert metrics["state"] == "processed"
    assert runtime._market_last_processed is not None

    logger: CapturingLogger = runtime._logger  # type: ignore[assignment]
    assert any("acknowledged" in record["message"] for record in logger.records)
    assert any("published health metrics" in record["message"] for record in logger.records)


async def test_results_iteration_clears_pending_and_logs_metrics() -> None:
    redis = FakeRedis()
    runtime = await _create_runtime(redis)

    rule = RiskRule(symbol="ES")
    event = RiskEvent(symbol="ES", trigger="stop_loss", price=4000.0, rule=rule, context={})
    event.command_id = "cmd-1"
    runtime._pending_commands[event.command_id] = event

    message = StreamMessage(
        stream=runtime.results_stream_key,
        message_id="5-2",
        data={"command_id": event.command_id, "status": "success"},
    )

    await runtime._process_stream_messages(  # type: ignore[attr-defined]
        loop_name="results",
        stream_key=runtime.results_stream_key,
        group=runtime.results_group,
        messages=[message],
        handler=runtime._handle_result_message,
        on_processed=runtime._set_results_last_processed,
    )

    assert event.command_id not in runtime._pending_commands
    assert redis.acked == [(runtime.results_stream_key, "5-2")]

    health_key = f"hash:service:health:{runtime.service_name}"
    metrics = redis.hashes[health_key]
    assert metrics["loop"] == "results"
    assert metrics["acknowledged"] == "1"
    assert metrics["state"] == "processed"

    logger: CapturingLogger = runtime._logger  # type: ignore[assignment]
    assert any("published health metrics" in record["message"] for record in logger.records)


async def test_idle_iteration_records_idle_metrics() -> None:
    redis = FakeRedis()
    runtime = await _create_runtime(redis)

    await runtime._process_stream_messages(  # type: ignore[attr-defined]
        loop_name="market",
        stream_key=runtime.market_stream_key,
        group=runtime.market_group,
        messages=[],
        handler=runtime._handle_market_message,
        on_processed=runtime._set_market_last_processed,
    )

    health_key = f"hash:service:health:{runtime.service_name}"
    metrics = redis.hashes[health_key]
    assert metrics["loop"] == "market"
    assert metrics["state"] == "idle"
    assert metrics["messages"] == "0"

    logger: CapturingLogger = runtime._logger  # type: ignore[assignment]
    assert any("loop idle" in record["message"] for record in logger.records)


async def test_dispatch_event_logs_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()
    runtime = await _create_runtime(redis)

    recorded: list[dict[str, Any]] = []

    async def _fake_enqueue(command: dict[str, Any], *, redis: Any, queue_key: str) -> int:
        recorded.append(command)
        return 1

    monkeypatch.setattr("services.risk.runtime.enqueue_ib_command", _fake_enqueue)

    rule = RiskRule(symbol="ES")
    event = RiskEvent(symbol="ES", trigger="stop_loss", price=4000.0, rule=rule, context={})

    await runtime._dispatch_event(event)

    logger: CapturingLogger = runtime._logger  # type: ignore[assignment]
    assert any("Triggered stop_loss" in record["message"] for record in logger.records)
    assert recorded
    payload = recorded[0]
    assert payload["kwargs"]["symbol"] == "ES"
    assert payload["kwargs"]["reason"] == "risk_stop_loss"


async def test_handle_result_message_logs_info() -> None:
    redis = FakeRedis()
    runtime = await _create_runtime(redis)

    rule = RiskRule(symbol="ES")
    event = RiskEvent(symbol="ES", trigger="stop_loss", price=4000.0, rule=rule, context={})
    event.command_id = "cmd-123"
    runtime._pending_commands[event.command_id] = event

    handled = await runtime._handle_result_message({"command_id": "cmd-123", "status": "SUCCESS"})
    assert handled is True

    logger: CapturingLogger = runtime._logger  # type: ignore[assignment]
    assert any("Received result" in record["message"] for record in logger.records)
    extras = [record["extra"] for record in logger.records if "Received result" in record["message"]]
    assert extras and extras[0]["status"] == "success"
    assert event.command_id not in runtime._pending_commands
