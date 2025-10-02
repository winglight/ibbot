from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from src.order_manager import OrderManager
from src.routes.service_clients import ServiceUnavailableError
from tests.services.redis_utils import InMemoryRedis


class StubOrdersClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.responses: dict[str, Mapping[str, Any]] = {}
        self.errors: dict[str, Exception] = {}

    async def place_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if "place_order" in self.errors:
            raise self.errors["place_order"]
        record = dict(payload)
        self.calls.append(("place_order", record))
        return self.responses.get("place_order", {"command_id": "svc-123", "status": "accepted"})

    async def cancel_order(self, order_id: str) -> Mapping[str, Any]:
        if "cancel_order" in self.errors:
            raise self.errors["cancel_order"]
        self.calls.append(("cancel_order", {"order_id": order_id}))
        return self.responses.get("cancel_order", {"command_id": "svc-cancel", "status": "accepted"})

    async def cancel_all(self) -> Mapping[str, Any]:
        if "cancel_all" in self.errors:
            raise self.errors["cancel_all"]
        self.calls.append(("cancel_all", {}))
        return self.responses.get("cancel_all", {"command_id": "svc-all", "status": "accepted"})

    async def close_position(self, symbol: str) -> Mapping[str, Any]:
        if "close_position" in self.errors:
            raise self.errors["close_position"]
        self.calls.append(("close_position", {"symbol": symbol}))
        return self.responses.get("close_position", {"command_id": "svc-close", "status": "accepted"})

    async def reverse_position(self, symbol: str) -> Mapping[str, Any]:
        if "reverse_position" in self.errors:
            raise self.errors["reverse_position"]
        self.calls.append(("reverse_position", {"symbol": symbol}))
        return self.responses.get("reverse_position", {"command_id": "svc-reverse", "status": "accepted"})

    async def list_orders(self, **params: Any) -> Mapping[str, Any]:
        self.calls.append(("list_orders", dict(params)))
        return {"orders": [], "params": dict(params)}

    async def get_order(self, order_id: str) -> Mapping[str, Any]:
        self.calls.append(("get_order", {"order_id": order_id}))
        return {"success": True, "order": {"id": order_id}}

    async def get_order_events(self, order_id: str) -> Mapping[str, Any]:
        self.calls.append(("get_order_events", {"order_id": order_id}))
        return {"success": True, "order_id": order_id, "events": []}

def test_submit_order_uses_orders_service() -> None:
    client = StubOrdersClient()
    manager = OrderManager(client=client)

    payload = {"symbol": "ES", "quantity": 1, "action": "BUY"}

    response = asyncio.run(manager.submit_order(payload))

    assert response["command_id"] == "svc-123"
    assert client.calls == [("place_order", payload)]


def test_submit_order_falls_back_to_queue_when_service_down() -> None:
    client = StubOrdersClient()
    client.errors["place_order"] = ServiceUnavailableError("orders", "service down")
    redis = InMemoryRedis()

    async def factory() -> InMemoryRedis:
        return redis

    manager = OrderManager(client=client, redis_factory=factory)
    payload = {"symbol": "NQ", "quantity": 2, "action": "SELL"}

    async def scenario() -> tuple[Mapping[str, Any], str, str, int]:
        result = await manager.submit_order(payload)
        queue_key = result["queue"]
        queued_raw = (await redis.lrange(queue_key, 0, -1))[0]
        queue_length = await redis.llen(queue_key)
        return result, queue_key, queued_raw, queue_length

    result, queue_key, queued_raw, queue_length = asyncio.run(scenario())

    assert result["status"] == "queued"
    assert result["fallback"] is True
    assert queue_length == 1
    queued = json.loads(queued_raw)
    assert queued["method"] == "place_order"
    assert queued["kwargs"] == payload


def test_cancel_order_fallback_enqueues_command() -> None:
    client = StubOrdersClient()
    client.errors["cancel_order"] = ServiceUnavailableError("orders", "down")
    redis = InMemoryRedis()

    async def factory() -> InMemoryRedis:
        return redis

    manager = OrderManager(client=client, redis_factory=factory)

    async def scenario() -> tuple[Mapping[str, Any], list[str]]:
        result = await manager.cancel_order("123")
        items = await redis.lrange(result["queue"], 0, -1)
        return result, items

    result, items = asyncio.run(scenario())

    assert result["status"] == "queued"
    payload = json.loads(items[0])
    assert payload["method"] == "cancel_order"
    assert payload["kwargs"] == {"order_id": "123"}


def test_read_operations_proxy_directly() -> None:
    client = StubOrdersClient()
    manager = OrderManager(client=client)

    async def scenario() -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        orders_payload = await manager.list_orders(limit=25)
        order_detail = await manager.get_order("abc")
        events_detail = await manager.get_order_events("abc")
        return orders_payload, order_detail, events_detail

    payload, order, events = asyncio.run(scenario())

    assert payload["params"]["limit"] == 25
    assert order["order"]["id"] == "abc"
    assert events["order_id"] == "abc"
    recorded = [call[0] for call in client.calls]
    assert recorded == ["list_orders", "get_order", "get_order_events"]

