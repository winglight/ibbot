from __future__ import annotations

import asyncio
import contextlib
import signal
from contextlib import AsyncExitStack
from typing import Any, Mapping

from services.risk import RiskRuntime
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

    snapshot_repository = None
    snapshot_session = None
    try:
        from src.ibbot.db import get_session_factory, risk_snapshot_repository
    except ModuleNotFoundError:
        pass
    else:
        try:
            session_factory = get_session_factory()
            snapshot_session = session_factory()
            snapshot_repository = risk_snapshot_repository(snapshot_session)
        except Exception:
            logger.warning("Failed to initialise risk snapshot repository", exc_info=True)
            snapshot_session = None
            snapshot_repository = None

    async def redis_factory() -> Any:
        return redis

    runtime = RiskRuntime(
        redis_factory=redis_factory,
        logger_name="risk-service",
        service_name=SERVICE_NAME,
        risk_snapshot_repository=snapshot_repository,
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

    session_cleanup = snapshot_session

    try:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(runtime.stop)
            stack.push_async_callback(provider.close)
            stack.push_async_callback(close_global_client)
            if snapshot_session is not None:
                stack.push_async_callback(snapshot_session.close)
                session_cleanup = None

            await runtime.start()
            await stop_event.wait()
    finally:
        if session_cleanup is not None:
            with contextlib.suppress(Exception):
                await session_cleanup.close()


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
