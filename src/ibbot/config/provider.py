"""Configuration provider backed by environment variables and MariaDB."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from copy import deepcopy
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional

from loguru import logger

try:  # redis is optional for some test environments
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis might be missing in certain CI setups
    Redis = None  # type: ignore

from src.config.env import PROJECT_ROOT, load_project_env
from src.db import ConfigRepository, ConfigRepositoryError, ModuleConfigSnapshot

Listener = Callable[[str, Mapping[str, str]], Optional[Awaitable[None]]]

DEFAULT_HASH_PREFIX = "hash:config"
DEFAULT_POLL_INTERVAL = 5.0
MIN_POLL_INTERVAL = 1.0


class ConfigProvider:
    """Load module configuration from environment variables and MariaDB."""

    update_channel = "pubsub:config:update"

    def __init__(
        self,
        *,
        redis_client: Redis | None = None,
        redis_url: str | None = None,
        hash_prefix: str = DEFAULT_HASH_PREFIX,
        extra_env_files: Iterable[str] | None = None,
        config_repository: ConfigRepository | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        load_project_env(extra_env_files)

        self._hash_prefix = hash_prefix.rstrip(":")
        self._redis: Redis | None = redis_client or self._create_client(redis_url)
        self._owns_client = redis_client is None and self._redis is not None
        self._repository: ConfigRepository | None = config_repository or ConfigRepository()
        self._poll_interval = max(float(poll_interval), MIN_POLL_INTERVAL)
        self._poll_task: asyncio.Task[None] | None = None
        self._cache: Dict[str, Dict[str, str]] = {}
        self._versions: dict[str, str | None] = {}
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._global_listeners: list[Listener] = []
        self._lock = asyncio.Lock()
        self._env_defaults: Dict[str, str] = self._load_env_defaults()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Begin polling for configuration change notifications."""

        if self._repository is None:
            return

        if self._poll_task is not None:
            return

        self._poll_task = asyncio.create_task(self._poll_worker())

    async def close(self) -> None:
        """Stop background workers and release resources."""

        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:  # pragma: no cover - cancellation path
                pass
            finally:
                self._poll_task = None

        if self._owns_client and self._redis is not None:
            try:
                await self._redis.close()
            except Exception:  # pragma: no cover - depends on redis client implementation
                pass
            finally:
                self._redis = None

    async def get_module(self, module: str, *, use_cache: bool = True) -> Mapping[str, str]:
        """Return the merged configuration for *module* as a mapping."""

        module = module.strip()
        if not module:
            raise ValueError("module name must be provided")

        if use_cache and module in self._cache:
            return deepcopy(self._cache[module])

        merged = await self._load_module(module)
        return deepcopy(merged)

    async def get(
        self,
        module: str,
        key: str,
        *,
        type_: Callable[[str], Any] | type = str,
        default: Any | None = None,
        use_cache: bool = True,
    ) -> Any:
        """Return a typed value from *module* configuration."""

        module_config = await self.get_module(module, use_cache=use_cache)
        value: str | None = None

        if key in module_config:
            value = module_config[key]
        else:
            env_value = os.getenv(key)
            value = env_value

        if value is None:
            return default

        try:
            return self._convert_value(value, type_)
        except Exception as exc:
            logger.warning(
                "Failed to convert config value", key=key, module=module, value=value, error=str(exc)
            )
            return default

    async def set_module(self, module: str, values: Mapping[str, Any]) -> Mapping[str, str]:
        """Persist *values* for *module* and refresh the local cache."""

        module = module.strip()
        if not module:
            raise ValueError("module name must be provided")

        serialised = self._stringify_mapping(values)

        async with self._lock:
            snapshot = await self._save_to_db(module, serialised)
            overrides = snapshot.values
            version = snapshot.version

            merged = {**self._env_defaults, **overrides}
            self._cache[module] = merged
            self._versions[module] = version

        if self._redis is not None:
            try:
                await self._redis.publish(self.update_channel, module)
            except Exception as exc:  # pragma: no cover - redis publish failure depends on environment
                logger.warning("Failed to publish config update", module=module, error=str(exc))

        await self._emit(module, self._cache[module])
        return deepcopy(self._cache[module])

    def subscribe(self, module: str | None, listener: Listener) -> None:
        """Register a listener that is invoked when *module* config changes."""

        if module is None or module == "*":
            self._global_listeners.append(listener)
        else:
            self._listeners[module].append(listener)

    async def refresh(self, module: str) -> Mapping[str, str]:
        """Force reload of module configuration and notify listeners."""

        merged = await self._load_module(module, bypass_cache=True)
        await self._emit(module, merged)
        return deepcopy(merged)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _create_client(self, redis_url: str | None) -> Redis | None:
        if Redis is None or redis_url is None:
            return None
        try:
            return Redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        except Exception as exc:  # pragma: no cover - network initialisation failure
            logger.warning(f"Failed to create Redis client: {exc}")
            return None

    async def _load_module(self, module: str, *, bypass_cache: bool = False) -> Dict[str, str]:
        async with self._lock:
            if not bypass_cache and module in self._cache:
                return self._cache[module]

            repository = self._get_repository()
            try:
                snapshot = await repository.load_module(module)
            except ConfigRepositoryError:
                raise
            except Exception as exc:  # pragma: no cover - defensive guard
                raise ConfigRepositoryError(str(exc)) from exc

            overrides = dict(snapshot.values)
            version = snapshot.version

            merged = {**self._env_defaults, **overrides}
            self._cache[module] = merged
            self._versions[module] = version
            return merged

    def _stringify_mapping(self, values: Mapping[str, Any]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key = str(raw_key).strip()
            if not key:
                continue
            if raw_value is None:
                continue
            if isinstance(raw_value, bool):
                result[key] = "true" if raw_value else "false"
            elif isinstance(raw_value, (int, float)):
                result[key] = str(raw_value)
            elif isinstance(raw_value, (dict, list, tuple)):
                try:
                    result[key] = json.dumps(raw_value, ensure_ascii=False)
                except (TypeError, ValueError):
                    result[key] = str(raw_value)
            else:
                result[key] = str(raw_value)
        return result

    async def _poll_worker(self) -> None:
        assert self._repository is not None
        try:
            while True:
                await asyncio.sleep(self._poll_interval)

                repository = self._get_repository()
                try:
                    versions = await repository.load_versions()
                except ConfigRepositoryError as exc:
                    logger.warning("Failed to load configuration versions: %s", str(exc))
                    continue
                except Exception as exc:  # pragma: no cover - defensive logging
                    logger.warning("Unexpected error while loading config versions: %s", str(exc))
                    continue

                tracked = set(self._cache.keys()) | set(self._versions.keys())
                for module in tracked:
                    previous = self._versions.get(module)
                    current = versions.get(module)
                    if previous == current:
                        continue
                    try:
                        await self.refresh(module)
                    except Exception as exc:  # pragma: no cover - defensive logging
                        logger.warning(
                            "Failed to refresh module after DB notification",
                            module=module,
                            error=str(exc),
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning(f"Config polling task terminated: {exc}")

    async def _emit(self, module: str, payload: Mapping[str, str]) -> None:
        listeners = list(self._listeners.get(module, ())) + list(self._global_listeners)
        if not listeners:
            return

        for listener in listeners:
            try:
                result = listener(module, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning(
                    "Config listener raised an exception", module=module, listener=repr(listener), error=str(exc)
                )

    def _load_env_defaults(self) -> Dict[str, str]:
        env_file = PROJECT_ROOT / ".env"
        defaults: Dict[str, str] = {}
        if env_file.exists():
            try:
                with env_file.open("r", encoding="utf-8") as file:
                    for line in file:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        defaults[key.strip()] = os.getenv(key.strip(), value.strip())
            except OSError as exc:  # pragma: no cover - file reading errors are unlikely
                logger.warning(f"Failed to read env defaults: {exc}")
        else:
            defaults.update({k: v for k, v in os.environ.items() if isinstance(v, str)})

        return {k: v for k, v in defaults.items() if v is not None}

    def _convert_value(self, value: str, type_: Callable[[str], Any] | type) -> Any:
        if type_ is str:
            return value
        if type_ is bool:
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if type_ is int:
            return int(value)
        if type_ is float:
            return float(value)
        return type_(value)

    async def _save_to_db(self, module: str, values: Mapping[str, str]) -> ModuleConfigSnapshot:
        repository = self._get_repository()
        try:
            return await repository.save_module(module, values)
        except ConfigRepositoryError:
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            raise ConfigRepositoryError(str(exc)) from exc

    def _get_repository(self) -> ConfigRepository:
        repository = self._repository
        if repository is None:
            raise ConfigRepositoryError("Configuration repository is not available")
        return repository
