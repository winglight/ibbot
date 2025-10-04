"""Core components for the strategy execution service."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.ibbot.redis import enqueue_ib_command


class StrategyError(Exception):
    """Base class for strategy related errors."""


class StrategyExecutionError(StrategyError):
    """Raised when a strategy fails during execution."""


class StrategyNotFoundError(StrategyError):
    """Raised when the requested strategy is not registered."""


@dataclass(slots=True)
class StrategyDefinition:
    """Configuration wrapper for a strategy factory."""

    name: str
    factory: Callable[[Mapping[str, Any]], Any]
    config: MutableMapping[str, Any] = field(default_factory=dict)


class MarketDataFacade:
    """Provide simplified access to market data snapshots stored in Redis."""

    def __init__(self, redis_client: Any, *, snapshot_prefix: str = "hash:market:snapshot") -> None:
        self._redis = redis_client
        self._snapshot_prefix = snapshot_prefix.rstrip(":")

    async def get_snapshot(self, symbol: str) -> Mapping[str, Any]:
        """Return the latest snapshot for *symbol* from Redis."""

        key = f"{self._snapshot_prefix}:{symbol}"
        try:
            snapshot = await self._redis.hgetall(key)
        except Exception as exc:  # pragma: no cover - network failure path
            raise StrategyExecutionError(f"failed to load market snapshot for {symbol}: {exc}") from exc
        if not snapshot:
            raise StrategyExecutionError(f"market snapshot missing for symbol: {symbol}")
        return snapshot


class StrategyContext:
    """Provide utilities exposed to strategy implementations."""

    def __init__(self, market_data: MarketDataFacade, *, task_id: str) -> None:
        self._market_data = market_data
        self._task_id = task_id
        self._signals: list[Mapping[str, Any]] = []

    async def get_snapshot(self, symbol: str) -> Mapping[str, Any]:
        """Return the market snapshot for *symbol*."""

        return await self._market_data.get_snapshot(symbol)

    async def get_price(self, symbol: str, field: str = "price") -> float:
        """Return a float price field from the snapshot."""

        snapshot = await self.get_snapshot(symbol)
        value = snapshot.get(field)
        if value is None:
            raise StrategyExecutionError(f"field '{field}' missing in snapshot for {symbol}")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:  # pragma: no cover - unlikely invalid data
            raise StrategyExecutionError(
                f"failed to convert snapshot field '{field}' to float for {symbol}: {value}"
            ) from exc

    def emit_signal(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Record a signal emitted by the strategy."""

        normalised = {**payload, "task_id": self._task_id}
        self._signals.append(normalised)
        return normalised

    @property
    def signals(self) -> Sequence[Mapping[str, Any]]:
        """Return the list of signals emitted so far."""

        return tuple(self._signals)


def _resolve_factory(spec: Any) -> Callable[[Mapping[str, Any]], Any]:
    if callable(spec):
        return spec
    if not isinstance(spec, str):
        raise StrategyExecutionError(f"invalid strategy factory specification: {spec!r}")

    module_path: str
    attribute: str
    if ":" in spec:
        module_path, attribute = spec.split(":", 1)
    else:
        module_path, attribute = spec.rsplit(".", 1)

    module = import_module(module_path)
    factory = getattr(module, attribute)
    if not callable(factory):
        raise StrategyExecutionError(f"strategy factory is not callable: {spec}")
    return factory


def _normalise_strategies(definitions: Mapping[str, Any]) -> dict[str, StrategyDefinition]:
    resolved: dict[str, StrategyDefinition] = {}
    for name, spec in definitions.items():
        params: Mapping[str, Any] = {}
        factory_spec: Any = spec
        if isinstance(spec, Mapping):
            factory_spec = spec.get("factory") or spec.get("callable")
            params = spec.get("params") or spec.get("config") or {}
        factory = _resolve_factory(factory_spec)
        resolved[name] = StrategyDefinition(name=name, factory=factory, config=dict(params))
    return resolved


