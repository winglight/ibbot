import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import pytest

from src.async_executor import AsyncExecutor
from src.database import DatabaseManager, RiskRule
from src.strategy_manager import StrategyManager, Strategy as StrategyRecord


@pytest.fixture
def async_executor():
    executor = AsyncExecutor()
    try:
        yield executor
    finally:
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_database_manager_async_roundtrip(tmp_path, async_executor):
    db_path = tmp_path / "async-test.db"
    db = DatabaseManager(db_path=str(db_path), async_executor=async_executor)

    try:
        rule = RiskRule(
            symbol="AAPL",
            stop_loss_price=120.5,
            take_profit_price=135.0,
            timestamp=datetime.now().isoformat(),
        )
        await db.save_risk_rule_async(rule)
        stored = await db.load_risk_rules_async()
        assert any(item.symbol == "AAPL" for item in stored)

        await db.delete_risk_rule_async("AAPL")
        after_delete = await db.load_risk_rules_async()
        assert all(item.symbol != "AAPL" for item in after_delete)
    finally:
        db._conn.close()


@pytest.mark.asyncio
async def test_strategy_manager_rebuild_cache_async(async_executor, monkeypatch):
    strategy_name = "demo-strategy"

    class StubDB:
        def __init__(self) -> None:
            self.calls: List[Tuple[str, str, Optional[int], Optional[str]]] = []

        async def load_strategy_orders_async(
            self,
            name: str,
            source: str = "strategy",
            limit: Optional[int] = None,
            symbol: Optional[str] = None,
        ):
            self.calls.append((name, source, limit, symbol))
            return [SimpleNamespace(order_id="order-1")]

    fake_db = StubDB()

    manager = StrategyManager.__new__(StrategyManager)
    manager.db_manager = fake_db
    manager._async_executor = async_executor
    manager.strategies = {
        strategy_name: StrategyRecord(name=strategy_name, strategy_type="external")
    }
    manager._event_loop = asyncio.get_running_loop()

    cache_calls: Dict[str, List[str]] = {}

    async def fake_rebuild(name, orders):
        cache_calls[name] = [item.order_id for item in orders]

    async def fake_clear(name):
        cache_calls.setdefault("cleared", []).append(name)

    monkeypatch.setattr("src.strategy_manager.cache_rebuild_strategy_cache", fake_rebuild)
    monkeypatch.setattr("src.strategy_manager.clear_strategy_cache", fake_clear)

    await manager.rebuild_strategy_cache_async([strategy_name])

    assert cache_calls[strategy_name] == ["order-1"]
    assert fake_db.calls == [(strategy_name, "strategy", None, None)]
    assert "cleared" not in cache_calls
