from __future__ import annotations

import pytest

from services.market_data.runtime import MarketDataRuntime

pytestmark = pytest.mark.anyio("asyncio")


async def test_account_snapshots_merge_and_stale_propagation(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MarketDataRuntime()
    runtime._snapshots["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "price": 100.0,
        "dom": None,
        "account": None,
        "updated_at": None,
        "latency_ms": None,
        "stale": False,
    }

    account_info_payloads = [
        {
            "data": {"equity": 1000.0},
            "updated_at": "2024-01-01T00:00:00+00:00",
            "stale": False,
        },
        {
            "data": {"equity": 1100.0},
            "updated_at": "2024-01-01T00:02:00+00:00",
            "stale": True,
            "stale_reason": "feed stale",
        },
    ]
    positions_payloads = [
        {
            "data": [{"symbol": "BTCUSDT", "position": 1}],
            "updated_at": "2024-01-01T00:00:30+00:00",
            "stale": False,
        },
        {
            "data": [{"symbol": "BTCUSDT", "position": 2}],
            "updated_at": "2024-01-01T00:02:30+00:00",
            "stale": False,
        },
    ]

    async def fake_read_account_info_snapshot():
        if account_info_payloads:
            return account_info_payloads.pop(0)
        return None

    async def fake_read_positions_snapshot():
        if positions_payloads:
            return positions_payloads.pop(0)
        return None

    monkeypatch.setattr(
        "services.market_data.runtime.read_account_info_snapshot",
        fake_read_account_info_snapshot,
    )
    monkeypatch.setattr(
        "services.market_data.runtime.read_positions_snapshot",
        fake_read_positions_snapshot,
    )

    await runtime._refresh_account_snapshot()
    first_snapshot = runtime.get_snapshot("BTCUSDT")
    assert first_snapshot["account_info"] == {"equity": 1000.0}
    assert first_snapshot["positions"] == [{"symbol": "BTCUSDT", "position": 1}]
    assert first_snapshot["account_updated_at"] == "2024-01-01T00:00:30+00:00"
    assert first_snapshot["account_stale"] is False
    assert runtime.health_state["account_snapshot"]["last_error"] is None

    await runtime._refresh_account_snapshot()
    stale_snapshot = runtime.get_snapshot("BTCUSDT")
    assert stale_snapshot["account_info"] == {"equity": 1100.0}
    assert stale_snapshot["positions"] == [{"symbol": "BTCUSDT", "position": 2}]
    assert stale_snapshot["account_updated_at"] == "2024-01-01T00:02:30+00:00"
    assert stale_snapshot["account_stale"] is True
    assert runtime.health_state["account_snapshot"]["stale"] is True
    assert runtime.health_state["account_snapshot"]["stale_reason"] == "feed stale"

    await runtime._refresh_account_snapshot()
    fallback_snapshot = runtime.get_snapshot("BTCUSDT")
    assert fallback_snapshot["account_info"] == {"equity": 1100.0}
    assert fallback_snapshot["positions"] == [{"symbol": "BTCUSDT", "position": 2}]
    assert fallback_snapshot["account_updated_at"] == "2024-01-01T00:02:30+00:00"
    assert runtime.health_state["account_snapshot"]["last_error"] == "missing_snapshot_data"
