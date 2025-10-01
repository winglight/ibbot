#!/usr/bin/env python3
"""Launch multiple background services with a single command."""

from __future__ import annotations

import argparse
import os
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
    "ib_command",
    "ib_feed",
    "strategy",
    "risk",
    "notification",
)

# Mapping of service name -> dedicated Redis URL override environment variable.
# If a service is not listed here it falls back to ``IBBOT_REDIS_URL``.
_SERVICE_REDIS_ENV: dict[str, str] = {
    "ib_command": "IB_COMMAND_REDIS_URL",
    "ib_feed": "IB_FEED_REDIS_URL",
    "strategy": "STRATEGY_REDIS_URL",
    "risk": "RISK_REDIS_URL",
    "notification": "NOTIFICATION_REDIS_URL",
}


def _load_env() -> Path | None:
    for candidate in (".env.local", ".env"):
        env_file = PROJECT_ROOT / candidate
        if env_file.exists():
            load_dotenv(env_file, override=False)
            return env_file
    return None


def _check_service_imports(services: Iterable[str]) -> list[str]:
    missing: list[str] = []
    for service in services:
        module_path = f"services.{service}.main"
        try:
            __import__(module_path)
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
            message.append(f"  python -m services.{service}.main")
        print("\n".join(message))
        return 0

    processes: list[tuple[str, subprocess.Popen[str]]] = []
    env = os.environ.copy()

    try:
        for service in services:
            module = f"services.{service}.main"
            cmd = [sys.executable, "-m", module]
            print(f"[INFO] 启动 {module}...")
            process = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=env)
            processes.append((service, process))

        exit_code = 0
        for service, process in processes:
            return_code = process.wait()
            if return_code != 0:
                print(
                    f"[ERROR] 服务 services.{service}.main 退出码 {return_code}，提前结束其余进程。",
                    file=sys.stderr,
                )
                exit_code = return_code
                break
    except KeyboardInterrupt:
        print("[INFO] 接收到中断信号，准备停止所有服务。")
        exit_code = 0
    finally:
        for service, process in processes:
            if process.poll() is None:
                process.terminate()

    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "按需批量启动多个后台服务。默认启动 ib_command、ib_feed、"
            "strategy、risk 与 notification。"
        )
    )
    parser.add_argument(
        "services",
        nargs="*",
        default=list(DEFAULT_SERVICES),
        help=(
            "要启动的服务名称列表。例如：python scripts/run_all.py "
            "ib_feed strategy。留空则使用默认组合。"
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
