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


def test_order_submission_persists_snapshot_and_normalises_request() -> None:
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
                        "order_type": "LIMIT",
                        "limit_price": 4450.5,
                        "source": "web",
                    },
                )
                list_response = await client.get("/orders")
            assert response.status_code == 202
            command_id = response.json()["command_id"]
            assert command_id

            queue_length = await redis.llen(runtime.command_queue)
            assert queue_length == 1

            snapshot_raw = await redis.get(runtime.snapshot_key)
            assert snapshot_raw is not None
            snapshot = json.loads(snapshot_raw)
            assert snapshot["active_orders"][0]["command_id"] == command_id
            assert snapshot["active_orders"][0]["symbol"] == "ES"
            assert snapshot["active_orders"][0]["limit_price"] == 4450.5
            assert snapshot["active_orders"][0]["events"][0]["type"] == "submission"

            history = await redis.lrange(runtime.history_key, 0, -1)
            assert history
            history_entry = json.loads(history[0])
            assert history_entry["event"] == "submitted"
            assert history_entry["command_id"] == command_id

            payload: dict[str, Any] = list_response.json()
            assert payload["total"] == 1
            assert payload["orders"][0]["symbol"] == "ES"
            assert payload["orders"][0]["limit_price"] == 4450.5
            assert payload["limit"] == 50
            assert payload["offset"] == 0
            assert payload["page"] == 0
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_cancel_order_enqueues_command_and_get_order_returns_active() -> None:
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
                detail = await client.get(f"/orders/{order_id}")
            assert cancel.status_code == 202
            cancel_body = cancel.json()
            assert cancel_body["command_id"]
            assert cancel_body["command_id"] != order_id

            queue_length = await redis.llen(runtime.command_queue)
            assert queue_length == 2

            history_entries = await redis.lrange(runtime.history_key, 0, -1)
            events = [json.loads(entry)["event"] for entry in history_entries]
            assert "cancel_order" in events

            detail_payload = detail.json()
            assert detail_payload["order"]["command_id"] == order_id
            assert detail_payload["order"]["symbol"] == "NQ"
            assert detail_payload["order"]["events"]
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_stream_updates_keep_filled_orders_in_history_and_detail() -> None:
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
                    "result": json.dumps({"avg_price": 80.5, "filled": 1}),
                },
            )

            processed = await runtime.poll_stream_once()
            assert processed == 1

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/orders")
                detail = await client.get("/orders/IB123")
            assert response.status_code == 200
            payload: dict[str, Any] = response.json()
            assert payload["orders"]
            order = next(order for order in payload["orders"] if order["order_id"] == "IB123")
            assert order["status"].lower() == "filled"
            assert order["avg_fill_price"] == 80.5
            assert order["events"]

            detail_payload = detail.json()
            assert detail_payload["order"]["order_id"] == "IB123"
            assert detail_payload["order"]["avg_fill_price"] == 80.5
            assert any(evt["type"] == "event" for evt in detail_payload["order"]["events"])

            snapshot_raw = await redis.get(runtime.snapshot_key)
            assert snapshot_raw is not None
            snapshot = json.loads(snapshot_raw)
            assert snapshot["history"][0]["order_id"] == "IB123"
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_list_orders_pagination_with_history_and_active() -> None:
    async def _scenario() -> None:
        runtime, redis = await _create_runtime()
        try:
            app = create_app(runtime, enable_events=False)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.post(
                    "/orders",
                    json={"symbol": "AAPL", "quantity": 10, "action": "BUY", "order_type": "LIMIT"},
                )
                second = await client.post(
                    "/orders",
                    json={"symbol": "MSFT", "quantity": 5, "action": "SELL", "order_type": "MARKET"},
                )
            first_id = first.json()["command_id"]
            second_id = second.json()["command_id"]

            await redis.xadd(
                runtime.results_stream,
                {
                    "command_id": first_id,
                    "order_id": "HIST1",
                    "status": "Filled",
                },
            )
            await runtime.poll_stream_once()

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                page_one = await client.get("/orders", params={"limit": 1, "offset": 0})
                page_two = await client.get("/orders", params={"limit": 1, "offset": 1})
            assert page_one.status_code == 200
            assert page_two.status_code == 200

            payload_one = page_one.json()
            payload_two = page_two.json()
            assert payload_one["total"] == 2
            assert payload_one["limit"] == 1
            assert payload_one["offset"] == 0
            assert payload_one["page"] == 0
            assert payload_two["offset"] == 1
            assert payload_two["page"] == 1
            # ensure both active and history orders are reachable via pagination
            ids = {payload_one["orders"][0]["command_id"], payload_two["orders"][0]["command_id"]}
            assert {first_id, second_id} == ids
        finally:
            await runtime.stop()

    asyncio.run(_scenario())
