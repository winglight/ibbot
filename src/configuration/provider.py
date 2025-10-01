"""Service configuration provider backed by Redis."""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field, fields
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from loguru import logger

from src.cache import ensure_redis

T = TypeVar("T")
ConfigCallback = Callable[[T], Union[Awaitable[None], None]]


@dataclass
class MarketDataServiceConfig:
    """Configuration for the market data service."""

    symbols: List[str] = field(
        default_factory=lambda: ["BTCUSDT"],
        metadata={"env": "MARKET_DATA_SYMBOLS"},
    )
    stream_length: int = field(
        default=1000,
        metadata={"env": "MARKET_DATA_STREAM_LENGTH"},
    )
    indicator_period: int = field(
        default=14,
        metadata={"env": "MARKET_DATA_INDICATOR_PERIOD"},
    )

    def __post_init__(self) -> None:
        if not isinstance(self.symbols, Iterable) or isinstance(self.symbols, (str, bytes)):
            raise ValueError("symbols must be a list of strings")
        validated_symbols: List[str] = []
        for symbol in self.symbols:
            if not isinstance(symbol, str):
                raise ValueError("symbols must contain string items")
            stripped = symbol.strip()
            if stripped:
                validated_symbols.append(stripped)
        if not validated_symbols:
            raise ValueError("at least one symbol must be provided")
        self.symbols = validated_symbols

        if not isinstance(self.stream_length, int) or self.stream_length <= 0:
            raise ValueError("stream_length must be a positive integer")
        if not isinstance(self.indicator_period, int) or self.indicator_period <= 0:
            raise ValueError("indicator_period must be a positive integer")


