"""FastAPI application exposing strategy management endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, ValidationError, validator

from services.strategy import StrategyTaskConsumer, sync_strategy_metadata
from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.ibbot.redis import DEFAULT_REDIS_URL, close_global_client, get_redis_client
from src.models import (
    ExternalStrategyOrderRequest,
    StrategyDataPushRequest,
    StrategyParameterUpdateRequest,
    StrategyRenameRequest,
    StrategyRiskSettings,
)
from src.strategy_risk import StrategyRiskManager
from src.utils import now_local, parse_local_datetime
from src.utils.strategy_cache import (
    compute_performance_metrics,
    get_daily_pnl,
    get_recent_fills,
    get_strategy_orders,
    get_strategy_orders_in_range,
    mark_strategy_cache_stale,
)

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


class StrategyTogglePayload(BaseModel):
    """Request payload for toggling strategy availability."""

    enabled: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def load_strategy_settings(redis_client: Any) -> dict[str, Any]:
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


async def persist_strategy_settings(redis_client: Any, settings: dict[str, Any]) -> None:
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

    _FILE_METADATA_KEYS = ("file", "file_path", "filepath", "path", "script", "scripts", "files", "paths")
    _STRATEGY_FILE_SUFFIXES = {".py", ".json", ".yaml", ".yml"}

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
        risk_manager: StrategyRiskManager | None = None,
        data_push_handler: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None,
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
        self._risk_manager = risk_manager
        self._data_push_handler = data_push_handler
        self.pending_tasks: int = 0
        self.last_event_at: str | None = None

    @property
    def queue_key(self) -> str:
        return self._queue_key

    @property
    def event_stream(self) -> str:
        return self._event_stream

    async def start(self) -> None:
        await self._synchronise_metadata()
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

    async def set_strategy_enabled(self, name: str, *, enabled: bool) -> StrategyDocument:
        async with self._lock:
            document = self._strategies.get(name)
            if document is None:
                raise StrategyNotRegistered(name)
            updated = document.copy(update={"enabled": bool(enabled), "updated_at": _utc_now().isoformat()})
            self._strategies[name] = updated
            await self._persist_document(updated)
        self._health_signal.set()
        return updated

    async def reset_strategy(self, name: str) -> StrategyDocument:
        async with self._lock:
            document = self._strategies.get(name)
            if document is None:
                raise StrategyNotRegistered(name)
            updated = document.copy(
                update={
                    "params": {},
                    "metadata": {},
                    "enabled": True,
                    "updated_at": _utc_now().isoformat(),
                }
            )
            self._strategies[name] = updated
            await self._persist_document(updated)
        self._health_signal.set()
        return updated

    async def list_strategy_files(self) -> list[str]:
        try:
            settings = await load_strategy_settings(self._redis)
        except Exception:  # pragma: no cover - defensive guard
            settings = dict(DEFAULT_SETTINGS)
        strategy_path = settings.get("strategy_path") or DEFAULT_SETTINGS.get("strategy_path")

        entries: set[str] = set()
        async with self._lock:
            for document in self._strategies.values():
                entries.update(self._extract_metadata_files(document.metadata))

        for candidate in self._scan_strategy_path(strategy_path):
            entries.add(candidate)

        return sorted(entries)

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

    async def get_executions(
        self,
        name: str,
        *,
        limit: int = 20,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)

        try:
            limit_value = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if limit_value <= 0:
            limit_value = 1

        if force_refresh:
            try:
                await mark_strategy_cache_stale(name)
            except Exception:
                self._logger.debug("Failed to mark strategy cache as stale", exc_info=True)

        snapshots: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        try:
            recent = await get_recent_fills(name, limit=limit_value)
        except Exception:
            self._logger.debug("Failed to fetch recent strategy fills", exc_info=True)
            recent = []

        try:
            orders = await get_strategy_orders(name)
        except Exception:
            self._logger.debug("Failed to fetch strategy orders", exc_info=True)
            orders = []

        for entry in recent + orders:
            normalised = self._normalise_execution_payload(name, entry)
            if normalised is None:
                continue
            identity = self._execution_identity(normalised)
            if identity in seen:
                continue
            seen.add(identity)
            snapshots.append(normalised)

        snapshots.sort(key=self._execution_sort_key, reverse=True)
        limited = snapshots[:limit_value] if limit_value else snapshots
        return {"success": True, "executions": limited}

    async def clear_executions(self, name: str) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)

        keys = self._strategy_cache_keys(name)
        try:
            await self._redis.delete(*keys)
        except Exception:
            self._logger.debug("Failed to delete strategy execution cache", exc_info=True)
        return {"success": True, "executions": []}

    async def submit_external_order(self, payload: ExternalStrategyOrderRequest) -> dict[str, Any]:
        message = {
            "type": "external_order",
            "order": payload.model_dump(exclude_none=True),
            "enqueued_at": _utc_now().isoformat(),
        }
        try:
            await self._redis.rpush(self._queue_key, json.dumps(message))
        except Exception as exc:  # pragma: no cover - defensive guard
            raise TaskEnqueueError(str(exc)) from exc
        self.pending_tasks = await self._get_queue_length()
        self._health_signal.set()
        return {"success": True, "queued": True, "order": message["order"]}

    async def get_internal_info(self, name: str) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        internal = await self._load_internal_snapshot(document)
        return {
            "success": True,
            "strategy": document.dict(),
            "internal_info": internal,
        }

    async def update_internal_parameter(
        self,
        name: str,
        parameter: StrategyParameterUpdateRequest,
    ) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        internal = await self._load_internal_snapshot(document)
        current = internal.get("parameters")
        if not isinstance(current, Mapping):
            current = {}
        updated = dict(current)
        updated[str(parameter.parameter_name)] = parameter.value
        internal["parameters"] = updated
        await self._persist_internal_info(name, internal)
        return {
            "success": True,
            "parameter": {"name": parameter.parameter_name, "value": parameter.value},
            "internal_info": internal,
        }

    async def get_strategy_status(self, name: str) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        status = await self._load_status_snapshot(name)
        data_push = await self._load_data_push_snapshot(name)
        if data_push:
            status.setdefault("data_push", data_push)
        status.setdefault("pending_tasks", self.pending_tasks)
        return {"success": True, "strategy": document.dict(), "status": status}

    async def request_data_push(self, name: str, payload: StrategyDataPushRequest) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        data = payload.model_dump(exclude_none=True)
        data["updated_at"] = _utc_now().isoformat()
        await self._persist_data_push_state(name, data)
        if self._data_push_handler is not None:
            try:
                await self._data_push_handler(name, data)
            except Exception:  # pragma: no cover - defensive guard
                self._logger.warning("Data push handler failed", exc_info=True)
        return {"success": True, "data_push": data}

    async def rename_strategy(self, name: str, payload: StrategyRenameRequest) -> StrategyDocument:
        new_name = (payload.new_name or "").strip()
        if not new_name:
            raise StrategyError("new strategy name must not be empty")
        async with self._lock:
            document = self._strategies.get(name)
            if document is None:
                raise StrategyNotRegistered(name)
            if new_name == name:
                return document.copy()
            if new_name in self._strategies:
                raise StrategyError(f"strategy already exists: {new_name}")
            updated = document.copy(
                update={"name": new_name, "updated_at": _utc_now().isoformat()}
            )
            self._strategies.pop(name)
            self._strategies[new_name] = updated
            await self._persist_document(updated)
            await self._persist_index()
        await self._redis.delete(self._metadata_key(name))
        await self._rename_related_keys(name, new_name)
        self._health_signal.set()
        return updated

    async def get_daily_pnl(self, name: str, *, force_refresh: bool = False) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        if force_refresh:
            try:
                await mark_strategy_cache_stale(name)
            except Exception:
                self._logger.debug("Failed to mark daily pnl cache stale", exc_info=True)
        pnl = await get_daily_pnl(name)
        return {"success": True, "strategy": document.dict(), "pnl": pnl}

    async def get_risk_settings(self, name: str) -> StrategyRiskSettings:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        payload: dict[str, Any] = {}
        stored = await self._redis.get(self._risk_settings_key(name))
        if stored:
            payload.update(_normalise_json_payload(stored))
        symbol = self._resolve_primary_symbol(document)
        if self._risk_manager is not None and symbol:
            try:
                rule = await self._risk_manager.load_rule(symbol)
            except Exception:  # pragma: no cover - defensive guard
                self._logger.debug("Failed to load risk settings for %s", symbol, exc_info=True)
            else:
                if isinstance(rule, Mapping):
                    payload.update({str(k): v for k, v in rule.items()})
        try:
            return StrategyRiskSettings(**payload)
        except ValidationError as exc:
            raise StrategyError(str(exc)) from exc

    async def update_risk_settings(self, name: str, settings: StrategyRiskSettings) -> StrategyRiskSettings:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        payload = settings.model_dump(exclude_none=True)
        await self._redis.set(self._risk_settings_key(name), json.dumps(payload))
        symbol = self._resolve_primary_symbol(document)
        if self._risk_manager is not None and symbol:
            try:
                await self._risk_manager.update_rule(symbol, payload=payload)
            except Exception:  # pragma: no cover - defensive guard
                self._logger.warning("Failed to persist risk settings", exc_info=True)
        return settings

    @staticmethod
    def _strategy_cache_keys(name: str) -> list[str]:
        base = f"strategy:{name}"
        return [
            f"{base}:orders",
            f"{base}:orders:zset",
            f"{base}:stats",
            f"{base}:recent",
            f"{base}:daily",
            f"{base}:daily:zset",
        ]

    @staticmethod
    def _serialise_datetime(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _normalise_execution_payload(
        self,
        strategy: str,
        entry: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(entry, Mapping):
            return None
        payload: dict[str, Any] = {str(key): value for key, value in entry.items()}
        order_id = payload.get("order_id") or payload.get("id")
        if order_id is not None:
            payload["order_id"] = str(order_id)
        payload.setdefault("strategy", strategy)

        timestamp = (
            payload.get("timestamp")
            or payload.get("executed_at")
            or payload.get("created_at")
        )
        serialised_timestamp = self._serialise_datetime(timestamp)
        if serialised_timestamp is not None:
            payload["timestamp"] = serialised_timestamp

        created = self._serialise_datetime(payload.get("created_at"))
        executed = self._serialise_datetime(payload.get("executed_at"))
        if created is not None:
            payload["created_at"] = created
        if executed is not None:
            payload["executed_at"] = executed

        quantity = payload.get("quantity")
        try:
            if quantity is not None:
                payload["quantity"] = int(quantity)
        except (TypeError, ValueError):
            pass

        return payload

    @staticmethod
    def _execution_identity(payload: Mapping[str, Any]) -> tuple[str, str, str]:
        order_id = str(payload.get("order_id") or "")
        timestamp = str(
            payload.get("timestamp")
            or payload.get("executed_at")
            or payload.get("created_at")
            or ""
        )
        symbol = str(payload.get("symbol") or "")
        return order_id, timestamp, symbol

    @staticmethod
    def _execution_sort_key(payload: Mapping[str, Any]) -> tuple[float, str]:
        raw_timestamp = (
            payload.get("timestamp")
            or payload.get("executed_at")
            or payload.get("created_at")
        )
        moment: datetime | None = None
        if isinstance(raw_timestamp, datetime):
            moment = raw_timestamp
        elif isinstance(raw_timestamp, str):
            moment = parse_local_datetime(raw_timestamp)
            if moment is None:
                try:
                    moment = datetime.fromisoformat(raw_timestamp)
                except ValueError:
                    moment = None
        if moment is None:
            return 0.0, str(payload.get("order_id") or "")
        try:
            timestamp_value = float(moment.timestamp())
        except Exception:
            timestamp_value = 0.0
        return timestamp_value, str(payload.get("order_id") or "")

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

    async def _synchronise_metadata(self) -> None:
        try:
            settings = await load_strategy_settings(self._redis)
        except Exception:  # pragma: no cover - defensive guard
            settings = dict(DEFAULT_SETTINGS)
        strategy_path = settings.get("strategy_path") or DEFAULT_SETTINGS.get("strategy_path")
        try:
            await sync_strategy_metadata(
                self._redis,
                {},
                metadata_prefix=self._metadata_prefix,
                strategy_path=strategy_path,
                logger=self._logger,
            )
        except Exception:  # pragma: no cover - defensive guard
            self._logger.warning("Failed to synchronise strategy metadata", exc_info=True)
        await self._load_metadata()

    @classmethod
    def _extract_metadata_files(cls, metadata: Mapping[str, Any]) -> set[str]:
        entries: set[str] = set()
        if not isinstance(metadata, Mapping):
            return entries
        for key in cls._FILE_METADATA_KEYS:
            value = metadata.get(key)
            cls._collect_file_values(value, entries)
        return entries

    @classmethod
    def _collect_file_values(cls, value: Any, entries: set[str]) -> None:
        if value is None:
            return
        if isinstance(value, str):
            candidate = value.strip()
            if candidate:
                entries.add(candidate)
            return
        if isinstance(value, Mapping):
            for sub_value in value.values():
                cls._collect_file_values(sub_value, entries)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                cls._collect_file_values(item, entries)

    @classmethod
    def _scan_strategy_path(cls, path_value: Any) -> set[str]:
        resolved = cls._resolve_strategy_path(path_value)
        if resolved is None:
            return set()
        entries: set[str] = set()
        try:
            if resolved.is_dir():
                base = resolved
                for candidate in base.rglob("*"):
                    if not candidate.is_file():
                        continue
                    if candidate.suffix.lower() not in cls._STRATEGY_FILE_SUFFIXES:
                        continue
                    try:
                        relative = candidate.relative_to(base).as_posix()
                    except ValueError:
                        relative = candidate.name
                    entries.add(relative)
            elif resolved.is_file():
                if resolved.suffix.lower() in cls._STRATEGY_FILE_SUFFIXES:
                    entries.add(resolved.name)
        except OSError:  # pragma: no cover - filesystem access guard
            return set()
        return entries

    @staticmethod
    def _resolve_strategy_path(path_value: Any) -> Path | None:
        if not path_value or not isinstance(path_value, str):
            return None
        candidate = Path(path_value).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            return candidate.resolve()
        except OSError:
            return candidate

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

    def _internal_info_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}:internal"

    def _status_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}:status"

    def _data_push_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}:data-push"

    def _risk_settings_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}:risk"

    async def _load_internal_snapshot(self, document: StrategyDocument) -> dict[str, Any]:
        cached = await self._redis.get(self._internal_info_key(document.name))
        payload = _normalise_json_payload(cached)
        if payload:
            return payload
        internal: dict[str, Any] = {}
        metadata = document.metadata or {}
        if isinstance(metadata, Mapping):
            entry = metadata.get("internal_info")
            if isinstance(entry, Mapping):
                internal.update({str(k): v for k, v in entry.items()})
        if document.description and "description" not in internal:
            internal["description"] = document.description
        if document.params and "parameters" not in internal:
            internal["parameters"] = dict(document.params)
        await self._persist_internal_info(document.name, internal)
        return internal

    async def _persist_internal_info(self, name: str, payload: Mapping[str, Any]) -> None:
        await self._redis.set(self._internal_info_key(name), json.dumps(dict(payload)))

    async def _load_status_snapshot(self, name: str) -> dict[str, Any]:
        stored = await self._redis.get(self._status_key(name))
        payload = _normalise_json_payload(stored)
        return payload if payload else {}

    async def _load_data_push_snapshot(self, name: str) -> dict[str, Any]:
        stored = await self._redis.get(self._data_push_key(name))
        payload = _normalise_json_payload(stored)
        return payload if payload else {}

    async def _persist_data_push_state(self, name: str, payload: Mapping[str, Any]) -> None:
        await self._redis.set(self._data_push_key(name), json.dumps(dict(payload)))

    async def _rename_related_keys(self, old: str, new: str) -> None:
        related = {
            self._internal_info_key(old): self._internal_info_key(new),
            self._status_key(old): self._status_key(new),
            self._data_push_key(old): self._data_push_key(new),
            self._risk_settings_key(old): self._risk_settings_key(new),
        }
        for source, target in related.items():
            try:
                payload = await self._redis.get(source)
            except Exception:  # pragma: no cover - defensive guard
                self._logger.debug("Failed to load key %s during rename", source, exc_info=True)
                continue
            if payload is None:
                continue
            try:
                await self._redis.set(target, payload)
                await self._redis.delete(source)
            except Exception:  # pragma: no cover - defensive guard
                self._logger.debug("Failed to rename key %s", source, exc_info=True)

    @staticmethod
    def _resolve_primary_symbol(document: StrategyDocument) -> str | None:
        metadata_symbol = None
        if isinstance(document.metadata, Mapping):
            candidate = document.metadata.get("primary_symbol")
            if isinstance(candidate, str) and candidate.strip():
                metadata_symbol = candidate.strip().upper()
        param_symbol = None
        if isinstance(document.params, Mapping):
            candidate = document.params.get("symbol")
            if isinstance(candidate, str) and candidate.strip():
                param_symbol = candidate.strip().upper()
        return metadata_symbol or param_symbol


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
    risk_manager_factory: Callable[[], StrategyRiskManager] | None = None,
    data_push_handler: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None,
) -> FastAPI:
    def _runtime_factory(redis: Any) -> StrategyRuntime:
        if runtime is not None:
            return runtime
        consumer = consumer_factory(redis) if consumer_factory is not None else None
        risk_manager = risk_manager_factory() if risk_manager_factory is not None else None
        return StrategyRuntime(
            redis,
            queue_key=queue_key,
            event_stream=event_stream,
            result_stream=result_stream,
            health_interval=health_interval,
            event_poll_interval=event_poll_interval,
            consumer=consumer,
            risk_manager=risk_manager,
            data_push_handler=data_push_handler,
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

    @app.get("/strategy", response_model=list[StrategyDocument])
    @app.get("/strategies", response_model=list[StrategyDocument])
    async def list_strategies(runtime: StrategyRuntime = Depends(_get_runtime)) -> list[StrategyDocument]:
        return await runtime.list_strategies()

    @app.get("/strategy/settings")
    async def get_strategy_settings(request: Request) -> dict[str, Any]:
        redis_client = await _get_redis(request)
        settings = await load_strategy_settings(redis_client)
        return {"success": True, "settings": settings}

    @app.post("/strategy/settings")
    async def update_strategy_settings(
        payload: StrategySettingsPayload,
        request: Request,
    ) -> dict[str, Any]:
        redis_client = await _get_redis(request)
        settings = await load_strategy_settings(redis_client)
        updates = payload.dict(exclude_none=True)
        if "api_payload_template" in updates and not isinstance(updates["api_payload_template"], dict):
            updates.pop("api_payload_template")
        settings.update(updates)
        settings["enable_auth"] = bool(settings.get("enable_auth"))
        settings["pause_data_push_on_holidays"] = bool(settings.get("pause_data_push_on_holidays", True))
        if "auth_token" not in settings:
            settings["auth_token"] = None
        await persist_strategy_settings(redis_client, settings)
        return {"success": True, "settings": settings}

    @app.post("/strategy/settings/generate-token")
    async def generate_strategy_token(request: Request) -> dict[str, Any]:
        redis_client = await _get_redis(request)
        settings = await load_strategy_settings(redis_client)
        token = secrets.token_urlsafe(24)
        settings["auth_token"] = token
        settings["enable_auth"] = True
        await persist_strategy_settings(redis_client, settings)
        return {"success": True, "token": token, "settings": settings}

    @app.get("/strategy/files")
    async def list_strategy_files(runtime: StrategyRuntime = Depends(_get_runtime)) -> dict[str, Any]:
        files = await runtime.list_strategy_files()
        return {"success": True, "files": files}

    @app.post("/strategy", status_code=status.HTTP_202_ACCEPTED)
    async def submit_external_order(
        payload: ExternalStrategyOrderRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            return await runtime.submit_external_order(payload)
        except TaskEnqueueError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    @app.post("/strategies", response_model=StrategyDocument, status_code=status.HTTP_201_CREATED)
    async def create_strategy(
        payload: StrategyUpsertRequest,
        response: Response,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> StrategyDocument:
        document, created = await runtime.upsert_strategy(payload)
        if not created:
            response.status_code = status.HTTP_200_OK
        return document

    @app.put("/strategy", response_model=StrategyDocument)
    @app.put("/strategies/{name}", response_model=StrategyDocument)
    async def update_strategy(
        payload: StrategyUpsertRequest,
        response: Response,
        runtime: StrategyRuntime = Depends(_get_runtime),
        name: str | None = None,
    ) -> StrategyDocument:
        if name is not None and payload.name != name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="strategy name mismatch",
            )
        document, created = await runtime.upsert_strategy(payload)
        if name is not None and created:
            response.status_code = status.HTTP_201_CREATED
        return document

    @app.get("/strategy/{name}", response_model=StrategyDocument)
    @app.get("/strategies/{name}", response_model=StrategyDocument)
    async def get_strategy(name: str, runtime: StrategyRuntime = Depends(_get_runtime)) -> StrategyDocument:
        document = await runtime.get_strategy(name)
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")
        return document

    @app.get("/strategies/{name}/internal-info")
    async def get_strategy_internal_info(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            return await runtime.get_internal_info(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.post("/strategies/{name}/internal-info/parameter")
    async def update_strategy_internal_parameter(
        name: str,
        payload: StrategyParameterUpdateRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            return await runtime.update_internal_parameter(name, payload)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get("/strategies/{name}/risk-settings")
    async def get_strategy_risk_settings(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            settings = await runtime.get_risk_settings(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except StrategyError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
        return {"success": True, "settings": settings.model_dump(exclude_none=True)}

    @app.put("/strategies/{name}/risk-settings")
    async def update_strategy_risk_settings(
        name: str,
        payload: StrategyRiskSettings,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            updated = await runtime.update_risk_settings(name, payload)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except StrategyError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
        return {"success": True, "settings": updated.model_dump(exclude_none=True)}

    @app.get("/strategies/{name}/status")
    async def get_strategy_status(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            return await runtime.get_strategy_status(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.post("/strategies/{name}/data-push")
    async def request_strategy_data_push(
        name: str,
        payload: StrategyDataPushRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            return await runtime.request_data_push(name, payload)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get("/strategies/{name}/performance")
    async def get_strategy_performance(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
        range_param: str | None = Query(None, alias="range"),
        start: str | None = Query(None),
        end: str | None = Query(None),
    ) -> dict[str, Any]:
        def _parse_datetime(value: str | None, label: str) -> datetime | None:
            if value is None:
                return None
            try:
                return parse_local_datetime(value)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"invalid {label} datetime",
                ) from exc

        start_date = _parse_datetime(start, "start")
        end_date = _parse_datetime(end, "end")

        range_value = (range_param or "").strip().lower()
        if range_value:
            now = now_local()
            if range_value == "1d":
                start_date = now - timedelta(days=1)
            elif range_value == "1w":
                start_date = now - timedelta(weeks=1)
            elif range_value == "1m":
                start_date = now - timedelta(days=30)
            elif range_value == "wtd":
                start_date = (now - timedelta(days=now.weekday())).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            elif range_value == "mtd":
                start_date = now.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
            elif range_value == "ytd":
                start_date = now.replace(hour=0, minute=0, second=0, microsecond=0, month=1, day=1)
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid range parameter",
                )

        if start_date and end_date and end_date < start_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="end must not be earlier than start",
            )

        document = await runtime.get_strategy(name)
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")

        if start_date or end_date:
            await get_strategy_orders_in_range(name, start_date=start_date, end_date=end_date)
        else:
            await get_strategy_orders(name)

        metrics = await compute_performance_metrics(
            name,
            start_date=start_date,
            end_date=end_date,
        )
        return {"success": True, "name": name, "metrics": metrics}

    @app.get("/strategy/{name}/executions")
    @app.get("/strategies/{name}/executions")
    async def get_strategy_executions(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
        limit: int = Query(20, ge=1, alias="limit"),
        force_refresh: bool = Query(False, alias="force_refresh"),
    ) -> dict[str, Any]:
        try:
            return await runtime.get_executions(
                name,
                limit=limit,
                force_refresh=force_refresh,
            )
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.delete("/strategy/{name}/executions")
    @app.delete("/strategies/{name}/executions")
    async def clear_strategy_executions(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            return await runtime.clear_executions(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.delete("/strategy/{name}", status_code=status.HTTP_204_NO_CONTENT)
    @app.delete("/strategies/{name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_strategy(name: str, runtime: StrategyRuntime = Depends(_get_runtime)) -> None:
        deleted = await runtime.delete_strategy(name)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")

    @app.post("/strategy/{name}/toggle")
    @app.post("/strategies/{name}/toggle")
    async def toggle_strategy(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
        enabled: bool | None = Query(None),
        payload: StrategyTogglePayload | None = None,
    ) -> dict[str, Any]:
        target: bool | None = None
        if payload is not None:
            target = payload.enabled
        elif enabled is not None:
            target = bool(enabled)
        if target is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="enabled flag required")
        try:
            document = await runtime.set_strategy_enabled(name, enabled=target)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {"success": True, "strategy": document}

    @app.post("/strategy/{name}/reset")
    @app.post("/strategies/{name}/reset")
    async def reset_strategy(name: str, runtime: StrategyRuntime = Depends(_get_runtime)) -> dict[str, Any]:
        try:
            document = await runtime.reset_strategy(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {"success": True, "strategy": document}

    @app.post("/strategies/{name}/rename", response_model=StrategyDocument)
    async def rename_strategy(
        name: str,
        payload: StrategyRenameRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> StrategyDocument:
        try:
            return await runtime.rename_strategy(name, payload)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except StrategyError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/strategies/{name}/pnl/daily")
    async def get_strategy_daily_pnl(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
        force_refresh: bool = Query(False, alias="force_refresh"),
    ) -> dict[str, Any]:
        try:
            return await runtime.get_daily_pnl(name, force_refresh=force_refresh)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

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
    "StrategyTogglePayload",
    "load_strategy_settings",
    "persist_strategy_settings",
    "create_app",
    "app",
]
