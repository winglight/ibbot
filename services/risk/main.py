from __future__ import annotations

import asyncio
import signal
from contextlib import AsyncExitStack
from typing import Any, Mapping

try:
    from src.event_loop_manager import setup_event_loop_policy
    _event_loop_manager_available = True
except ImportError:
    _event_loop_manager_available = False

from services.risk import RiskRuntime
from services.risk.runtime import (
    ConfigRepositoryFactory,
    RiskRulesRepositoryFactory,
    create_config_repository_factory,
    create_risk_rules_repository_factory,
)
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

    snapshot_repository_factory: RiskRulesRepositoryFactory | None = (
        create_risk_rules_repository_factory()
    )
    config_repository_factory: ConfigRepositoryFactory | None = (
        create_config_repository_factory()
    )

    async def redis_factory() -> Any:
        return redis

    runtime = RiskRuntime(
        redis_factory=redis_factory,
        logger_name="risk-service",
        service_name=SERVICE_NAME,
        risk_rules_repository_factory=snapshot_repository_factory,
        config_repository_factory=config_repository_factory,
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
    except Exception as e:
        logger.error(f"Risk service error: {e}")
        raise


def main() -> None:
    # 设置事件循环策略以提高兼容性
    if _event_loop_manager_available:
        try:
            setup_event_loop_policy()
            print("Risk service: 事件循环策略设置成功")
        except Exception as e:
            print(f"Risk service: 事件循环策略设置失败: {e}")
    
    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
