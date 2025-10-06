from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

strategy_spec = importlib.util.spec_from_file_location(
    "services.strategy", ROOT / "services" / "strategy" / "__init__.py"
)
strategy_module = importlib.util.module_from_spec(strategy_spec)
assert strategy_spec.loader is not None
strategy_spec.loader.exec_module(strategy_module)
StrategyAdapter = strategy_module.StrategyAdapter

redis_spec = importlib.util.spec_from_file_location(
    "tests.services.redis_utils", ROOT / "tests" / "services" / "redis_utils.py"
)
redis_module = importlib.util.module_from_spec(redis_spec)
assert redis_spec.loader is not None
redis_spec.loader.exec_module(redis_module)
InMemoryRedis = redis_module.InMemoryRedis


class DemoStrategy:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = dict(config)

    async def run(
        self,
        *,
        context: Any,
        task: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        symbol = task["symbol"]
        price = await context.get_price(symbol)
        size = int(config.get("quantity", 1))
        context.emit_signal({"type": "price-check", "symbol": symbol, "price": price})
        return {
            "orders": [
                {
                    "command_id": f"{task['task_id']}-order",
                    "method": "placeOrder",
                    "args": [symbol, size, price],
                }
            ],
            "signals": [
                {"type": "decision", "symbol": symbol, "score": price * size},
            ],
        }


def demo_strategy_factory(config: Mapping[str, Any]) -> DemoStrategy:
    return DemoStrategy(config)


@pytest.fixture
def redis_client() -> InMemoryRedis:
    client = InMemoryRedis()
    asyncio.run(client.flushall())
    try:
        yield client
    finally:
        asyncio.run(client.flushall())
        asyncio.run(client.close())


def test_strategy_adapter_enqueues_orders_and_events(redis_client: InMemoryRedis) -> None:
    asyncio.run(_run_adapter_test(redis_client))


async def _run_adapter_test(redis_client: InMemoryRedis) -> None:
    await redis_client.hset("hash:market:snapshot:TEST", mapping={"price": "101.5"})

    adapter = StrategyAdapter(
        redis_client,
        strategies={
            "demo": {
                "strategy_type": demo_strategy_factory,
                "params": {"quantity": 2},
            }
        },
        command_queue="queue:test:commands",
        event_stream="stream:test:strategy",
    )

    task = {"task_id": "task-1", "strategy": "demo", "symbol": "TEST", "params": {"quantity": 3}}
    await adapter.execute_task(task)

    queued_orders = await redis_client.lrange("queue:test:commands", 0, -1)
    assert len(queued_orders) == 1
    order_payload = json.loads(queued_orders[0])
    assert order_payload["strategy"] == "demo"
    assert order_payload["task_id"] == "task-1"
    assert order_payload["args"][1] == 3

    stream_entries = await redis_client.xrange("stream:test:strategy")
    statuses = [entry[1]["status"] for entry in stream_entries]
    assert statuses == ["started", "completed"]

    completed_entry = stream_entries[-1][1]
    assert completed_entry["orders"] == "1"
    signals = json.loads(completed_entry["signals"])
    assert len(signals) == 2
    for signal in signals:
        assert signal["task_id"] == "task-1"
