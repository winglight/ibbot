#!/usr/bin/env python3
"""Launch multiple background services with a single command."""

from __future__ import annotations

import argparse
import importlib
import os
import shlex
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse, urlsplit, urlunsplit

from dotenv import dotenv_values

from scripts.run_service import SERVICE_ALIASES, _resolve_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Ensure the repository root is available for absolute imports like `services.*`
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.dataclass_compat import enable_slots_compat

enable_slots_compat()
from src.ibbot.redis import DEFAULT_REDIS_URL, DEFAULT_REDIS_URL_ENV


DEFAULT_SERVICES: tuple[str, ...] = (
    "connection_control",
    "account",
    "orders",
    "ib_gateway",
    "strategy",
    "risk",
    "notification",
    "market_data",
    "queue_monitor",
    "optimizer",
)

# Mapping of service name -> dedicated Redis URL override environment variable.
# If a service is not listed here it falls back to ``IBBOT_REDIS_URL``.
_SERVICE_REDIS_ENV: dict[str, str] = {
    "connection_control": "CONNECTION_CONTROL_REDIS_URL",
    "ib_gateway": "IB_GATEWAY_REDIS_URL",
    "strategy": "STRATEGY_REDIS_URL",
    "risk": "RISK_REDIS_URL",
    "notification": "NOTIFICATION_REDIS_URL",
    "market_data": "DATA_LAYER_REDIS_URL",
    "queue_monitor": "QUEUE_MONITOR_REDIS_URL",
    "optimizer": "OPTIMIZER_REDIS_URL",
}

_SERVICE_REDIS_FALLBACK: dict[str, tuple[str, ...]] = {
    "ib_gateway": ("IB_COMMAND_REDIS_URL", "IB_FEED_REDIS_URL"),
}


def _build_service_command(service: str) -> tuple[list[str], str]:
    """Return the command used to start *service* and a descriptive label."""

    canonical = SERVICE_ALIASES.get(service, service)
    host = os.getenv("SERVICE_HOST", "0.0.0.0")

    if canonical == "account":
        port = os.getenv("ACCOUNT_SERVICE_PORT", "8020")
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "services.account.main:app",
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "orders":
        port = os.getenv("ORDERS_SERVICE_PORT", "8050")
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "services.orders.main:app",
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "risk":
        port = os.getenv("RISK_SERVICE_PORT", "8030")
        app_path = os.getenv("RISK_SERVICE_APP", "services.risk.http:app")
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "strategy":
        port = os.getenv("STRATEGY_SERVICE_PORT", "8045")
        app_path = os.getenv("STRATEGY_SERVICE_APP", "services.strategy.http:app")
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "connection_control":
        port = os.getenv("CONNECTION_CONTROL_PORT", "8056")
        app_path = "services.connection_control.main:create_app"
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--factory",
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "market_data":
        port = os.getenv("MARKET_DATA_PORT", "8010")
        app_path = "services.market_data.main:create_app"
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--factory",
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "queue_monitor":
        port = os.getenv("QUEUE_MONITOR_PORT", "8011")
        app_path = "services.queue_monitor.main:create_app"
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--factory",
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "notification":
        port = os.getenv("NOTIFICATION_SERVICE_PORT", "8055")
        app_path = "services.notification.main:app"
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "optimizer":
        port = os.getenv("OPTIMIZER_PORT", "8001")
        app_path = "services.optimizer.main:create_app"
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--factory",
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif canonical == "ib_gateway":
        module = "services.ib_gateway.main"
        cmd = [sys.executable, "-m", module]
    else:
        raise ValueError(f"Unknown service: {service}")

    return cmd, shlex.join(cmd)


