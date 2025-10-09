from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import httpx
import orjson
from redis.asyncio import Redis, from_url

DEFAULT_DURATION = 10.0
DEFAULT_CHANNELS: tuple[str, ...] = (
    "ib:account:updates",
    "ib:account:portfolio",
    "ib:orders:status",
    "ib:orders:executions",
    "ib:market:ticks",
    "ib:market:dom",
    "ib:market:tick-by-tick",
    "ib:market:real-time-bars",
)


async def _safe_post(client: httpx.AsyncClient, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.post(url, json=payload, timeout=10.0)
        return {
            "url": url,
            "status": response.status_code,
            "body": response.text,
        }
    except Exception as exc:  # pragma: no cover - defensive guard
        return {"url": url, "error": str(exc)}


async def _safe_get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    try:
        response = await client.get(url, timeout=10.0)
        return {
            "url": url,
            "status": response.status_code,
            "body": response.text,
        }
    except Exception as exc:  # pragma: no cover - defensive guard
        return {"url": url, "error": str(exc)}


async def _trigger_service_endpoints(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    account_base = os.getenv("ACCOUNT_SERVICE_URL", "http://localhost:8020")
    orders_base = os.getenv("ORDERS_SERVICE_URL", "http://localhost:8050")
    market_base = os.getenv("MARKET_DATA_SERVICE_URL", "http://localhost:8010")

    tasks = [
        _safe_post(
            client,
            f"{account_base}/subscriptions/account",
            {"accounts": [os.getenv("DEFAULT_ACCOUNT_CODE", "DU000000")], "tags": ["NetLiquidation"]},
        ),
        _safe_post(client, f"{orders_base}/subscriptions/orders", {}),
        _safe_post(
            client,
            f"{market_base}/subscriptions/quotes",
            {
                "contract": {"conId": 265598, "symbol": "ES", "secType": "FUT", "exchange": "GLOBEX"},
                "tickTypes": ["1", "2", "4"],
                "snapshot": False,
            },
        ),
        _safe_get(client, f"{market_base}/config"),
    ]
    return await asyncio.gather(*tasks)


async def _collect_messages(
    redis_url: str,
    channels: Iterable[str],
    duration: float,
) -> list[dict[str, Any]]:
    redis: Redis = from_url(redis_url)
    pubsub = redis.pubsub()
    await pubsub.subscribe(*channels)
    messages: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    end_time = loop.time() + duration
    try:
        while loop.time() < end_time:
            payload = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not payload:
                continue
            data = payload.get("data")
            if isinstance(data, bytes):
                try:
                    decoded = orjson.loads(data)
                except orjson.JSONDecodeError:
                    decoded = data.decode("utf-8", errors="replace")
            else:
                decoded = data
            messages.append(
                {
                    "channel": payload.get("channel"),
                    "pattern": payload.get("pattern"),
                    "data": decoded,
                    "received_at": datetime.utcnow().isoformat() + "Z",
                }
            )
    finally:
        await pubsub.unsubscribe(*channels)
        await pubsub.close()
        await redis.close()
    return messages


async def _run(duration: float, output: Path, channels: Iterable[str]) -> dict[str, Any]:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    async with httpx.AsyncClient() as client:
        endpoint_results = await _trigger_service_endpoints(client)
        messages = await _collect_messages(redis_url, channels, duration)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "redis_url": redis_url,
        "duration": duration,
        "channels": list(channels),
        "endpoint_results": endpoint_results,
        "messages": messages,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trigger refactored IB services and capture Redis payload snapshots.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION,
        help=f"Collection duration in seconds (default: {DEFAULT_DURATION})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/regression/ib_stream_baseline.json"),
        help="Path to write the captured payloads (default: data/regression/ib_stream_baseline.json)",
    )
    parser.add_argument(
        "--channels",
        nargs="*",
        default=list(DEFAULT_CHANNELS),
        help="Optional list of Redis channels to subscribe to during the capture.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    payload = asyncio.run(
        _run(
            duration=args.duration,
            output=args.output,
            channels=args.channels or DEFAULT_CHANNELS,
        )
    )

    print(f"Captured {len(payload['messages'])} messages to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
