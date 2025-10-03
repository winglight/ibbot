from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping, Protocol

from services.risk.engine import RiskEngine, RiskEvent, RiskRule
from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.ibbot.redis import (
    ack_stream,
    enqueue_ib_command,
    read_stream,
    ResponseError,
    StreamMessage,
)

try:  # Optional dependency during testing environments.
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis might be unavailable in CI
    Redis = Any  # type: ignore[misc, assignment]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _maybe_await(result: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(result):
        return await result  # type: ignore[return-value]
    return result


class RiskSnapshotRepositoryProtocol(Protocol):
    async def save_config_snapshot(
        self,
        *,
        service: str,
        payload: Mapping[str, Any],
        rules_count: int,
        updated_at: str | datetime | None = None,
        captured_at: datetime | str | None = None,
    ) -> None:
        ...

    async def bulk_upsert_rule_snapshots(
        self,
        *,
        service: str,
        rules: Mapping[str, Mapping[str, Any]],
        updated_at: str | datetime | None = None,
        captured_at: datetime | str | None = None,
    ) -> None:
        ...


RiskSnapshotRepositoryFactory = Callable[
    [],
    Awaitable[RiskSnapshotRepositoryProtocol] | RiskSnapshotRepositoryProtocol,
]


@dataclass(slots=True)
class RiskRuntime:
    """Orchestrate the risk engine and connect it to external services."""

    redis_factory: Callable[[], Awaitable[Redis]]
    risk_snapshot_repository: RiskSnapshotRepositoryProtocol | None = None
    risk_snapshot_repository_factory: RiskSnapshotRepositoryFactory | None = None
    logger_name: str = "risk-service"
    service_name: str = "risk-service"
    market_stream_key: str = "stream:market-service"
    market_group: str = "group:risk:market"
    results_stream_key: str = "stream:ib:results"
    results_group: str = "group:risk:results"
    command_queue: str = "queue:ib:commands"
    poll_interval: float = 0.5
    log_dir: str | os.PathLike[str] | None = None
    batch_size: int = 20
    reconnect_delay: float = 1.0
    config_cache_key: str = "json:risk:config"
    rules_cache_key: str = "json:risk:rules"
    rules_update_channel: str = "pubsub:risk:rules:update"
    config_hash_key: str = "hash:risk:config"
    rules_hash_key: str = "hash:risk:rules"
    rules_history_stream: str = "stream:risk:rules_history"
    sizing_history_stream: str = "stream:risk:sizing_history"
    rules_history_maxlen: int = 2000
    sizing_history_maxlen: int = 5000
    cache_source: str = "risk-service"
    config_measurement: str = "risk_config_snapshot"
    rules_measurement: str = "risk_rule_snapshot"

    _engine: RiskEngine = field(default_factory=RiskEngine, init=False, repr=False)
    _redis: Redis | None = field(default=None, init=False, repr=False)
    _logger: Any = field(default=None, init=False, repr=False)
    _market_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _results_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _consumer_name: str = field(default_factory=lambda: f"risk-{uuid.uuid4().hex[:8]}", init=False, repr=False)
    _pending_commands: MutableMapping[str, RiskEvent] = field(default_factory=dict, init=False, repr=False)
    _configured: bool = field(default=False, init=False, repr=False)
    _config_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _market_last_processed: datetime | None = field(default=None, init=False, repr=False)
    _results_last_processed: datetime | None = field(default=None, init=False, repr=False)
    _health_last_error: str = field(default="", init=False, repr=False)
    _last_exception_message: str = field(default="", init=False, repr=False)
    _config_snapshot: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _rules_snapshot: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _position_last_requested: datetime | None = field(default=None, init=False, repr=False)
    _config_snapshot_timestamp: str | None = field(default=None, init=False, repr=False)
    _rules_snapshot_timestamp: str | None = field(default=None, init=False, repr=False)
    _sizing_history_length: int = field(default=0, init=False, repr=False)
    _rules_history_length: int = field(default=0, init=False, repr=False)
    _risk_snapshot_repository_close: Callable[[], Awaitable[None]] | None = field(
        default=None, init=False, repr=False
    )
    _risk_snapshot_repository_created: bool = field(
        default=False, init=False, repr=False
    )

    # ------------------------------------------------------------------
    # redis helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_symbol(value: str | None) -> str:
        return value.strip().upper() if value else ""

    def _encode_value(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=self._json_default)

    @staticmethod
    def _decode_value(raw: Any) -> Any:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="ignore")
        else:
            text = str(raw)
        if text == "":
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _stream_fields(self, payload: Mapping[str, Any]) -> dict[str, str]:
        fields: dict[str, str] = {}
        for key, value in payload.items():
            if value is None:
                continue
            key_str = str(key)
            if isinstance(value, (str, bytes)):
                if isinstance(value, bytes):
                    fields[key_str] = value.decode("utf-8", errors="ignore")
                else:
                    fields[key_str] = value
            else:
                fields[key_str] = self._encode_value(value)
        return fields

    async def load_risk_config(self) -> dict[str, Any]:
        redis = await self._get_cache_redis()
        if redis is None:
            return dict(self._config_snapshot)
        try:
            payload = await redis.hgetall(self.config_hash_key)
        except Exception:
            return dict(self._config_snapshot)
        if not payload:
            return dict(self._config_snapshot)
        config: dict[str, Any] = {}
        for key, value in payload.items():
            key_str = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            config[key_str] = self._decode_value(value)
        return config

    async def save_risk_config(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        config = await self.load_risk_config()
        timestamp = _utc_now().isoformat()
        removed_fields: list[str] = []
        for key, value in updates.items():
            key_str = str(key)
            if key_str in {"rules", "updated_at"}:
                continue
            if value is None:
                if key_str in config:
                    config.pop(key_str, None)
                removed_fields.append(key_str)
            else:
                config[key_str] = value
        config["updated_at"] = timestamp

        redis = await self._get_cache_redis()
        if redis is not None:
            mapping = {key: self._encode_value(value) for key, value in config.items()}
            await redis.hset(self.config_hash_key, mapping=mapping)
            if removed_fields:
                try:
                    await redis.hdel(self.config_hash_key, *removed_fields)
                except TypeError:
                    for field in removed_fields:
                        await redis.hdel(self.config_hash_key, field)
        return config

    async def load_risk_rules(self) -> dict[str, dict[str, Any]]:
        redis = await self._get_cache_redis()
        if redis is None:
            return self.get_rules_snapshot()
        try:
            payload = await redis.hgetall(self.rules_hash_key)
        except Exception:
            return self.get_rules_snapshot()
        if not payload:
            return {}
        rules: dict[str, dict[str, Any]] = {}
        updates: dict[str, str] = {}
        removals: set[str] = set()
        for key, value in payload.items():
            symbol_raw = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            data = self._decode_value(value)
            if not isinstance(data, Mapping):
                removals.add(symbol_raw)
                continue
            original_payload = {str(k): v for k, v in data.items()}
            working_payload = dict(original_payload)
            symbol_normalised = self._normalise_symbol(symbol_raw)
            rule_key = symbol_normalised or symbol_raw
            working_payload.setdefault("symbol", rule_key)
            normalised = RiskRule.normalise_payload(working_payload)
            normalised_symbol = self._normalise_symbol(
                str(normalised.get("symbol") or rule_key)
            )
            if normalised_symbol:
                normalised["symbol"] = normalised_symbol
                rule_key = normalised_symbol
            else:
                normalised.setdefault("symbol", rule_key)
            rules[rule_key] = normalised

            needs_update = original_payload != normalised or rule_key != symbol_raw
            if needs_update:
                updates[rule_key] = self._encode_value(normalised)
                if rule_key != symbol_raw:
                    removals.add(symbol_raw)

        if (updates or removals) and redis is not None:
            try:
                if updates:
                    await redis.hset(self.rules_hash_key, mapping=updates)
                if removals:
                    try:
                        await redis.hdel(self.rules_hash_key, *removals)
                    except TypeError:
                        for field in removals:
                            await redis.hdel(self.rules_hash_key, field)
            except Exception:
                logger = self._logger or get_logger(self.logger_name)
                logger.debug("Failed to migrate cached risk rules", exc_info=True)
        return rules

    def _merge_rule_payload(
        self, existing: Mapping[str, Any], updates: Mapping[str, Any], symbol: str
    ) -> dict[str, Any]:
        merged = {str(k): v for k, v in existing.items() if k != "updated_at"}
        for key, value in updates.items():
            key_str = str(key)
            if key_str in {"symbol", "updated_at"}:
                continue
            if value is None:
                merged.pop(key_str, None)
            else:
                merged[key_str] = value
        merged["symbol"] = symbol
        return merged

    async def save_risk_rule(
        self, symbol: str, updates: Mapping[str, Any]
    ) -> dict[str, Any]:
        symbol_normalised = self._normalise_symbol(symbol)
        if not symbol_normalised:
            symbol_normalised = self._normalise_symbol(str(updates.get("symbol")))
        if not symbol_normalised:
            raise ValueError("risk rule requires a symbol")
        existing_rules = await self.load_risk_rules()
        existing = existing_rules.get(symbol_normalised, {"symbol": symbol_normalised})
        merged = self._merge_rule_payload(existing, updates, symbol_normalised)
        timestamp = _utc_now().isoformat()
        merged["updated_at"] = timestamp
        normalised = RiskRule.normalise_payload(merged)
        normalised["symbol"] = symbol_normalised
        normalised["updated_at"] = timestamp

        redis = await self._get_cache_redis()
        if redis is not None:
            mapping = {symbol_normalised: self._encode_value(normalised)}
            await redis.hset(self.rules_hash_key, mapping=mapping)
            history_payload = {
                "event": "upsert",
                "symbol": symbol_normalised,
                "payload": self._encode_value(normalised),
                "updated_at": timestamp,
            }
            try:
                await redis.xadd(
                    self.rules_history_stream,
                    self._stream_fields(history_payload),
                    maxlen=self.rules_history_maxlen,
                    approximate=True,
                )
                self._rules_history_length = int(
                    await redis.xlen(self.rules_history_stream)
                )
            except Exception:
                pass
        return normalised

    async def delete_risk_rule(self, symbol: str) -> bool:
        symbol_normalised = self._normalise_symbol(symbol)
        if not symbol_normalised:
            return False
        redis = await self._get_cache_redis()
        removed = False
        if redis is not None:
            try:
                removed = bool(await redis.hdel(self.rules_hash_key, symbol_normalised))
            except Exception:
                removed = False
            if removed:
                timestamp = _utc_now().isoformat()
                history_payload = {
                    "event": "delete",
                    "symbol": symbol_normalised,
                    "updated_at": timestamp,
                }
                try:
                    await redis.xadd(
                        self.rules_history_stream,
                        self._stream_fields(history_payload),
                        maxlen=self.rules_history_maxlen,
                        approximate=True,
                    )
                    self._rules_history_length = int(
                        await redis.xlen(self.rules_history_stream)
                    )
                except Exception:
                    pass
        return removed

    async def reload_from_storage(
        self,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        config = await self.load_risk_config()
        rules = await self.load_risk_rules()
        payload = dict(config)
        payload["rules"] = list(rules.values())
        await self.update_config(payload)
        return self.get_config_snapshot(), self.get_rules_snapshot()

    async def append_sizing_history(
        self, payload: Mapping[str, Any]
    ) -> str | None:
        redis = await self._get_cache_redis()
        if redis is None:
            return None
        timestamp = str(payload.get("timestamp") or payload.get("created_at") or "")
        if not timestamp:
            timestamp = _utc_now().isoformat()
        entry = dict(payload)
        entry.setdefault("timestamp", timestamp)
        try:
            message_id = await redis.xadd(
                self.sizing_history_stream,
                self._stream_fields(entry),
                maxlen=self.sizing_history_maxlen,
                approximate=True,
            )
            self._sizing_history_length = int(
                await redis.xlen(self.sizing_history_stream)
            )
            return str(message_id)
        except Exception:
            return None

    async def fetch_rules_history(
        self, *, symbol: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        redis = await self._get_cache_redis()
        limit_clamped = max(1, min(int(limit or 1), 500))
        symbol_filter = self._normalise_symbol(symbol)
        if redis is None:
            return {"success": True, "items": [], "count": 0}
        try:
            entries = await redis.xrevrange(
                self.rules_history_stream,
                count=limit_clamped * 4,
            )
        except Exception:
            entries = []
        results: list[dict[str, Any]] = []
        for entry_id, fields in entries or []:
            if isinstance(fields, Mapping):
                payload = {
                    str(k if not isinstance(k, bytes) else k.decode("utf-8")): self._decode_value(v)
                    for k, v in fields.items()
                }
            else:
                payload = {}
            payload.setdefault("id", entry_id if isinstance(entry_id, str) else str(entry_id))
            payload_symbol = self._normalise_symbol(
                str(payload.get("symbol") or "")
            )
            if symbol_filter and payload_symbol != symbol_filter:
                continue
            results.append(payload)
            if len(results) >= limit_clamped:
                break
        results.reverse()
        history = [dict(item) for item in results]
        return {
            "success": True,
            "items": history,
            "history": history,
            "count": len(history),
        }

    async def update_config(self, config: Mapping[str, Any]) -> None:
        snapshot: tuple[dict[str, Any], dict[str, Any], str] | None = None
        async with self._config_lock:
            self._engine.load_config(config)
            self.command_queue = str(config.get("command_queue", self.command_queue))
            self.market_stream_key = str(config.get("market_stream", self.market_stream_key))
            self.market_group = str(config.get("market_group", self.market_group))
            self.results_stream_key = str(config.get("results_stream", self.results_stream_key))
            self.results_group = str(config.get("results_group", self.results_group))
            poll_value = config.get("poll_interval", self.poll_interval)
            try:
                self.poll_interval = max(0.05, float(poll_value))
            except (TypeError, ValueError):
                pass
            batch_value = config.get("batch_size", self.batch_size)
            try:
                batch = int(batch_value)
                if batch > 0:
                    self.batch_size = batch
            except (TypeError, ValueError):
                pass
            snapshot = self._build_snapshots(config)
            self._configured = True

        if snapshot is not None:
            config_payload, rules_payload, timestamp = snapshot
            self._config_snapshot = dict(config_payload)
            self._rules_snapshot = {
                str(symbol): dict(payload)
                for symbol, payload in rules_payload.items()
            }
            self._config_snapshot.setdefault("updated_at", timestamp)
            self._config_snapshot["rules_count"] = int(len(self._rules_snapshot))
            for payload in self._rules_snapshot.values():
                payload.setdefault("updated_at", timestamp)
            self._config_snapshot_timestamp = str(self._config_snapshot.get("updated_at") or timestamp)
            rule_timestamps = [
                str(value.get("updated_at"))
                for value in self._rules_snapshot.values()
                if value.get("updated_at")
            ]
            if rule_timestamps:
                self._rules_snapshot_timestamp = max(rule_timestamps)
            else:
                self._rules_snapshot_timestamp = self._config_snapshot_timestamp
            await self._publish_snapshots(*snapshot)

    def _build_snapshots(
        self, config: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        timestamp = _utc_now().isoformat()
        config_payload: dict[str, Any] = {}
        for key, value in config.items():
            if str(key) == "rules":
                continue
            config_payload[str(key)] = value
        config_payload.setdefault("updated_at", timestamp)
        rules_payload = self._engine.export_rules()
        config_payload["rules_count"] = int(len(rules_payload))
        return config_payload, rules_payload, timestamp

    async def _publish_snapshots(
        self,
        config_payload: Mapping[str, Any],
        rules_payload: Mapping[str, Any],
        timestamp: str,
    ) -> None:
        rules_snapshot = {
            str(symbol): dict(payload)
            for symbol, payload in rules_payload.items()
            if isinstance(payload, Mapping)
        }
        try:
            await self._write_cache_snapshot(config_payload, rules_snapshot, timestamp)
        except Exception:
            logger = self._logger or get_logger(self.logger_name)
            logger.warning("Failed to update risk cache", exc_info=True)
        try:
            await self._publish_rules_update_event(rules_snapshot, timestamp)
        except Exception:
            logger = self._logger or get_logger(self.logger_name)
            logger.warning("Failed to publish risk rules update", exc_info=True)
        try:
            await self._persist_repository_snapshot(config_payload, rules_snapshot, timestamp)
        except Exception:
            logger = self._logger or get_logger(self.logger_name)
            logger.warning("Failed to persist risk snapshot to repository", exc_info=True)

    async def _get_cache_redis(self) -> Redis | None:
        try:
            if self._redis is not None:
                return self._redis
            return await self.redis_factory()
        except Exception:
            logger = self._logger or get_logger(self.logger_name)
            logger.warning("Failed to acquire Redis client for cache refresh", exc_info=True)
            return None

    async def _write_cache_snapshot(
        self,
        config_payload: Mapping[str, Any],
        rules_payload: Mapping[str, Mapping[str, Any]],
        timestamp: str,
    ) -> None:
        redis = await self._get_cache_redis()
        if redis is None:
            return

        config_cache = dict(config_payload)
        config_cache.setdefault("updated_at", timestamp)
        rules_cache: dict[str, Any] = {
            "success": True,
            "rules": dict(rules_payload),
            "updated_at": timestamp,
            "source": self.cache_source,
            "count": len(rules_payload),
        }

        try:
            pipeline = redis.pipeline()
        except Exception:
            await redis.set(self.config_cache_key, self._dump_json(config_cache))
            await redis.set(self.rules_cache_key, self._dump_json(rules_cache))
            return

        pipeline.set(self.config_cache_key, self._dump_json(config_cache))
        pipeline.set(self.rules_cache_key, self._dump_json(rules_cache))
        await pipeline.execute()

    async def _publish_rules_update_event(
        self,
        rules_payload: Mapping[str, Mapping[str, Any]],
        timestamp: str,
    ) -> None:
        redis = await self._get_cache_redis()
        if redis is None:
            return

        message: dict[str, Any] = {
            "type": "risk_rules",
            "success": True,
            "rules": dict(rules_payload),
            "updated_at": timestamp,
            "source": self.cache_source,
            "count": len(rules_payload),
        }

        await redis.publish(
            self.rules_update_channel,
            self._dump_json(message),
        )

    async def _persist_repository_snapshot(
        self,
        config_payload: Mapping[str, Any],
        rules_payload: Mapping[str, Mapping[str, Any]],
        timestamp: str,
    ) -> None:
        repository = await self._ensure_risk_snapshot_repository()
        if repository is None:
            return

        config_snapshot = dict(config_payload)
        config_snapshot.setdefault("updated_at", timestamp)
        config_snapshot["rules_count"] = int(len(rules_payload))

        await repository.save_config_snapshot(
            service=self.service_name,
            payload=config_snapshot,
            rules_count=int(len(rules_payload)),
            updated_at=config_snapshot.get("updated_at"),
            captured_at=timestamp,
        )

        if not rules_payload:
            return

        normalised_rules: dict[str, dict[str, Any]] = {}
        for symbol, rule_payload in rules_payload.items():
            symbol_str = str(symbol)
            payload = dict(rule_payload)
            payload.setdefault("symbol", symbol_str)
            payload.setdefault("updated_at", timestamp)
            normalised_rules[symbol_str] = payload

        await repository.bulk_upsert_rule_snapshots(
            service=self.service_name,
            rules=normalised_rules,
            captured_at=timestamp,
        )

    async def _ensure_risk_snapshot_repository(
        self,
    ) -> RiskSnapshotRepositoryProtocol | None:
        if self.risk_snapshot_repository is not None:
            return self.risk_snapshot_repository
        try:
            await self._initialise_risk_snapshot_repository()
        except Exception as exc:
            logger = self._logger or get_logger(self.logger_name)
            logger.warning(
                "Failed to initialise risk snapshot repository", exc_info=True
            )
            self._last_exception_message = str(exc)
            return None
        return self.risk_snapshot_repository

    async def _initialise_risk_snapshot_repository(self) -> None:
        if self.risk_snapshot_repository is not None:
            return
        factory = self.risk_snapshot_repository_factory
        if factory is None:
            return
        repository_candidate = factory()
        repository = await _maybe_await(repository_candidate)
        if repository is None:
            self._risk_snapshot_repository_created = False
            self._risk_snapshot_repository_close = None
            return
        self.risk_snapshot_repository = repository
        self._risk_snapshot_repository_created = True
        self._risk_snapshot_repository_close = self._resolve_repository_close(
            repository
        )

    async def _shutdown_risk_snapshot_repository(self) -> None:
        closer = self._risk_snapshot_repository_close
        self._risk_snapshot_repository_close = None
        if closer is not None:
            try:
                await closer()
            except Exception:
                logger = self._logger or get_logger(self.logger_name)
                logger.debug(
                    "Failed to close risk snapshot repository", exc_info=True
                )
        if self._risk_snapshot_repository_created:
            self.risk_snapshot_repository = None
            self._risk_snapshot_repository_created = False

    def _resolve_repository_close(
        self, repository: RiskSnapshotRepositoryProtocol
    ) -> Callable[[], Awaitable[None]] | None:
        for attr in ("aclose", "close"):
            closer = getattr(repository, attr, None)
            if callable(closer):
                async def _close(
                    closer: Callable[[], Awaitable[Any] | Any] = closer,
                ) -> None:
                    await _maybe_await(closer())

                return _close

        session = getattr(repository, "_session", None)
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                async def _close_session(
                    close: Callable[[], Awaitable[Any] | Any] = close,
                ) -> None:
                    await _maybe_await(close())

                return _close_session
        return None

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, set):
            return list(value)
        return value

    def _dump_json(self, payload: Mapping[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, default=self._json_default)

    def _health_metrics(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "market_last_processed_at": self._market_last_processed.isoformat()
            if self._market_last_processed
            else "",
            "results_last_processed_at": self._results_last_processed.isoformat()
            if self._results_last_processed
            else "",
            "pending_commands": len(self._pending_commands),
        }
        if self._last_exception_message:
            metrics["last_exception"] = self._last_exception_message
        if extra:
            metrics.update(extra)
        if self._position_last_requested is not None:
            metrics["position_last_requested_at"] = self._position_last_requested.isoformat()
        if self._config_snapshot_timestamp:
            metrics["config_snapshot_at"] = self._config_snapshot_timestamp
        if self._rules_snapshot_timestamp:
            metrics["rules_snapshot_at"] = self._rules_snapshot_timestamp
        metrics["sizing_history_events"] = self._sizing_history_length
        metrics["rules_history_events"] = self._rules_history_length
        return metrics

    async def _update_health(
        self,
        *,
        healthy: bool,
        last_error: str | None = None,
        extra_metrics: Mapping[str, Any] | None = None,
    ) -> None:
        if self._redis is None:
            return
        if healthy:
            self._health_last_error = ""
        elif last_error is not None:
            self._health_last_error = last_error
            self._last_exception_message = last_error
        metrics = self._health_metrics(extra_metrics)
        await update_service_health(
            self._redis,
            self.service_name,
            healthy=healthy,
            last_error=self._health_last_error,
            metrics=metrics,
            logger=self._logger,
        )
        self._log_loop_event(
            str(extra_metrics.get("loop") if extra_metrics else "health"),
            "Published risk runtime health metrics",
            extra={"healthy": healthy, "metrics": dict(metrics)},
        )

    def _set_market_last_processed(self, timestamp: datetime) -> None:
        self._market_last_processed = timestamp

    def _set_results_last_processed(self, timestamp: datetime) -> None:
        self._results_last_processed = timestamp

    def _log_loop_event(
        self,
        loop_name: str,
        message: str,
        *,
        level: str = "debug",
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if not self._logger:
            return
        payload = {"loop": loop_name}
        if extra:
            payload.update({str(k): v for k, v in extra.items()})
        log_method = getattr(self._logger, level, None)
        if log_method is None:
            log_method = getattr(self._logger, "debug", None)
        if log_method is None:
            return
        try:
            log_method(message, extra=payload)
        except Exception:
            pass

    async def _process_stream_messages(
        self,
        *,
        loop_name: str,
        stream_key: str,
        group: str,
        messages: Iterable[StreamMessage],
        handler: Callable[[Mapping[str, Any]], Awaitable[bool]],
        on_processed: Callable[[datetime], None],
    ) -> None:
        assert self._redis is not None
        batch = list(messages)
        if not batch:
            metrics = {"loop": loop_name, "messages": 0, "state": "idle"}
            await self._update_health(healthy=True, extra_metrics=metrics)
            self._log_loop_event(
                loop_name,
                f"{loop_name.title()} loop idle",
                extra={"metrics": metrics},
            )
            return

        timestamp = _utc_now()
        on_processed(timestamp)
        ack_ids: list[str] = []
        for message in batch:
            try:
                handled = await handler(message.data)
            except Exception:
                self._log_loop_event(
                    loop_name,
                    f"Failed to handle {loop_name} message",
                    level="warning",
                    extra={"message_id": message.message_id},
                )
                continue
            if handled:
                ack_ids.append(message.message_id)

        acknowledged = 0
        if ack_ids:
            try:
                acknowledged = await ack_stream(
                    ack_ids,
                    stream_key=stream_key,
                    group=group,
                    redis=self._redis,
                )
                self._log_loop_event(
                    loop_name,
                    f"{loop_name.title()} loop acknowledged {acknowledged} messages in Redis",
                    extra={"ack_ids": list(ack_ids)},
                )
            except Exception as exc:
                self._log_loop_event(
                    loop_name,
                    f"{loop_name.title()} loop failed to acknowledge messages",
                    level="warning",
                    extra={"error": str(exc), "pending": len(batch)},
                )
                await self._update_health(
                    healthy=False,
                    last_error=str(exc),
                    extra_metrics={
                        "loop": loop_name,
                        "state": "ack_failed",
                        "messages": len(batch),
                    },
                )
                return
        else:
            self._log_loop_event(
                loop_name,
                f"{loop_name.title()} loop had no messages to acknowledge",
                extra={"pending": len(batch)},
            )

        metrics = {
            "loop": loop_name,
            "messages": len(batch),
            "state": "processed",
            "acknowledged": acknowledged,
        }
        await self._update_health(healthy=True, extra_metrics=metrics)
        self._log_loop_event(
            loop_name,
            f"{loop_name.title()} loop published health metrics",
            extra={"metrics": metrics},
        )

    async def _consume_stream_iteration(
        self,
        *,
        loop_name: str,
        stream_key: str,
        group: str,
        consumer: str,
        handler: Callable[[Mapping[str, Any]], Awaitable[bool]],
        on_processed: Callable[[datetime], None],
    ) -> None:
        assert self._redis is not None
        block_ms = max(1, int(self.poll_interval * 1000))
        try:
            messages = await asyncio.wait_for(
                read_stream(
                    stream_key=stream_key,
                    group=group,
                    consumer=consumer,
                    count=self.batch_size,
                    block=block_ms,
                    redis=self._redis,
                ),
                timeout=self.poll_interval + 1.0,
            )
        except asyncio.TimeoutError:
            messages = []
        except Exception as exc:
            if self._logger:
                self._logger.warning(
                    "Failed to read %s stream", stream_key, exc_info=True
                )
            self._log_loop_event(
                loop_name,
                f"{loop_name.title()} loop encountered read failure",
                level="warning",
                extra={"error": str(exc)},
            )
            await self._update_health(
                healthy=False,
                last_error=str(exc),
                extra_metrics={"loop": loop_name, "state": "read_failed"},
            )
            await asyncio.sleep(self.reconnect_delay)
            return

        await self._process_stream_messages(
            loop_name=loop_name,
            stream_key=stream_key,
            group=group,
            messages=messages,
            handler=handler,
            on_processed=on_processed,
        )

    async def start(self) -> None:
        if not self._configured:
            raise RuntimeError("RiskRuntime must be configured before starting")
        self._logger = get_logger(self.logger_name, log_dir=self.log_dir)
        try:
            await self._initialise_risk_snapshot_repository()
        except Exception as exc:
            logger = self._logger or get_logger(self.logger_name)
            logger.warning(
                "Failed to initialise risk snapshot repository", exc_info=True
            )
            self._last_exception_message = str(exc)
        self._redis = await self.redis_factory()
        await self._ensure_stream_groups()
        self._stop_event.clear()
        self._market_task = asyncio.create_task(self._market_loop(), name="risk-market-loop")
        self._results_task = asyncio.create_task(self._results_loop(), name="risk-results-loop")
        await self._update_health(healthy=True, extra_metrics={"state": "started"})

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._market_task, self._results_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(self._await_task(task) for task in (self._market_task, self._results_task)),
            return_exceptions=True,
        )
        self._market_task = None
        self._results_task = None
        if self._redis is not None:
            await self._update_health(
                healthy=False,
                last_error="stopped",
                extra_metrics={"state": "stopped"},
            )
        self._redis = None
        self._pending_commands.clear()
        await self._shutdown_risk_snapshot_repository()

    async def _await_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _ensure_stream_groups(self) -> None:
        if self._redis is None:
            return
        for stream, group in (
            (self.market_stream_key, self.market_group),
            (self.results_stream_key, self.results_group),
        ):
            try:
                await self._redis.xgroup_create(stream, group, id="$", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            except Exception:
                if self._logger:
                    self._logger.warning(
                        "Failed to create consumer group for %s/%s", stream, group, exc_info=True
                    )

    async def _market_loop(self) -> None:
        assert self._redis is not None
        while not self._stop_event.is_set():
            await self._consume_stream_iteration(
                loop_name="market",
                stream_key=self.market_stream_key,
                group=self.market_group,
                consumer=self._consumer_name,
                handler=self._handle_market_message,
                on_processed=self._set_market_last_processed,
            )

    async def _results_loop(self) -> None:
        assert self._redis is not None
        while not self._stop_event.is_set():
            await self._consume_stream_iteration(
                loop_name="results",
                stream_key=self.results_stream_key,
                group=self.results_group,
                consumer=f"{self._consumer_name}-results",
                handler=self._handle_result_message,
                on_processed=self._set_results_last_processed,
            )

    async def _handle_market_message(self, payload: Mapping[str, Any]) -> bool:
        symbol = payload.get("symbol")
        if not symbol:
            return True
        body: Mapping[str, Any]
        raw = payload.get("payload")
        if isinstance(raw, str):
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
        elif isinstance(raw, Mapping):
            body = raw
        else:
            body = payload
        price_value = body.get("last") or body.get("price") or body.get("close")
        try:
            price = float(price_value)
        except (TypeError, ValueError):
            return True
        events = self._engine.evaluate_price(str(symbol), price, body)
        for event in events:
            await self._dispatch_event(event)
        return True

    async def _handle_result_message(self, payload: Mapping[str, Any]) -> bool:
        command_id = payload.get("command_id")
        if not command_id:
            return True
        command_id = str(command_id)
        event = self._pending_commands.pop(command_id, None)
        if not event:
            return True
        status = str(payload.get("status") or "").lower()
        self._engine.reset_trigger(event.symbol, event.trigger)
        message = f"Received result for {event.symbol}: {status or 'unknown'}"
        logger = self._logger or get_logger(self.logger_name)
        extra = {
            "symbol": event.symbol,
            "trigger": event.trigger,
            "status": status or "unknown",
        }
        logger.info(message, extra=extra)
        return True

    async def _dispatch_event(self, event: RiskEvent) -> None:
        command_id = uuid.uuid4().hex
        event.command_id = command_id
        payload = event.build_command(command_id=command_id)
        try:
            await enqueue_ib_command(payload, redis=self._redis, queue_key=self.command_queue)
        except Exception:
            self._engine.reset_trigger(event.symbol, event.trigger)
            if self._logger:
                self._logger.error("Failed to enqueue risk command", extra={"symbol": event.symbol})
            return
        self._pending_commands[command_id] = event
        message = f"Triggered {event.trigger} for {event.symbol} at {event.price:.4f}"
        logger = self._logger or get_logger(self.logger_name)
        extra = {
            "symbol": event.symbol,
            "trigger": event.trigger,
            "price": float(event.price),
        }
        logger.warning(message, extra=extra)

    def get_config_snapshot(self) -> dict[str, Any]:
        """Return the latest cached configuration snapshot."""

        return dict(self._config_snapshot)

    def get_rules_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return the latest cached rule snapshot keyed by symbol."""

        return {symbol: dict(payload) for symbol, payload in self._rules_snapshot.items()}

    def health_snapshot(self) -> dict[str, Any]:
        """Expose health metrics collected by the runtime."""

        return self._health_metrics()

    async def fetch_order_queue_status(self) -> dict[str, Any]:
        """Return a lightweight snapshot of the order queue state."""

        metrics = self.health_snapshot()
        pending = int(metrics.get("pending_commands", 0) or 0)
        payload = {
            "success": True,
            "queue": self.command_queue,
            "pending": pending,
            "metrics": metrics,
        }
        if self._market_last_processed is not None:
            payload["market_last_processed_at"] = self._market_last_processed.isoformat()
        if self._results_last_processed is not None:
            payload["results_last_processed_at"] = self._results_last_processed.isoformat()
        return payload

    async def fetch_sizing_history(
        self,
        *,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Load persisted sizing recommendations from Redis."""

        limit_clamped = max(1, min(int(limit or 1), 500))
        symbol_filter = self._normalise_symbol(symbol)
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
            except Exception:
                since_dt = None

        redis = await self._get_cache_redis()
        if redis is None:
            return {"success": True, "items": [], "count": 0}

        try:
            entries = await redis.xrevrange(
                self.sizing_history_stream,
                count=limit_clamped * 4,
            )
            self._sizing_history_length = int(
                await redis.xlen(self.sizing_history_stream)
            )
        except Exception:
            entries = []

        results: list[dict[str, Any]] = []
        for entry_id, fields in entries or []:
            if isinstance(fields, Mapping):
                payload = {
                    str(k if not isinstance(k, bytes) else k.decode("utf-8")): self._decode_value(v)
                    for k, v in fields.items()
                }
            else:
                payload = {}
            payload.setdefault("id", entry_id if isinstance(entry_id, str) else str(entry_id))
            item_symbol = self._normalise_symbol(str(payload.get("symbol") or ""))
            if symbol_filter and item_symbol != symbol_filter:
                continue
            if since_dt is not None:
                ts_raw = payload.get("timestamp") or payload.get("created_at")
                ts_value = None
                if isinstance(ts_raw, str) and ts_raw:
                    try:
                        ts_value = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except Exception:
                        ts_value = None
                if ts_value is not None and ts_value < since_dt:
                    continue
            results.append(payload)
            if len(results) >= limit_clamped:
                break
        results.reverse()
        history = [dict(item) for item in results]
        return {
            "success": True,
            "items": history,
            "history": history,
            "count": len(history),
        }

    async def position_sizing(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Estimate a contract count for the provided payload."""

        symbol = str(payload.get("symbol") or "").strip().upper()
        stop_distance_raw = payload.get("stop_distance")
        try:
            stop_distance = float(stop_distance_raw)
        except (TypeError, ValueError):
            stop_distance = 0.0
        if stop_distance <= 0:
            raise ValueError("stop_distance must be positive")

        config = self.get_config_snapshot()
        risk_fraction_raw = (
            config.get("risk_per_trade_r")
            or config.get("risk_per_trade")
            or payload.get("risk_per_trade")
            or 0.01
        )
        try:
            risk_fraction = float(risk_fraction_raw)
        except (TypeError, ValueError):
            risk_fraction = 0.01
        if risk_fraction < 0:
            risk_fraction = 0.0

        equity_raw = (
            payload.get("account_equity")
            or config.get("account_equity")
            or config.get("equity")
            or 100000.0
        )
        try:
            equity = float(equity_raw)
        except (TypeError, ValueError):
            equity = 100000.0
        if equity < 0:
            equity = 0.0

        point_value_map = config.get("point_value_map")
        point_value_raw = payload.get("point_value")
        if point_value_raw is None and isinstance(point_value_map, Mapping):
            candidate = None
            if symbol:
                candidate = point_value_map.get(symbol)
                if candidate is None:
                    for key, value in point_value_map.items():
                        try:
                            key_str = str(key).upper()
                        except Exception:
                            key_str = str(key)
                        if key_str and symbol.startswith(key_str):
                            candidate = value
                            break
            point_value_raw = candidate
        try:
            point_value = float(point_value_raw) if point_value_raw is not None else 1.0
        except (TypeError, ValueError):
            point_value = 1.0
        if point_value <= 0:
            point_value = 1.0

        risk_capital = max(0.0, equity * risk_fraction)
        denominator = point_value * stop_distance
        if denominator <= 0:
            raise ValueError("invalid sizing inputs")

        raw_contracts = risk_capital / denominator
        contracts = max(0, int(raw_contracts))
        self._position_last_requested = _utc_now()

        timestamp = _utc_now().isoformat()
        result = {
            "success": True,
            "symbol": symbol,
            "contracts": contracts,
            "risk_capital": risk_capital,
            "stop_distance": stop_distance,
            "risk_fraction": risk_fraction,
            "point_value": point_value,
            "raw_contracts": raw_contracts,
            "timestamp": timestamp,
        }
        await self.append_sizing_history(result)
        return result
