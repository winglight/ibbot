from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_run_service_dry_run(capsys) -> None:
    run_service = importlib.import_module("scripts.run_service")

    exit_code = run_service.run_service("ib_command", dry_run=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ib_gateway" in captured.out
    assert "python -m services.ib_gateway.main" in captured.out


def test_run_service_unknown_module(capsys) -> None:
    run_service = importlib.import_module("scripts.run_service")

    exit_code = run_service.run_service("unknown_service", dry_run=True)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "无法找到服务模块" in captured.err


def test_run_all_dry_run_subset(capsys) -> None:
    run_all = importlib.import_module("scripts.run_all")

    exit_code = run_all.run_services(["ib_command", "ib_feed"], dry_run=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ib_gateway" in captured.out
    assert "services.ib_gateway.main" in captured.out


def test_run_all_reports_missing_service(capsys) -> None:
    run_all = importlib.import_module("scripts.run_all")

    exit_code = run_all.run_services(["ib_command", "missing_service"], dry_run=True)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "无法导入以下服务模块" in captured.err


def test_run_all_cli_dry_run_ib_command() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_all.py", "--dry-run", "ib_command"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