def _redact_url_secret(value: str) -> str:
    """Return *value* with credentials removed for logging."""

    if "://" not in value:
        return value

    try:
        parsed = urlsplit(value)
    except ValueError:
        return value

    username = parsed.username or ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{username}@" if username else ""
    redacted_netloc = f"{auth}{hostname}{port}"

    return urlunsplit(
        (
            parsed.scheme,
            redacted_netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _log_environment_summary(
    env_source: Path | None,
    *,
    overridden: Sequence[str] = (),
    newly_added: Sequence[str] = (),
) -> None:
    if env_source is None:
        print("[INFO] 未找到 .env.local/.env，沿用当前进程环境变量。")
        return

    try:
        relative_path = env_source.relative_to(PROJECT_ROOT)
    except ValueError:
        relative_path = env_source

    print(f"[INFO] 从 {relative_path} 加载了环境变量。")

    if overridden:
        overridden_keys = "、".join(sorted(set(overridden)))
        print(f"        覆盖已有变量：{overridden_keys}")
    if newly_added:
        added_keys = "、".join(sorted(set(newly_added)))
        print(f"        新增变量：{added_keys}")

    watched_keys = (
        DEFAULT_REDIS_URL_ENV,
        "ORDERS_REDIS_URL",
        "IBBOT_DB_URL",
        "DATA_LAYER_DB_URL",
    )

    for key in watched_keys:
        value = os.getenv(key)
        if not value:
            continue
        print(f"        {key} = {_redact_url_secret(value)}")


def _load_env() -> tuple[Path | None, tuple[str, ...], tuple[str, ...]]:
    overridden: list[str] = []
    newly_added: list[str] = []

    for candidate in (".env.local", ".env"):
        env_file = PROJECT_ROOT / candidate
        if not env_file.exists():
            continue

        # Mimic ``python -m dotenv run`` semantics: values in the dotenv file
        # override anything that is already defined in the environment.
        values = {
            key: value
            for key, value in dotenv_values(env_file).items()
            if value is not None
        }

        for key, value in values.items():
            previous = os.environ.get(key)
            if previous is None:
                newly_added.append(key)
            elif previous != value:
                overridden.append(key)
            os.environ[key] = value

        return env_file, tuple(overridden), tuple(newly_added)

    return None, tuple(), tuple()


def _resolve_import_module(service: str) -> str:
    canonical = SERVICE_ALIASES.get(service, service)
    if service == "risk":
        app_path = os.getenv("RISK_SERVICE_APP", "services.risk.http:app")
        return app_path.split(":", 1)[0]
    if service == "strategy":
        app_path = os.getenv("STRATEGY_SERVICE_APP", "services.strategy.http:app")
        return app_path.split(":", 1)[0]
    if canonical in {"account", "orders"}:
        return f"services.{canonical}.main"
    return f"services.{canonical}.main"


def _check_service_imports(services: Iterable[str]) -> list[str]:
    missing: list[str] = []
    for service in services:
        module_path = _resolve_import_module(service)
        try:
            importlib.import_module(module_path)
        except ModuleNotFoundError:
            missing.append(service)
    return missing


def _iter_redis_urls(services: Sequence[str]) -> set[str]:
    """Return the set of Redis URLs referenced by the given services."""

    urls: set[str] = set()
    default_url = os.getenv(DEFAULT_REDIS_URL_ENV, DEFAULT_REDIS_URL)
    for service in services:
        canonical = SERVICE_ALIASES.get(service, service)
        env_key = _SERVICE_REDIS_ENV.get(canonical)
        url = os.getenv(env_key) if env_key else None
        if not url:
            for fallback in _SERVICE_REDIS_FALLBACK.get(canonical, ()):  # pragma: no cover - legacy env
                url = os.getenv(fallback)
                if url:
                    break
        urls.add(url or default_url)
    return urls


def _probe_redis_url(url: str) -> tuple[bool, str | None]:
    """Check whether we can establish a TCP connection to the Redis endpoint."""

    parsed = urlparse(url)
    if parsed.scheme == "unix":  # pragma: no cover - niche configuration
        if os.path.exists(parsed.path):
            return True, None
        return False, f"unix socket {parsed.path} 不存在"

    host = parsed.hostname or "localhost"
    port = parsed.port or (6380 if parsed.scheme == "rediss" else 6379)
    try:
        with closing(socket.create_connection((host, port), timeout=1.5)):
            return True, None
    except OSError as exc:  # pragma: no cover - depends on environment
        return False, f"无法连接 {host}:{port} - {exc.strerror or exc}"


def _ensure_redis_available(services: Sequence[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    for url in _iter_redis_urls(services):
        ok, message = _probe_redis_url(url)
        if not ok and message:
            errors.append(f"{url} -> {message}")
    return not errors, errors


def run_services(
    services: Sequence[str],
    dry_run: bool = False,
    *,
    skip_redis_check: bool = False,
) -> int:
    deduped: list[str] = []
    seen_canonical: set[str] = set()
    for service in services:
        canonical = SERVICE_ALIASES.get(service, service)
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)
        deduped.append(service)
    services = tuple(deduped)

    env_source, overridden, newly_added = _load_env()
    _log_environment_summary(env_source, overridden=overridden, newly_added=newly_added)

    missing = _check_service_imports(services)
    if missing:
        print(
            "[ERROR] 无法导入以下服务模块：" + ", ".join(missing) +
            "。请确认名称或代码路径是否正确。",
            file=sys.stderr,
        )
        return 2

    should_skip_redis_check = skip_redis_check or dry_run

    if not should_skip_redis_check:
        ok, errors = _ensure_redis_available(services)
        if not ok:
            print(
                "[ERROR] Redis 服务不可用。请先启动 redis-server 或在 .env 中指向可用实例。",
                file=sys.stderr,
            )
            for error in errors:
                print(f"        {error}", file=sys.stderr)
            print(
                "可以使用 --skip-redis-check 跳过此检查。",
                file=sys.stderr,
            )
            return 3

    if dry_run:
        message = [
            "[OK] 所有服务模块均可导入。",
            f"环境变量来源：{env_source.relative_to(PROJECT_ROOT) if env_source else '未找到 .env.local/.env'}",
            "启动命令示例：",
        ]
        for service in services:
            try:
                _, label = _build_service_command(service)
            except ValueError as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                return 2
            message.append(f"  {label}")
        print("\n".join(message))
        return 0

    processes: list[tuple[str, subprocess.Popen[str], str]] = []
    env = os.environ.copy()

    try:
        for service in services:
            try:
                cmd, label = _build_service_command(service)
            except ValueError as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                return 2
            print(f"[INFO] 启动 {service}: {label}")
            process = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=env)
            processes.append((service, process, label))

        exit_code = 0
        for service, process, label in processes:
            return_code = process.wait()
            if return_code != 0:
                print(
                    f"[ERROR] 服务 {service} ({label}) 退出码 {return_code}，提前结束其余进程。",
                    file=sys.stderr,
                )
                exit_code = return_code
                break
    except KeyboardInterrupt:
        print("[INFO] 接收到中断信号，准备停止所有服务。")
        exit_code = 0
    finally:
        for service, process, _ in processes:
            if process.poll() is None:
                process.terminate()

    return exit_code


def build_parser() -> argparse.ArgumentParser:
    default_services_display = "、".join(DEFAULT_SERVICES)
    parser = argparse.ArgumentParser(
        description=(
            "按需批量启动多个后台服务。默认启动以下服务："
            f"{default_services_display}。"
        )
    )
    parser.add_argument(
        "services",
        nargs="*",
        default=list(DEFAULT_SERVICES),
        help=(
            "要启动的服务名称列表。例如：python scripts/run_all.py "
            "ib_feed strategy。留空则使用默认组合（"
            f"{default_services_display}）。"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅检查模块导入与环境加载，不真正运行服务。",
    )
    parser.add_argument(
        "--skip-redis-check",
        action="store_true",
        help="跳过 Redis 可达性检查（例如使用云端 Redis 时）。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_services(
        args.services,
        dry_run=args.dry_run,
        skip_redis_check=args.skip_redis_check,
    )


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