class StrategyAdapter:
    """Execute strategy tasks and handle side-effects."""

    def __init__(
        self,
        redis_client: Any,
        *,
        strategies: Mapping[str, Any],
        command_queue: str,
        event_stream: str,
        snapshot_prefix: str = "hash:market:snapshot",
        logger_name: str = "ibbot.services.strategy.adapter",
    ) -> None:
        if not strategies:
            raise StrategyExecutionError("no strategies configured")

        self._redis = redis_client
        self._definitions = _normalise_strategies(strategies)
        self._command_queue = command_queue
        self._event_stream = event_stream
        self._market = MarketDataFacade(redis_client, snapshot_prefix=snapshot_prefix)
        self._logger = get_logger(logger_name)

    async def execute_task(self, task: Mapping[str, Any]) -> None:
        """Execute a single strategy task and emit lifecycle events."""

        task_id = str(task.get("task_id")) if task.get("task_id") is not None else ""
        strategy_name = task.get("strategy")
        if not task_id:
            raise StrategyExecutionError("task_id is required")
        if not strategy_name:
            raise StrategyExecutionError("strategy name is required")

        base_event = {"task_id": task_id, "strategy": str(strategy_name)}
        await self._emit_event("started", base_event)
        definition = self._definitions.get(str(strategy_name))
        if definition is None:
            message = f"unknown strategy: {strategy_name}"
            await self._emit_event("failed", {**base_event, "error": message})
            raise StrategyNotFoundError(message)

        strategy_config = dict(definition.config)
        task_params = task.get("params")
        if isinstance(task_params, Mapping):
            strategy_config.update(task_params)

        context = StrategyContext(self._market, task_id=task_id)
        try:
            handler = definition.factory(strategy_config)
            result = await self._invoke_handler(handler, context, task, strategy_config)
        except StrategyError as exc:
            await self._emit_event("failed", {**base_event, "error": str(exc)})
            raise
        except Exception as exc:
            self._logger.exception(
                "Strategy execution failed for task %s (%s)",
                task_id,
                strategy_name,
                extra={"task_id": task_id, "strategy": strategy_name},
            )
            await self._emit_event("failed", {**base_event, "error": str(exc)})
            raise StrategyExecutionError(str(exc)) from exc

        orders = await self._dispatch_orders(task_id, strategy_name, result)
        signals = await self._collect_signals(context, result)

        event_payload = {
            **base_event,
            "status": "completed",
            "orders": str(len(orders)),
            "signals": json.dumps(signals, ensure_ascii=False),
        }
        await self._emit_event("completed", event_payload)
        self._logger.info(
            "Strategy task completed",
            extra={"task_id": task_id, "strategy": strategy_name, "orders": len(orders)},
        )

    async def _invoke_handler(
        self,
        handler: Any,
        context: StrategyContext,
        task: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> Mapping[str, Any] | Sequence[Mapping[str, Any]] | None:
        """Invoke the strategy handler and normalise the result."""

        candidate: Any
        if hasattr(handler, "run") and callable(handler.run):
            candidate = handler.run(context=context, task=task, config=config)
        else:
            candidate = handler(context=context, task=task, config=config)

        if inspect.isawaitable(candidate):
            candidate = await candidate

        if candidate is None:
            return {}

        if isinstance(candidate, Mapping):
            return candidate

        if isinstance(candidate, Iterable):
            return {"orders": list(candidate)}

        raise StrategyExecutionError("strategy returned unsupported result type")

    async def _dispatch_orders(
        self,
        task_id: str,
        strategy_name: str,
        result: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    ) -> list[Mapping[str, Any]]:
        orders: list[Mapping[str, Any]] = []
        if isinstance(result, Mapping):
            order_payload = result.get("orders")
        else:
            order_payload = None
        if order_payload is None:
            return orders

        for order in order_payload:
            payload = dict(order)
            payload.setdefault("task_id", task_id)
            payload.setdefault("strategy", strategy_name)
            await enqueue_ib_command(payload, redis=self._redis, queue_key=self._command_queue)
            orders.append(payload)
        return orders

    async def _collect_signals(
        self,
        context: StrategyContext,
        result: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    ) -> list[Mapping[str, Any]]:
        signals = list(context.signals)
        if isinstance(result, Mapping):
            extra_signals = result.get("signals")
            if extra_signals:
                for signal in extra_signals:
                    context.emit_signal(signal)
                signals = list(context.signals)
        return signals

    async def _emit_event(self, status: str, payload: Mapping[str, Any]) -> None:
        body = {"status": status, **payload}
        fields = {key: json.dumps(value) if isinstance(value, (dict, list)) else str(value) for key, value in body.items()}
        await self._redis.xadd(self._event_stream, fields)


class StrategyTaskConsumer:
    """Background consumer that processes strategy tasks from a Redis list."""

    def __init__(
        self,
        redis_client: Any,
        adapter: StrategyAdapter,
        *,
        queue_key: str,
        poll_timeout: float = 1.0,
        logger_name: str = "ibbot.services.strategy.consumer",
    ) -> None:
        self._redis = redis_client
        self._adapter = adapter
        self._queue_key = queue_key
        self._poll_timeout = poll_timeout
        self._stopped = asyncio.Event()
        self._logger = get_logger(logger_name)
        self._health_last_error: str = ""
        self._service_name = "strategy"

    def stop(self) -> None:
        """Signal the consumer loop to stop."""

        self._stopped.set()

    async def _update_health(
        self,
        *,
        healthy: bool,
        state: str,
        last_error: str | None = None,
        task: Mapping[str, Any] | None = None,
    ) -> None:
        if healthy:
            self._health_last_error = ""
        elif last_error is not None:
            self._health_last_error = last_error
        metrics: dict[str, Any] = {"state": state, "queue": self._queue_key}
        if task is not None:
            task_id = task.get("task_id")
            strategy = task.get("strategy")
            if task_id is not None:
                metrics["task_id"] = str(task_id)
            if strategy is not None:
                metrics["strategy"] = str(strategy)
        await update_service_health(
            self._redis,
            self._service_name,
            healthy=healthy,
            last_error=self._health_last_error,
            metrics=metrics,
            logger=self._logger,
        )

    async def run_forever(self) -> None:
        """Continuously consume tasks until stopped."""

        while not self._stopped.is_set():
            try:
                item = await self._redis.blpop(self._queue_key, timeout=self._poll_timeout)
            except Exception as exc:  # pragma: no cover - network/redis failure
                self._logger.warning("Failed to fetch strategy task", exc_info=exc)
                await self._update_health(
                    healthy=False,
                    state="redis_error",
                    last_error=str(exc),
                )
                await asyncio.sleep(self._poll_timeout)
                continue

            if item is None:
                await self._update_health(healthy=True, state="idle")
                continue

            _, raw_payload = item
            try:
                task = json.loads(raw_payload)
            except json.JSONDecodeError as exc:
                self._logger.error("Failed to decode strategy task: %s", exc)
                await self._update_health(
                    healthy=False,
                    state="decode_error",
                    last_error=str(exc),
                )
                continue

            try:
                await self._adapter.execute_task(task)
            except StrategyNotFoundError as exc:
                self._logger.error("Strategy not found: %s", exc, extra={"task": task})
                await self._update_health(
                    healthy=False,
                    state="strategy_not_found",
                    last_error=str(exc),
                    task=task,
                )
            except StrategyExecutionError as exc:
                self._logger.error("Strategy execution error: %s", exc, extra={"task": task})
                await self._update_health(
                    healthy=False,
                    state="execution_error",
                    last_error=str(exc),
                    task=task,
                )
            else:
                await self._update_health(healthy=True, state="processed", task=task)

        await self._update_health(healthy=False, state="stopped")
        self._logger.info("Strategy consumer stopped")
