"""FastAPI application exposing strategy management endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import secrets
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError, validator

from services.strategy import StrategyTaskConsumer
from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.ibbot.redis import DEFAULT_REDIS_URL, close_global_client, get_redis_client

SERVICE_NAME = "strategy"
DEFAULT_STRATEGY_QUEUE = "list:queue:strategy"
DEFAULT_EVENT_STREAM = "stream:strategy:events"
DEFAULT_RESULT_STREAM = "stream:ib:results"
DEFAULT_HEALTH_INTERVAL = 15.0
DEFAULT_EVENT_POLL_INTERVAL = 1.0
DEFAULT_METADATA_PREFIX = "json:strategy"
DEFAULT_SETTINGS_KEY = f"{DEFAULT_METADATA_PREFIX}:settings"

DEFAULT_SETTINGS: dict[str, Any] = {
    "api_endpoint": "/api/strategy",
    "api_method": "POST",
    "api_payload_template": {
        "strategy": "{{strategy_name}}",
        "symbol": "{{symbol}}",
        "action": "{{action}}",
        "qty": "{{quantity}}",
        "type": "{{order_type}}",
        "price": "{{price}}",
    },
    "strategy_path": "src/strategies/",
    "enable_auth": False,
    "auth_token": None,
    "pause_data_push_on_holidays": True,
}


class StrategyDocument(BaseModel):
    """Representation of a stored strategy definition."""

    name: str
    factory: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    enabled: bool = True
    created_at: str
    updated_at: str


class StrategyUpsertRequest(BaseModel):
    """Payload for creating or updating a strategy definition."""

    name: str
    factory: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    enabled: bool = True

    @validator("name")
    def _validate_name(cls, value: str) -> str:  # noqa: D401 - short validation helper
        value = value.strip()
        if not value:
            raise ValueError("strategy name must not be empty")
        return value


class StrategyRunRequest(BaseModel):
    """Payload describing a task submission request."""

    task_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class StrategyRunResponse(BaseModel):
    """Response returned when a strategy task is enqueued."""

    task_id: str
    queue_size: int


class StrategySettingsPayload(BaseModel):
    """Payload describing strategy service configuration settings."""

    api_endpoint: str | None = None
    api_method: str | None = None
    api_payload_template: dict[str, Any] | None = None
    strategy_path: str | None = None
    enable_auth: bool | None = None
    auth_token: str | None = None
    pause_data_push_on_holidays: bool | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_json_payload(data: Any) -> dict[str, Any]:
    if not data:
        return {}
    if isinstance(data, Mapping):
        return {str(k): v for k, v in data.items()}
    if isinstance(data, str):
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return {}
        if isinstance(payload, Mapping):
            return {str(k): v for k, v in payload.items()}
    return {}


class StrategyRuntime:
    """Runtime container coordinating strategy metadata and task queue state."""

    def __init__(
        self,
        redis_client: Any,
        *,
        queue_key: str = DEFAULT_STRATEGY_QUEUE,
        event_stream: str = DEFAULT_EVENT_STREAM,
        result_stream: str = DEFAULT_RESULT_STREAM,
        metadata_prefix: str = DEFAULT_METADATA_PREFIX,
        health_interval: float = DEFAULT_HEALTH_INTERVAL,
        event_poll_interval: float = DEFAULT_EVENT_POLL_INTERVAL,
        consumer: StrategyTaskConsumer | None = None,
    ) -> None:
        self._redis = redis_client
        self._queue_key = queue_key
        self._event_stream = event_stream
        self._result_stream = result_stream
        self._metadata_prefix = metadata_prefix.rstrip(":")
        self._metadata_index_key = f"{self._metadata_prefix}:index"
        self._strategies: dict[str, StrategyDocument] = {}
        self._lock = asyncio.Lock()
        self._health_interval = max(0.1, float(health_interval))
        self._event_poll_interval = max(0.1, float(event_poll_interval))
        self._stop = asyncio.Event()
        self._health_signal = asyncio.Event()
        self._event_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._last_event_id: str | None = None
        self._last_result_id: str | None = None
        self._logger = get_logger("ibbot.services.strategy.http")
        self._consumer = consumer
        self.pending_tasks: int = 0
        self.last_event_at: str | None = None

    @property
    def queue_key(self) -> str:
        return self._queue_key

    @property
    def event_stream(self) -> str:
        return self._event_stream

    async def start(self) -> None:
        await self._load_metadata()
        self.pending_tasks = await self._get_queue_length()
        self._stop.clear()
        if self._consumer is not None:
            self._consumer_task = asyncio.create_task(self._consumer.run_forever())
        self._event_task = asyncio.create_task(self._event_loop())
        self._health_task = asyncio.create_task(self._health_loop())
        await self._publish_health(healthy=True)

    async def stop(self) -> None:
        self._stop.set()
        if self._event_task is not None:
            self._event_task.cancel()
        if self._health_task is not None:
            self._health_task.cancel()
        if self._consumer is not None:
            self._consumer.stop()
        tasks = [t for t in (self._event_task, self._health_task, self._consumer_task) if t is not None]
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):  # type: ignore[name-defined]
                await task
        await self._publish_health(healthy=False)

    async def list_strategies(self) -> list[StrategyDocument]:
        async with self._lock:
            return [doc.copy() for doc in sorted(self._strategies.values(), key=lambda d: d.name)]

    async def get_strategy(self, name: str) -> StrategyDocument | None:
        async with self._lock:
            document = self._strategies.get(name)
            return document.copy() if document is not None else None

    async def upsert_strategy(self, payload: StrategyUpsertRequest) -> tuple[StrategyDocument, bool]:
        now = _utc_now().isoformat()
        async with self._lock:
            existing = self._strategies.get(payload.name)
            created = existing is None
            created_at = existing.created_at if existing is not None else now
            document = StrategyDocument(
                name=payload.name,
                factory=payload.factory,
                params=payload.params,
                metadata=payload.metadata,
                description=payload.description,
                enabled=payload.enabled,
                created_at=created_at,
                updated_at=now,
            )
            self._strategies[payload.name] = document
            await self._persist_document(document)
            await self._persist_index()
        self._health_signal.set()
        return document, created

    async def delete_strategy(self, name: str) -> bool:
        async with self._lock:
            document = self._strategies.pop(name, None)
            if document is None:
                return False
            await self._persist_index()
        metadata_key = self._metadata_key(name)
        try:
            await self._redis.delete(metadata_key)
        except Exception:
            self._logger.debug("Failed to delete strategy metadata", exc_info=True)
        self._health_signal.set()
        return True

    async def enqueue_task(self, name: str, payload: StrategyRunRequest) -> StrategyRunResponse:
        strategy = await self.get_strategy(name)
        if strategy is None:
            raise StrategyNotRegistered(name)
        task_id = payload.task_id or f"{name}-{_utc_now().timestamp():.6f}"
        task_payload = {
            "task_id": task_id,
            "strategy": name,
            "params": payload.params,
        }
        try:
            await self._redis.rpush(self._queue_key, json.dumps(task_payload))
        except Exception as exc:
            raise TaskEnqueueError(str(exc)) from exc
        self.pending_tasks = await self._get_queue_length()
        self._health_signal.set()
        return StrategyRunResponse(task_id=task_id, queue_size=self.pending_tasks)

    async def _get_queue_length(self) -> int:
        try:
            return int(await self._redis.llen(self._queue_key))
        except Exception:
            return self.pending_tasks

    async def _persist_document(self, document: StrategyDocument) -> None:
        metadata_key = self._metadata_key(document.name)
        await self._redis.set(metadata_key, json.dumps(document.dict()))

    async def _persist_index(self) -> None:
        payload = [doc.name for doc in sorted(self._strategies.values(), key=lambda d: d.name)]
        await self._redis.set(self._metadata_index_key, json.dumps(payload))

    def _metadata_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}"

    async def _load_metadata(self) -> None:
        raw_index = await self._redis.get(self._metadata_index_key)
        names = []
        if isinstance(raw_index, str):
            try:
                decoded = json.loads(raw_index)
            except json.JSONDecodeError:
                decoded = []
            if isinstance(decoded, list):
                names = [str(entry) for entry in decoded]
        elif isinstance(raw_index, list):
            names = [str(entry) for entry in raw_index]
        async with self._lock:
            self._strategies.clear()
            for name in names:
                raw = await self._redis.get(self._metadata_key(name))
                if not raw:
                    continue
                payload = _normalise_json_payload(raw)
                if not payload:
                    continue
                try:
                    document = StrategyDocument.parse_obj(payload)
                except ValidationError:
                    continue
                self._strategies[name] = document

    async def _event_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await self._poll_events()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._event_poll_interval)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise

    async def _poll_events(self) -> None:
        await self._consume_stream(self._event_stream, "_last_event_id", adjust_pending=True)
        await self._consume_stream(self._result_stream, "_last_result_id", adjust_pending=False)

    async def _consume_stream(self, stream: str, attr: str, *, adjust_pending: bool) -> None:
        if not stream:
            return
        try:
            entries = await self._redis.xrange(stream)
        except Exception:
            self._logger.debug("Failed to read stream %s", stream, exc_info=True)
            return
        if not entries:
            return
        last_id: str | None = getattr(self, attr)
        new_entries: list[tuple[str, dict[str, str]]] = []
        for message_id, data in entries:
            if last_id is not None and message_id <= last_id:
                continue
            new_entries.append((message_id, data))
        if not new_entries:
            return
        setattr(self, attr, new_entries[-1][0])
        self.last_event_at = _utc_now().isoformat()
        if adjust_pending:
            for _, data in new_entries:
                status = str(data.get("status") or "").lower()
                if status in {"completed", "failed"} and self.pending_tasks > 0:
                    self.pending_tasks = max(0, self.pending_tasks - 1)
        self._health_signal.set()

    async def _health_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await self._publish_health(healthy=True)
                try:
                    await asyncio.wait_for(self._health_signal.wait(), timeout=self._health_interval)
                    self._health_signal.clear()
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise

    async def _publish_health(self, *, healthy: bool) -> None:
        metrics = {
            "pending_tasks": self.pending_tasks,
            "last_event_at": self.last_event_at,
        }
        try:
            await update_service_health(
                self._redis,
                SERVICE_NAME,
                healthy=healthy,
                metrics=metrics,
            )
        except Exception:
            self._logger.debug("Failed to publish strategy health", exc_info=True)


class StrategyError(RuntimeError):
    """Base error for strategy HTTP operations."""


class StrategyNotRegistered(StrategyError):
    """Raised when attempting to access a strategy that is not stored."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown strategy: {name}")
        self.name = name


