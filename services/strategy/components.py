"""Core components for the strategy execution service."""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, MutableMapping, Sequence

from services.strategy.base import StrategyEventStream
from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.routes.service_clients import OrdersServiceClient, ServiceClientError


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
    required_channels: tuple[str, ...] = ()
    channel_map: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    latency_budget_ms: float | None = None
    use_delayed_data: bool = False


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

    def __init__(
        self,
        market_data: MarketDataFacade,
        *,
        task_id: str,
        event_stream: StrategyEventStream | None = None,
        channel_aliases: Mapping[str, Mapping[str, str]] | None = None,
        use_delayed_data: bool = False,
    ) -> None:
        self._market_data = market_data
        self._task_id = task_id
        self._signals: list[Mapping[str, Any]] = []
        self._event_stream = event_stream
        self._channel_aliases = {
            str(symbol): {str(kind): str(channel) for kind, channel in mapping.items()}
            for symbol, mapping in (channel_aliases or {}).items()
        }
        self._use_delayed_data = bool(use_delayed_data)

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

    def _require_event_stream(self) -> StrategyEventStream:
        if self._event_stream is None:
            raise StrategyExecutionError("strategy was not configured for streaming market data")
        return self._event_stream

    def _resolve_channel(self, symbol: str, kind: str) -> str:
        mapping = self._channel_aliases.get(symbol)
        if not mapping:
            raise StrategyExecutionError(
                f"no channel mapping configured for symbol '{symbol}'"
            )
        channel = mapping.get(kind)
        if not channel:
            raise StrategyExecutionError(
                f"channel type '{kind}' missing for symbol '{symbol}'"
            )
        return channel

    async def next_event(
        self, channel: str, *, timeout: float | None = None
    ) -> Mapping[str, Any]:
        """Return the next payload published on *channel*."""

        stream = self._require_event_stream()
        return await stream.next_event(channel, timeout=timeout)

    def stream_channel(self, channel: str) -> AsyncIterator[Mapping[str, Any]]:
        """Yield an iterator streaming payloads from *channel*."""

        stream = self._require_event_stream()
        return stream.iter_channel(channel)

    def stream_quotes(self, symbol: str) -> AsyncIterator[Mapping[str, Any]]:
        """Return an iterator yielding quote ticks for *symbol*."""

        channel = self._resolve_channel(symbol, "quotes")
        return self.stream_channel(channel)

    def stream_dom(self, symbol: str) -> AsyncIterator[Mapping[str, Any]]:
        """Return an iterator yielding DOM updates for *symbol*."""

        channel = self._resolve_channel(symbol, "dom")
        return self.stream_channel(channel)

    def latest_event(self, channel: str) -> Mapping[str, Any] | None:
        """Return the most recent cached payload for *channel*."""

        stream = self._require_event_stream()
        return stream.latest_event(channel)

    @property
    def use_delayed_data(self) -> bool:
        """Return whether delayed market data is allowed for the strategy."""

        return self._use_delayed_data


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


