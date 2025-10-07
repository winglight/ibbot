"""Compatibility entrypoint delegating to :mod:`services.ib_gateway`."""

from __future__ import annotations

import asyncio

from src.ibbot.logging import get_logger

from services.ib_gateway import run_service as _run_gateway_service


async def run_service() -> None:
    logger = get_logger("ibbot.services.ib_command")
    logger.warning(
        "ib-command service is now provided by ib-gateway; forwarding to unified runtime",
    )
    await _run_gateway_service()


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
