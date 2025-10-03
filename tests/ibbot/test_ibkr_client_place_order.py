import asyncio
from typing import Any

import pytest

from src.ibkr_client import IBKRClient


def test_place_order_returns_serialisable_mapping(monkeypatch: Any) -> None:
    monkeypatch.setenv("IBBOT_DISABLE_TRADING", "1")
    client = IBKRClient()

    try:
        result = asyncio.run(
            client.place_order(
                symbol="ES",
                quantity=1,
                action="BUY",
                order_type="LIMIT",
                limit_price=4321.75,
                exchange="CME",
                sec_type="FUT",
            )
        )
    finally:
        client.disconnect()
        monkeypatch.delenv("IBBOT_DISABLE_TRADING", raising=False)

    assert isinstance(result, dict)
    assert result["status"] == "Simulated"
    assert result["order_id"]
    assert result["symbol"] == "ES"
    assert result["exchange"] == "CME"
    assert result["sec_type"] == "FUT"
    assert result["order_type"] == "LIMIT"
    assert result["action"] == "BUY"
    assert pytest.approx(result["quantity"], rel=1e-6) == 1.0
    assert pytest.approx(result["remaining"], rel=1e-6) == 1.0
    assert pytest.approx(result["limit_price"], rel=1e-6) == 4321.75
    assert result.get("avg_fill_price") is None
    assert result.get("fills") in (None, [])
