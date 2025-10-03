from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

import pytest
from httpx import ASGITransport, AsyncClient

from services.orders.main import OrderSubmission, OrdersRuntime, create_app
from src.utils.ibkr_serialization import trade_to_dict
from tests.services.redis_utils import InMemoryRedis


async def _create_runtime(
    write_fn: Callable[[Iterable[Mapping[str, Any]]], int] | None = None,
    **runtime_kwargs: Any,
) -> tuple[OrdersRuntime, InMemoryRedis]:
    redis = InMemoryRedis()

    async def factory() -> InMemoryRedis:
        return redis

    kwargs: dict[str, Any] = {
        "redis_factory": factory,
        "stream_poll_interval": 0.0,
        "health_interval": 0.0,
    }
    if write_fn is not None:
        kwargs["write_fn"] = write_fn
    kwargs.update(runtime_kwargs)
    runtime = OrdersRuntime(**kwargs)
    await runtime.start()
    return runtime, redis


class RecordingWriter:
    def __init__(self) -> None:
        self.points: list[dict[str, Any]] = []

    def __call__(self, points: Iterable[Mapping[str, Any]]) -> int:
        batch: list[dict[str, Any]] = []
        for point in points:
            if hasattr(point, "as_dict"):
                payload = point.as_dict()
            elif isinstance(point, Mapping):
                payload = dict(point)
            else:
                payload = dict(point)  # type: ignore[arg-type]
            batch.append(payload)
        self.points.extend(batch)
        return len(batch)


def _build_trade_payload(
    *,
    order_id: str,
    symbol: str,
    exchange: str,
    status: str,
    avg_fill_price: float,
    filled: float,
    remaining: float,
    commission: float,
    realized_pnl: float,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    event_time = timestamp or datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)
    order = SimpleNamespace(
        orderId=order_id,
        action="BUY",
        totalQuantity=filled + remaining,
        orderType="LIMIT",
        lmtPrice=avg_fill_price,
    )
    order_status = SimpleNamespace(
        status=status,
        avgFillPrice=avg_fill_price,
        filled=filled,
        remaining=remaining,
        commission=commission,
        commissionCurrency="USD",
        realizedPNL=realized_pnl,
        submittedTime=event_time,
        completedTime=event_time,
    )
    contract = SimpleNamespace(
        symbol=symbol,
        localSymbol=f"{symbol}FUT",
        exchange=exchange,
        secType="FUT",
    )
    execution = SimpleNamespace(
        execId=f"exec-{order_id}",
        time=event_time,
        side="BOT",
        price=avg_fill_price,
        avgPrice=avg_fill_price,
        shares=filled,
        exchange=exchange,
        acctNumber="DU123",
        orderId=order_id,
    )
    commission_report = SimpleNamespace(
        commission=commission,
        currency="USD",
        realizedPNL=realized_pnl,
    )
    fill = SimpleNamespace(
        execution=execution,
        commissionReport=commission_report,
    )
    trade = SimpleNamespace(
        order=order,
        orderStatus=order_status,
        contract=contract,
        fills=[fill],
        log=[SimpleNamespace(time=event_time)],
    )
    return trade_to_dict(trade)