class ConfigProvider(Iterable[T]):
    """Load and cache service configuration from Redis with environment defaults."""

    def __init__(
        self,
        service: str,
        config_cls: Type[T],
        *,
        redis_factory: Callable[[], Awaitable[Optional[Any]]] = ensure_redis,
        channel: str = "pubsub:config:update",
    ) -> None:
        self._service = service
        self._config_cls = config_cls
        self._redis_factory = redis_factory
        self._channel = channel
        self._config: Optional[T] = None
        self._callbacks: List[ConfigCallback[T]] = []
        self._lock = asyncio.Lock()
        self._listener_task: Optional[asyncio.Task[None]] = None
        self._pubsub: Optional[Any] = None
        self._redis: Optional[Any] = None
        self._type_hints = get_type_hints(config_cls)

    def __iter__(self):  # pragma: no cover - legacy compatibility
        if self._config is None:
            raise RuntimeError("configuration has not been loaded yet")
        return iter([self._config])

    @property
    def redis_key(self) -> str:
        return f"config:{self._service}"

    def register_callback(self, callback: ConfigCallback[T]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def unregister_callback(self, callback: ConfigCallback[T]) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def get_config(self) -> T:
        if self._config is None:
            await self.refresh()
        assert self._config is not None  # for mypy
        return self._config

    async def refresh(self) -> T:
        async with self._lock:
            base_kwargs = self._build_base_kwargs()
            redis_data = await self._load_from_redis()
            base_kwargs.update(redis_data)
            try:
                config = self._config_cls(**base_kwargs)
            except Exception as exc:
                if self._config is None:
                    raise
                logger.warning("Failed to refresh configuration, keeping previous values", error=str(exc))
                return self._config
            self._config = config
        await self._notify_callbacks(config)
        return config

    def _build_base_kwargs(self) -> Dict[str, Any]:
        instance = self._config_cls()  # type: ignore[call-arg]
        base = {field_def.name: getattr(instance, field_def.name) for field_def in fields(instance)}
        for field_def in fields(instance):
            metadata = getattr(field_def, "metadata", None)
            env_name = metadata.get("env") if metadata else None
            if env_name:
                raw_value = os.getenv(env_name)
                if raw_value is not None:
                    try:
                        base[field_def.name] = self._coerce(field_def, raw_value)
                    except Exception as exc:
                        logger.warning(
                            "Failed to parse environment override", field=field_def.name, error=str(exc)
                        )
        return base

    async def _load_from_redis(self) -> Dict[str, Any]:
        if self._redis is None:
            self._redis = await self._redis_factory()
        if self._redis is None:
            return {}
        try:
            data = await self._redis.hgetall(self.redis_key)
        except Exception as exc:
            logger.warning("Failed to load configuration from Redis", error=str(exc))
            return {}
        result: Dict[str, Any] = {}
        field_map = {f.name: f for f in fields(self._config_cls)}
        for key, value in data.items():
            field_def = field_map.get(key)
            if field_def is None:
                continue
            try:
                result[key] = self._coerce(field_def, value)
            except Exception as exc:
                logger.warning(
                    "Failed to parse Redis configuration value", field=key, error=str(exc)
                )
        return result

    def _coerce(self, field_def, value: Any) -> Any:
        field_type = self._type_hints.get(field_def.name, field_def.type)
        origin = get_origin(field_type)
        if origin in (list, List):
            args = get_args(field_type)
            subtype = args[0] if args else str
            if isinstance(value, str):
                items = [item.strip() for item in value.split(",") if item.strip()]
            elif isinstance(value, Iterable):
                items = list(value)
            else:
                raise ValueError("list value must be iterable or comma separated string")
            return [self._convert_simple(subtype, item) for item in items]
        return self._convert_simple(field_type, value)

    def _convert_simple(self, expected_type: Any, value: Any) -> Any:
        origin = get_origin(expected_type)
        if origin is Union:
            for arg in get_args(expected_type):
                if arg is type(None):  # noqa: E721
                    continue
                try:
                    return self._convert_simple(arg, value)
                except Exception:
                    continue
            raise ValueError(f"value '{value}' does not match any allowed type")
        if expected_type in (int, float):
            return expected_type(value)
        if expected_type is bool:
            if isinstance(value, str):
                value_lower = value.lower()
                if value_lower in {"1", "true", "yes", "on"}:
                    return True
                if value_lower in {"0", "false", "no", "off"}:
                    return False
                raise ValueError(f"Cannot parse boolean from '{value}'")
            return bool(value)
        if expected_type is str:
            return str(value)
        return value

    async def _notify_callbacks(self, config: T) -> None:
        for callback in list(self._callbacks):
            try:
                result = callback(config)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning("Configuration callback raised an exception", error=str(exc))

    async def start(self) -> None:
        if self._listener_task is not None and not self._listener_task.done():
            return
        await self.refresh()
        if self._redis is None:
            logger.info("Config provider started without Redis client; updates disabled")
            return
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        await self._pubsub.subscribe(self._channel)
        self._listener_task = asyncio.create_task(self._listen_for_updates())

    async def stop(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:  # pragma: no cover - expected cancellation
                pass
        self._listener_task = None
        if self._pubsub is not None:
            try:
                await self._pubsub.close()
            except Exception:  # pragma: no cover - cleanup best effort
                pass
            self._pubsub = None

    async def _listen_for_updates(self) -> None:
        assert self._pubsub is not None
        try:
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                if data not in {self._service, "*", "all"}:
                    continue
                await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Error while listening for configuration updates", error=str(exc))
        finally:
            if self._pubsub is not None:
                try:
                    await self._pubsub.unsubscribe(self._channel)
                except Exception:  # pragma: no cover - cleanup best effort
                    pass


_provider: Optional[ConfigProvider[MarketDataServiceConfig]] = None
_provider_lock = asyncio.Lock()


async def get_provider() -> ConfigProvider[MarketDataServiceConfig]:
    global _provider
    if _provider is not None:
        return _provider
    async with _provider_lock:
        if _provider is None:
            _provider = ConfigProvider("market-data", MarketDataServiceConfig)
        return _provider


async def start() -> None:
    provider = await get_provider()
    await provider.start()


async def stop() -> None:
    provider = await get_provider()
    await provider.stop()


async def register_config_callback(callback: ConfigCallback[MarketDataServiceConfig]) -> None:
    provider = await get_provider()
    provider.register_callback(callback)
