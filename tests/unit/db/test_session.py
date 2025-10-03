from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from src.ibbot.db.session import get_async_engine, get_session_factory, session_scope
from src.ibbot.db.schemas import ServiceMetric


def test_get_async_engine_returns_singleton(db_engine) -> None:
    first = get_async_engine()
    second = get_async_engine()
    assert first is second


@pytest.mark.asyncio
async def test_session_scope_commits_changes(db_engine, db_session_factory) -> None:
    async with session_scope() as session:
        await session.execute(
            insert(ServiceMetric).values(
                service="unit-test",
                metric="latency",
                value=1.5,
            )
        )

    async with db_session_factory() as verification_session:
        result = await verification_session.execute(select(ServiceMetric))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].service == "unit-test"


@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_error(db_engine, db_session_factory) -> None:
    class ExampleError(RuntimeError):
        pass

    with pytest.raises(ExampleError):
        async with session_scope() as session:
            await session.execute(
                insert(ServiceMetric).values(
                    service="rollback",
                    metric="latency",
                    value=3.3,
                )
            )
            raise ExampleError("trigger rollback")

    async with db_session_factory() as verification_session:
        result = await verification_session.execute(select(ServiceMetric))
        assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_get_session_factory_returns_new_sessions(db_engine) -> None:
    factory = get_session_factory()
    async with factory() as first:
        async with factory() as second:
            assert first is not second
