"""Tests for project-wide environment loading behaviour."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cache import _load_config
from src.config import load_project_env


def test_project_env_file_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """`load_project_env` should populate key defaults from the tracked `.env`."""

    env_path = Path(__file__).resolve().parents[2] / ".env"
    expected = dotenv_values(env_path)
    keys = ["WEB_PORT", "IBKR_CLIENT_ID", "ADMIN_USERNAME"]

    load_project_env.cache_clear()
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    load_project_env()

    for key in keys:
        assert os.getenv(key) == expected[key]


def test_redis_config_uses_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Redis variables are absent, code defaults should be applied."""

    for key in ["REDIS_URL", "REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD", "REDIS_SSL"]:
        monkeypatch.delenv(key, raising=False)

    config = _load_config()

    assert config.url is None
    assert config.host == "localhost"
    assert config.port == 6379
    assert config.db == 0
    assert config.password is None
    assert config.ssl is False
