"""Compatibility entrypoint delegating to :mod:`services.ib_gateway`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from src.ibbot.logging import get_logger

try:
    from src.event_loop_manager import setup_event_loop_policy
    _event_loop_manager_available = True
except ImportError:
    _event_loop_manager_available = False

from services.ib_gateway import run_service as _run_gateway_service


async def run_service() -> None:
    logger = get_logger("ibbot.services.ib_command")
    logger.warning(
        "ib-command service is now provided by ib-gateway; forwarding to unified runtime",
    )
    await _run_gateway_service()


def create_app() -> FastAPI:
    logger = get_logger("ibbot.services.ib_command")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.warning(
            "Starting legacy ib-command service; delegating to ib-gateway runtime",
        )
        task = asyncio.create_task(run_service())

        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            logger.warning("ib-command service stopped; ib-gateway runtime shut down")

    app = FastAPI(
        title="IB Command Service (legacy shim)",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/healthz", tags=["health"])
    async def healthcheck() -> dict[str, str]:
        return {
            "service": "ib-command",
            "status": "running",
            "mode": "delegated",
            "delegate": "ib-gateway",
        }

    return app


app = create_app()


def main() -> None:
    # 设置事件循环策略以提高兼容性
    if _event_loop_manager_available:
        try:
            setup_event_loop_policy()
            print("IB Command service: 事件循环策略设置成功")
        except Exception as e:
            print(f"IB Command service: 事件循环策略设置失败: {e}")

    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
