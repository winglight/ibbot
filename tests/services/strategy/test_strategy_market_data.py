import asyncio
import json
from typing import Any, Mapping

import pytest

from services.strategy import StrategyAdapter
from tests.services.redis_utils import InMemoryRedis


class StreamingStrategy:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = dict(config)
        self._symbol = str(self._config.get("symbol", "ES"))
        self._threshold = float(self._config.get("threshold", 0))

    async def run(self, *, context, task, config):  # type: ignore[no-untyped-def]
        async for tick in context.stream_quotes(self._symbol):
            price = float(tick.get("value", 0))
            context.emit_signal({"type": "tick", "symbol": self._symbol, "price": price})
            if price >= self._threshold:
                return {
                    "orders": [
                        {
                            "contract": {
                                "symbol": self._symbol,
                                "exchange": "SMART",
                                "currency": "USD",
                            },
                            "order": {
                                "action": "BUY",
                                "total_quantity": int(config.get("size", 1)),
                                "order_type": "LIMIT",
                                "lmt_price": price,
                            },
                        }
                    ]
                }


def streaming_strategy_factory(config: Mapping[str, Any]) -> StreamingStrategy:
    return StreamingStrategy(config)


class StubOrdersClient:
    def __init__(self) -> None:
        self.requests: list[Mapping[str, Any]] = []

    async def place_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.requests.append(dict(payload))
        return {"order_id": f"order-{len(self.requests)}", "status": "accepted"}


@pytest.mark.asyncio
async def test_strategy_consumes_market_data_stream() -> None:
    redis_client = InMemoryRedis()
    try:
        orders_client = StubOrdersClient()
        adapter = StrategyAdapter(
            redis_client,
            strategies={
                "stream": {
                    "strategy_type": streaming_strategy_factory,
                    "params": {
                        "symbol": "ES",
                        "threshold": 4300.5,
                        "size": 1,
                        "symbol_to_channel_map": {"ES": {"quotes": "md:quotes:ES"}},
                        "required_channels": ["md:quotes:ES"],
                        "latency_budget_ms": 2000,
                    },
                }
            },
            command_queue="queue:unused",
            event_stream="stream:strategy:events",
            orders_client=orders_client,
        )

        task_payload = {"task_id": "tick-task", "strategy": "stream", "symbol": "ES"}
        execute_task = asyncio.create_task(adapter.execute_task(task_payload))

        await asyncio.sleep(0.05)
        tick_payload = {"contract_id": 1, "tick_type": "LAST", "value": "4300.75"}
        await redis_client.publish("md:quotes:ES", json.dumps(tick_payload))

        await execute_task

        assert len(orders_client.requests) == 1
        submitted_order = orders_client.requests[0]
        assert submitted_order["contract"]["symbol"] == "ES"
        assert submitted_order["order"]["total_quantity"] == 1
        metadata = submitted_order["order"].get("metadata")
        assert metadata and metadata["task_id"] == "tick-task"

        entries = await redis_client.xrange("stream:strategy:events")
        assert entries[-1][1]["orders"] == "1"
        signals = json.loads(entries[-1][1]["signals"])
        assert any(signal["type"] == "tick" for signal in signals)
    finally:
        await redis_client.close()


class DefaultStreamingStrategy:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = dict(config)

    async def run(self, *, context, task, config):  # type: ignore[no-untyped-def]
        symbol = str(self._config.get("symbol", task.get("symbol", "ES")))
        context.emit_signal({"type": "delayed-flag", "value": context.use_delayed_data})
        async for tick in context.stream_quotes(symbol):
            price = float(tick.get("value", 0))
            if price >= float(self._config.get("threshold", 0)):
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
                                "total_quantity": int(config.get("size", 1)),
                                "order_type": "LIMIT",
                                "lmt_price": price,
                            },
                        }
                    ]
                }


def default_streaming_strategy_factory(config: Mapping[str, Any]) -> DefaultStreamingStrategy:
    return DefaultStreamingStrategy(config)


@pytest.mark.asyncio
async def test_strategy_uses_default_channel_configuration() -> None:
    redis_client = InMemoryRedis()
    try:
        orders_client = StubOrdersClient()
        adapter = StrategyAdapter(
            redis_client,
            strategies={
                "stream": {
                    "strategy_type": default_streaming_strategy_factory,
                    "params": {
                        "symbol": "ES",
                        "threshold": 4300.25,
                        "size": 2,
                    },
                }
            },
            command_queue="queue:unused",
            event_stream="stream:strategy:events",
            orders_client=orders_client,
            default_channel_map={"ES": {"quotes": "md:quotes:ES"}},
            default_required_channels=("md:quotes:ES",),
            default_latency_budget_ms=1500,
            use_delayed_data=True,
        )

        task_payload = {"task_id": "defaults", "strategy": "stream", "symbol": "ES"}
        execute_task = asyncio.create_task(adapter.execute_task(task_payload))

        await asyncio.sleep(0.05)
        tick_payload = {"contract_id": 2, "tick_type": "LAST", "value": "4300.50"}
        await redis_client.publish("md:quotes:ES", json.dumps(tick_payload))

        await execute_task

        assert len(orders_client.requests) == 1
        request = orders_client.requests[0]
        assert request["contract"]["symbol"] == "ES"
        assert request["order"]["total_quantity"] == 2

        entries = await redis_client.xrange("stream:strategy:events")
        completed = entries[-1][1]
        signals = json.loads(completed["signals"])
        assert any(signal.get("type") == "delayed-flag" and signal.get("value") is True for signal in signals)
    finally:
        await redis_client.close()