class _FailingWriter:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, points: Iterable[Mapping[str, Any]]) -> int:
        self.calls += 1
        raise self.error


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
                        "notes": "Entry at support",
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
            assert snapshot["active_orders"][0]["notes"] == "Entry at support"
            assert snapshot["active_orders"][0]["events"][0]["type"] == "submission"

            history = await redis.lrange(runtime.history_key, 0, -1)
            assert history
            history_entry = json.loads(history[0])
            assert history_entry["event"] == "submitted"
            assert history_entry["command_id"] == command_id
            assert history_entry["order"]["notes"] == "Entry at support"

            payload: dict[str, Any] = list_response.json()
            assert payload["total"] == 1
            assert payload["orders"][0]["symbol"] == "ES"
            assert payload["orders"][0]["limit_price"] == 4450.5
            assert payload["orders"][0]["notes"] == "Entry at support"
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
                events_response = await client.get(f"/orders/{order_id}/events")
            assert cancel.status_code == 202
            cancel_body = cancel.json()
            assert cancel_body["command_id"]
            assert cancel_body["command_id"] != order_id

            queue_length = await redis.llen(runtime.command_queue)
            assert queue_length == 2

            history_entries = await redis.lrange(runtime.history_key, 0, -1)
            event_names = [json.loads(entry)["event"] for entry in history_entries]
            assert "cancel_order" in event_names

            detail_payload = detail.json()
            assert detail_payload["order"]["command_id"] == order_id
            assert detail_payload["order"]["symbol"] == "NQ"
            assert detail_payload["order"]["events"]
            events_payload = events_response.json()
            assert events_payload["success"] is True
            assert events_payload["events"]
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
                    "status": "success",
                    "result": json.dumps(
                        _build_trade_payload(
                            order_id="IB123",
                            symbol="CL",
                            exchange="NYMEX",
                            status="Filled",
                            avg_fill_price=80.5,
                            filled=1.0,
                            remaining=0.0,
                            commission=1.25,
                            realized_pnl=45.0,
                        )
                    ),
                },
            )

            processed = await runtime.poll_stream_once()
            assert processed == 1

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/orders")
                detail = await client.get("/orders/IB123")
                events_response = await client.get("/orders/IB123/events")
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
            events_payload = events_response.json()
            assert events_payload["order_id"] in {"IB123", "cmd"}
            assert any(evt.get("type") for evt in events_payload["events"])

            snapshot_raw = await redis.get(runtime.snapshot_key)
            assert snapshot_raw is not None
            snapshot = json.loads(snapshot_raw)
            assert snapshot["history"][0]["order_id"] == "IB123"
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_patch_fill_metrics_updates_runtime_and_influx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _scenario() -> None:
        monkeypatch.setattr("services.orders.main.init_influx_client", lambda **_: None)
        writer = RecordingWriter()
        runtime, redis = await _create_runtime(write_fn=writer)
        try:
            app = create_app(runtime, enable_events=False)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                submit = await client.post(
                    "/orders",
                    json={
                        "symbol": "GC",
                        "quantity": 1,
                        "action": "BUY",
                        "order_type": "MARKET",
                    },
                )
                order_id = submit.json()["command_id"]
                patch = await client.patch(
                    f"/orders/{order_id}/fills",
                    json={
                        "commission": 12.5,
                        "realized_pnl": 45.0,
                        "unrealized_pnl": 5.5,
                        "filled_quantity": 1,
                    },
                )
                detail = await client.get(f"/orders/{order_id}")

            assert patch.status_code == 200
            patch_payload = patch.json()
            assert patch_payload["success"] is True
            patched_order = patch_payload["order"]
            assert patched_order["commission"] == pytest.approx(12.5)
            assert patched_order["realized_pnl"] == pytest.approx(45.0)
            assert patched_order["unrealized_pnl"] == pytest.approx(5.5)
            assert patched_order["filled_quantity"] == pytest.approx(1.0)
            assert any(evt["type"] == "fill_metrics_updated" for evt in patched_order["events"])

            async with runtime._lock:
                active = runtime._active_orders.get(order_id)
            assert active is not None
            assert active["result"]["commission"] == pytest.approx(12.5)
            assert active["result"]["realized_pnl"] == pytest.approx(45.0)
            assert active["result"]["unrealized_pnl"] == pytest.approx(5.5)
            assert active["commission"] == pytest.approx(12.5)

            detail_payload = detail.json()
            assert detail_payload["order"]["commission"] == pytest.approx(12.5)
            assert detail_payload["order"]["unrealized_pnl"] == pytest.approx(5.5)
            assert any(
                evt["type"] == "fill_metrics_updated"
                for evt in detail_payload["order"]["events"]
            )

            snapshot_raw = await redis.get(runtime.snapshot_key)
            assert snapshot_raw is not None
            snapshot = json.loads(snapshot_raw)
            stored_order = snapshot["active_orders"][0]
            assert stored_order["commission"] == pytest.approx(12.5)

            history_entries = await redis.lrange(runtime.history_key, 0, -1)
            assert any(
                json.loads(entry)["event"] == "fill_metrics_updated"
                for entry in history_entries
            )

            assert writer.points
            point_fields = writer.points[-1]["fields"]
            assert point_fields["commission"] == pytest.approx(12.5)
            assert point_fields["realized_pnl"] == pytest.approx(45.0)
            assert point_fields["filled_quantity"] == pytest.approx(1.0)
            assert point_fields["unrealized_pnl"] == pytest.approx(5.5)
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


