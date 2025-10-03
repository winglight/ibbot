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
from urllib.parse import urlparse, urlunparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from ibbot.config import ConfigProvider  # noqa: E402
from ibbot.logging import get_log_file, get_logger  # noqa: E402
from src.db.config import DatabaseConfigError, resolve_database_url  # noqa: E402
from src.db.schema import REQUIRED_TABLES  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.engine.url import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402


REQUIRED_DB_TABLES: frozenset[str] = frozenset(REQUIRED_TABLES)


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


def _redact_db_url(url: str | None) -> str:
    if not url:
        return "<not configured>"
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        if ":" in userinfo:
            username = userinfo.split(":", 1)[0]
            userinfo = f"{username}:***"
        elif userinfo:
            userinfo = f"{userinfo}:***"
        netloc = f"{userinfo}@{host}" if userinfo else host

    return urlunparse(parsed._replace(netloc=netloc))


def _collect_table_names(sync_connection: Any) -> set[str]:
    inspector = inspect(sync_connection)
    return set(inspector.get_table_names())


async def _diagnose_mariadb(config: Mapping[str, str], *, dry_run: bool) -> DiagnosticResult:
    details: dict[str, Any] = {
        "required_tables": sorted(REQUIRED_DB_TABLES),
    }

    try:
        resolved_url = resolve_database_url(config)
    except DatabaseConfigError as exc:
        return DiagnosticResult(
            name="MariaDB",
            success=False,
            message=str(exc),
            details=details,
        )

    details["url"] = _redact_db_url(resolved_url)

    try:
        parsed_url = make_url(resolved_url)
        details["driver"] = parsed_url.drivername
        details["database"] = parsed_url.database or "<default>"
        if parsed_url.username:
            details["user"] = parsed_url.username
    except Exception:  # pragma: no cover - defensive parsing guard
        details["driver"] = "<unknown>"

    if dry_run:
        return DiagnosticResult(
            name="MariaDB",
            success=True,
            message="Dry-run: skipped database connectivity check.",
            details=details,
        )

    try:
        engine: AsyncEngine = create_async_engine(resolved_url, pool_pre_ping=True)
    except Exception as exc:  # pragma: no cover - driver discovery depends on environment
        return DiagnosticResult(
            name="MariaDB",
            success=False,
            message=f"Failed to create async engine: {exc}",
            details=details,
        )

    try:
        async with engine.connect() as connection:
            try:
                version = await connection.scalar(text("SELECT VERSION()"))
            except Exception:  # pragma: no cover - depends on server permissions
                version = None
            details["server_version"] = version or "<unknown>"

            try:
                result = await connection.execute(text("SELECT 1"))
                details["select_result"] = result.scalar_one()
            except Exception as exc:
                return DiagnosticResult(
                    name="MariaDB",
                    success=False,
                    message=f"Connectivity probe failed: {exc}",
                    details=details,
                )

            try:
                available_tables = await connection.run_sync(_collect_table_names)
            except Exception as exc:
                return DiagnosticResult(
                    name="MariaDB",
                    success=False,
                    message=f"Failed to introspect tables: {exc}",
                    details=details,
                )

            missing_tables = sorted(REQUIRED_DB_TABLES - available_tables)
            details["tables_present"] = sorted(available_tables)
            details["missing_tables"] = missing_tables

            if missing_tables:
                return DiagnosticResult(
                    name="MariaDB",
                    success=False,
                    message=(
                        "Database is reachable but required tables are missing: "
                        + ", ".join(missing_tables)
                    ),
                    details=details,
                )
    except Exception as exc:  # pragma: no cover - network/database failures vary
        return DiagnosticResult(
            name="MariaDB",
            success=False,
            message=f"Database check failed: {exc}",
            details=details,
        )
    finally:
        try:
            await engine.dispose()
        except Exception:  # pragma: no cover - defensive cleanup
            pass

    return DiagnosticResult(
        name="MariaDB",
        success=True,
        message="Connectivity and schema checks succeeded.",
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
    results.append(await _diagnose_mariadb(config, dry_run=args.dry_run))
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

