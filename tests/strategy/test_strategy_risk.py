from __future__ import annotations

from typing import Any

import pytest

from src.strategy_risk import StrategyRiskManager


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class StubRiskClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rules_payload: Any = {"rules": {"ES": {"symbol": "ES", "enabled": True}}}
        self.config_payload: Any = {"config": {"risk_per_trade_r": 0.02}}
        self.history_payload: Any = {"history": [{"symbol": "ES", "enabled": True}]}

    async def list_rules(self) -> Any:
        self.calls.append(("list_rules", None))
        return self.rules_payload

    async def get_config(self) -> Any:
        self.calls.append(("get_config", None))
        return self.config_payload

    async def fetch_rules_history(self, *, symbol: str | None = None, limit: int | None = None) -> Any:
        self.calls.append(("fetch_rules_history", {"symbol": symbol, "limit": limit}))
        return self.history_payload


@pytest.mark.anyio("asyncio")
async def test_strategy_risk_manager_loads_rules() -> None:
    stub = StubRiskClient()
    manager = StrategyRiskManager(client=stub)

    rules = await manager.load_rules()

    assert rules == {"ES": {"symbol": "ES", "enabled": True}}
    assert stub.calls[0][0] == "list_rules"


@pytest.mark.anyio("asyncio")
async def test_strategy_risk_manager_load_rule_for_symbol() -> None:
    stub = StubRiskClient()
    manager = StrategyRiskManager(client=stub)

    rule = await manager.load_rule("es")

    assert rule == {"symbol": "ES", "enabled": True}


@pytest.mark.anyio("asyncio")
async def test_strategy_risk_manager_loads_config() -> None:
    stub = StubRiskClient()
    manager = StrategyRiskManager(client=stub)

    config = await manager.load_config()

    assert config == {"risk_per_trade_r": 0.02}
    assert any(call[0] == "get_config" for call in stub.calls)


@pytest.mark.anyio("asyncio")
async def test_strategy_risk_manager_loads_history() -> None:
    stub = StubRiskClient()
    manager = StrategyRiskManager(client=stub)

    history = await manager.load_rules_history(symbol="ES", limit=5)

    assert history == [{"symbol": "ES", "enabled": True}]
    assert stub.calls[-1] == ("fetch_rules_history", {"symbol": "ES", "limit": 5})


@pytest.mark.anyio("asyncio")
async def test_strategy_risk_manager_handles_missing_rule() -> None:
    stub = StubRiskClient()
    manager = StrategyRiskManager(client=stub)

    rule = await manager.load_rule("nq")

    assert rule is None