def test_list_orders_filters_by_start_date() -> None:
    async def _scenario() -> None:
        runtime, _ = await _create_runtime()
        try:
            older = datetime(2024, 1, 1, tzinfo=timezone.utc)
            newer = datetime(2024, 1, 5, tzinfo=timezone.utc)
            cutoff = newer - timedelta(days=1)

            async with runtime._lock:
                runtime._active_orders["recent"] = {
                    "command_id": "recent",
                    "order_id": "recent",
                    "status": "Submitted",
                    "updated_at": newer.isoformat(),
                }
                runtime._history.appendleft(
                    {
                        "command_id": "old",
                        "order_id": "old",
                        "status": "Filled",
                        "completed_at": older.isoformat(),
                    }
                )
                runtime._last_updated_at = newer

            result = await runtime.list_orders(
                limit=50,
                offset=0,
                start_date=cutoff.isoformat(),
            )

            assert result["total"] == 1
            assert len(result["orders"]) == 1
            assert result["orders"][0]["command_id"] == "recent"
            assert result["start_date"] == cutoff.isoformat()
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_snapshot_reload_normalises_cached_records() -> None:
    async def _scenario() -> None:
        runtime, redis = await _create_runtime()
        try:
            snapshot_payload = {
                "active_orders": [
                    {
                        "id": "active-1",
                        "request": {
                            "ticker": "ESU4",
                            "qty": "2",
                            "side": "BUY",
                            "lmt_price": "4350.5",
                        },
                        "result": {
                            "avgPrice": "4349.75",
                            "strategy_id": "alpha",
                            "source": "web",
                        },
                        "events": [
                            {
                                "timestamp": "2024-05-10T02:00:00+00:00",
                                "payload": {"status": "Submitted"},
                            }
                        ],
                    }
                ],
                "history": [
                    {
                        "ib_order_id": "hist-1",
                        "response": {
                            "symbol": "CL",
                            "action": "SELL",
                            "limit_price": "80.5",
                        },
                        "result": {"avg_price": "79.75"},
                    }
                ],
                "updated_at": "2024-05-10T03:00:00+00:00",
                "last_event_id": "1-1",
            }
            await redis.set(runtime.snapshot_key, json.dumps(snapshot_payload))

            await runtime._load_snapshot()

            async with runtime._lock:
                active = runtime._active_orders.get("active-1")
                history = runtime._history[0] if runtime._history else None

            assert active is not None
            assert active["symbol"] == "ESU4"
            assert active["action"] == "BUY"
            assert active["limit_price"] == 4350.5
            assert active["avg_fill_price"] == 4349.75
            assert active["source"] == "web"
            assert active["strategy"] == "alpha"
            assert isinstance(active.get("events"), list)
            assert active["events"]

            assert history is not None
            assert history["symbol"] == "CL"
            assert history["action"] == "SELL"
            assert history["limit_price"] == 80.5
            assert history["avg_fill_price"] == 79.75
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_list_orders_defaults_to_configured_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2024, 5, 10, 10, 15, tzinfo=timezone.utc)
    monkeypatch.setattr("services.orders.main._utc_now", lambda: fixed_now)

    async def _scenario() -> None:
        runtime, _ = await _create_runtime(default_timezone="America/New_York")
        try:
            async with runtime._lock:
                runtime._active_orders["recent"] = {
                    "command_id": "recent",
                    "updated_at": "2024-05-10T08:00:00+00:00",
                }
                runtime._history.appendleft(
                    {
                        "command_id": "older",
                        "completed_at": "2024-05-09T20:00:00+00:00",
                    }
                )

            result = await runtime.list_orders()

            assert result["total"] == 1
            assert len(result["orders"]) == 1
            assert result["orders"][0]["command_id"] == "recent"
            assert result["start_date"] == "2024-05-10T00:00:00-04:00"
            assert result["start_date_timezone"] == "America/New_York"
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_order_submission_writes_influx_point(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.orders.main.init_influx_client", lambda **_: None)

    async def _scenario() -> None:
        writer = RecordingWriter()
        runtime, _ = await _create_runtime(write_fn=writer)
        try:
            submission = OrderSubmission(
                symbol="ES",
                quantity=2,
                action="BUY",
                order_type="LIMIT",
                limit_price=4450.5,
                exchange="CME",
                notes="Hedge entry",
            )
            await runtime.submit_order(submission)
            assert writer.points
            point = writer.points[-1]
            assert point["measurement"] == "orders"
            tags = point.get("tags", {})
            assert tags.get("symbol") == "ES"
            assert tags.get("side") == "BUY"
            assert tags.get("exchange") == "CME"
            fields = point["fields"]
            assert fields["status"] == "pending"
            assert pytest.approx(fields["quantity"], rel=1e-6) == 2.0
            assert pytest.approx(fields["limit_price"], rel=1e-6) == 4450.5
            assert fields["order_type"] == "LIMIT"
            assert fields["notes"] == "Hedge entry"
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_order_update_writes_influx_point(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.orders.main.init_influx_client", lambda **_: None)

    async def _scenario() -> None:
        writer = RecordingWriter()
        runtime, _ = await _create_runtime(write_fn=writer)
        try:
            submission = OrderSubmission(symbol="CL", quantity=1, action="BUY")
            command_id = await runtime.submit_order(submission)
            writer.points.clear()
            trade_payload = _build_trade_payload(
                order_id="IB123",
                symbol="CL",
                exchange="NYMEX",
                status="Filled",
                avg_fill_price=80.5,
                filled=1.0,
                remaining=0.0,
                commission=2.5,
                realized_pnl=120.0,
            )
            payload = {
                "command_id": command_id,
                "status": "success",
                "result": json.dumps(trade_payload),
            }
            updated = await runtime._apply_order_event(payload)
            assert updated is True
            assert writer.points
            point = writer.points[-1]
            fields = point["fields"]
            assert pytest.approx(fields["avg_fill_price"], rel=1e-6) == 80.5
            assert pytest.approx(fields["filled_quantity"], rel=1e-6) == 1.0
            assert pytest.approx(fields["commission"], rel=1e-6) == 2.5
            assert pytest.approx(fields["realized_pnl"], rel=1e-6) == 120.0
            tags = point.get("tags", {})
            assert tags.get("order_id") == "IB123"
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_apply_order_event_updates_order_from_serialised_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.orders.main.init_influx_client", lambda **_: None)

    async def _scenario() -> None:
        writer = RecordingWriter()
        runtime, _ = await _create_runtime(write_fn=writer)
        try:
            submission = OrderSubmission(symbol="ES", quantity=2, action="SELL")
            command_id = await runtime.submit_order(submission)
            trade_payload = {
                **_build_trade_payload(
                    order_id="IB999",
                    symbol="ES",
                    exchange="CME",
                    status="Filled",
                    avg_fill_price=4321.5,
                    filled=2.0,
                    remaining=0.0,
                    commission=3.25,
                    realized_pnl=275.0,
                ),
            }
            payload = {
                "command_id": command_id,
                "status": "success",
                "result": json.dumps(trade_payload),
            }
            updated = await runtime._apply_order_event(payload)
            assert updated is True

            detail = await runtime.get_order("IB999")
            order_payload = detail["order"]
            assert order_payload["order_id"] == "IB999"
            assert pytest.approx(order_payload["avg_fill_price"], rel=1e-6) == 4321.5
            assert pytest.approx(order_payload["filled_quantity"], rel=1e-6) == 2.0
            assert pytest.approx(order_payload["commission"], rel=1e-6) == 3.25
            result_section = order_payload.get("result", {})
            assert result_section.get("fills")
            first_fill = result_section["fills"][0]
            assert first_fill["exec_id"] == "exec-IB999"
            assert pytest.approx(first_fill["price"], rel=1e-6) == 4321.5
        finally:
            await runtime.stop()

    asyncio.run(_scenario())


def test_influx_failures_are_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("services.orders.main.init_influx_client", lambda **_: None)
    caplog.set_level(logging.INFO, logger="ibbot.services.orders")

    async def _scenario() -> None:
        writer = _FailingWriter(ValueError("missing bucket configuration"))
        runtime, _ = await _create_runtime(write_fn=writer)
        try:
            submission = OrderSubmission(symbol="NQ", quantity=1, action="BUY")
            await runtime.submit_order(submission)
            assert writer.calls == 1
            await runtime.submit_order(OrderSubmission(symbol="ES", quantity=1, action="SELL"))
            assert writer.calls == 1
        finally:
            await runtime.stop()

    asyncio.run(_scenario())
    messages = [record.getMessage() for record in caplog.records]
    assert any("Influx" in message for message in messages)
