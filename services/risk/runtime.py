from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping

from services.risk.engine import RiskEngine, RiskEvent
from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.ibbot.redis import (
    ack_stream,
    enqueue_ib_command,
    read_stream,
    ResponseError,
)
from src.ibbot.influx import write_points

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


@dataclass(slots=True)
class RiskRuntime:
    """Orchestrate the risk engine and connect it to external services."""

    redis_factory: Callable[[], Awaitable[Redis]]
    write_fn: Callable[[Iterable[Mapping[str, Any]]], Awaitable[int] | int] = write_points
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
    _active_config: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _rules_state: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _config_snapshot: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _rules_snapshot: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _snapshot_timestamp: str | None = field(default=None, init=False, repr=False)
    _sizing_history: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

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
                self._active_config = {
                    str(key): value
                    for key, value in config_payload.items()
                    if str(key) != "updated_at"
                }
                self._rules_state = self._normalise_rules_state(rules_payload)
                self._config_snapshot = dict(config_payload)
                self._rules_snapshot = self._build_rules_response(self._rules_state)
                self._snapshot_timestamp = timestamp

        if snapshot is not None:
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
        return config_payload, rules_payload, timestamp

    def _normalise_rules_state(
        self, rules_payload: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        rules: dict[str, dict[str, Any]] = {}
        for symbol, payload in rules_payload.items():
            symbol_key = str(symbol).strip().upper()
            rule = {"symbol": symbol_key}
            for key, value in dict(payload).items():
                if key == "symbol":
                    continue
                if key == "metadata" and isinstance(value, Mapping):
                    rule["metadata"] = dict(value)
                else:
                    rule[key] = value
            rules[symbol_key] = rule
        return rules

    def _build_rules_response(
        self, rules: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        response: dict[str, dict[str, Any]] = {}
        for symbol, payload in rules.items():
            rule_payload: dict[str, Any] = {"symbol": str(symbol).strip().upper()}
            metadata: Mapping[str, Any] | None = None
            for key, value in payload.items():
                if key == "metadata" and isinstance(value, Mapping):
                    metadata = value
                    continue
                if key == "symbol":
                    continue
                rule_payload[key] = value
            if metadata:
                rule_payload["metadata"] = dict(metadata)
                for meta_key, meta_value in metadata.items():
                    rule_payload.setdefault(str(meta_key), meta_value)
            response[rule_payload["symbol"]] = rule_payload
        return response

    async def _publish_snapshots(
        self,
        config_payload: Mapping[str, Any],
        rules_payload: Mapping[str, Any],
        timestamp: str,
    ) -> None:
        try:
            await self._write_cache_snapshot(config_payload, rules_payload, timestamp)
        except Exception:
            logger = self._logger or get_logger(self.logger_name)
            logger.warning("Failed to update risk cache", exc_info=True)
        try:
            await self._write_influx_snapshot(config_payload, rules_payload, timestamp)
        except Exception:
            logger = self._logger or get_logger(self.logger_name)
            logger.warning("Failed to persist risk snapshot to InfluxDB", exc_info=True)

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
        rules_payload: Mapping[str, Any],
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

    async def _write_influx_snapshot(
        self,
        config_payload: Mapping[str, Any],
        rules_payload: Mapping[str, Any],
        timestamp: str,
    ) -> None:
        points: list[Mapping[str, Any]] = []

        config_fields: dict[str, Any] = {
            "payload": self._dump_json(config_payload),
            "updated_at": timestamp,
            "rules_count": int(len(rules_payload)),
        }
        points.append(
            {
                "measurement": self.config_measurement,
                "tags": {"service": self.service_name},
                "fields": config_fields,
            }
        )

        for symbol, rule_payload in rules_payload.items():
            points.append(
                {
                    "measurement": self.rules_measurement,
                    "tags": {"service": self.service_name, "symbol": str(symbol)},
                    "fields": {
                        "payload": self._dump_json(rule_payload),
                        "updated_at": timestamp,
                    },
                }
            )

        await _maybe_await(self.write_fn(points))

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

    async def start(self) -> None:
        if not self._configured:
            raise RuntimeError("RiskRuntime must be configured before starting")
        self._logger = get_logger(self.logger_name, log_dir=self.log_dir)
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
            try:
                messages = await read_stream(
                    stream_key=self.market_stream_key,
                    group=self.market_group,
                    consumer=self._consumer_name,
                    count=self.batch_size,
                    block=int(self.poll_interval * 1000),
                    redis=self._redis,
                )
            except Exception as exc:
                if self._logger:
                    self._logger.warning("Failed to read market stream", exc_info=True)
                await self._update_health(
                    healthy=False,
                    last_error=str(exc),
                    extra_metrics={"loop": "market", "state": "read_failed"},
                )
                await asyncio.sleep(self.reconnect_delay)
                continue
            if not messages:
                await self._update_health(
                    healthy=True,
                    extra_metrics={"loop": "market", "messages": 0, "state": "idle"},
                )
                continue
            ack_ids: list[str] = []
            self._market_last_processed = _utc_now()
            for message in messages:
                handled = await self._handle_market_message(message.data)
                if handled:
                    ack_ids.append(message.message_id)
            if ack_ids:
                try:
                    await ack_stream(ack_ids, stream_key=self.market_stream_key, group=self.market_group, redis=self._redis)
                except Exception as exc:
                    if self._logger:
                        self._logger.warning("Failed to acknowledge market messages", exc_info=True)
                    await self._update_health(
                        healthy=False,
                        last_error=str(exc),
                        extra_metrics={
                            "loop": "market",
                            "state": "ack_failed",
                            "messages": len(messages),
                        },
                    )
                    continue
            await self._update_health(
                healthy=True,
                extra_metrics={"loop": "market", "messages": len(messages), "state": "processed"},
            )

    async def _results_loop(self) -> None:
        assert self._redis is not None
        while not self._stop_event.is_set():
            try:
                messages = await read_stream(
                    stream_key=self.results_stream_key,
                    group=self.results_group,
                    consumer=f"{self._consumer_name}-results",
                    count=self.batch_size,
                    block=int(self.poll_interval * 1000),
                    redis=self._redis,
                )
            except Exception as exc:
                if self._logger:
                    self._logger.warning("Failed to read results stream", exc_info=True)
                await self._update_health(
                    healthy=False,
                    last_error=str(exc),
                    extra_metrics={"loop": "results", "state": "read_failed"},
                )
                await asyncio.sleep(self.reconnect_delay)
                continue
            if not messages:
                await self._update_health(
                    healthy=True,
                    extra_metrics={"loop": "results", "messages": 0, "state": "idle"},
                )
                continue
            ack_ids: list[str] = []
            self._results_last_processed = _utc_now()
            for message in messages:
                handled = await self._handle_result_message(message.data)
                if handled:
                    ack_ids.append(message.message_id)
            if ack_ids:
                try:
                    await ack_stream(ack_ids, stream_key=self.results_stream_key, group=self.results_group, redis=self._redis)
                except Exception as exc:
                    if self._logger:
                        self._logger.warning("Failed to acknowledge result messages", exc_info=True)
                    await self._update_health(
                        healthy=False,
                        last_error=str(exc),
                        extra_metrics={
                            "loop": "results",
                            "state": "ack_failed",
                            "messages": len(messages),
                        },
                    )
                    continue
            await self._update_health(
                healthy=True,
                extra_metrics={"loop": "results", "messages": len(messages), "state": "processed"},
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
        if self._logger:
            self._logger.info(message)
        await self._write_log_point(
            level="INFO",
            message=message,
            symbol=event.symbol,
            trigger=event.trigger,
            status=status or "unknown",
        )
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
        if self._logger:
            self._logger.warning(message)
        await self._write_log_point(
            level="WARN",
            message=message,
            symbol=event.symbol,
            trigger=event.trigger,
            price=float(event.price),
        )

    async def _write_log_point(self, **fields: Any) -> None:
        timestamp = _utc_now()
        level = str(fields.pop("level", "INFO"))
        message = str(fields.pop("message", ""))
        point = {
            "measurement": "logs",
            "tags": {
                "service": self.service_name,
                "level": level,
            },
            "fields": {
                "message": message,
                **{k: v for k, v in fields.items() if isinstance(v, (int, float, str))},
            },
            "time": timestamp,
        }
        try:
            await _maybe_await(self.write_fn([point]))
        except Exception:
            if self._logger:
                self._logger.debug("Failed to write log point to Influx", exc_info=True)

    async def report_health(
        self,
        *,
        healthy: bool,
        last_error: str | None = None,
        extra_metrics: Mapping[str, Any] | None = None,
    ) -> None:
        await self._update_health(
            healthy=healthy,
            last_error=last_error,
            extra_metrics=extra_metrics,
        )

    async def fetch_rules(self) -> Mapping[str, Any]:
        if not self._configured:
            return {
                "success": False,
                "rules": {},
                "detail": "risk runtime not configured",
            }
        payload = self._build_rules_response(self._rules_state)
        return {
            "success": True,
            "rules": payload,
            "count": len(payload),
            "updated_at": self._snapshot_timestamp or _utc_now().isoformat(),
            "source": self.cache_source,
        }

    async def _apply_rules_state(self, rules: Mapping[str, Mapping[str, Any]]) -> None:
        config_payload = dict(self._active_config)
        config_payload["rules"] = [dict(rule) for rule in rules.values()]
        await self.update_config(config_payload)

    def _prepare_rule_update(
        self,
        payload: Mapping[str, Any],
        *,
        existing: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if "symbol" in payload:
            symbol = str(payload["symbol"]).strip().upper()
        elif existing and "symbol" in existing:
            symbol = str(existing["symbol"]).strip().upper()
        else:
            raise ValueError("symbol is required")

        rule: dict[str, Any] = {"symbol": symbol}
        metadata: dict[str, Any] = {}

        if existing:
            for key, value in existing.items():
                if key == "metadata" and isinstance(value, Mapping):
                    metadata.update(value)
                elif key != "symbol":
                    rule[key] = value

        for key, value in payload.items():
            if key == "symbol":
                continue
            if key in {"stop_loss", "stop_loss_price", "take_profit", "take_profit_price"}:
                if value is None or value == "":
                    if key.startswith("stop"):
                        rule.pop("stop_loss", None)
                        rule.pop("stop_loss_price", None)
                    else:
                        rule.pop("take_profit", None)
                        rule.pop("take_profit_price", None)
                    continue
                try:
                    price = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid price value") from exc
                if key.startswith("stop"):
                    rule["stop_loss"] = price
                    rule["stop_loss_price"] = price
                else:
                    rule["take_profit"] = price
                    rule["take_profit_price"] = price
            elif key == "enabled":
                rule["enabled"] = bool(value)
            elif key == "max_position":
                try:
                    rule["max_position"] = int(value) if value is not None else None
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid max_position") from exc
            elif key == "metadata" and isinstance(value, Mapping):
                metadata.update(value)
            else:
                metadata[key] = value

        if metadata:
            rule["metadata"] = metadata
        elif "metadata" in rule:
            rule.pop("metadata", None)
        return rule

    async def add_rule(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._configured:
            raise RuntimeError("RiskRuntime is not configured")
        rule = self._prepare_rule_update(payload)
        symbol = rule["symbol"]
        self._rules_state[symbol] = rule
        await self._apply_rules_state(self._rules_state)
        return await self.fetch_rules()

    async def update_rule(
        self,
        symbol: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not self._configured:
            raise RuntimeError("RiskRuntime is not configured")
        key = str(symbol).strip().upper()
        existing = self._rules_state.get(key)
        if existing is None:
            raise KeyError(symbol)
        rule = self._prepare_rule_update({"symbol": key, **payload}, existing=existing)
        self._rules_state[key] = rule
        await self._apply_rules_state(self._rules_state)
        return await self.fetch_rules()

    async def delete_rule(self, symbol: str) -> Mapping[str, Any]:
        if not self._configured:
            raise RuntimeError("RiskRuntime is not configured")
        key = str(symbol).strip().upper()
        if key not in self._rules_state:
            raise KeyError(symbol)
        self._rules_state.pop(key, None)
        await self._apply_rules_state(self._rules_state)
        return await self.fetch_rules()

    async def fetch_config(self) -> Mapping[str, Any]:
        if not self._configured:
            return {
                "success": False,
                "config": {},
                "detail": "risk runtime not configured",
            }
        payload = dict(self._config_snapshot)
        payload.setdefault("updated_at", self._snapshot_timestamp or _utc_now().isoformat())
        payload.setdefault("source", self.cache_source)
        return {"success": True, "config": payload}

    async def update_config_values(self, updates: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._configured:
            raise RuntimeError("RiskRuntime is not configured")
        base = dict(self._active_config)
        for key, value in updates.items():
            normalised_key = str(key)
            if value is None:
                base.pop(normalised_key, None)
            else:
                base[normalised_key] = value
        base["rules"] = [dict(rule) for rule in self._rules_state.values()]
        await self.update_config(base)
        return await self.fetch_config()

    async def fetch_order_queue_status(self) -> Mapping[str, Any]:
        return {
            "success": True,
            "pending_commands": len(self._pending_commands),
            "market_last_processed_at": (
                self._market_last_processed.isoformat() if self._market_last_processed else ""
            ),
            "results_last_processed_at": (
                self._results_last_processed.isoformat() if self._results_last_processed else ""
            ),
        }

    async def position_sizing(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._configured:
            raise RuntimeError("RiskRuntime is not configured")
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        try:
            stop_distance = float(payload.get("stop_distance", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("stop_distance must be numeric") from exc
        if stop_distance <= 0:
            raise ValueError("stop_distance must be greater than zero")
        try:
            rv_current = float(payload.get("rv_current", 0) or 0)
        except (TypeError, ValueError):
            rv_current = 0.0

        equity = float(self._active_config.get("equity", 100_000))
        risk_fraction = float(self._active_config.get("risk_per_trade", 0.01))
        point_value = float(self._active_config.get("point_value", 50))

        risk_amount = max(0.0, equity * risk_fraction)
        denominator = max(point_value * stop_distance, 1e-6)
        contracts_float = risk_amount / denominator
        contracts = max(0, int(round(contracts_float)))

        timestamp = _utc_now().isoformat()
        record = {
            "symbol": symbol,
            "contracts": contracts,
            "stop_distance": stop_distance,
            "rv_current": rv_current,
            "calculated_at": timestamp,
        }
        self._sizing_history.append(record)
        if len(self._sizing_history) > 500:
            self._sizing_history = self._sizing_history[-500:]
        return {"success": True, **record, "contracts_float": contracts_float}

    async def fetch_sizing_history(
        self,
        *,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> Mapping[str, Any]:
        entries = list(self._sizing_history)
        if symbol:
            key = str(symbol).strip().upper()
            entries = [entry for entry in entries if entry.get("symbol") == key]
        if since:
            try:
                threshold = datetime.fromisoformat(since)
            except ValueError:
                threshold = None
            if threshold is not None:
                entries = [
                    entry
                    for entry in entries
                    if _parse_datetime(entry.get("calculated_at")) >= threshold
                ]
        limited = entries[-max(1, min(limit, 500)) :]
        return {"success": True, "entries": limited}

    async def fetch_config_rules(self) -> Mapping[str, Any]:
        if not self._configured:
            return {
                "success": False,
                "g_rules": {},
                "s_rules": {},
                "detail": "risk runtime not configured",
            }
        g_rules = self._active_config.get("g_rules")
        s_rules = self._active_config.get("s_rules")
        return {
            "success": True,
            "g_rules": dict(g_rules) if isinstance(g_rules, Mapping) else {},
            "s_rules": dict(s_rules) if isinstance(s_rules, Mapping) else {},
        }

    async def update_config_rules(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        updates: dict[str, Any] = {}
        if "g_rules" in payload:
            updates["g_rules"] = payload.get("g_rules")
        if "s_rules" in payload:
            updates["s_rules"] = payload.get("s_rules")
        if not updates:
            return await self.fetch_config_rules()
        await self.update_config_values(updates)
        return await self.fetch_config_rules()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return _utc_now()
    return _utc_now()
