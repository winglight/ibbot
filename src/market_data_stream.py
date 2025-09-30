"""Streaming helpers for price/volume real-time bars.

This module provides a lightweight publish/subscribe facade that the
ATR trailing service can use to receive real-time bars without binding
directly to the ``ib_insync`` APIs during unit tests.  When connected to
IBKR the stream can be extended to request live data, but in tests we
simply inject bars via :meth:`PriceVolumeStream.publish_bar`.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional, Set


CallbackType = Callable[[str, "RealTimeBar"], Optional[Awaitable[None]]]


@dataclass(frozen=True)
class RealTimeBar:
    """Lightweight representation of IBKR ``RealTimeBar`` records."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class _Subscription:
    callbacks: Set[CallbackType]
    queue: "asyncio.Queue[RealTimeBar]"
    task: Optional[asyncio.Task] = None
    downsample: int = 1
    counter: int = 0


class PriceVolumeStream:
    """Manage real-time bar subscriptions and dispatch to listeners.

    The stream keeps an internal queue per symbol.  Tests can push bars
    to the queue via :meth:`publish_bar`, while production code can be
    extended to forward data from the ``ib_insync`` client.
    """

    def __init__(
        self,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        default_interval_seconds: int = 5,
    ) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = loop
        self._default_interval_seconds = max(1, default_interval_seconds)
        self._subscriptions: Dict[str, _Subscription] = {}

    def _resolve_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Ensure dispatch tasks run on an active event loop."""

        loop = self._loop
        if loop is not None:
            if loop.is_closed():
                loop = None
            elif loop.is_running():
                return loop

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return loop

        if loop is not None and not loop.is_running():
            for sub in self._subscriptions.values():
                if sub.task is not None and not sub.task.done():
                    sub.task.cancel()
                sub.task = None

        self._loop = running
        return running

    def _ensure_dispatch(self, symbol: str) -> None:
        loop = self._resolve_loop()
        if loop is None:
            return

        sub = self._subscriptions.get(symbol)
        if sub is None:
            return
        if sub.task is not None and not sub.task.done():
            return
        sub.task = loop.create_task(self._dispatch(symbol))

    def subscribe(
        self,
        symbol: str,
        callback: CallbackType,
        *,
        interval_seconds: Optional[int] = None,
    ) -> None:
        """Register *callback* to receive bars for *symbol*.

        ``interval_seconds`` controls down-sampling of the native
        5-second IBKR bars.  For example, requesting a 15 second interval
        will deliver every third bar.
        """

        if interval_seconds is None:
            interval_seconds = self._default_interval_seconds
        downsample = max(1, int(round(interval_seconds / 5)))

        sub = self._subscriptions.get(symbol)
        if sub is None:
            queue: "asyncio.Queue[RealTimeBar]" = asyncio.Queue()
            sub = _Subscription(callbacks=set(), queue=queue, downsample=downsample)
            self._subscriptions[symbol] = sub
        else:
            sub.downsample = downsample

        sub.callbacks.add(callback)
        self._ensure_dispatch(symbol)

    def unsubscribe(self, symbol: str, callback: CallbackType) -> None:
        """Remove *callback* from the subscription set for *symbol*."""

        sub = self._subscriptions.get(symbol)
        if not sub:
            return

        sub.callbacks.discard(callback)
        if not sub.callbacks:
            if sub.task is not None:
                sub.task.cancel()
            self._subscriptions.pop(symbol, None)

    async def publish_bar(self, symbol: str, bar: RealTimeBar) -> None:
        """Inject a bar into the stream for *symbol* (primarily for tests)."""

        sub = self._subscriptions.get(symbol)
        if not sub:
            return
        self._ensure_dispatch(symbol)
        await sub.queue.put(bar)

    async def _dispatch(self, symbol: str) -> None:
        sub = self._subscriptions[symbol]
        try:
            while True:
                bar = await sub.queue.get()
                if sub.downsample > 1:
                    sub.counter = (sub.counter + 1) % sub.downsample
                    if sub.counter != 0:
                        continue

                callbacks_snapshot = list(sub.callbacks)
                for callback in callbacks_snapshot:
                    try:
                        result = callback(symbol, bar)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        # Individual callback failures should not stop
                        # the stream; callers are expected to handle
                        # their own logging.
                        continue
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise


__all__ = ["PriceVolumeStream", "RealTimeBar"]

