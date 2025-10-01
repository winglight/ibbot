"""Configuration helpers for standalone ibbot services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    """Return a non-negative integer value from the environment."""

    raw = os.getenv(name)
    if raw is None:
        return max(minimum, default)
    try:
        cleaned = raw.split("#", 1)[0].strip()
        if cleaned == "":
            return max(minimum, default)
        value = int(cleaned)
    except Exception:
        return max(minimum, default)
    return max(minimum, value)


def _str_env(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = raw.strip()
    return cleaned if cleaned else default


@dataclass(slots=True)
class AccountSnapshotServiceConfig:
    """Configuration parameters for the account snapshot service."""

    redis_url: str
    service_name: str
    refresh_interval_seconds: int
    check_interval_seconds: int
    request_timeout_seconds: int
    command_timeout_seconds: int
    command_queue_key: str
    command_result_prefix: str
    command_result_ttl_seconds: int
    control_queue_key: str
    signal_fast_positions: bool


def resolve_redis_url(prefix: Optional[str] = None) -> str:
    """Resolve the Redis URL for a service."""

    if prefix:
        explicit = os.getenv(f"{prefix}_REDIS_URL")
        if explicit:
            return explicit

    shared = os.getenv("REDIS_URL") or os.getenv("DATA_LAYER_REDIS_URL")
    if shared:
        return shared

    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = os.getenv("REDIS_PORT", "6379")
    db = os.getenv("REDIS_DB", "0")
    password = os.getenv("REDIS_PASSWORD")

    if password:
        return f"redis://:{password}@{host}:{port}/{db}"

    return f"redis://{host}:{port}/{db}"


def load_account_snapshot_config() -> AccountSnapshotServiceConfig:
    """Load configuration for the account snapshot service from the environment."""

    redis_url = resolve_redis_url("ACCOUNT_SNAPSHOT")
    service_name = _str_env(
        "ACCOUNT_SNAPSHOT_SERVICE_NAME", "account-snapshot-service"
    )
    refresh_interval = _int_env(
        "ACCOUNT_SNAPSHOT_REFRESH_INTERVAL_SECONDS", 300, minimum=0
    )
    check_interval_default = min(60, max(15, refresh_interval // 2 or 30))
    check_interval = _int_env(
        "ACCOUNT_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        check_interval_default,
        minimum=1,
    )
    request_timeout = _int_env(
        "ACCOUNT_SNAPSHOT_REFRESH_TIMEOUT_SECONDS", 15, minimum=1
    )
    command_timeout = _int_env(
        "ACCOUNT_SNAPSHOT_COMMAND_TIMEOUT_SECONDS", request_timeout, minimum=1
    )
    command_queue_key = _str_env(
        "ACCOUNT_SNAPSHOT_COMMAND_QUEUE_KEY", "queue:ib:commands"
    )
    command_result_prefix = _str_env(
        "ACCOUNT_SNAPSHOT_COMMAND_RESULT_PREFIX", "hash:ib:result:"
    )
    command_result_ttl = _int_env(
        "ACCOUNT_SNAPSHOT_RESULT_TTL_SECONDS", 300, minimum=10
    )
    control_queue_key = _str_env(
        "ACCOUNT_SNAPSHOT_CONTROL_QUEUE_KEY",
        "queue:account-snapshot:control",
    )
    signal_fast_positions = _str_env(
        "ACCOUNT_SNAPSHOT_FAST_POSITIONS", "true"
    ).lower() in {"1", "true", "yes", "y", "on"}

    return AccountSnapshotServiceConfig(
        redis_url=redis_url,
        service_name=service_name,
        refresh_interval_seconds=refresh_interval,
        check_interval_seconds=check_interval,
        request_timeout_seconds=request_timeout,
        command_timeout_seconds=command_timeout,
        command_queue_key=command_queue_key,
        command_result_prefix=command_result_prefix,
        command_result_ttl_seconds=command_result_ttl,
        control_queue_key=control_queue_key,
        signal_fast_positions=signal_fast_positions,
    )


__all__ = [
    "AccountSnapshotServiceConfig",
    "load_account_snapshot_config",
    "resolve_redis_url",
]
