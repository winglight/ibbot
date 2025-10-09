#!/usr/bin/env python3
"""Utility script for launching individual background services.

This helper mirrors ``python -m services.<module>.main`` while making sure
``.env.local`` (and ``.env`` as a fallback) are loaded before the service
starts.  It also exposes a ``--dry-run`` mode which only performs import
checks so we can surface helpful diagnostics without touching external
dependencies such as Redis or MariaDB.
"""

from __future__ import annotations

import argparse
import importlib
import os
import runpy
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SERVICE_ALIASES: dict[str, str] = {}

LEGACY_SERVICES = {"ib_command", "ib_feed"}


def _load_env() -> Path | None:
    """Load ``.env.local`` (or ``.env``) into the current environment."""

    loaded_from: Path | None = None
    env_candidates: Iterable[str] = (".env.local", ".env")

    for file_name in env_candidates:
        candidate = PROJECT_ROOT / file_name
        if candidate.exists() and load_dotenv(candidate, override=False):
            loaded_from = candidate
            break

    # ``load_dotenv`` returns ``False`` if the file exists but is empty; in
    # that scenario we still want to report its path as the chosen source.
    if loaded_from is None:
        for file_name in env_candidates:
            candidate = PROJECT_ROOT / file_name
            if candidate.exists():
                loaded_from = candidate
                break

    return loaded_from


def _resolve_module(service_name: str) -> tuple[str, str]:
    if service_name in LEGACY_SERVICES:
        raise RuntimeError(
            f"Service '{service_name}' has been removed as part of the Interactive Brokers refactor"
        )
    canonical = SERVICE_ALIASES.get(service_name, service_name)
    module_path = f"services.{canonical}.main"
    return canonical, module_path


def _import_service(service_name: str) -> tuple[ModuleType, str]:
    """Import the ``services.<name>.main`` module."""

    _, module_path = _resolve_module(service_name)

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised in tests
        missing = exc.name or module_path
        raise ModuleNotFoundError(
            f"无法找到服务模块 '{module_path}'。请确认名称是否正确 "
            "（例如 strategy、notification）。"
        ) from exc

    if not hasattr(module, "main"):
        raise AttributeError(
            f"模块 '{module_path}' 缺少 'main' 入口函数，无法直接启动。"
        )

    return module, module_path


def run_service(service_name: str, dry_run: bool = False) -> int:
    """Run or validate a single service.

    Parameters
    ----------
    service_name:
        The short name of the service under ``services/<name>/main.py``.
    dry_run:
        When ``True`` the function only validates imports and prints hints.
    """

    env_path = _load_env()
    try:
        canonical_name, module_path = _resolve_module(service_name)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    try:
        module, _ = _import_service(service_name)
    except (ModuleNotFoundError, AttributeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except ImportError as exc:  # pragma: no cover - defensive guard
        print(
            f"[ERROR] 导入模块 '{module_path}' 时发生异常：{exc}.\n"
            "请确认依赖是否安装完整（pip install -r requirements.txt）。",
            file=sys.stderr,
        )
        return 2

    env_msg = (
        f"环境变量来源：{env_path.relative_to(PROJECT_ROOT) if env_path else '未找到 .env.local/.env'}"
    )

    if dry_run:
        print(
            f"[OK] {module_path} 导入成功，找到 main()。\n"
            f"{env_msg}.\n"
            "使用以下命令即可启动：\n"
            f"  python -m {module_path}",
        )
        return 0

    if canonical_name != service_name:
        print(
            f"[INFO] 服务 '{service_name}' 已重定向到 {module_path}。"
            "原始名称将逐步下线。"
        )

    print(f"[INFO] 正在启动 {module_path} (Ctrl+C 退出)。")
    print(env_msg)

    try:
        runpy.run_module(module_path, run_name="__main__")
    except KeyboardInterrupt:
        print("[INFO] 接收到中断信号，准备退出。")
        return 0
    except Exception as exc:  # pragma: no cover - runtime safety
        print(
            f"[ERROR] 服务 '{module_path}' 运行失败：{exc}",
            file=sys.stderr,
        )
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "启动单个后台服务（等价于 python -m services.<module>.main），"
            "并自动加载 .env.local/.env。"
        )
    )
    parser.add_argument(
        "service",
        help="服务模块名称，例如 strategy、notification 等。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅检查导入与环境加载，不真正运行服务。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_service(args.service, dry_run=args.dry_run)


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
