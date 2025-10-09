from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

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
StrategyTaskConsumer = strategy_module.StrategyTaskConsumer

redis_spec = importlib.util.spec_from_file_location(
    "tests.services.redis_utils", ROOT / "tests" / "services" / "redis_utils.py"
)
redis_module = importlib.util.module_from_spec(redis_spec)
assert redis_spec.loader is not None
redis_spec.loader.exec_module(redis_module)
InMemoryRedis = redis_module.InMemoryRedis


class FlowStrategy:
    def __init__(self, config: dict[str, str]) -> None:
        self._config = config

    async def run(self, *, context, task, config):  # type: ignore[no-untyped-def]
        symbol = task["symbol"]
        price = await context.get_price(symbol)
        size = int(config.get("size", 1))
        context.emit_signal({"type": "entry", "symbol": symbol, "price": price})
        return {
            "orders": [
                {
                    "contract": {
                        "symbol": symbol,
                        "exchange": "SMART",
                        "currency": "USD",
                    },
                    "order": {
                        "action": "BUY",
                        "total_quantity": size,
                        "order_type": "LIMIT",
                        "lmt_price": float(price),
                    },
                }
            ],
        }


def flow_strategy_factory(config: dict[str, str]) -> FlowStrategy:
    return FlowStrategy(config)


class StubOrdersClient:
    def __init__(self) -> None:
        self.requests: list[Mapping[str, Any]] = []

    async def place_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.requests.append(dict(payload))
        return {"order_id": f"order-{len(self.requests)}", "status": "accepted"}


@pytest.fixture
def redis_client() -> InMemoryRedis:
    client = InMemoryRedis()
    asyncio.run(client.flushall())
    try:
        yield client
    finally:
        asyncio.run(client.flushall())
        asyncio.run(client.close())


def test_strategy_task_to_command_to_receipt(redis_client: InMemoryRedis) -> None:
    asyncio.run(_run_flow_test(redis_client))


async def _run_flow_test(redis_client: InMemoryRedis) -> None:
    await redis_client.hset("hash:market:snapshot:ES", mapping={"price": "4300.0"})

    orders_client = StubOrdersClient()
    adapter = StrategyAdapter(
        redis_client,
        strategies={
            "flow": {
                "strategy_type": flow_strategy_factory,
                "params": {"size": 2},
            }
        },
        command_queue="queue:test:commands",
        event_stream="stream:strategy:events",
        orders_client=orders_client,
    )
    consumer = StrategyTaskConsumer(
        redis_client,
        adapter,
        queue_key="list:queue:strategy",
        poll_timeout=0.05,
    )

    consumer_task = asyncio.create_task(consumer.run_forever())

    try:
        task_payload = {"task_id": "job-1", "strategy": "flow", "symbol": "ES"}
        await redis_client.rpush("list:queue:strategy", json.dumps(task_payload))

        strategy_events = await redis_client.xrange("stream:strategy:events")
        assert [entry[1]["status"] for entry in strategy_events] == ["started", "completed"]
        assert strategy_events[-1][1]["orders"] == "1"

        assert len(orders_client.requests) == 1
        placed = orders_client.requests[0]
        assert placed["contract"]["symbol"] == "ES"
        assert placed["order"]["total_quantity"] == 2
    finally:
        consumer.stop()
        await asyncio.sleep(0.1)
        await consumer_task
