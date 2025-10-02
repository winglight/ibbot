from __future__ import annotations

import asyncio
import json
from typing import Any

from httpx import ASGITransport, AsyncClient

from services.orders.main import OrdersRuntime, create_app
from tests.services.redis_utils import InMemoryRedis


async def _create_runtime() -> tuple[OrdersRuntime, InMemoryRedis]:
    redis = InMemoryRedis()

    async def factory() -> InMemoryRedis:
        return redis

    runtime = OrdersRuntime(
        redis_factory=factory,
        stream_poll_interval=0.0,
        health_interval=0.0,
    )
    await runtime.start()
    return runtime, redis


def test_order_submission_persists_snapshot_and_enqueues() -> None:
    async def _scenario() -> None:
        runtime, redis = await _create_runtime()
        try:
            app = create_app(runtime, enable_events=False)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/orders",
                    json={
                        "symbol": "ES",
                        "quantity": 1,
                        "action": "BUY",
                        "order_type": "MARKET",
                    },
                )
            assert response.status_code == 202
            command_id = response.json()["command_id"]
            assert command_id

            queue_length = await redis.llen(runtime.command_queue)
            assert queue_length == 1

            snapshot_raw = await redis.get(runtime.snapshot_key)
            assert snapshot_raw is not None
            snapshot = json.loads(snapshot_raw)
            assert snapshot["orders"][0]["command_id"] == command_id

            history = await redis.lrange(runtime.history_key, 0, -1)
            assert history
            history_entry = json.loads(history[0])
            assert history_entry["event"] == "submitted"
            assert history_entry["command_id"] == command_id
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_cancel_order_enqueues_command() -> None:
    async def _scenario() -> None:
        runtime, redis = await _create_runtime()
        try:
            app = create_app(runtime, enable_events=False)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                submit = await client.post(
                    "/orders",
                    json={"symbol": "NQ", "quantity": 2, "action": "SELL", "order_type": "LIMIT"},
                )
                order_id = submit.json()["command_id"]
                cancel = await client.post(f"/orders/{order_id}/cancel")
            assert cancel.status_code == 202
            cancel_body = cancel.json()
            assert cancel_body["command_id"]
            assert cancel_body["command_id"] != order_id

            queue_length = await redis.llen(runtime.command_queue)
            assert queue_length == 2

            history_entries = await redis.lrange(runtime.history_key, 0, -1)
            events = [json.loads(entry)["event"] for entry in history_entries]
            assert "cancel_order" in events
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_stream_updates_remove_filled_orders() -> None:
    async def _scenario() -> None:
        runtime, redis = await _create_runtime()
        try:
            app = create_app(runtime, enable_events=False)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                submit = await client.post(
                    "/orders",
                    json={"symbol": "CL", "quantity": 1, "action": "BUY", "order_type": "MARKET"},
                )
            command_id = submit.json()["command_id"]

            await redis.xadd(
                runtime.results_stream,
                {
                    "command_id": command_id,
                    "order_id": "IB123",
                    "status": "Filled",
                },
            )

            processed = await runtime.poll_stream_once()
            assert processed == 1

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/orders")
            assert response.status_code == 200
            payload: dict[str, Any] = response.json()
            assert payload["orders"] == []

            history_entries = await redis.lrange(runtime.history_key, 0, -1)
            assert any(json.loads(entry)["event"] == "stream" for entry in history_entries)
        finally:
            await runtime.stop()

    asyncio.run(_scenario())
