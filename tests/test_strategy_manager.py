import pytest

from src.strategy_manager import StrategyManager
from src.routes.service_clients import ServiceUnavailableError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class StubStrategyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def create_strategy(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("create_strategy", (payload,), {}))
        return {"created": True}


class UnavailableStrategyClient:
    async def list_strategies(self) -> None:
        raise ServiceUnavailableError("strategy", "service unavailable")


@pytest.mark.anyio("asyncio")
async def test_strategy_manager_forwards_calls_to_service_client() -> None:
    client = StubStrategyClient()
    manager = StrategyManager(client=client)

    payload = {"name": "demo", "strategy_type": "internal"}
    result = await manager.create_strategy(payload)

    assert result == {"created": True}
    assert client.calls == [("create_strategy", (payload,), {})]


@pytest.mark.anyio("asyncio")
async def test_strategy_manager_propagates_service_errors() -> None:
    manager = StrategyManager(client=UnavailableStrategyClient())

    with pytest.raises(ServiceUnavailableError):
        await manager.list_strategies()


@pytest.mark.anyio("asyncio")
async def test_strategy_manager_raises_attribute_error_for_unknown_method() -> None:
    manager = StrategyManager(client=object())

    with pytest.raises(AttributeError):
        await manager.toggle_strategy("demo", enabled=True)
