from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
from collections.abc import Iterable
from typing import Any

from ibapi.client import EClient
from ibapi.reader import EReader
from ibapi.wrapper import EWrapper
from loguru import logger


class BaseServiceIBClient(EWrapper, EClient):
    """Common connection management for service-local IB clients."""

    DEFAULT_TIMEOUT = 10.0

    def __init__(
        self,
        *,
        service_name: str,
        host_env: str,
        port_env: str,
        client_id_env: str,
    ) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self._service_name = service_name
        self._host_env = host_env
        self._port_env = port_env
        self._client_id_env = client_id_env
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connection_ready: asyncio.Event | None = None
        self._callback_queue: asyncio.Queue[tuple[str, Any]] | None = None
        self._async_request_lock: asyncio.Lock | None = None
        self._reader: EReader | None = None
        self._reader_thread: threading.Thread | None = None
        self._run_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._next_valid_id: int | None = None
        self._next_request_id: int = 0
        self._request_id_lock = threading.Lock()
        self._async_request_waiters: list[asyncio.Future[int]] = []
        self._id_guard = threading.Lock()
        self.msg_queue: queue.Queue[Any] = queue.Queue()

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def callback_queue(self) -> asyncio.Queue[tuple[str, Any]]:
        if self._callback_queue is None:
            raise RuntimeError("Client not connected; callback queue unavailable")
        return self._callback_queue

    async def connect(self, *, timeout: float | None = None) -> None:
        if self.isConnected():
            logger.debug("%s already connected", self._service_name)
            return

        loop = asyncio.get_running_loop()
        self._loop = loop
        ready_event = asyncio.Event()
        self._connection_ready = ready_event
        self._callback_queue = asyncio.Queue()
        self._async_request_lock = asyncio.Lock()
        self._stop_event.clear()
        self.msg_queue = queue.Queue()
        self._next_valid_id = None
        self._next_request_id = 0
        self._async_request_waiters.clear()

        host = os.getenv(self._host_env, "127.0.0.1")
        port = int(os.getenv(self._port_env, "7497"))
        client_id = int(os.getenv(self._client_id_env, "0"))

        logger.info(
            "%s connecting to %s:%s with client id %s",
            self._service_name,
            host,
            port,
            client_id,
        )

        try:
            EClient.connect(self, host, port, client_id)
        except Exception:
            logger.exception("%s failed to initiate connection", self._service_name)
            raise

        self._reader = EReader(self, self.msg_queue)
        self._reader_thread = threading.Thread(
            target=self._reader.run,
            name=f"{self._service_name}-ereader",
            daemon=True,
        )
        self._reader_thread.start()

        self._run_thread = threading.Thread(
            target=self._pump_messages,
            name=f"{self._service_name}-ib-pump",
            daemon=True,
        )
        self._run_thread.start()

        try:
            await self._wait_for_ready(ready_event, timeout or self.DEFAULT_TIMEOUT)
        except Exception:
            logger.exception("%s connection handshake timed out", self._service_name)
            await self.async_disconnect()
            raise

    async def async_disconnect(self) -> None:
        await asyncio.to_thread(self.disconnect)

    def disconnect(self) -> None:  # type: ignore[override]
        if self._connection_ready is None:
            return

        self._stop_event.set()
        self._cancel_waiters()

        if self.isConnected():
            logger.info("%s disconnecting from IB", self._service_name)
        reader = self._reader
        if reader is not None and hasattr(reader, "stop"):
            try:
                reader.stop()
            except Exception:  # pragma: no cover - defensive guard
                logger.exception("%s failed to stop EReader thread", self._service_name)
        try:
            EClient.disconnect(self)
        except Exception:
            logger.exception("%s error during disconnect", self._service_name)

        self._join_threads()

        self._connection_ready = None
        self._callback_queue = None
        self._async_request_lock = None
        self._loop = None
        self._reader = None
        self._reader_thread = None
        self._run_thread = None
        self.msg_queue = queue.Queue()
        with self._id_guard:
            self._next_valid_id = None
        self._next_request_id = 0

    def _join_threads(self) -> None:
        threads: Iterable[threading.Thread | None] = (
            self._reader_thread,
            self._run_thread,
        )
        for thread in threads:
            if thread and thread.is_alive():
                thread.join(timeout=2.0)

    async def _wait_for_ready(self, event: asyncio.Event, timeout: float) -> None:
        await asyncio.wait_for(event.wait(), timeout)

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB API signature
        super().nextValidId(orderId)
        logger.debug("%s received nextValidId=%s", self._service_name, orderId)
        waiters: list[asyncio.Future[int]]
        with self._id_guard:
            self._next_valid_id = orderId
            if orderId > self._next_request_id:
                self._next_request_id = orderId
            waiters = list(self._async_request_waiters)
            self._async_request_waiters.clear()
        loop = self._loop
        event = self._connection_ready
        if loop and event is not None:
            loop.call_soon_threadsafe(event.set)
            for future in waiters:
                loop.call_soon_threadsafe(self._resolve_future, future, orderId)

    def _resolve_future(self, future: asyncio.Future[int], value: int) -> None:
        if not future.done():
            future.set_result(value)

    async def wait_for_next_valid_id(self, timeout: float | None = None) -> int:
        loop = asyncio.get_running_loop()
        with self._id_guard:
            if self._next_valid_id is not None:
                return self._next_valid_id
            future = loop.create_future()
            self._async_request_waiters.append(future)
        return await asyncio.wait_for(
            future,
            timeout if timeout is not None else self.DEFAULT_TIMEOUT,
        )

    async def next_request_id(self) -> int:
        if self._async_request_lock is None:
            raise RuntimeError("Client not connected")
        async with self._async_request_lock:
            with self._request_id_lock:
                self._next_request_id += 1
                return self._next_request_id

    def _pump_messages(self) -> None:
        try:
            from ibapi.client import EClient as _EClient

            _EClient.run(self)
        except Exception:
            logger.exception("%s IB client pump failed", self._service_name)
        finally:
            self._stop_event.set()
            loop = self._loop
            if loop and self._connection_ready is not None:
                loop.call_soon_threadsafe(self._connection_ready.set)

    def _cancel_waiters(self) -> None:
        loop = self._loop
        with self._id_guard:
            waiters = list(self._async_request_waiters)
            self._async_request_waiters.clear()
        if not loop:
            return
        for future in waiters:
            loop.call_soon_threadsafe(self._cancel_future, future)

    def _cancel_future(self, future: asyncio.Future[int]) -> None:
        if not future.done():
            future.cancel()

    def _forward_event(self, event_type: str, payload: Any) -> None:
        queue_ = self._callback_queue
        loop = self._loop
        if not queue_ or not loop:
            logger.debug(
                "%s dropping event %s because callback queue unavailable",
                self._service_name,
                event_type,
            )
            return

        def _enqueue() -> None:
            queue_.put_nowait((event_type, payload))

        loop.call_soon_threadsafe(_enqueue)

    @staticmethod
    def _sleep(delay: float) -> None:
        time.sleep(delay)

    async def wait_until_ready(self, timeout: float | None = None) -> None:
        event = self._connection_ready
        if event is None:
            raise RuntimeError("Client not connected")
        await asyncio.wait_for(
            event.wait(),
            timeout if timeout is not None else self.DEFAULT_TIMEOUT,
        )

    @property
    def next_valid_id_value(self) -> int | None:
        with self._id_guard:
            return self._next_valid_id

    def publish_event(self, event_type: str, payload: Any) -> None:
        self._forward_event(event_type, payload)

