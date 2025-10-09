from __future__ import annotations

"""DepthSubscriber module for IBKR DOM data streaming."""

import atexit
import os
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from loguru import logger

from .ibkr_client import INDEX_FUTURE_SYMBOLS
from .utils import now_local

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from .connection_manager import ConnectionManager

# Module-specific logger
_depth_logger = logger.bind(module="depth_subscriber", request_id="-")

_SUBSCRIBE_TIMEOUT_SEC = 15.0
_CANCEL_TIMEOUT_SEC = 5.0


@dataclass
class _DepthStream:
    """Container for active depth subscriptions."""

    ticker: Optional[Any]
    contract: Any
    callbacks: List[Callable]
    handler: Optional[Callable[..., None]] = None
    event: Optional[Any] = None
    use_adapter: bool = False
    adapter_stream: Optional[Any] = None


class DepthSubscriber:
    """Subscribe to IBKR market depth data and forward updates via callbacks."""

    def __init__(self, connection_manager: 'ConnectionManager'):
        self.connection_manager = connection_manager
        self._streams: Dict[str, _DepthStream] = {}
        self._lock = Lock()
        self._last_error: Optional[str] = None
        self._last_error_status: int = 200
        workers = max(2, min(4, (os.cpu_count() or 2))) if callable(getattr(os, "cpu_count", None)) else 4
        self._callback_executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="depth-subscriber-cb",
        )
        atexit.register(self._shutdown_executor)

    def _shutdown_executor(self) -> None:
        executor = getattr(self, "_callback_executor", None)
        if not executor:
            return
        try:
            executor.shutdown(wait=False)
        except Exception as exc:  # pragma: no cover - defensive guard
            try:
                _depth_logger.debug(f"Depth callback executor shutdown failed: {exc}")
            except Exception:
                pass
        finally:
            self._callback_executor = None

    def _invoke_callback(self, symbol: str, callback: Callable[[Dict[str, Any]], None], data: Dict[str, Any]) -> None:
        try:
            callback(data)
        except Exception as e:  # pragma: no cover - runtime safety
            _depth_logger.warning(f"Callback error for {symbol}: {e}")

    def _set_last_error(self, message: Optional[str], status: int = 200) -> None:
        self._last_error = message
        self._last_error_status = status

    @staticmethod
    def _resolve_depth_event(ticker: Optional[Any]) -> Optional[Any]:
        if not ticker:
            return None
        for attr in ("dom_update_event", "domUpdateEvent", "updateEvent", "update_event"):
            event = getattr(ticker, attr, None)
            if event is not None:
                return event
        return None

    def _detach_stream_handlers(self, stream: _DepthStream) -> None:
        handler = stream.handler
        event = stream.event
        if handler and event:
            try:
                event -= handler  # type: ignore[operator]
            except TypeError:
                removed = False
                if hasattr(event, "remove"):
                    with suppress(ValueError):
                        event.remove(handler)
                        removed = True
                if not removed and hasattr(event, "discard"):
                    try:
                        event.discard(handler)  # type: ignore[attr-defined]
                        removed = True
                    except Exception:
                        pass
                if not removed and hasattr(event, "clear"):
                    try:
                        event.clear()
                    except Exception:
                        pass

        if not stream.use_adapter and stream.ticker is not None:
            update_event = getattr(stream.ticker, "updateEvent", None)
            if update_event is not None and handler and update_event is not event:
                try:
                    update_event -= handler  # type: ignore[operator]
                except TypeError:
                    if hasattr(update_event, "remove"):
                        with suppress(ValueError):
                            update_event.remove(handler)
                    elif hasattr(update_event, "discard"):
                        try:
                            update_event.discard(handler)  # type: ignore[attr-defined]
                        except Exception:
                            pass
            try:
                update_event = getattr(stream.ticker, "updateEvent", None)
                if update_event is not None and hasattr(update_event, "clear"):
                    update_event.clear()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def start_depth_stream(
        self,
        symbol: str,
        callback: Callable[[Dict[str, Any]], None],
        num_rows: int = 5,
        *,
        force_restart: bool = False,
    ) -> bool:
        """Start market depth streaming for a symbol.

        Args:
            symbol: Symbol to subscribe.
            callback: Function invoked with depth updates.
            num_rows: Number of depth levels to request.
        Returns:
            True if subscription succeeded.
        """
        self._set_last_error(None, 200)

        raw_num_rows = num_rows
        try:
            requested_rows = int(raw_num_rows)
        except (TypeError, ValueError):
            requested_rows = 5
            _depth_logger.debug(
                f"Invalid num_rows {raw_num_rows!r} for {symbol}; defaulting to {requested_rows}"
            )
        sanitized_rows = max(1, min(requested_rows, 30))
        if sanitized_rows != requested_rows:
            _depth_logger.debug(
                f"Clamped num_rows from {requested_rows} to {sanitized_rows} for {symbol}"
            )
        num_rows = sanitized_rows

        if not self.connection_manager.is_connected():
            reason = "IBKR connection not active for depth stream"
            self._set_last_error(reason, 400)
            _depth_logger.error(reason)
            return False

        adapter = getattr(self.connection_manager, "_adapter", None)
        adapter_connected = False
        if adapter is not None:
            try:
                adapter_connected = bool(adapter.is_connected())
            except Exception as exc:  # pragma: no cover - diagnostic only
                _depth_logger.debug(
                    "Failed to query adapter connection state", error=str(exc)
                )
        use_adapter = adapter is not None and adapter_connected

        callbacks: List[Callable] = [callback]
        prior_stream: Optional[_DepthStream] = None

        with self._lock:
            existing = self._streams.get(symbol)
            if existing and not force_restart:
                if callback not in existing.callbacks:
                    existing.callbacks.append(callback)
                    _depth_logger.debug(
                        f"Added callback to existing stream for {symbol}"
                    )
                else:
                    _depth_logger.debug(
                        f"Callback already registered for {symbol}; skipping duplicate"
                    )
                self._set_last_error(None, 200)
                return True
            if existing and force_restart:
                callbacks = list(existing.callbacks)
                if callback not in callbacks:
                    callbacks.append(callback)
                prior_stream = existing
                self._streams.pop(symbol, None)
            elif existing:
                callbacks = list(existing.callbacks)
                if callback not in callbacks:
                    callbacks.append(callback)

        if prior_stream:
            self._detach_stream_handlers(prior_stream)

            adapter_for_cancel = getattr(self.connection_manager, "_adapter", None)
            if prior_stream.use_adapter and prior_stream.adapter_stream and adapter_for_cancel:
                try:
                    self.connection_manager.run_async_in_connection_thread(
                        adapter_for_cancel.cancel_market_depth(prior_stream.adapter_stream),
                        timeout=_CANCEL_TIMEOUT_SEC,
                    )
                except Exception as exc:
                    _depth_logger.warning(
                        f"Failed to cancel adapter depth stream for {symbol}: {exc}"
                    )
            else:

                def _cancel_previous():
                    ib = self.connection_manager.get_ib_instance()
                    if ib:
                        try:
                            ib.cancelMktDepth(prior_stream.contract)
                        except Exception as e:
                            _depth_logger.error(
                                f"cancelMktDepth error while restarting {symbol}: {e}"
                            )

                try:
                    self.connection_manager.run_in_connection_thread(
                        _cancel_previous,
                        timeout=_CANCEL_TIMEOUT_SEC,
                    )
                except Exception as exc:
                    _depth_logger.warning(
                        f"Failed to cancel previous depth stream for {symbol}: {exc}"
                    )

        ibkr_client = self._resolve_ibkr_client()
        if not ibkr_client:
            reason = f"IBKR client unavailable when building contract for {symbol}"
            self._set_last_error(reason, 500)
            _depth_logger.error(reason)
            return False

        try:
            if symbol.upper() in INDEX_FUTURE_SYMBOLS:
                contract = ibkr_client.get_future_contract(symbol, exchange="CME")
            else:
                contract = ibkr_client.get_stock_contract(symbol)
        except Exception as e:
            reason = f"Contract build failed for {symbol}: {e}"
            self._set_last_error(reason, 500)
            _depth_logger.error(reason)
            return False

        adapter_stream: Optional[Any] = None
        ticker: Optional[Any] = None
        event_ref: Optional[Any] = None

        if use_adapter:
            try:
                adapter_stream = self.connection_manager.run_async_in_connection_thread(
                    adapter.request_market_depth(
                        contract,
                        num_rows=num_rows,
                        is_smart_depth=False,
                    ),
                    timeout=_SUBSCRIBE_TIMEOUT_SEC,
                )
            except Exception as exc:
                _depth_logger.error(
                    f"Adapter depth subscription failed for {symbol}: {exc}"
                )
                adapter_stream = None

            if not adapter_stream:
                reason = f"Failed to subscribe market depth for {symbol}"
                self._set_last_error(reason, 500)
                _depth_logger.error(reason)
                return False

            ticker = getattr(adapter_stream, "ticker", None)
            event_ref = getattr(adapter_stream, "event", None)
            if event_ref is None and ticker is not None:
                event_ref = self._resolve_depth_event(ticker)
        else:

            def _subscribe():
                ib = self.connection_manager.get_ib_instance()
                if not ib:
                    return None
                ticker_local = ib.reqMktDepth(
                    contract, numRows=num_rows, isSmartDepth=False
                )
                return ticker_local, contract

            result = self.connection_manager.run_in_connection_thread(
                _subscribe,
                timeout=_SUBSCRIBE_TIMEOUT_SEC,
            )
            if not result:
                reason = f"Failed to subscribe market depth for {symbol}"
                self._set_last_error(reason, 500)
                _depth_logger.error(reason)
                return False
            ticker, _ = result
            event_ref = self._resolve_depth_event(ticker)

        ticker_ref = ticker

        def _on_update(*args):
            nonlocal ticker_ref
            ticker_obj = ticker_ref
            if args:
                candidate = args[0]
                if hasattr(candidate, "domBids") or hasattr(candidate, "domAsks"):
                    ticker_obj = candidate
            if ticker_obj is None and adapter_stream is not None:
                ticker_obj = getattr(adapter_stream, "ticker", None)
            if ticker_obj is None:
                return

            bids = [(d.price, d.size) for d in getattr(ticker_obj, "domBids", [])]
            asks = [(d.price, d.size) for d in getattr(ticker_obj, "domAsks", [])]
            timestamp = now_local()

            streams = self._streams.get(symbol)
            if not streams:
                return
            callbacks_local = list(streams.callbacks)
            executor = getattr(self, "_callback_executor", None)
            for cb in callbacks_local:
                payload = {
                    "bids": list(bids),
                    "asks": list(asks),
                    "timestamp": timestamp,
                }
                if executor:
                    try:
                        executor.submit(self._invoke_callback, symbol, cb, payload)
                        continue
                    except RuntimeError:
                        executor = None
                self._invoke_callback(symbol, cb, payload)

        handler_event = event_ref
        if handler_event is None and ticker_ref is not None:
            handler_event = getattr(ticker_ref, "updateEvent", None)

        if handler_event is not None:
            try:
                handler_event += _on_update  # type: ignore[operator]
            except TypeError:
                if hasattr(handler_event, "append"):
                    handler_event.append(_on_update)  # type: ignore[call-arg]
                else:
                    handler_event = None
        elif ticker_ref is not None:
            update_event = getattr(ticker_ref, "updateEvent", None)
            if update_event is not None:
                try:
                    update_event += _on_update  # type: ignore[operator]
                    handler_event = update_event
                except TypeError:
                    if hasattr(update_event, "append"):
                        update_event.append(_on_update)  # type: ignore[call-arg]
                        handler_event = update_event

        with self._lock:
            self._streams[symbol] = _DepthStream(
                ticker=ticker_ref,
                contract=contract,
                callbacks=list(callbacks),
                handler=_on_update,
                event=handler_event,
                use_adapter=use_adapter,
                adapter_stream=adapter_stream,
            )
        _depth_logger.info(
            f"Started depth stream for {symbol}" + (" (forced restart)" if prior_stream else "")
        )
        self._set_last_error(None, 200)
        return True

    # ------------------------------------------------------------------
    def stop_depth_stream(self, symbol: str) -> bool:
        """Stop market depth streaming for a symbol."""
        with self._lock:
            stream = self._streams.pop(symbol, None)
        if not stream:
            _depth_logger.warning(f"No active depth stream for {symbol}")
            return False

        self._detach_stream_handlers(stream)

        adapter = getattr(self.connection_manager, "_adapter", None)
        if stream.use_adapter and stream.adapter_stream and adapter is not None:
            try:
                self.connection_manager.run_async_in_connection_thread(
                    adapter.cancel_market_depth(stream.adapter_stream),
                    timeout=_CANCEL_TIMEOUT_SEC,
                )
            except Exception as exc:
                _depth_logger.error(
                    f"Adapter cancelMktDepth error for {symbol}: {exc}"
                )
        else:

            def _cancel():
                ib = self.connection_manager.get_ib_instance()
                if ib:
                    try:
                        ib.cancelMktDepth(stream.contract)
                    except Exception as e:
                        _depth_logger.error(f"cancelMktDepth error for {symbol}: {e}")

            self.connection_manager.run_in_connection_thread(
                _cancel,
                timeout=_CANCEL_TIMEOUT_SEC,
            )
        _depth_logger.info(f"Stopped depth stream for {symbol}")
        return True

    def get_last_error(self) -> tuple[Optional[str], int]:
        return self._last_error, self._last_error_status

    # ------------------------------------------------------------------
    def is_depth_stream_active(self, symbol: Optional[str] = None) -> bool:
        """Check whether a depth stream is currently active for ``symbol``."""

        with self._lock:
            if symbol is None:
                return bool(self._streams)

            symbol_key = symbol if isinstance(symbol, str) else str(symbol)
            if symbol_key in self._streams:
                return True

            alt_key = symbol_key.upper()
            if alt_key != symbol_key and alt_key in self._streams:
                return True

        return False

    # ------------------------------------------------------------------
    def _resolve_ibkr_client(self):
        """Return an IBKR client instance, creating it lazily when required."""

        try:
            from src.ib_client_registry import get_client_sync  # type: ignore
        except Exception:
            get_client_sync = None  # type: ignore[assignment]
        else:
            try:
                return get_client_sync("secondary")
            except Exception as exc:
                _depth_logger.error(f"Failed to resolve IBKR client from registry: {exc}")

        try:
            from importlib import import_module

            core = import_module("core")
        except Exception as exc:  # pragma: no cover - defensive guard
            _depth_logger.error(f"Unable to import core module for depth stream: {exc}")
            return None

        client = getattr(core, "ibkr_client", None)
        if client is not None:
            return client

        client_cls = getattr(core, "IBKRClient", None)
        if client_cls is None:  # pragma: no cover - compatibility fallback
            try:
                from src.ibkr_client import IBKRClient as client_cls
            except Exception as exc:
                _depth_logger.error(f"Unable to import IBKRClient class: {exc}")
                return None

        try:
            client = client_cls()
        except Exception as exc:
            _depth_logger.error(f"Failed to instantiate IBKR client: {exc}")
            return None

        setattr(core, "ibkr_client", client)

        return client


__all__ = ["DepthSubscriber"]
