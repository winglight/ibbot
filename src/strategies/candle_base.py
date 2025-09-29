"""Utility base class for strategies that consume candle data via the unified data layer."""

from __future__ import annotations

import asyncio
from collections import deque
import threading
from threading import RLock
from typing import Deque, Dict, List, Optional

from loguru import logger

from src.data_layer import (
    BusSubscriptionToken,
    DataSourceError,
    DataSubscriptionRequest,
    DataSubscriptionToken,
    EventEnvelope,
    get_data_layer_mode,
    get_data_source_manager,
    is_unified_mode,
)
from src.connection_manager import connection_manager

from .base import BaseStrategy


class CandleSubscriptionStrategy(BaseStrategy):
    """Base class that manages unified candle subscriptions for strategies."""

    candle_interval: str = "1m"
    candle_history: int = 200

    def __init__(
        self,
        *,
        name: str,
        description: str = "",
        symbol: str,
        interval: Optional[str] = None,
        history_bars: Optional[int] = None,
    ) -> None:
        super().__init__(name=name, description=description)
        self.primary_symbol = symbol.upper()
        self.candle_interval = (interval or self.candle_interval).lower()
        self.candle_history = int(history_bars or self.candle_history)
        self._candles: Deque[Dict[str, float]] = deque(maxlen=self.candle_history)
        self._candles_lock = RLock()
        self._manager = None
        self._subscription_token: Optional[DataSubscriptionToken] = None
        self._event_token: Optional[BusSubscriptionToken] = None
        self._last_event_ts: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscription_retry_stop = threading.Event()
        self._subscription_retry_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if not is_unified_mode(get_data_layer_mode()):
            self.logger.warning(
                "Candle subscriptions require DATA_LAYER_MODE=unified; strategy disabled"
            )
            return False
        started = super().start()
        if not started:
            return False
        try:
            self._ensure_subscription()
            self._cancel_retry_thread()
        except DataSourceError as exc:
            if self._is_connection_unavailable_error(exc):
                self.logger.warning(
                    "Failed to initialise candle subscription because IBKR is not connected; will retry automatically",
                    symbol=self.primary_symbol,
                    interval=self.candle_interval,
                )
                self._start_retry_thread()
                return True
            self.logger.error("Failed to initialise candle subscription: {}", exc)
            super().stop()
            return False
        except Exception as exc:
            self.logger.error("Failed to initialise candle subscription: {}", exc)
            super().stop()
            return False
        return True

    # ------------------------------------------------------------------
    def stop(self) -> bool:
        stopped = super().stop()
        if stopped:
            self._cancel_retry_thread()
            self._teardown_subscription()
        return stopped

    # ------------------------------------------------------------------
    def get_candles(self) -> List[Dict[str, float]]:
        with self._candles_lock:
            return [dict(entry) for entry in self._candles]

    # ------------------------------------------------------------------
    def _ensure_subscription(self) -> None:
        if self._subscription_token is not None:
            return
        manager = self._manager or get_data_source_manager()
        self._manager = manager

        history = self._run_coro(
            manager.history(
                DataSubscriptionRequest(
                    channel="market.tick",
                    symbol=self.primary_symbol,
                    interval=self.candle_interval,
                )
            )
        )
        if history:
            with self._candles_lock:
                self._candles.clear()
                for candle in history:
                    if not candle:
                        continue
                    if candle.get("symbol") and candle["symbol"].upper() != self.primary_symbol:
                        continue
                    if candle.get("is_closed"):
                        self._candles.append(dict(candle))

        request = DataSubscriptionRequest(
            channel="market.tick",
            symbol=self.primary_symbol,
            options={"interval": self.candle_interval},
        )
        self._subscription_token = self._run_coro(manager.subscribe(request))

        pattern = f"market.candle.{self.candle_interval}"
        self._event_token = manager.event_bus.subscribe(pattern, self._handle_candle_event)
        self.logger.info(
            "Subscribed to candles", symbol=self.primary_symbol, interval=self.candle_interval
        )

    # ------------------------------------------------------------------
    def _teardown_subscription(self) -> None:
        manager = self._manager
        if manager is None:
            return
        if self._event_token is not None:
            try:
                manager.event_bus.unsubscribe(self._event_token)
            except Exception as exc:  # pragma: no cover - defensive guard
                self.logger.debug("Failed to unsubscribe event bus: {}", exc)
        if self._subscription_token is not None:
            try:
                self._run_coro(manager.unsubscribe(self._subscription_token))
            except Exception as exc:  # pragma: no cover - defensive guard
                self.logger.debug("Failed to cancel data subscription: {}", exc)
        self._subscription_token = None
        self._event_token = None

    # ------------------------------------------------------------------
    def _is_connection_unavailable_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return "ibkr connection is not active" in message or "ibkr connection not active" in message

    # ------------------------------------------------------------------
    def _start_retry_thread(self) -> None:
        if self._subscription_retry_thread and self._subscription_retry_thread.is_alive():
            return
        self._subscription_retry_stop.clear()
        thread = threading.Thread(
            target=self._retry_subscription_loop,
            name=f"{self.name}-candle-retry",
            daemon=True,
        )
        self._subscription_retry_thread = thread
        thread.start()

    # ------------------------------------------------------------------
    def _cancel_retry_thread(self) -> None:
        thread = self._subscription_retry_thread
        if not thread:
            self._subscription_retry_stop.clear()
            return
        self._subscription_retry_stop.set()
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass
        self._subscription_retry_thread = None
        self._subscription_retry_stop = threading.Event()

    # ------------------------------------------------------------------
    def _retry_subscription_loop(self) -> None:
        delay = 2.0
        try:
            self.logger.info(
                "Waiting for IBKR connection to restore candle subscription",
                symbol=self.primary_symbol,
                interval=self.candle_interval,
            )
        except Exception:
            pass

        while not self._subscription_retry_stop.is_set():
            if not self.active or not self.enabled:
                break

            if connection_manager.is_connected():
                try:
                    self._ensure_subscription()
                    try:
                        self.logger.info(
                            "Candle subscription restored after IBKR connection became available",
                            symbol=self.primary_symbol,
                            interval=self.candle_interval,
                        )
                    except Exception:
                        pass
                    return
                except DataSourceError as exc:
                    if not self._is_connection_unavailable_error(exc):
                        try:
                            self.logger.error(
                                "Failed to restore candle subscription after reconnect: {}",
                                exc,
                            )
                        except Exception:
                            pass
                        return
                except Exception as exc:
                    try:
                        self.logger.error(
                            "Unexpected error while restoring candle subscription: {}",
                            exc,
                        )
                    except Exception:
                        pass
                    return

            if self._subscription_retry_stop.wait(delay):
                break
            delay = min(delay * 1.5, 30.0)

        self._subscription_retry_thread = None

    # ------------------------------------------------------------------
    def _handle_candle_event(self, envelope: EventEnvelope) -> None:
        payload = envelope.payload or {}
        symbol = (payload.get("symbol") or envelope.symbol or "").upper()
        if symbol != self.primary_symbol:
            return
        if not payload.get("is_closed"):
            return
        candle = dict(payload)
        candle.setdefault("interval", self.candle_interval)
        candle.setdefault("topic", envelope.topic)
        with self._candles_lock:
            self._candles.append(candle)
        self._last_event_ts = envelope.ts_ns
        try:
            self.on_candle(candle)
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.exception("Candle handler failed: {}", exc)

    # ------------------------------------------------------------------
    def on_candle(self, candle: Dict[str, float]) -> None:  # pragma: no cover - hook
        """Hook invoked when a closed candle arrives."""

    # ------------------------------------------------------------------
    def _run_coro(self, coro):
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
        except RuntimeError:
            loop = self._loop

        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result()
        return asyncio.run(coro)

