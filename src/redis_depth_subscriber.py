"""Redis-backed DOM depth subscriber.

Provides a lightweight replacement for the legacy :class:`DepthSubscriber`
that consumed data directly from an IBKR connection.  The new subscriber
listens to DOM snapshots published by the ``ib-feed`` service via Redis
Pub/Sub, allowing the main application to operate without a local IB
Gateway connection.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

try:  # redis is available in production but optional during some tests
    import redis
except Exception:  # pragma: no cover - fallback for missing dependency
    redis = None  # type: ignore

_DOM_CHANNEL_TEMPLATE = os.getenv("IB_FEED_DOM_CHANNEL", "pubsub:ib:dom:{symbol}")
_DEFAULT_REDIS_URL = (
    os.getenv("DATA_LAYER_REDIS_URL")
    or os.getenv("IB_FEED_REDIS_URL")
    or os.getenv("IBBOT_REDIS_URL")
    or "redis://localhost:6379/0"
)


@dataclass(slots=True)
class _StreamHandle:
    """Track the worker thread and callbacks for an active subscription."""

    callbacks: List[Callable[[Dict[str, Any]], None]]
    thread: threading.Thread
    stop_event: threading.Event
    client: Any
    pubsub: Any
    channel: str


class RedisDomSubscriber:
    """Subscribe to DOM depth snapshots published in Redis."""

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        client_factory: Callable[[str], "redis.Redis"] | None = None,
        channel_template: str | None = None,
    ) -> None:
        if redis is None and client_factory is None:  # pragma: no cover - redis missing
            raise RuntimeError("redis package is required for RedisDomSubscriber")
        self._redis_url = redis_url or _DEFAULT_REDIS_URL
        self._channel_template = channel_template or _DOM_CHANNEL_TEMPLATE
        if client_factory is not None:
            self._client_factory = client_factory
            if redis is None:
                logger.debug(
                    "RedisDomSubscriber using custom client factory without redis package",
                    redis_url=self._redis_url,
                    channel_template=self._channel_template,
                )
        else:
            assert redis is not None  # for type checkers
            self._client_factory = lambda url: redis.Redis.from_url(  # type: ignore[union-attr]
                url, decode_responses=True
            )
        self._streams: Dict[str, _StreamHandle] = {}
        self._lock = threading.Lock()
        self._last_error: Optional[str] = None
        self._last_error_status: int = 200
        logger.debug(
            "RedisDomSubscriber initialised",
            redis_url=self._redis_url,
            channel_template=self._channel_template,
        )

    # ------------------------------------------------------------------
    def start_depth_stream(
        self,
        symbol: str,
        callback: Callable[[Dict[str, Any]], None],
        num_rows: int = 5,
        *,
        force_restart: bool = False,
    ) -> bool:
        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            self._set_last_error("Symbol is required", status=400)
            return False

        logger.debug(
            "Starting Redis DOM stream", symbol=symbol_key, force_restart=force_restart, num_rows=num_rows
        )

        with self._lock:
            existing = self._streams.get(symbol_key)
            if existing and not force_restart:
                if callback not in existing.callbacks:
                    existing.callbacks.append(callback)
                    logger.debug("Added callback to existing Redis DOM stream", symbol=symbol_key)
                self._set_last_error(None, status=200)
                return True
            if existing:
                logger.debug("Force restarting Redis DOM stream", symbol=symbol_key)
                self._stop_stream_locked(symbol_key, existing)

            client = None
            pubsub = None
            channel = self._channel_template.format(symbol=symbol_key)
            try:
                client = self._client_factory(self._redis_url)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(channel)
            except Exception as exc:  # pragma: no cover - connection errors
                message = f"Failed to initialise Redis subscription: {exc}"
                self._set_last_error(message, status=503)
                logger.error(message, symbol=symbol_key)
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # pragma: no cover - best effort cleanup
                        pass
                return False

            stop_event = threading.Event()
            handle = _StreamHandle(
                callbacks=[callback],
                thread=threading.Thread(
                    target=self._listen_for_updates,
                    name=f"redis-dom-{symbol_key}",
                    args=(symbol_key, client, pubsub, stop_event),
                    daemon=True,
                ),
                stop_event=stop_event,
                client=client,
                pubsub=pubsub,
                channel=channel,
            )
            self._streams[symbol_key] = handle
            handle.thread.start()

        self._set_last_error(None, status=200)
        return True

    # ------------------------------------------------------------------
    def stop_depth_stream(self, symbol: str) -> bool:
        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            return False

        with self._lock:
            handle = self._streams.pop(symbol_key, None)
        if not handle:
            logger.debug("No Redis DOM stream to stop", symbol=symbol_key)
            return False

        logger.debug("Stopping Redis DOM stream", symbol=symbol_key)
        self._stop_stream_locked(symbol_key, handle)
        return True

    # ------------------------------------------------------------------
    def get_last_error(self) -> tuple[Optional[str], int]:
        return self._last_error, self._last_error_status

    # ------------------------------------------------------------------
    def _set_last_error(self, message: Optional[str], *, status: int = 200) -> None:
        self._last_error = message
        self._last_error_status = status

    # ------------------------------------------------------------------
    def _stop_stream_locked(self, symbol: str, handle: _StreamHandle) -> None:
        try:
            handle.stop_event.set()
            try:
                if handle.pubsub is not None and hasattr(handle.pubsub, "close"):
                    try:
                        handle.pubsub.close()
                    except Exception as exc:  # pragma: no cover - best effort cleanup
                        logger.debug(
                            "Error closing Redis pubsub", symbol=symbol, error=str(exc)
                        )
                if handle.client is not None and hasattr(handle.client, "close"):
                    try:
                        handle.client.close()
                    except Exception:
                        pass
            finally:
                if handle.thread.is_alive():
                    handle.thread.join(timeout=2.0)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.debug("Error stopping Redis DOM stream", symbol=symbol, error=str(exc))

    # ------------------------------------------------------------------
    def _listen_for_updates(
        self,
        symbol: str,
        client: "redis.Redis",
        pubsub: "redis.client.PubSub",
        stop_event: threading.Event,
    ) -> None:
        channel = self._channel_template.format(symbol=symbol)
        logger.debug("Redis DOM listener starting", symbol=symbol, channel=channel)

        try:
            while not stop_event.is_set():
                try:
                    message = pubsub.get_message(timeout=1.0)
                except Exception as exc:  # pragma: no cover - connection failure
                    if stop_event.is_set():
                        break
                    logger.error(
                        "Redis pubsub error", symbol=symbol, error=str(exc), channel=channel
                    )
                    self._set_last_error(f"Redis pubsub error: {exc}", status=503)
                    continue
                if message is None:
                    continue
                if message.get("type") != "message":
                    continue
                raw_payload = message.get("data")
                if raw_payload is None:
                    continue
                try:
                    payload = json.loads(raw_payload)
                except Exception as exc:  # pragma: no cover - malformed payload
                    logger.debug(
                        "Failed to decode Redis DOM payload", symbol=symbol, error=str(exc)
                    )
                    continue

                bids, asks, ts_ns, timestamp = self._coerce_snapshot(payload)
                data = {
                    "bids": bids,
                    "asks": asks,
                    "ts_ns": ts_ns,
                    "timestamp": timestamp,
                }
                logger.debug(
                    "Received Redis DOM update",
                    symbol=symbol,
                    bids=len(bids),
                    asks=len(asks),
                    ts_ns=ts_ns,
                )
                callbacks = self._snapshot_callbacks(symbol)
                for cb in callbacks:
                    try:
                        cb(dict(data))
                    except Exception as exc:  # pragma: no cover - callback safety
                        logger.warning("DOM callback raised", symbol=symbol, error=str(exc))
        finally:
            try:
                if hasattr(pubsub, "close"):
                    pubsub.close()
            except Exception:
                pass
            try:
                if hasattr(client, "close"):
                    client.close()
            except Exception:
                pass
            logger.debug("Redis DOM listener stopped", symbol=symbol)

    # ------------------------------------------------------------------
    def _snapshot_callbacks(self, symbol: str) -> List[Callable[[Dict[str, Any]], None]]:
        with self._lock:
            handle = self._streams.get(symbol)
            if not handle:
                return []
            return list(handle.callbacks)

    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_snapshot(payload: Dict[str, Any]) -> tuple[List[tuple[float, float]], List[tuple[float, float]], int, str]:
        bids_raw = payload.get("bids") or []
        asks_raw = payload.get("asks") or []
        bids = [
            (float(entry.get("price", 0.0) or 0.0), float(entry.get("size", 0.0) or 0.0))
            for entry in bids_raw
        ]
        asks = [
            (float(entry.get("price", 0.0) or 0.0), float(entry.get("size", 0.0) or 0.0))
            for entry in asks_raw
        ]

        ts_ns_value = payload.get("ts_ns")
        ts_ns: Optional[int] = None
        if isinstance(ts_ns_value, (int, float)):
            ts_ns = int(ts_ns_value)
        elif isinstance(ts_ns_value, str):
            try:
                ts_ns = int(float(ts_ns_value))
            except ValueError:
                ts_ns = None

        timestamp_value = payload.get("timestamp")
        if isinstance(timestamp_value, str):
            try:
                dt = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now(timezone.utc)
        elif isinstance(timestamp_value, datetime):
            dt = timestamp_value
        else:
            if ts_ns is not None and ts_ns > 0:
                dt = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
            else:
                dt = datetime.now(timezone.utc)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if ts_ns is None or ts_ns <= 0:
            ts_ns = int(dt.timestamp() * 1_000_000_000)
        timestamp_iso = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return bids, asks, ts_ns, timestamp_iso


__all__ = ["RedisDomSubscriber"]