class TaskEnqueueError(StrategyError):
    """Raised when a strategy task cannot be enqueued."""


@asynccontextmanager
async def _lifespan(
    app: FastAPI,
    *,
    runtime_factory: Callable[[Any], StrategyRuntime],
    redis_factory: Callable[[], Any] | None = None,
) -> AsyncIterator[None]:
    logger = get_logger("ibbot.services.strategy.http")
    if redis_factory is None:
        redis = await get_redis_client()
        owns_redis = True
    else:
        candidate = redis_factory()
        redis = await candidate if asyncio.iscoroutine(candidate) else candidate
        owns_redis = False
    runtime = runtime_factory(redis)
    try:
        await runtime.start()
        app.state.redis = redis
        app.state.runtime = runtime
        yield
    finally:
        with contextlib.suppress(Exception):  # type: ignore[name-defined]
            await runtime.stop()
        if owns_redis:
            with contextlib.suppress(Exception):
                await close_global_client()


def create_app(
    *,
    runtime: StrategyRuntime | None = None,
    redis_factory: Callable[[], Any] | None = None,
    queue_key: str = DEFAULT_STRATEGY_QUEUE,
    event_stream: str = DEFAULT_EVENT_STREAM,
    result_stream: str = DEFAULT_RESULT_STREAM,
    health_interval: float = DEFAULT_HEALTH_INTERVAL,
    event_poll_interval: float = DEFAULT_EVENT_POLL_INTERVAL,
    consumer_factory: Callable[[Any], StrategyTaskConsumer] | None = None,
) -> FastAPI:
    def _runtime_factory(redis: Any) -> StrategyRuntime:
        if runtime is not None:
            return runtime
        consumer = consumer_factory(redis) if consumer_factory is not None else None
        return StrategyRuntime(
            redis,
            queue_key=queue_key,
            event_stream=event_stream,
            result_stream=result_stream,
            health_interval=health_interval,
            event_poll_interval=event_poll_interval,
            consumer=consumer,
        )

    app = FastAPI(
        title="Strategy Service",
        version="1.0.0",
        lifespan=lambda app: _lifespan(app, runtime_factory=_runtime_factory, redis_factory=redis_factory),
    )

    async def _get_runtime(request: Request) -> StrategyRuntime:
        runtime_obj: StrategyRuntime | None = getattr(request.app.state, "runtime", None)
        if runtime_obj is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="strategy runtime unavailable")
        return runtime_obj

    async def _get_redis(request: Request) -> Any:
        redis_client: Any | None = getattr(request.app.state, "redis", None)
        if redis_client is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="strategy redis unavailable")
        return redis_client

    async def _load_settings(redis_client: Any) -> dict[str, Any]:
        try:
            raw = await redis_client.get(DEFAULT_SETTINGS_KEY)
        except Exception:
            raw = None
        payload: dict[str, Any] | None = None
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                payload = decoded
        elif isinstance(raw, Mapping):
            payload = dict(raw)

        settings = {**DEFAULT_SETTINGS}
        if payload:
            for key, value in payload.items():
                if value is None:
                    continue
                settings[key] = value
        if not isinstance(settings.get("api_payload_template"), dict):
            settings["api_payload_template"] = dict(DEFAULT_SETTINGS["api_payload_template"])
        settings["enable_auth"] = bool(settings.get("enable_auth"))
        settings["pause_data_push_on_holidays"] = bool(settings.get("pause_data_push_on_holidays", True))
        return settings

    async def _persist_settings(redis_client: Any, settings: dict[str, Any]) -> None:
        payload = json.dumps(settings, ensure_ascii=False)
        updated_at = _utc_now().isoformat()
        try:
            await redis_client.set(DEFAULT_SETTINGS_KEY, payload)
        except Exception:  # pragma: no cover - defensive guard
            logger = get_logger("ibbot.services.strategy.http")
            logger.warning("Failed to persist strategy settings", exc_info=True)
            return

        try:
            await update_service_health(
                redis_client,
                SERVICE_NAME,
                healthy=True,
                metrics={"settings_updated_at": updated_at},
            )
        except Exception:  # pragma: no cover - defensive guard
            logger = get_logger("ibbot.services.strategy.http")
            logger.debug("Failed to update strategy settings health metrics", exc_info=True)

    @app.get("/strategy", response_model=list[StrategyDocument])
    async def list_strategies(runtime: StrategyRuntime = Depends(_get_runtime)) -> list[StrategyDocument]:
        return await runtime.list_strategies()

    @app.get("/strategy/settings")
    async def get_strategy_settings(request: Request) -> dict[str, Any]:
        redis_client = await _get_redis(request)
        settings = await _load_settings(redis_client)
        return {"success": True, "settings": settings}

    @app.post("/strategy/settings")
    async def update_strategy_settings(
        payload: StrategySettingsPayload,
        request: Request,
    ) -> dict[str, Any]:
        redis_client = await _get_redis(request)
        settings = await _load_settings(redis_client)
        updates = payload.dict(exclude_none=True)
        if "api_payload_template" in updates and not isinstance(updates["api_payload_template"], dict):
            updates.pop("api_payload_template")
        settings.update(updates)
        settings["enable_auth"] = bool(settings.get("enable_auth"))
        settings["pause_data_push_on_holidays"] = bool(settings.get("pause_data_push_on_holidays", True))
        if "auth_token" not in settings:
            settings["auth_token"] = None
        await _persist_settings(redis_client, settings)
        return {"success": True, "settings": settings}

    @app.post("/strategy/settings/generate-token")
    async def generate_strategy_token(request: Request) -> dict[str, Any]:
        redis_client = await _get_redis(request)
        settings = await _load_settings(redis_client)
        token = secrets.token_urlsafe(24)
        settings["auth_token"] = token
        settings["enable_auth"] = True
        await _persist_settings(redis_client, settings)
        return {"success": True, "token": token, "settings": settings}

    @app.get("/strategy/{name}", response_model=StrategyDocument)
    async def get_strategy(name: str, runtime: StrategyRuntime = Depends(_get_runtime)) -> StrategyDocument:
        document = await runtime.get_strategy(name)
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")
        return document

    @app.post("/strategy", response_model=StrategyDocument, status_code=status.HTTP_201_CREATED)
    async def create_strategy(
        payload: StrategyUpsertRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> StrategyDocument:
        document, created = await runtime.upsert_strategy(payload)
        if not created:
            return document
        return document

    @app.put("/strategy", response_model=StrategyDocument)
    async def update_strategy(
        payload: StrategyUpsertRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> StrategyDocument:
        document, _ = await runtime.upsert_strategy(payload)
        return document

    @app.delete("/strategy/{name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_strategy(name: str, runtime: StrategyRuntime = Depends(_get_runtime)) -> None:
        deleted = await runtime.delete_strategy(name)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")

    @app.post("/strategy/{name}/run", response_model=StrategyRunResponse, status_code=status.HTTP_202_ACCEPTED)
    async def run_strategy(
        name: str,
        payload: StrategyRunRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> StrategyRunResponse:
        try:
            return await runtime.enqueue_task(name, payload)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except TaskEnqueueError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    return app


app = create_app()


__all__ = [
    "StrategyRuntime",
    "StrategyDocument",
    "StrategyUpsertRequest",
    "StrategyRunRequest",
    "StrategyRunResponse",
    "create_app",
    "app",
]
