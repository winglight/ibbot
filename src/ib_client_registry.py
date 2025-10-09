from __future__ import annotations

import asyncio
import inspect
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from src.connection_manager import ConnectionManager
from src.ibkr_client import IBKRClient


ClientFactory = Callable[..., IBKRClient]
ConnectionManagerFactory = Callable[[], ConnectionManager]


@dataclass
class _ClientSlot:
    """Container tracking a lazily instantiated IBKR client."""

    name: str
    client: Optional[IBKRClient] = None
    connection_manager: Optional[ConnectionManager] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    client_kwargs: Dict[str, Any] = field(default_factory=dict)
    client_factory: Optional[ClientFactory] = None
    connection_manager_factory: Optional[ConnectionManagerFactory] = None
    async_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sync_lock: threading.Lock = field(default_factory=threading.Lock)


class IBClientRegistry:
    """Central registry providing shared IBKR client instances."""

    def __init__(self) -> None:
        self._slots: Dict[str, _ClientSlot] = {}
        self._slots_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Slot management helpers

    def _get_slot(self, name: str) -> _ClientSlot:
        normalized = name or "primary"
        key = normalized.lower()
        with self._slots_lock:
            slot = self._slots.get(key)
            if slot is None:
                slot = _ClientSlot(name=normalized)
                self._slots[key] = slot
            return slot

    def configure_slot(
        self,
        name: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        client_kwargs: Optional[Dict[str, Any]] = None,
        client_factory: Optional[ClientFactory] = None,
        connection_manager_factory: Optional[ConnectionManagerFactory] = None,
    ) -> None:
        """Configure metadata and factories for the named slot."""

        slot = self._get_slot(name)
        if metadata:
            slot.metadata.update(metadata)
        if client_kwargs:
            slot.client_kwargs.update(client_kwargs)
        if client_factory is not None:
            slot.client_factory = client_factory
        if connection_manager_factory is not None:
            slot.connection_manager_factory = connection_manager_factory

    # ------------------------------------------------------------------
    # Client retrieval APIs

    async def get_client(
        self,
        name: str = "primary",
        *,
        client_factory: Optional[ClientFactory] = None,
        connection_manager: Optional[ConnectionManager] = None,
        metadata: Optional[Dict[str, Any]] = None,
        client_kwargs: Optional[Dict[str, Any]] = None,
    ) -> IBKRClient:
        """Return a lazily instantiated shared IBKR client."""

        slot = self._get_slot(name)

        async with slot.async_lock:
            if slot.client is None:
                factory = client_factory or slot.client_factory or IBKRClient

                manager = connection_manager
                if manager is None:
                    if slot.connection_manager is not None:
                        manager = slot.connection_manager
                    elif slot.connection_manager_factory is not None:
                        manager = slot.connection_manager_factory()
                    else:
                        manager = ConnectionManager()
                    slot.connection_manager = manager

                combined_metadata: Dict[str, Any] = {}
                combined_metadata.update(slot.metadata)
                if metadata:
                    combined_metadata.update(metadata)

                kwargs: Dict[str, Any] = {}
                kwargs.update(slot.client_kwargs)
                if client_kwargs:
                    kwargs.update(client_kwargs)

                kwargs.setdefault("connection_manager", manager)
                if combined_metadata:
                    kwargs.setdefault("metadata", combined_metadata)

                slot.client = factory(**kwargs)

            return slot.client

    def get_client_sync(
        self,
        name: str = "primary",
        **kwargs: Any,
    ) -> IBKRClient:
        """Blocking variant of :meth:`get_client` for synchronous callers."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.get_client(name, **kwargs))

        if loop.is_running():
            raise RuntimeError("get_client_sync cannot be called from a running event loop")

        return loop.run_until_complete(self.get_client(name, **kwargs))

    async def shutdown(self) -> None:
        """Disconnect and clear all managed clients."""

        slots = list(self._slots.values())
        for slot in slots:
            async with slot.async_lock:
                client = slot.client
                slot.client = None
                manager = slot.connection_manager
                slot.connection_manager = None

                if client is None:
                    continue

                disconnect = getattr(client, "disconnect", None)
                if callable(disconnect):
                    try:
                        if inspect.iscoroutinefunction(disconnect):
                            await disconnect()
                        else:
                            await asyncio.to_thread(disconnect)
                    except Exception:
                        pass

                if manager is not None:
                    try:
                        await asyncio.to_thread(manager.disconnect)
                    except Exception:
                        pass


_REGISTRY = IBClientRegistry()


def _build_connection_manager(client_id_env: str, default_client_id: int) -> ConnectionManager:
    client_id = default_client_id
    raw = os.getenv(client_id_env, "").strip()
    if raw:
        try:
            client_id = int(raw)
        except Exception:
            pass

    manager = ConnectionManager()
    try:
        normalized = manager._normalize_client_id(client_id)  # type: ignore[attr-defined]
    except Exception:
        normalized = client_id

    try:
        manager.config.client_id = normalized
    except Exception:
        pass

    try:
        setattr(manager, "_next_client_id_hint", normalized)
    except Exception:
        pass

    return manager


_REGISTRY.configure_slot(
    "primary",
    metadata={"name": "primary", "purpose": "command"},
    connection_manager_factory=lambda: _build_connection_manager("IBKR_PRIMARY_CLIENT_ID", 1),
)
_REGISTRY.configure_slot(
    "secondary",
    metadata={"name": "secondary", "purpose": "market-data"},
    connection_manager_factory=lambda: _build_connection_manager("IBKR_SECONDARY_CLIENT_ID", 2),
)
_REGISTRY.configure_slot(
    "gateway",
    metadata={"name": "gateway", "purpose": "unified"},
    connection_manager_factory=lambda: _build_connection_manager("IBKR_GATEWAY_CLIENT_ID", 1),
)


async def get_client(
    name: str = "primary",
    **kwargs: Any,
) -> IBKRClient:
    return await _REGISTRY.get_client(name, **kwargs)


def get_client_sync(name: str = "primary", **kwargs: Any) -> IBKRClient:
    return _REGISTRY.get_client_sync(name, **kwargs)


async def shutdown() -> None:
    await _REGISTRY.shutdown()


__all__ = [
    "get_client",
    "get_client_sync",
    "shutdown",
    "IBClientRegistry",
]

