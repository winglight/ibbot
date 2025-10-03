from __future__ import annotations

from src.routes import websocket as websocket_module


def test_serialise_order_prefers_canonical_fields() -> None:
    order = {
        "order_id": "1",
        "symbol": "ES",
        "action": "BUY",
        "order_type": "LMT",
        "limit_price": "123.45",
        "avg_fill_price": "120.0",
        "filled_quantity": "2",
        "remaining_quantity": "3",
        "commission": "1.5",
        "source": "strategy",
        "strategy": "alpha",
        "submitted_at": "2024-05-01T10:00:00+08:00",
        "notes": "Primary entry",
    }

    payload = websocket_module._serialise_order(order)

    assert payload["limit_price"] == 123.45
    assert payload["avg_fill_price"] == 120.0
    assert payload["filled"] == 2.0
    assert payload["remaining"] == 3.0
    assert payload["commission"] == 1.5
    assert payload["order_source"] == "strategy"
    assert payload["strategy_name"] == "alpha"
    assert payload["submit_time"] == "2024-05-01T10:00:00+08:00"
    assert payload["notes"] == "Primary entry"


def test_serialise_order_falls_back_to_legacy_fields() -> None:
    order = {
        "order_id": "legacy",
        "symbol": "NQ",
        "action": "SELL",
        "order_type": "limit",
        "price": "456.0",
        "fill_price": "455.5",
        "status": "Filled",
        "quantity": 3,
        "order_source": "manual",
        "strategy_name": "legacy",
        "created_at": "2024-05-02T09:00:00",
        "commission": "1.0",
        "note": "Legacy fallback",
    }

    payload = websocket_module._serialise_order(order)

    assert payload["limit_price"] == 456.0
    assert payload["avg_fill_price"] == 455.5
    assert payload["filled"] == 3
    assert payload["remaining"] == 0
    assert payload["order_source"] == "manual"
    assert payload["strategy_name"] == "legacy"
    assert payload["commission"] == 1.0
    assert payload["submit_time"] == "2024-05-02T09:00:00+08:00"
    assert payload["notes"] == "Legacy fallback"


def test_serialise_order_reads_metadata_notes() -> None:
    order = {
        "order_id": "meta",
        "symbol": "ES",
        "action": "BUY",
        "order_type": "MKT",
        "quantity": 1,
        "status": "Submitted",
        "metadata": {"notes": "Metadata supplied"},
    }

    payload = websocket_module._serialise_order(order)

    assert payload["notes"] == "Metadata supplied"


def test_orders_payload_includes_start_date_metadata() -> None:
    payload = websocket_module._orders_payload(
        [{"order_id": "1"}],
        limit=5,
        start_date="2024-05-01T00:00:00+08:00",
        start_date_timezone="Asia/Shanghai",
        page=1,
        total=1,
    )

    assert payload["meta"]["start_date"] == "2024-05-01T00:00:00+08:00"
    assert payload["meta"]["start_date_timezone"] == "Asia/Shanghai"
