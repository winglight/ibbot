#!/usr/bin/env python3
"""Diagnostics for Redis, database and logging dependencies."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from ibbot.config import ConfigProvider  # noqa: E402
from ibbot.logging import get_log_file, get_logger  # noqa: E402


@dataclass(slots=True)
class DiagnosticResult:
    """Structured result for a single diagnostic check."""

    name: str
    success: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "OK" if self.success else "FAIL"


def _first_non_empty(config: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = config.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _redis_available() -> bool:
    return importlib.util.find_spec("redis.asyncio") is not None


async def _diagnose_redis(config: Mapping[str, str], *, dry_run: bool) -> DiagnosticResult:
    """Check Redis connectivity using configuration from the provider."""

    if not _redis_available():
        return DiagnosticResult(
            name="Redis",
            success=False,
            message="redis.asyncio is not installed. Install redis>=4.2 to enable Redis diagnostics.",
        )

    redis_asyncio = importlib.import_module("redis.asyncio")
    redis_url = _first_non_empty(config, "REDIS_URL", "ACCOUNT_CACHE_REDIS_URL", "IBBOT_REDIS_URL")
    if redis_url:
        connection_repr = redis_url
        client = redis_asyncio.Redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    else:
        host = config.get("REDIS_HOST", "127.0.0.1")
        port = int(config.get("REDIS_PORT", "6379"))
        db = int(config.get("REDIS_DB", "0"))
        password = config.get("REDIS_PASSWORD") or None
        ssl_enabled = _is_truthy(config.get("REDIS_SSL"))
        connection_repr = f"redis://{host}:{port}/{db}?ssl={'1' if ssl_enabled else '0'}"
        client = redis_asyncio.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            ssl=ssl_enabled,
            encoding="utf-8",
            decode_responses=True,
        )

    if dry_run:
        closer = getattr(client, "aclose", None)
        if callable(closer):
            await closer()
        else:
            await client.close()
        return DiagnosticResult(
            name="Redis",
            success=True,
            message="Dry-run: skipped connection test.",
            details={"target": connection_repr},
        )

    try:
        response = await client.ping()
    except Exception as exc:  # pragma: no cover - network errors depend on environment
        closer = getattr(client, "aclose", None)
        if callable(closer):
            await closer()
        else:
            await client.close()
        return DiagnosticResult(
            name="Redis",
            success=False,
            message=f"Ping failed: {exc}",
            details={"target": connection_repr},
        )

    closer = getattr(client, "aclose", None)
    if callable(closer):
        await closer()
    else:
        await client.close()
    return DiagnosticResult(
        name="Redis",
        success=bool(response),
        message="Ping response received." if response else "Ping returned falsy result.",
        details={"target": connection_repr},
    )


def _influx_available() -> bool:
    return importlib.util.find_spec("influxdb_client") is not None


def _diagnose_influx_sync(config: Mapping[str, str], *, dry_run: bool) -> DiagnosticResult:
    if not _influx_available():
        return DiagnosticResult(
            name="InfluxDB",
            success=False,
            message=(
                "SQLAlchemy with the asyncio extra (and asyncmy) is required for MariaDB diagnostics. "
                "Install SQLAlchemy[asyncio] and asyncmy to enable database checks."
            ),
        )

    influx_module = importlib.import_module("influxdb_client")
    influx_url = _first_non_empty(config, "INFLUX_URL", "IBBOT_INFLUX_URL", "DATA_LAYER_INFLUX_URL")
    token = _first_non_empty(config, "INFLUX_TOKEN", "IBBOT_INFLUX_TOKEN", "DATA_LAYER_INFLUX_TOKEN")
    org = _first_non_empty(config, "INFLUX_ORG", "IBBOT_INFLUX_ORG", "DATA_LAYER_INFLUX_ORG")
    bucket = _first_non_empty(config, "INFLUX_BUCKET", "IBBOT_INFLUX_BUCKET", "DATA_LAYER_INFLUX_BUCKET")
    timeout_ms = config.get("INFLUX_TIMEOUT_MS") or config.get("DATA_LAYER_INFLUX_TIMEOUT_MS")
    timeout = float(timeout_ms) / 1000 if timeout_ms else None

    details: dict[str, Any] = {
        "url": influx_url or "<not configured>",
        "org": org or "<not configured>",
        "bucket": bucket or "<not configured>",
    }

    if dry_run:
        return DiagnosticResult(
            name="InfluxDB",
            success=True,
            message="Dry-run: skipped health check.",
            details=details,
        )

    if influx_url is None:
        return DiagnosticResult(
            name="InfluxDB",
            success=False,
            message="INFLUX_URL is not configured.",
        )

    client_kwargs: dict[str, Any] = {"url": influx_url, "token": token, "org": org}
    if timeout is not None:
        client_kwargs["timeout"] = timeout

    try:
        client = influx_module.InfluxDBClient(**client_kwargs)
    except Exception as exc:  # pragma: no cover - constructor failure depends on environment
        return DiagnosticResult(
            name="InfluxDB",
            success=False,
            message=f"Failed to initialise client: {exc}",
            details=details,
        )

    try:
        health = client.health()
        status = getattr(health, "status", None)
        if status and str(status).lower() == "pass":
            message = "Health check returned PASS."
            success = True
        else:
            message = f"Health check returned {status or 'unknown'}"
            success = str(status).lower() == "pass" if status else False
        details["status"] = status
    except Exception as exc:  # pragma: no cover - network failures vary
        client.close()
        return DiagnosticResult(
            name="InfluxDB",
            success=False,
            message=f"Health check failed: {exc}",
            details=details,
        )

    client.close()
    return DiagnosticResult(name="InfluxDB", success=success, message=message, details=details)


async def _diagnose_influx(config: Mapping[str, str], *, dry_run: bool) -> DiagnosticResult:
    return await asyncio.to_thread(_diagnose_influx_sync, config, dry_run=dry_run)


def _diagnose_logging(config: Mapping[str, str], *, dry_run: bool) -> DiagnosticResult:
    module_name = config.get("FOUNDATION_LOGGER_NAME", "foundation.diagnostics")
    log_dir_value = config.get("FOUNDATION_LOG_DIR") or config.get("LOG_DIR")
    log_dir = Path(log_dir_value).expanduser() if log_dir_value else None

    if dry_run:
        log_file = get_log_file(module_name, log_dir=log_dir)
        return DiagnosticResult(
            name="Logging",
            success=True,
            message="Dry-run: skipped log write.",
            details={"log_file": str(log_file)},
        )

    logger = get_logger(module_name, log_dir=log_dir)
    test_message = "foundation diagnostics log entry"
    logger.info(test_message)
    for handler in logger.handlers:
        flush = getattr(handler, "flush", None)
        if callable(flush):
            flush()

    log_file = get_log_file(module_name, log_dir=log_dir)
    if not log_file.exists():
        return DiagnosticResult(
            name="Logging",
            success=False,
            message="Log file was not created.",
            details={"log_file": str(log_file)},
        )

    try:
        with log_file.open("r", encoding="utf-8") as handle:
            tail = handle.readlines()[-5:]
    except OSError as exc:  # pragma: no cover - depends on filesystem state
        return DiagnosticResult(
            name="Logging",
            success=False,
            message=f"Failed to read log file: {exc}",
            details={"log_file": str(log_file)},
        )

    return DiagnosticResult(
        name="Logging",
        success=True,
        message="Log entry written successfully.",
        details={"log_file": str(log_file), "sample": "".join(tail).strip()},
    )


async def run(args: argparse.Namespace) -> int:
    provider = ConfigProvider()
    module_name = args.module
    try:
        config = await provider.get_module(module_name)
    except Exception as exc:
        await provider.close()
        print(f"Failed to load configuration module '{module_name}': {exc}")
        return 2

    results: list[DiagnosticResult] = []
    results.append(await _diagnose_redis(config, dry_run=args.dry_run))
    results.append(await _diagnose_influx(config, dry_run=args.dry_run))
    results.append(_diagnose_logging(config, dry_run=args.dry_run))

    print("=== Foundation Diagnostics ===")
    print(f"Configuration module: {module_name}")
    if args.dry_run:
        print("Mode: dry-run (no external calls were executed)")
    print()

    for result in results:
        print(f"[{result.status}] {result.name}: {result.message}")
        for key, value in result.details.items():
            print(f"    - {key}: {value}")
        if not result.details:
            print("    (no additional details)")
        print()

    overall_success = all(r.success for r in results)
    await provider.close()
    return 0 if overall_success else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose foundational dependencies (Redis, database, logging)"
    )
    parser.add_argument(
        "--module",
        default="foundation",
        help="Configuration module name to load from ibbot.config (default: foundation)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip external connectivity checks while showing resolved targets.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive usage safeguard
        print("Interrupted by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
