#!/usr/bin/env python3
"""Diagnostics for Redis, database and logging dependencies."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import os
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
from ibbot.db.config import (  # noqa: E402
    DatabaseConfigError,
    resolve_database_url,
)


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


_REQUIRED_TABLES: tuple[str, ...] = (
    "service_metrics",
    "orders",
    "market_events",
    "market_prices",
    "risk_config_snapshots",
    "risk_rule_snapshots",
)


def _database_imports_available() -> tuple[bool, str | None]:
    if importlib.util.find_spec("sqlalchemy") is None:
        return False, "SQLAlchemy is not installed. Install sqlalchemy[asyncio] to enable diagnostics."
    if importlib.util.find_spec("asyncmy") is None:
        return False, "asyncmy driver is missing. Install asyncmy to connect to MariaDB."
    return True, None


def _prepare_env(config: Mapping[str, Any]) -> dict[str, str]:
    merged = dict(os.environ)
    for key, value in config.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            merged[str(key)] = str(value)
    return merged


async def _diagnose_database(config: Mapping[str, Any], *, dry_run: bool) -> DiagnosticResult:
    available, reason = _database_imports_available()
    if not available:
        return DiagnosticResult(
            name="MariaDB",
            success=False,
            message=reason or "Required dependencies for MariaDB diagnostics are missing.",
        )

    env = _prepare_env(config)
    try:
        database_url = resolve_database_url(env)
    except DatabaseConfigError as exc:
        return DiagnosticResult(
            name="MariaDB",
            success=False,
            message=str(exc),
        )

    details: dict[str, Any] = {"url": database_url}

    if dry_run:
        return DiagnosticResult(
            name="MariaDB",
            success=True,
            message="Dry-run: skipped connectivity test.",
            details=details,
        )

    from sqlalchemy import inspect, text  # imported lazily to avoid unnecessary dependency cost
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            try:
                result = await connection.execute(text("SELECT 1"))
                details["test_query"] = result.scalar()
            except Exception as exc:  # pragma: no cover - depends on DB availability
                return DiagnosticResult(
                    name="MariaDB",
                    success=False,
                    message=f"Connectivity test failed: {exc}",
                    details=details,
                )

            try:
                table_names = await connection.run_sync(
                    lambda sync_conn: set(inspect(sync_conn).get_table_names())
                )
            except Exception as exc:  # pragma: no cover - environment specific
                return DiagnosticResult(
                    name="MariaDB",
                    success=False,
                    message=f"Failed to inspect tables: {exc}",
                    details=details,
                )

            missing = sorted(table for table in _REQUIRED_TABLES if table not in table_names)
            details["tables"] = sorted(table_names)
            if missing:
                details["missing_tables"] = missing
                return DiagnosticResult(
                    name="MariaDB",
                    success=False,
                    message="Required tables are missing from the database.",
                    details=details,
                )
    finally:
        await engine.dispose()

    return DiagnosticResult(
        name="MariaDB",
        success=True,
        message="Connectivity test succeeded and required tables are present.",
        details=details,
    )


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
    results.append(await _diagnose_database(config, dry_run=args.dry_run))
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
