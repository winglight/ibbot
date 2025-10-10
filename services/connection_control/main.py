"""FastAPI application exposing IB connection control endpoints."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from src.connection_manager import ConnectionConfig
from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    DEFAULT_REDIS_URL,
    close_global_client,
    enqueue_ib_command,
    get_queue_length,
    get_redis_client,
)

SERVICE_NAME = "connection_control"
HEALTH_INTERVAL = 30.0


class ConnectionConfigPayload(BaseModel):
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=7497, ge=0, le=65535)
    client_id: int = Field(default=1, ge=0)
    paper_trading: bool = Field(default=True)

    def normalised(self) -> dict[str, Any]:
        return {
            "host": str(self.host).strip() or "127.0.0.1",
            "port": int(self.port),
            "client_id": int(self.client_id),
            "paper_trading": bool(self.paper_trading),
        }


def _resolve_redis_url() -> str:
    for key in ("CONNECTION_CONTROL_REDIS_URL", "IBBOT_REDIS_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return DEFAULT_REDIS_URL


def _coerce_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return int(default)
            return int(float(cleaned))
    except (TypeError, ValueError):
        return int(default)
    return int(default)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if not cleaned:
            return default
        if cleaned in {"1", "true", "yes", "y", "on", "paper"}:
            return True
        if cleaned in {"0", "false", "no", "n", "off", "live"}:
            return False
    return default


def _connection_config_from_env() -> dict[str, Any]:
    config = ConnectionConfig.from_env().to_dict()

    env_port = os.getenv("IBKR_PORT")
    env_client_id = os.getenv("IBKR_CLIENT_ID")
    env_paper = os.getenv("IBKR_PAPER_TRADING")
    env_host = os.getenv("IBKR_HOST")

    config["host"] = str(env_host.strip()) if isinstance(env_host, str) and env_host.strip() else config["host"]
    config["port"] = _coerce_int(env_port, config["port"])
    config["client_id"] = _coerce_int(env_client_id, config["client_id"])
    config["paper_trading"] = _coerce_bool(env_paper, config["paper_trading"])

    return config


async def _resolve_queue_key(app: FastAPI) -> str:
    cached = getattr(app.state, "queue_key", None)
    if isinstance(cached, str) and cached.strip():
        return cached

    default = DEFAULT_COMMAND_QUEUE
    override = os.getenv("CONNECTION_CONTROL_QUEUE_KEY") or os.getenv("IB_COMMAND_QUEUE")
    if isinstance(override, str) and override.strip():
        queue_key_str = override.strip()
    else:
        queue_key_str = default

    app.state.queue_key = queue_key_str
    return queue_key_str


async def _enqueue_command(app: FastAPI, method: str, *, metadata: Mapping[str, Any] | None = None) -> str:
    redis = getattr(app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis unavailable")

    queue_key = await _resolve_queue_key(app)
    command_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "command_id": command_id,
        "method": method,
        "args": [],
        "kwargs": {},
        "metadata": {"source": SERVICE_NAME, **(dict(metadata) if metadata else {})},
    }

    try:
        await enqueue_ib_command(payload, redis=redis, queue_key=queue_key)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to enqueue command") from exc

    return command_id


async def _load_config(app: FastAPI) -> dict[str, Any]:
    config = _connection_config_from_env()
    app.state.cached_config = dict(config)
    return dict(config)


async def _persist_config(app: FastAPI, values: Mapping[str, Any]) -> dict[str, Any]:
    _ = values
    config = await _load_config(app)
    return config


async def _publish_health(app: FastAPI, *, healthy: bool = True, last_error: str | None = None) -> None:
    redis = getattr(app.state, "redis", None)
    if redis is None:
        return

    try:
        queue_key = await _resolve_queue_key(app)
        queue_depth = await get_queue_length(queue_key, redis=redis)
    except Exception:
        queue_key = await _resolve_queue_key(app)
        queue_depth = -1

    metrics = {
        "queue_key": queue_key,
        "queue_depth": queue_depth,
    }

    config = getattr(app.state, "cached_config", None)
    if isinstance(config, Mapping):
        metrics.update({
            "host": config.get("host", ""),
            "port": config.get("port", 0),
            "client_id": config.get("client_id", 0),
            "paper_trading": config.get("paper_trading", False),
        })

    try:
        await update_service_health(
            redis,
            SERVICE_NAME,
            healthy=healthy,
            status="healthy" if healthy else "unhealthy",
            last_error=last_error,
            metrics=metrics,
        )
    except Exception:
        logger = get_logger(SERVICE_NAME)
        logger.debug("Failed to publish connection control health", exc_info=True)


async def _health_loop(app: FastAPI, interval: float) -> None:
    try:
        while interval > 0:
            await asyncio.sleep(interval)
            await _publish_health(app)
    except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
        raise


def create_app(*, health_interval: float = HEALTH_INTERVAL) -> FastAPI:
    logger = get_logger(SERVICE_NAME)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis_url = _resolve_redis_url()
        redis = await get_redis_client(redis_url)
        app.state.redis = redis
        app.state.queue_key = None
        app.state.cached_config = None

        if health_interval > 0:
            health_task = asyncio.create_task(_health_loop(app, float(health_interval)))
        else:
            health_task = None
        app.state.health_task = health_task

        await _load_config(app)
        await _publish_health(app)

        logger.info("Connection control service started", extra={"redis_url": redis_url})

        try:
            yield
        finally:
            task = getattr(app.state, "health_task", None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:  # pragma: no cover - depends on runtime timing
                    pass

            await close_global_client()

            logger.info("Connection control service stopped")

    app = FastAPI(title="Connection Control Service", version="1.0.0", lifespan=lifespan)

    @app.get("/connection/config")
    async def get_connection_config() -> Mapping[str, Any]:
        config = await _load_config(app)
        await _publish_health(app)
        return config

    @app.put("/connection/config")
    async def put_connection_config(payload: ConnectionConfigPayload) -> Mapping[str, Any]:
        config = await _persist_config(app, payload.normalised())
        await _publish_health(app)
        return config

    @app.get("/connection/status")
    async def get_connection_status() -> Mapping[str, Any]:
        config = await _load_config(app)
        queue_key = await _resolve_queue_key(app)
        redis = getattr(app.state, "redis", None)
        queue_depth = None
        if redis is not None:
            try:
                queue_depth = await get_queue_length(queue_key, redis=redis)
            except Exception:
                queue_depth = None

        await _publish_health(app)
        return {
            "service": SERVICE_NAME,
            "config": config,
            "queue_key": queue_key,
            "queue_depth": queue_depth,
        }

    @app.post("/connection/connect")
    async def post_connection_connect(payload: ConnectionConfigPayload) -> Mapping[str, Any]:
        config = await _persist_config(app, payload.normalised())

        command_id = await _enqueue_command(app, "connect", metadata={"config": config})

        await _publish_health(app)
        return {
            "status": "queued",
            "command_id": command_id,
            "config": config,
        }

    @app.post("/connection/disconnect")
    async def post_connection_disconnect() -> Mapping[str, Any]:
        command_id = await _enqueue_command(app, "disconnect")
        await _publish_health(app)
        return {
            "status": "queued",
            "command_id": command_id,
        }

    return app


def main() -> None:  # pragma: no cover - CLI entrypoint
    import uvicorn

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8056)


if __name__ == "__main__":  # pragma: no cover - CLI execution
    main()
