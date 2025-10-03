from __future__ import annotations

import asyncio
import signal
from contextlib import AsyncExitStack
from typing import Any, Mapping

from services.risk import RiskRuntime
from services.risk.runtime import RiskSnapshotRepositoryFactory
from src.ibbot.config import ConfigProvider
from src.ibbot.logging import get_logger
from src.ibbot.redis import close_global_client, get_redis_client

SERVICE_NAME = "risk-service"


async def run_service() -> None:
    logger = get_logger("risk-service")
    redis = await get_redis_client()

    provider = ConfigProvider(redis_client=redis, hash_prefix="hash:config")
    await provider.start()

    initial_config = await provider.get_module(SERVICE_NAME)

    snapshot_repository_factory: RiskSnapshotRepositoryFactory | None = None
    try:
        from src.ibbot.db import get_session_factory, risk_snapshot_repository
    except ModuleNotFoundError:
        pass
    else:
        session_factory = get_session_factory()

        async def _snapshot_repository_factory() -> Any:
            session = session_factory()
            return risk_snapshot_repository(session)

        snapshot_repository_factory = _snapshot_repository_factory

    async def redis_factory() -> Any:
        return redis

    runtime = RiskRuntime(
        redis_factory=redis_factory,
        logger_name="risk-service",
        service_name=SERVICE_NAME,
        risk_snapshot_repository_factory=snapshot_repository_factory,
    )
    await runtime.update_config(initial_config)

    async def _config_listener(_: str, payload: Mapping[str, Any]) -> None:
        await runtime.update_config(payload)
        logger.info("Risk configuration updated")

    provider.subscribe(SERVICE_NAME, _config_listener)

    stop_event = asyncio.Event()

    def _handle_stop(*_: Any) -> None:
        logger.info("Received stop signal; shutting down")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, _handle_stop)
        except NotImplementedError:  # pragma: no cover - platform dependent
            signal.signal(signame, lambda *_: stop_event.set())

    try:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(runtime.stop)
            stack.push_async_callback(provider.close)
            stack.push_async_callback(close_global_client)

            await runtime.start()
            await stop_event.wait()


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