def _normalise_strategies(
    definitions: Mapping[str, Any],
    *,
    default_required_channels: Iterable[str] | None = None,
    default_channel_map: Mapping[str, Mapping[str, str]] | None = None,
    default_latency_budget_ms: float | None = None,
    default_use_delayed_data: bool | None = None,
) -> dict[str, StrategyDefinition]:
    def _normalise_channel_map(mapping: Mapping[str, Any]) -> dict[str, dict[str, str]]:
        normalised: dict[str, dict[str, str]] = {}
        for symbol, raw in mapping.items():
            if not isinstance(raw, Mapping):
                continue
            normalised[str(symbol)] = {
                str(kind): str(value)
                for kind, value in raw.items()
                if isinstance(value, (str, bytes))
            }
        return normalised

    default_channel_map_norm = _normalise_channel_map(default_channel_map or {})
    default_required = tuple(
        str(channel)
        for channel in default_required_channels
        if isinstance(channel, (str, bytes))
    ) if default_required_channels else ()
    resolved: dict[str, StrategyDefinition] = {}
    for name, spec in definitions.items():
        params: Mapping[str, Any] = {}
        factory_spec: Any = spec
        required_channels: tuple[str, ...] = ()
        channel_map: dict[str, dict[str, str]] = {}
        latency_budget_ms: float | None = None
        if isinstance(spec, Mapping):
            factory_spec = spec.get("strategy_type") or spec.get("callable")
            params = spec.get("params") or spec.get("config") or {}
            raw_channels = spec.get("required_channels")
            if isinstance(raw_channels, Iterable) and not isinstance(raw_channels, (str, bytes)):
                required_channels = tuple(str(channel) for channel in raw_channels)
            raw_channel_map = spec.get("symbol_to_channel_map") or {}
            if isinstance(raw_channel_map, Mapping):
                channel_map = _normalise_channel_map(raw_channel_map)
            raw_latency = spec.get("latency_budget_ms")
            if isinstance(raw_latency, (int, float)):
                latency_budget_ms = float(raw_latency)
        factory = _resolve_factory(factory_spec)
        runtime_config = dict(params)

        merged_channel_map: dict[str, dict[str, str]] = {
            symbol: dict(mapping)
            for symbol, mapping in default_channel_map_norm.items()
        }
        for symbol, mapping in channel_map.items():
            target = merged_channel_map.setdefault(symbol, {})
            target.update(mapping)

        merged_required = list(default_required)
        merged_required.extend(required_channels)
        for mapping in merged_channel_map.values():
            merged_required.extend(mapping.values())
        seen = {}
        deduped_required = tuple(seen.setdefault(channel, channel) for channel in merged_required if channel not in seen)

        effective_latency = latency_budget_ms
        if effective_latency is None and default_latency_budget_ms is not None:
            effective_latency = float(default_latency_budget_ms)

        if deduped_required and "required_channels" not in runtime_config:
            runtime_config["required_channels"] = list(deduped_required)
        if merged_channel_map and "symbol_to_channel_map" not in runtime_config:
            runtime_config["symbol_to_channel_map"] = merged_channel_map
        if effective_latency is not None and "latency_budget_ms" not in runtime_config:
            runtime_config["latency_budget_ms"] = effective_latency

        use_delayed_data = runtime_config.get("use_delayed_data")
        if not isinstance(use_delayed_data, bool):
            if default_use_delayed_data is not None:
                use_delayed_data = bool(default_use_delayed_data)
                runtime_config.setdefault("use_delayed_data", use_delayed_data)
            else:
                use_delayed_data = False

        resolved[name] = StrategyDefinition(
            name=name,
            factory=factory,
            config=runtime_config,
            required_channels=deduped_required,
            channel_map=merged_channel_map,
            latency_budget_ms=effective_latency,
            use_delayed_data=bool(use_delayed_data),
        )
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
        orders_client: OrdersServiceClient | None = None,
        default_required_channels: Iterable[str] | None = None,
        default_channel_map: Mapping[str, Mapping[str, str]] | None = None,
        default_latency_budget_ms: float | None = None,
        use_delayed_data: bool | None = None,
    ) -> None:
        if not strategies:
            raise StrategyExecutionError("no strategies configured")

        self._redis = redis_client
        self._definitions = _normalise_strategies(
            strategies,
            default_required_channels=default_required_channels,
            default_channel_map=default_channel_map,
            default_latency_budget_ms=default_latency_budget_ms,
            default_use_delayed_data=use_delayed_data,
        )
        self._command_queue = command_queue
        self._event_stream = event_stream
        self._market = MarketDataFacade(redis_client, snapshot_prefix=snapshot_prefix)
        self._logger = get_logger(logger_name)
        self._orders_client = orders_client or OrdersServiceClient()
        self._default_channel_map = {
            str(symbol): {str(kind): str(channel) for kind, channel in mapping.items()}
            for symbol, mapping in (default_channel_map or {}).items()
            if isinstance(mapping, Mapping)
        }
        self._default_required_channels = (
            tuple(
                str(channel)
                for channel in default_required_channels
                if isinstance(channel, (str, bytes))
            )
            if default_required_channels
            else ()
        )
        self._default_latency_budget_ms = (
            float(default_latency_budget_ms)
            if isinstance(default_latency_budget_ms, (int, float))
            else None
        )
        self._default_use_delayed_data = bool(use_delayed_data) if use_delayed_data is not None else False

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

        channel_aliases: dict[str, dict[str, str]] = {
            symbol: dict(mapping) for symbol, mapping in self._default_channel_map.items()
        }
        for symbol, mapping in definition.channel_map.items():
            target = channel_aliases.setdefault(symbol, {})
            target.update(mapping)
        runtime_channel_map = strategy_config.get("symbol_to_channel_map")
        if isinstance(runtime_channel_map, Mapping):
            for symbol, mapping in runtime_channel_map.items():
                if not isinstance(mapping, Mapping):
                    continue
                channel_aliases[str(symbol)] = {
                    str(kind): str(channel)
                    for kind, channel in mapping.items()
                    if isinstance(channel, (str, bytes))
                }

        required_channels = set(self._default_required_channels)
        required_channels.update(definition.required_channels)
        runtime_required = strategy_config.get("required_channels")
        if isinstance(runtime_required, Iterable) and not isinstance(runtime_required, (str, bytes)):
            required_channels.update(str(channel) for channel in runtime_required)
        for mapping in channel_aliases.values():
            required_channels.update(mapping.values())

        latency_budget_ms = definition.latency_budget_ms
        if latency_budget_ms is None and self._default_latency_budget_ms is not None:
            latency_budget_ms = self._default_latency_budget_ms
        runtime_latency = strategy_config.get("latency_budget_ms")
        if isinstance(runtime_latency, (int, float)):
            latency_budget_ms = float(runtime_latency)
        timeout_seconds = max(0.1, (latency_budget_ms or 1000.0) / 1000.0)

        use_delayed_data = definition.use_delayed_data or self._default_use_delayed_data
        runtime_delayed_flag = strategy_config.get("use_delayed_data")
        if isinstance(runtime_delayed_flag, bool):
            use_delayed_data = runtime_delayed_flag

        async with AsyncExitStack() as stack:
            event_stream: StrategyEventStream | None = None
            if required_channels:
                event_stream = StrategyEventStream(
                    self._redis,
                    channels=sorted(required_channels),
                )
                await stack.enter_async_context(event_stream)
                try:
                    await event_stream.wait_until_ready(timeout=timeout_seconds)
                except RuntimeError as exc:
                    await self._emit_event("failed", {**base_event, "error": str(exc)})
                    raise StrategyExecutionError(str(exc)) from exc

            context = StrategyContext(
                self._market,
                task_id=task_id,
                event_stream=event_stream,
                channel_aliases=channel_aliases,
                use_delayed_data=use_delayed_data,
            )

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
        order_payload: Iterable[Mapping[str, Any]] | None
        if isinstance(result, Mapping):
            raw_orders = result.get("orders")
            if raw_orders is None:
                return orders
            if isinstance(raw_orders, Mapping):
                order_payload = [raw_orders]
            elif isinstance(raw_orders, Iterable) and not isinstance(raw_orders, (str, bytes)):
                order_payload = [dict(entry) for entry in raw_orders if isinstance(entry, Mapping)]
            else:
                raise StrategyExecutionError("orders payload must be a mapping or iterable")
        else:
            return orders

        for entry in order_payload:
            contract = entry.get("contract")
            order_details = entry.get("order")
            if not isinstance(contract, Mapping) or not isinstance(order_details, Mapping):
                raise StrategyExecutionError("strategy order missing contract/order payload")
            submission = {
                "contract": dict(contract),
                "order": dict(order_details),
            }
            metadata = submission["order"].get("metadata")
            if isinstance(metadata, Mapping):
                metadata = dict(metadata)
            else:
                metadata = {}
            metadata.setdefault("task_id", task_id)
            metadata.setdefault("strategy", strategy_name)
            submission["order"]["metadata"] = metadata
            try:
                response = await self._orders_client.place_order(submission)
            except ServiceClientError as exc:
                raise StrategyExecutionError(f"failed to place order: {exc}") from exc
            orders.append({"request": submission, "response": response})
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
