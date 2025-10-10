from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

try:  # pragma: no cover - dependency may be missing in CI
    from ib_async import IB
except ImportError:  # pragma: no cover - fallback to lightweight stub used in tests
    from test.ib_insync_stub import IB  # type: ignore


connection_logger = logger.bind(module="connection", request_id="-")


@dataclass
class ConnectionConfig:
    """Simple configuration container for IBKR connectivity."""

    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    paper_trading: bool = True

    @classmethod
    def from_env(cls) -> "ConnectionConfig":
        """Build a configuration instance from environment variables."""

        def _parse_bool(value: str | None, default: bool) -> bool:
            if value is None:
                return default
            text = value.strip().lower()
            if not text:
                return default
            return text in {"1", "true", "yes", "on"}

        host = os.getenv("IBKR_HOST", cls.host)
        port = int(os.getenv("IBKR_PORT", cls.port))
        client_id = int(os.getenv("IBKR_CLIENT_ID", cls.client_id))
        paper = _parse_bool(os.getenv("IBKR_PAPER_TRADING"), cls.paper_trading)
        return cls(host=host, port=port, client_id=client_id, paper_trading=paper)

    def to_dict(self) -> dict[str, Any]:
        """Return the configuration as a plain dictionary."""

        return dict(asdict(self))


class ConnectionManager:
    """Light-weight wrapper around :class:`ib_async.IB`."""

    def __init__(self, config: ConnectionConfig | None = None) -> None:
        self.config = config or ConnectionConfig.from_env()
        self._lock = threading.Lock()
        self.ib: Optional[IB] = None
        self.connected: bool = False
        self._last_error: str | None = None
        self._adapter = None  # retained for backwards compatibility with callers
        self._project_root = Path(__file__).resolve().parents[1]
        self._client_id_min = 0
        self._client_id_max = max(self._client_id_min, self.config.client_id)
        self._client_id_cycle_size = max(1, (self._client_id_max - self._client_id_min) + 1)
        self._next_client_id_hint = self.config.client_id

    # ------------------------------------------------------------------
    # Connection lifecycle helpers

    def _reset_state(self) -> None:
        self.ib = None
        self.connected = False

    def _normalize_client_id(self, candidate: int) -> int:
        try:
            value = int(candidate)
        except Exception:
            value = self.config.client_id
        if value < self._client_id_min:
            value = self._client_id_min
        if value > self._client_id_max:
            span = max(1, self._client_id_cycle_size)
            value = self._client_id_min + ((value - self._client_id_min) % span)
        return value

    def connect(self) -> bool:
        """Establish a connection to IBKR using ``ib_async``."""

        with self._lock:
            if self.ib is not None and self.ib.isConnected():
                self.connected = True
                return True

            self.disconnect()
            try:
                ib = IB()
                client_id = self._normalize_client_id(self.config.client_id)
                connection_logger.info(
                    "Connecting to IBKR host=%s port=%s client_id=%s",  # type: ignore[str-bytes-safe]
                    self.config.host,
                    self.config.port,
                    client_id,
                )
                ib.connect(self.config.host, self.config.port, clientId=client_id)
            except Exception as exc:  # pragma: no cover - network/environment dependent
                self._last_error = str(exc)
                connection_logger.error("IBKR connection failed: %s", exc)
                self._reset_state()
                return False

            self.ib = ib
            self.connected = True
            self._last_error = None
            self._next_client_id_hint = self.config.client_id
            connection_logger.info("IBKR connection established")
            return True

    def disconnect(self) -> None:
        """Terminate the active IBKR session if present."""

        with self._lock:
            if self.ib is not None:
                try:
                    self.ib.disconnect()
                except Exception as exc:  # pragma: no cover - defensive
                    connection_logger.warning("Error while disconnecting IBKR: %s", exc)
                finally:
                    self._reset_state()
            else:
                self._reset_state()

    def is_connected(self) -> bool:
        with self._lock:
            if self.ib is None:
                self.connected = False
                return False
            connected = bool(self.ib.isConnected())
            self.connected = connected
            return connected

    # ------------------------------------------------------------------
    # Convenience helpers

    def ensure_connected(self) -> IB:
        """Ensure there is an active :class:`IB` session and return it."""

        if not self.connect():
            error = self._last_error or "failed to connect"
            raise RuntimeError(f"Unable to connect to IBKR: {error}")
        assert self.ib is not None
        return self.ib

    def get_ib_instance(self) -> Optional[IB]:
        """Return the current :class:`IB` instance if connected."""

        with self._lock:
            if self.ib is None or not self.ib.isConnected():
                return None
            return self.ib

    def get_last_connect_error(self) -> Optional[str]:
        return self._last_error

    # ------------------------------------------------------------------
    # Compatibility execution helpers

    def run_in_connection_thread(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Synchronously execute ``func`` after ensuring connectivity."""

        if not callable(func):
            raise TypeError("func must be callable")
        self.ensure_connected()
        return func(*args, **kwargs)

    def run_async_in_connection_thread(
        self,
        coro: Any,
        timeout: float | None = None,
    ) -> Any:
        """Schedule ``coro`` on the ``ib_async`` event loop if available."""

        ib = self.ensure_connected()
        loop = getattr(ib, "loop", None)
        if asyncio.iscoroutine(coro):
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout)
            return asyncio.run(coro)
        raise TypeError("run_async_in_connection_thread expects a coroutine")


connection_manager = ConnectionManager()
