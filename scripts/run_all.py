#!/usr/bin/env python3
"""Launch multiple background services with a single command."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Ensure the repository root is available for absolute imports like `services.*`
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_SERVICES: tuple[str, ...] = (
    "ib_command",
    "ib_feed",
    "strategy",
    "risk",
    "notification",
)


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


def run_services(services: Sequence[str], dry_run: bool = False) -> int:
    env_source = _load_env()

    missing = _check_service_imports(services)
    if missing:
        print(
            "[ERROR] 无法导入以下服务模块：" + ", ".join(missing) +
            "。请确认名称或代码路径是否正确。",
            file=sys.stderr,
        )
        return 2

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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_services(args.services, dry_run=args.dry_run)


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
