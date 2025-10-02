"""WebSocket manager for market data streaming."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, MutableSet

from fastapi import WebSocket
from loguru import logger

from .runtime import MarketDataRuntime


@dataclass(slots=True)
class _Connection:
    """Represent a single websocket connection."""

    identifier: int
    websocket: WebSocket
    symbols: set[str] | None = None
    event_types: set[str] | None = None
    last_pong: float = field(default_factory=time.time)
    tasks: MutableSet[asyncio.Task[Any]] = field(default_factory=set)


class MarketDataWebSocketManager:
    """Manage websocket connections for market data updates."""

    def __init__(
        self,
        runtime: MarketDataRuntime,
        *,
        max_connections: int = 100,
        ping_interval: float = 30.0,
        ping_timeout: float = 10.0,
    ) -> None:
        self._runtime = runtime
        self._max_connections = max_connections
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._connections: dict[int, _Connection] = {}
        self._lock = asyncio.Lock()
        self._next_identifier = 0
        self._listener_registered = False
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._metrics = {
            "pings_sent": 0,
            "pongs_received": 0,
            "disconnects": 0,
        }

    @property
    def ping_interval(self) -> float:
        return self._ping_interval

    @ping_interval.setter
    def ping_interval(self, value: float) -> None:
        self._ping_interval = value

    @property
    def ping_timeout(self) -> float:
        return self._ping_timeout

    @ping_timeout.setter
    def ping_timeout(self, value: float) -> None:
        self._ping_timeout = value

    async def start(self) -> None:
        if self._listener_registered:
            return
        self._runtime.add_update_listener(self._handle_runtime_event)
        self._listener_registered = True

    async def stop(self) -> None:
        if self._listener_registered:
            self._runtime.remove_update_listener(self._handle_runtime_event)
            self._listener_registered = False

        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()

        for connection in connections:
            await self._disconnect(connection, reason="server_shutdown", code=1001)

        for task in list(self._pending_tasks):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self._pending_tasks.clear()

    async def handle_connection(
        self,
        websocket: WebSocket,
        *,
        symbols: set[str] | None = None,
        event_types: set[str] | None = None,
    ) -> None:
        await websocket.accept()

        connection = await self._register_connection(
            websocket, symbols=symbols, event_types=event_types
        )
        if connection is None:
            await websocket.close(code=1013, reason="too_many_connections")
            return

        try:
            await self._send_snapshot(connection)
            await self._run_connection(connection)
        finally:
            await self._disconnect(connection, reason="disconnect")

    async def _register_connection(
        self,
        websocket: WebSocket,
        *,
        symbols: set[str] | None,
        event_types: set[str] | None,
    ) -> _Connection | None:
        async with self._lock:
            if len(self._connections) >= self._max_connections:
                return None

            identifier = self._next_identifier
            self._next_identifier += 1
            connection = _Connection(
                identifier=identifier,
                websocket=websocket,
                symbols=set(symbols) if symbols else None,
                event_types=set(event_types) if event_types else None,
            )
            self._connections[identifier] = connection
            return connection

    async def _run_connection(self, connection: _Connection) -> None:
        receive_task = asyncio.create_task(self._receive_loop(connection))
        ping_task = asyncio.create_task(self._ping_loop(connection))

        connection.tasks.update({receive_task, ping_task})

        try:
            await receive_task
        finally:
            ping_task.cancel()
            with suppress(asyncio.CancelledError):
                await ping_task

    async def _send_snapshot(self, connection: _Connection) -> None:
        snapshots = self._runtime.get_snapshots()
        if connection.symbols is not None:
            snapshots = [
                snapshot
                for snapshot in snapshots
                if snapshot.get("symbol") in connection.symbols
            ]

        payload = {"type": "snapshot", "data": snapshots}
        try:
            await connection.websocket.send_json(payload)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to send snapshot", error=str(exc))

    async def _receive_loop(self, connection: _Connection) -> None:
        websocket = connection.websocket
        while True:
            try:
                message = await websocket.receive()
            except Exception:
                break

            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                break
            if message_type not in {"websocket.receive", "websocket.connect"}:
                continue

            data = message.get("text") or message.get("bytes")
            if data is None:
                continue
            if isinstance(data, bytes):
                try:
                    data = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue

            if not isinstance(payload, Mapping):
                continue

            msg_type = payload.get("type")
            if msg_type == "pong":
                connection.last_pong = time.time()
                self._metrics["pongs_received"] += 1
            elif msg_type == "subscribe":
                await self._update_subscription(connection, payload)

    async def _ping_loop(self, connection: _Connection) -> None:
        websocket = connection.websocket
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                now = time.time()
                if now - connection.last_pong > self._ping_timeout:
                    self._metrics["disconnects"] += 1
                    logger.info(
                        "Closing websocket due to missed pong",
                        connection=connection.identifier,
                    )
                    await websocket.close(code=1011, reason="ping_timeout")
                    break

                payload = {"type": "ping", "ts": now}
                try:
                    await websocket.send_json(payload)
                    self._metrics["pings_sent"] += 1
                except Exception:
                    break
        except asyncio.CancelledError:
            raise

    async def _update_subscription(
        self, connection: _Connection, payload: Mapping[str, Any]
    ) -> None:
        symbols = payload.get("symbols")
        event_types = payload.get("event_types") or payload.get("types")

        if symbols is None:
            pass
        else:
            connection.symbols = self._normalise_filter(symbols)

        if event_types is None:
            pass
        else:
            connection.event_types = self._normalise_filter(event_types)

    def _normalise_filter(self, value: Any) -> set[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
            return set(items) or None
        if isinstance(value, Iterable):
            items = {str(item).strip() for item in value if str(item).strip()}
            return items or None
        return None

    def _handle_runtime_event(self, event: Mapping[str, Any]) -> None:
        task = asyncio.create_task(self._broadcast(event))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _broadcast(self, event: Mapping[str, Any]) -> None:
        connections = await self._snapshot_connections()
        if not connections:
            return

        symbol = str(event.get("symbol") or "")
        event_type = str(event.get("type") or "")

        payload = {
            "symbol": symbol,
            "type": event_type,
            "data": event.get("data"),
        }

        stale_connections: list[_Connection] = []
        for connection in connections:
            if not self._match_subscription(connection, symbol, event_type):
                continue
            try:
                await connection.websocket.send_json(payload)
            except Exception:
                stale_connections.append(connection)

        for connection in stale_connections:
            await self._disconnect(connection, reason="send_error")

    async def _snapshot_connections(self) -> list[_Connection]:
        async with self._lock:
            return list(self._connections.values())

    def _match_subscription(
        self, connection: _Connection, symbol: str, event_type: str
    ) -> bool:
        if connection.symbols is not None and symbol not in connection.symbols:
            return False
        if connection.event_types is not None and event_type not in connection.event_types:
            return False
        return True

    async def _disconnect(
        self,
        connection: _Connection,
        *,
        reason: str,
        code: int = 1000,
    ) -> None:
        async with self._lock:
            existing = self._connections.pop(connection.identifier, None)

        if existing is None:
            return

        for task in list(connection.tasks):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        connection.tasks.clear()

        try:
            await connection.websocket.close(code=code, reason=reason)
        except Exception:
            pass


__all__ = ["MarketDataWebSocketManager"]
