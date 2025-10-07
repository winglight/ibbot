"""Lightweight IB client wrapper used when the shared registry is unavailable."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any

from src.connection_config import ConnectionConfig
from src.ibbot.logging import get_logger


def _load_ib_class() -> type:
    """Return the ``ib_insync.IB`` class or fall back to the local stub."""

    try:  # pragma: no cover - optional dependency for production deployments
        from ib_insync import IB  # type: ignore
    except Exception:  # pragma: no cover - test environments use the stub implementation
        from test.ib_insync_stub import IB  # type: ignore
    return IB


@dataclass(slots=True)
class _ConnectionManagerProxy:
    """Expose a subset of the historical connection manager API."""

    client: "LocalIBCommandClient"

    def is_connected(self) -> bool:
        return self.client.is_connected()

    def is_connecting(self) -> bool:
        return self.client.is_connecting

    def get_last_connect_error(self) -> str | None:
        return self.client.get_last_connect_error()

    def wait_for_connection(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self.is_connected():
                return True
            time.sleep(min(0.1, deadline - time.monotonic()))
        return self.is_connected()

    def get_ib_instance(self) -> Any:
        return self.client.ib


class LocalIBCommandClient:
    """Minimal IB client facade used when :mod:`src.ib_client_registry` is absent."""

    def __init__(self, config: ConnectionConfig | None = None) -> None:
        self._logger = get_logger("ibbot.services.ib_command.local_client")
        self._config = config or ConnectionConfig.from_env()
        self._ib: Any | None = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._connecting = False
        self._last_error: str | None = None
        self.connection_manager = _ConnectionManagerProxy(self)
        self.metadata = {"purpose": "command"}

    # ------------------------------------------------------------------
    # Compatibility helpers matching the legacy client API
    def is_connected(self) -> bool:
        return self._connected

    def isConnected(self) -> bool:  # pragma: no cover - legacy naming
        return self.is_connected()

    @property
    def connected(self) -> bool:  # pragma: no cover - attribute-style access
        return self.is_connected()

    @property
    def is_connecting(self) -> bool:
        return self._connecting

    def get_last_connect_error(self) -> str | None:
        return self._last_error

    @property
    def ib(self) -> Any | None:
        return self._ib

    def get_connection_manager(self) -> _ConnectionManagerProxy:
        """Return the lightweight connection manager facade."""

        return self.connection_manager

    # ------------------------------------------------------------------
    async def ensure_connection(self, *, reason: str | None = None) -> None:
        async with self._lock:
            if self._ib is None:
                self._ib = _load_ib_class()()
            if self._connected and self._ib is not None:
                checker = getattr(self._ib, "isConnected", None)
                if callable(checker):
                    try:
                        if checker():
                            return
                    except Exception:  # pragma: no cover - defensive guard
                        pass
                else:
                    return

            await self._connect_locked(reason=reason)

    async def connect(self) -> bool:
        await self.ensure_connection(reason="manual-connect")
        return self.is_connected()

    async def disconnect(self) -> None:
        async with self._lock:
            if self._ib is None:
                return
            disconnect = getattr(self._ib, "disconnect", None)
            if callable(disconnect):
                try:
                    result = disconnect()
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await result  # pragma: no cover - defensive guard
                except Exception:  # pragma: no cover - defensive guard
                    self._logger.debug("IB disconnect raised an exception", exc_info=True)
            self._connected = False

    # ------------------------------------------------------------------
    async def _connect_locked(self, *, reason: str | None = None) -> None:
        self._connecting = True
        try:
            ib = self._ib or _load_ib_class()()
            host = self._config.host
            port = int(self._config.port)
            client_id = int(self._config.client_id)

            connect_async = getattr(ib, "connectAsync", None)
            try:
                if callable(connect_async):
                    await connect_async(host, port, clientId=client_id)
                else:
                    connect_sync = getattr(ib, "connect", None)
                    if callable(connect_sync):
                        await asyncio.to_thread(connect_sync, host, port, client_id)
                    else:  # pragma: no cover - unexpected client interface
                        raise RuntimeError("IB client does not expose a connect method")
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
                raise

            is_connected = False
            checker = getattr(ib, "isConnected", None)
            if callable(checker):
                try:
                    is_connected = bool(checker())
                except Exception:  # pragma: no cover - defensive guard
                    is_connected = False

            self._connected = is_connected
            self._last_error = None if is_connected else self._last_error
            self._ib = ib

            if is_connected:
                market_data_type = 3 if self._config.paper_trading else 1
                req_type = getattr(ib, "reqMarketDataType", None)
                if callable(req_type):
                    try:
                        req_type(market_data_type)
                    except Exception:  # pragma: no cover - best effort only
                        self._logger.debug("reqMarketDataType call failed", exc_info=True)
        finally:
            self._connecting = False

    # ------------------------------------------------------------------
    # Attribute delegation
    def __getattr__(self, name: str) -> Any:
        """Dynamically delegate unknown attributes to the IB instance."""

        if name.startswith("_"):
            raise AttributeError(name)

        async def _delegate(*args: Any, **kwargs: Any) -> Any:
            await self.ensure_connection(reason=f"local-client:{name}")
            ib = self._ib
            if ib is None:
                raise RuntimeError("IB client unavailable after ensure_connection")
            try:
                target = getattr(ib, name)
            except AttributeError:
                raise

            if callable(target):
                result = target(*args, **kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result

            if args or kwargs:
                raise TypeError(f"Attribute '{name}' is not callable")
            return target

        _delegate.__name__ = name
        _delegate.__qualname__ = f"LocalIBCommandClient.{name}"
        return _delegate


__all__ = ["LocalIBCommandClient"]

