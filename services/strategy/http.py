"""FastAPI application exposing strategy management endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import importlib
import time
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import secrets
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, ValidationError, field_validator

from services.strategy import StrategyTaskConsumer
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
    schedule_strategy_cache_refresh,
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

    @field_validator("name")
    @classmethod
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
        data_push_handler: Callable[[str, Mapping[str, Any]], Any] | None = None,
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
        self._metadata_sync_lock = asyncio.Lock()
        self._last_metadata_sync: float | None = None

    @property
    def queue_key(self) -> str:
        return self._queue_key

    @property
    def event_stream(self) -> str:
        return self._event_stream

    async def start(self) -> None:
        await self._synchronise_metadata(force=True)
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
        await self._synchronise_metadata(force=True)
        async with self._lock:
            return [
                doc.model_copy()
                for doc in sorted(self._strategies.values(), key=lambda d: d.name)
            ]

    async def get_strategy(self, name: str) -> StrategyDocument | None:
        async with self._lock:
            document = self._strategies.get(name)
            return document.model_copy() if document is not None else None

    async def get_internal_info_payload(self, name: str) -> tuple[dict[str, Any], bool]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        cached, _ = await self._load_cached_internal_info(name)
        used_cached = False
        try:
            info = await self._generate_internal_info(document)
            stale = False
        except Exception:
            self._logger.debug("Failed to refresh internal info for %s", name, exc_info=True)
            info = cached or {
                "name": document.name,
                "params": dict(document.params),
                "metadata": dict(document.metadata),
                "description": document.description,
                "factory": document.factory,
                "enabled": document.enabled,
            }
            stale = True
            used_cached = True
        if stale:
            info = {**info, "stale": True}
        await self._persist_internal_info(name, info, stale=stale)
        return info, used_cached

    async def update_internal_parameter_value(
        self,
        name: str,
        parameter: str,
        value: Any,
    ) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        applied = await self._apply_parameter_update(document, parameter, value)
        overrides = await self._load_parameters(name)
        overrides[str(parameter)] = applied
        await self._persist_parameters(name, overrides)
        async with self._lock:
            stored = self._strategies.get(name)
            if stored is not None:
                new_params = dict(stored.params)
                new_params[str(parameter)] = applied
                updated = stored.model_copy(
                    update={"params": new_params, "updated_at": _utc_now().isoformat()}
                )
                self._strategies[name] = updated
                await self._persist_document(updated)
        await self.get_internal_info_payload(name)
        return {"name": parameter, "value": applied}

    async def load_risk_settings(self, name: str) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        return await self._load_risk_settings(document.name)

    async def persist_risk_settings(self, name: str, settings: Mapping[str, Any]) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        await self._persist_risk_settings(document.name, settings)
        return await self._load_risk_settings(document.name)

    async def load_status(self, name: str) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        status = await self._default_status_payload(document.name)
        status.setdefault("pending_tasks", self.pending_tasks)
        return status

    async def request_data_push_state(
        self,
        name: str,
        payload: StrategyDataPushRequest,
    ) -> dict[str, Any]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        enabled = bool(payload.enabled)
        subscription = payload.subscription
        symbol = payload.symbol
        if enabled:
            if not subscription:
                raise ValueError("subscription is required when enabling data push")
            if not symbol:
                raise ValueError("symbol is required when enabling data push")
        state = {
            "active": enabled,
            "subscription": subscription,
            "symbol": symbol,
            "force_restart": bool(payload.force_restart),
            "status_reason": "Enabled" if enabled else "Disabled",
            "awaiting_data": enabled,
            "updated_at": _utc_now().isoformat(),
        }
        if self._data_push_handler is not None:
            try:
                result = self._data_push_handler(document.name, state)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, Mapping):
                    state.update({str(k): v for k, v in result.items()})
            except Exception as exc:
                raise ValueError(f"data push handler failed: {exc}") from exc
        status = await self._update_status_data_push(document.name, state)
        return status["data_push"]

    async def rename_strategy_entry(self, name: str, new_name: str) -> StrategyDocument:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("new strategy name must not be empty")
        if new_name == name:
            document = await self.get_strategy(name)
            if document is None:
                raise StrategyNotRegistered(name)
            return document
        async with self._lock:
            if new_name in self._strategies:
                raise ValueError("target strategy name already exists")
            document = self._strategies.get(name)
            if document is None:
                raise StrategyNotRegistered(name)
            updated = document.model_copy(
                update={"name": new_name, "updated_at": _utc_now().isoformat()}
            )
            self._strategies.pop(name, None)
            self._strategies[new_name] = updated
            await self._persist_document(updated)
            await self._persist_index()
        try:
            await self._redis.delete(self._metadata_key(name))
        except Exception:
            self._logger.debug("Failed to delete old metadata key for %s", name, exc_info=True)
        await self._rename_auxiliary_keys(name, new_name)
        self._health_signal.set()
        return updated

    async def load_daily_pnl(self, name: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        document = await self.get_strategy(name)
        if document is None:
            raise StrategyNotRegistered(name)
        if force_refresh:
            try:
                await mark_strategy_cache_stale(name)
                schedule_strategy_cache_refresh(name)
            except Exception:
                self._logger.debug("Failed to mark strategy cache stale for %s", name, exc_info=True)
        try:
            return await get_daily_pnl(name)
        except Exception:
            self._logger.debug("Failed to fetch daily pnl for %s", name, exc_info=True)
            return []

    async def submit_external_order(
        self,
        payload: ExternalStrategyOrderRequest,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = {
            "strategy": payload.strategy,
            "symbol": payload.symbol,
            "action": payload.action,
            "qty": payload.qty,
            "type": payload.type,
            "price": payload.price,
            "exchange": payload.exchange,
            "sec_type": payload.sec_type,
            "token": payload.token,
            "queued_at": now_local().isoformat(),
        }
        if metadata:
            command["metadata"] = dict(metadata)
        try:
            await self._redis.rpush(self._queue_key, json.dumps(command, ensure_ascii=False))
        except Exception as exc:
            raise TaskEnqueueError(str(exc)) from exc
        self.pending_tasks = await self._get_queue_length()
        self._health_signal.set()
        return command
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
        await self._delete_auxiliary_keys(name)
        self._health_signal.set()
        return True

    async def set_strategy_enabled(self, name: str, *, enabled: bool) -> StrategyDocument:
        async with self._lock:
            document = self._strategies.get(name)
            if document is None:
                raise StrategyNotRegistered(name)
            updated = document.model_copy(
                update={"enabled": bool(enabled), "updated_at": _utc_now().isoformat()}
            )
            self._strategies[name] = updated
            await self._persist_document(updated)
        self._health_signal.set()
        return updated

    async def reset_strategy(self, name: str) -> StrategyDocument:
        async with self._lock:
            document = self._strategies.get(name)
            if document is None:
                raise StrategyNotRegistered(name)
            updated = document.model_copy(
                update={
                    "params": {},
                    "metadata": {},
                    "enabled": True,
                    "updated_at": _utc_now().isoformat(),
                }
            )
            self._strategies[name] = updated
            await self._persist_document(updated)
        try:
            await self._redis.delete(
                self._parameters_key(name),
                self._internal_info_key(name),
            )
        except Exception:
            self._logger.debug("Failed to purge runtime cache for %s", name, exc_info=True)
        self._health_signal.set()
        return updated

    async def list_strategy_files(self) -> list[str]:
        await self._synchronise_metadata(force=True)
        entries: set[str] = set()
        async with self._lock:
            for document in self._strategies.values():
                entries.update(self._extract_metadata_files(document.metadata))
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
                schedule_strategy_cache_refresh(name)
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
        await self._redis.set(metadata_key, json.dumps(document.model_dump()))

    async def _persist_index(self) -> None:
        payload = [doc.name for doc in sorted(self._strategies.values(), key=lambda d: d.name)]
        await self._redis.set(self._metadata_index_key, json.dumps(payload))

    def _metadata_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}"

    def _internal_info_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}:internal-info"

    def _parameters_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}:parameters"

    def _status_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}:status"

    def _risk_settings_key(self, name: str) -> str:
        return f"{self._metadata_prefix}:{name}:risk-settings"

    async def _load_cached_internal_info(self, name: str) -> tuple[dict[str, Any] | None, bool]:
        key = self._internal_info_key(name)
        try:
            raw = await self._redis.get(key)
        except Exception:
            return None, False
        payload = _normalise_json_payload(raw)
        if not payload:
            return None, False
        info: dict[str, Any] | None = None
        if isinstance(payload.get("info"), Mapping):
            info = {str(k): v for k, v in payload["info"].items()}
        elif isinstance(payload, Mapping):
            info = {str(k): v for k, v in payload.items() if k not in {"stale", "cached_at"}}
        stale = bool(payload.get("stale"))
        if info is None:
            return None, stale
        if stale:
            info = {**info, "stale": True}
        return info, stale

    async def _persist_internal_info(self, name: str, info: Mapping[str, Any], *, stale: bool) -> None:
        key = self._internal_info_key(name)
        payload = {
            "info": dict(info),
            "stale": bool(stale),
            "cached_at": _utc_now().isoformat(),
        }
        try:
            await self._redis.set(key, json.dumps(payload, ensure_ascii=False))
        except Exception:
            self._logger.debug("Failed to persist internal info for %s", name, exc_info=True)

    async def _load_parameters(self, name: str) -> dict[str, Any]:
        key = self._parameters_key(name)
        try:
            raw = await self._redis.get(key)
        except Exception:
            return {}
        payload = _normalise_json_payload(raw)
        if not payload:
            return {}
        return {str(k): v for k, v in payload.items()}

    async def _persist_parameters(self, name: str, parameters: Mapping[str, Any]) -> None:
        key = self._parameters_key(name)
        try:
            await self._redis.set(key, json.dumps(dict(parameters), ensure_ascii=False))
        except Exception:
            self._logger.debug("Failed to persist parameter overrides for %s", name, exc_info=True)

    async def _load_status(self, name: str) -> dict[str, Any]:
        key = self._status_key(name)
        try:
            raw = await self._redis.get(key)
        except Exception:
            return {}
        payload = _normalise_json_payload(raw)
        if not payload:
            return {}
        return {str(k): v for k, v in payload.items()}

    async def _persist_status(self, name: str, status: Mapping[str, Any]) -> None:
        key = self._status_key(name)
        payload = dict(status)
        payload["updated_at"] = payload.get("updated_at") or _utc_now().isoformat()
        try:
            await self._redis.set(key, json.dumps(payload, ensure_ascii=False))
        except Exception:
            self._logger.debug("Failed to persist status for %s", name, exc_info=True)

    async def _load_risk_settings(self, name: str) -> dict[str, Any]:
        if self._risk_manager is not None:
            try:
                result = self._risk_manager.load_strategy_settings(name)
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception:
                self._logger.debug("Failed to load risk settings via manager for %s", name, exc_info=True)
            else:
                if isinstance(result, Mapping):
                    return {str(k): v for k, v in result.items()}
        key = self._risk_settings_key(name)
        try:
            raw = await self._redis.get(key)
        except Exception:
            return {}
        payload = _normalise_json_payload(raw)
        if not payload:
            return {}
        return {str(k): v for k, v in payload.items()}

    async def _persist_risk_settings(self, name: str, settings: Mapping[str, Any]) -> None:
        payload = dict(settings)
        if self._risk_manager is not None:
            try:
                result = self._risk_manager.persist_strategy_settings(name, payload)
                if asyncio.iscoroutine(result):
                    await result
                return
            except Exception:
                self._logger.debug("Failed to persist risk settings via manager for %s", name, exc_info=True)
        key = self._risk_settings_key(name)
        try:
            await self._redis.set(key, json.dumps(payload, ensure_ascii=False))
        except Exception:
            self._logger.debug("Failed to persist risk settings for %s", name, exc_info=True)

    @staticmethod
    def _resolve_factory(spec: str) -> Callable[[Mapping[str, Any]], Any]:
        if ":" in spec:
            module_path, attr = spec.split(":", 1)
        else:
            module_path, attr = spec.rsplit(".", 1)
        module = importlib.import_module(module_path)
        factory = getattr(module, attr)
        if not callable(factory):
            raise TypeError(f"strategy factory is not callable: {spec}")
        return factory

    def _instantiate_strategy(
        self,
        document: StrategyDocument,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> Any:
        if not document.factory:
            raise ValueError("strategy factory is not defined")
        factory = self._resolve_factory(document.factory)
        config = dict(document.params)
        if overrides:
            for key, value in overrides.items():
                config[str(key)] = value
        try:
            instance = factory(config)
        except Exception as exc:
            raise RuntimeError(f"failed to instantiate strategy: {exc}") from exc
        if self._risk_manager is not None and hasattr(instance, "set_dependencies"):
            try:
                instance.set_dependencies(strategy_risk_manager=self._risk_manager)
            except Exception:
                self._logger.debug("Failed to inject risk manager into strategy", exc_info=True)
        return instance

    @staticmethod
    async def _maybe_await(result: Any) -> Any:
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _generate_internal_info(self, document: StrategyDocument) -> dict[str, Any]:
        overrides = await self._load_parameters(document.name)
        strategy = self._instantiate_strategy(document, overrides=overrides)
        info: dict[str, Any] = {}
        helper = getattr(strategy, "get_internal_info", None)
        if callable(helper):
            try:
                result = await self._maybe_await(helper())
            except Exception as exc:
                raise RuntimeError(f"failed to collect internal info: {exc}") from exc
            if isinstance(result, Mapping):
                info = {str(k): v for k, v in result.items()}
        if not info:
            describer = getattr(strategy, "describe_parameters", None)
            if callable(describer):
                try:
                    described = await self._maybe_await(describer())
                except Exception:
                    described = None
                if isinstance(described, list):
                    info["configurable_parameters"] = [
                        dict(entry) if isinstance(entry, Mapping) else entry for entry in described
                    ]
        if overrides:
            info.setdefault("runtime_parameters", {str(k): v for k, v in overrides.items()})
        info.setdefault("name", document.name)
        info.setdefault("factory", document.factory)
        metadata = dict(document.metadata)
        existing_metadata = info.get("metadata") if isinstance(info.get("metadata"), Mapping) else None
        if existing_metadata:
            merged_metadata = {**metadata, **{str(k): v for k, v in existing_metadata.items()}}
            info["metadata"] = merged_metadata
        else:
            info.setdefault("metadata", metadata)
        params = dict(document.params)
        existing_params = info.get("params") if isinstance(info.get("params"), Mapping) else None
        if existing_params:
            merged_params = {**params, **{str(k): v for k, v in existing_params.items()}}
            info["params"] = merged_params
        else:
            info.setdefault("params", params)
        if document.description is not None:
            info["description"] = document.description
        else:
            info.setdefault("description", document.description)
        info["enabled"] = document.enabled
        return info

    async def _apply_parameter_update(
        self,
        document: StrategyDocument,
        parameter: str,
        value: Any,
    ) -> Any:
        overrides = await self._load_parameters(document.name)
        strategy = self._instantiate_strategy(document, overrides=overrides)
        updater = getattr(strategy, "apply_parameter_updates", None)
        if not callable(updater):
            return value
        try:
            applied = await self._maybe_await(updater({parameter: value}))
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        if isinstance(applied, Mapping) and parameter in applied:
            return applied[parameter]
        return value

    async def _default_status_payload(self, name: str) -> dict[str, Any]:
        status = await self._load_status(name)
        if not isinstance(status, Mapping):
            status = {}
        result = {"strategy": name, **status}
        result.setdefault("data_push", {})
        result.setdefault("pending_tasks", self.pending_tasks)
        return result

    async def _update_status_data_push(
        self,
        name: str,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        status = await self._default_status_payload(name)
        payload = dict(status)
        payload["data_push"] = {**state}
        payload["data_push_reason"] = state.get("status_reason")
        await self._persist_status(name, payload)
        return payload

    async def _delete_auxiliary_keys(self, name: str) -> None:
        keys = [
            self._internal_info_key(name),
            self._parameters_key(name),
            self._status_key(name),
            self._risk_settings_key(name),
        ]
        try:
            await self._redis.delete(*keys)
        except Exception:
            self._logger.debug("Failed to delete auxiliary keys for %s", name, exc_info=True)
        if self._risk_manager is not None:
            try:
                result = self._risk_manager.delete_strategy_settings(name)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self._logger.debug("Failed to delete risk settings via manager", exc_info=True)

    async def _rename_auxiliary_keys(self, source: str, target: str) -> None:
        mapping = {
            self._internal_info_key(source): self._internal_info_key(target),
            self._parameters_key(source): self._parameters_key(target),
            self._status_key(source): self._status_key(target),
            self._risk_settings_key(source): self._risk_settings_key(target),
        }
        for old_key, new_key in mapping.items():
            try:
                payload = await self._redis.get(old_key)
            except Exception:
                payload = None
            if payload is None:
                continue
            try:
                await self._redis.set(new_key, payload)
                await self._redis.delete(old_key)
            except Exception:
                self._logger.debug("Failed to rename key %s to %s", old_key, new_key, exc_info=True)
        if self._risk_manager is not None:
            try:
                result = self._risk_manager.rename_strategy_settings(source, target)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self._logger.debug("Failed to rename risk settings via manager", exc_info=True)

    async def _synchronise_metadata(self, *, force: bool = False) -> None:
        async with self._metadata_sync_lock:
            if not force and self._last_metadata_sync is not None:
                return
            try:
                await self._load_metadata()
            except Exception:  # pragma: no cover - defensive guard
                self._logger.warning("Failed to refresh strategy metadata", exc_info=True)
                return
            self._last_metadata_sync = time.monotonic()

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
                    document = StrategyDocument.model_validate(payload)
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
    runtime_factory: Callable[[Any], StrategyRuntime | Awaitable[StrategyRuntime]],
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
    runtime_candidate = runtime_factory(redis)
    runtime = await runtime_candidate if asyncio.iscoroutine(runtime_candidate) else runtime_candidate
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
    metadata_prefix: str = DEFAULT_METADATA_PREFIX,
    risk_manager_factory: Callable[[Any], StrategyRiskManager] | None = None,
    data_push_handler: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> FastAPI:
    async def _runtime_factory(redis: Any) -> StrategyRuntime:
        if runtime is not None:
            return runtime
        consumer = consumer_factory(redis) if consumer_factory is not None else None
        risk_manager: StrategyRiskManager | None = None
        if risk_manager_factory is not None:
            candidate = risk_manager_factory(redis)
            risk_manager = await candidate if asyncio.iscoroutine(candidate) else candidate
        else:
            risk_manager = StrategyRiskManager(redis_client=redis, settings_prefix=metadata_prefix)
        return StrategyRuntime(
            redis,
            queue_key=queue_key,
            event_stream=event_stream,
            result_stream=result_stream,
            health_interval=health_interval,
            event_poll_interval=event_poll_interval,
            consumer=consumer,
            metadata_prefix=metadata_prefix,
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
        updates = payload.model_dump(exclude_none=True)
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

    @app.post("/strategy", status_code=status.HTTP_202_ACCEPTED)
    async def submit_external_strategy_order(
        payload: ExternalStrategyOrderRequest,
        request: Request,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        client = request.client
        if client and client.host:
            metadata["client"] = client.host
        try:
            order = await runtime.submit_external_order(payload, metadata=metadata or None)
        except TaskEnqueueError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        return {"success": True, "order": order}

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
    async def get_internal_strategy_info(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            info, cached = await runtime.get_internal_info_payload(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {"success": True, "strategy": info, "cached": cached}

    @app.post("/strategies/{name}/internal-info/parameter")
    async def update_internal_parameter(
        name: str,
        payload: StrategyParameterUpdateRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        parameter_name = payload.parameter_name
        try:
            updated = await runtime.update_internal_parameter_value(name, parameter_name, payload.value)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {"success": True, "parameter": updated}

    @app.get("/strategies/{name}/risk-settings")
    async def get_strategy_risk_settings(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            settings = await runtime.load_risk_settings(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        model = StrategyRiskSettings(**settings)
        return {"success": True, "settings": model.model_dump()}

    @app.put("/strategies/{name}/risk-settings")
    async def update_strategy_risk_settings(
        name: str,
        payload: StrategyRiskSettings,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            current = await runtime.load_risk_settings(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        updates = payload.model_dump(exclude_unset=True)
        current.update(updates)
        model = StrategyRiskSettings(**current)
        try:
            stored = await runtime.persist_risk_settings(name, model.model_dump())
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {
            "success": True,
            "settings": StrategyRiskSettings(**stored).model_dump(),
        }

    @app.get("/strategies/{name}/status")
    async def get_strategy_status(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            status_payload = await runtime.load_status(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {"success": True, "status": status_payload}

    @app.post("/strategies/{name}/data-push")
    async def update_strategy_data_push(
        name: str,
        payload: StrategyDataPushRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            state = await runtime.request_data_push_state(name, payload)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {"success": True, "state": state}

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

    @app.get("/strategies/{name}/pnl/daily")
    async def get_strategy_daily_pnl(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
        force_refresh: bool = Query(False, alias="force_refresh"),
    ) -> dict[str, Any]:
        try:
            entries = await runtime.load_daily_pnl(name, force_refresh=force_refresh)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {"success": True, "name": name, "pnl": entries}

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

    @app.delete(
        "/strategy/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    @app.delete(
        "/strategies/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_strategy(
        name: str,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> Response:
        deleted = await runtime.delete_strategy(name)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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

    @app.post("/strategies/{name}/rename")
    async def rename_strategy(
        name: str,
        payload: StrategyRenameRequest,
        runtime: StrategyRuntime = Depends(_get_runtime),
    ) -> dict[str, Any]:
        try:
            document = await runtime.rename_strategy_entry(name, payload.new_name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {"success": True, "strategy": document}

    @app.post("/strategy/{name}/reset")
    @app.post("/strategies/{name}/reset")
    async def reset_strategy(name: str, runtime: StrategyRuntime = Depends(_get_runtime)) -> dict[str, Any]:
        try:
            document = await runtime.reset_strategy(name)
        except StrategyNotRegistered as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {"success": True, "strategy": document}

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
