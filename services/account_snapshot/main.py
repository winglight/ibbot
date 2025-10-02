"""Command line entrypoint for the account snapshot service."""

from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress
from typing import Optional

import uvicorn
from loguru import logger

from .runtime import AccountSnapshotRuntime


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the account snapshot service")
    parser.add_argument(
        "--mode",
        choices=["worker", "api"],
        default="worker",
        help="Run the background worker or expose a FastAPI application",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host for FastAPI mode",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8082,
        help="Port for FastAPI mode",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload in FastAPI mode",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="Log level for FastAPI mode",
    )
    return parser.parse_args(argv)


async def _run_worker() -> None:
    runtime = AccountSnapshotRuntime()
    await runtime.startup()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        if not stop_event.is_set():
            logger.info("Received shutdown signal")
            stop_event.set()

    for signame in (signal.SIGINT, signal.SIGTERM):
        with suppress(Exception):  # pragma: no cover - platform dependent
            loop.add_signal_handler(signame, _handle_signal)

    try:
        await stop_event.wait()
    finally:
        await runtime.shutdown()


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)

    if args.mode == "api":
        uvicorn.run(
            "services.account_snapshot.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level=args.log_level,
        )
        return

    asyncio.run(_run_worker())


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
