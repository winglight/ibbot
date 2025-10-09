"""Placeholder for the legacy combined IB gateway service."""

from __future__ import annotations

import asyncio


async def run_service() -> None:
    raise RuntimeError(
        "ib_gateway service has been removed; migrate to service-local IB clients"
    )


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    main()
