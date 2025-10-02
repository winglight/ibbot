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
from urllib.parse import urlparse

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Ensure the repository root is available for absolute imports like `services.*`
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.ibbot.redis import DEFAULT_REDIS_URL, DEFAULT_REDIS_URL_ENV


DEFAULT_SERVICES: tuple[str, ...] = (
    "connection_control",
    "account",
    "orders",
    "ib_command",
    "ib_feed",
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
    "ib_command": "IB_COMMAND_REDIS_URL",
    "ib_feed": "IB_FEED_REDIS_URL",
    "strategy": "STRATEGY_REDIS_URL",
    "risk": "RISK_REDIS_URL",
    "notification": "NOTIFICATION_REDIS_URL",
    "market_data": "DATA_LAYER_REDIS_URL",
    "queue_monitor": "QUEUE_MONITOR_REDIS_URL",
    "optimizer": "OPTIMIZER_REDIS_URL",
}


def _build_service_command(service: str) -> tuple[list[str], str]:
    """Return the command used to start *service* and a descriptive label."""

    host = os.getenv("SERVICE_HOST", "0.0.0.0")

    if service == "account":
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
    elif service == "orders":
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
    elif service == "risk":
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
    elif service == "strategy":
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
    elif service == "connection_control":
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
    elif service == "market_data":
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
    elif service == "queue_monitor":
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
    elif service == "notification":
        port = os.getenv("NOTIFICATION_SERVICE_PORT", "8055")
        app_path = "services.notification.main:create_app"
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
    elif service == "optimizer":
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
    elif service == "ib_command":
        module = "services.ib_command.main"
        cmd = [sys.executable, "-m", module]
    elif service == "ib_feed":
        module = "services.ib_feed.main"
        cmd = [sys.executable, "-m", module]
    else:
        raise ValueError(f"Unknown service: {service}")

    return cmd, shlex.join(cmd)


def _load_env() -> Path | None:
    for candidate in (".env.local", ".env"):
        env_file = PROJECT_ROOT / candidate
        if env_file.exists():
            load_dotenv(env_file, override=False)
            return env_file
    return None


def _resolve_import_module(service: str) -> str:
    if service == "risk":
        app_path = os.getenv("RISK_SERVICE_APP", "services.risk.http:app")
        return app_path.split(":", 1)[0]
    if service == "strategy":
        app_path = os.getenv("STRATEGY_SERVICE_APP", "services.strategy.http:app")
        return app_path.split(":", 1)[0]
    if service in {"account", "orders"}:
        return f"services.{service}.main"
    return f"services.{service}.main"


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
        env_key = _SERVICE_REDIS_ENV.get(service)
        url = os.getenv(env_key) if env_key else None
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
    env_source = _load_env()

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
