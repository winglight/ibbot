from __future__ import annotations

"""DepthSubscriber module for IBKR DOM data streaming."""

import atexit
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
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
    ticker: Any
    contract: Any
    callbacks: List[Callable]


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
                self.connection_manager.run_in_connection_thread_blocking(
                    _cancel_previous,
                    timeout=_CANCEL_TIMEOUT_SEC,
                )
            except Exception as exc:
                _depth_logger.warning(
                    f"Failed to cancel previous depth stream for {symbol}: {exc}"
                )
            try:
                prior_stream.ticker.updateEvent.clear()
            except Exception:
                pass

        def _subscribe():
            ib = self.connection_manager.get_ib_instance()
            if not ib:
                return None

            ibkr_client = self._resolve_ibkr_client()
            if not ibkr_client:
                reason = f"IBKR client unavailable when building contract for {symbol}"
                self._set_last_error(reason, 500)
                _depth_logger.error(reason)
                return None

            # Choose contract helper from IBKR client when available
            contract = None
            try:
                if symbol.upper() in INDEX_FUTURE_SYMBOLS:
                    contract = ibkr_client.get_future_contract(symbol, exchange="CME")
                else:
                    contract = ibkr_client.get_stock_contract(symbol)
            except Exception as e:
                reason = f"Contract build failed for {symbol}: {e}"
                self._set_last_error(reason, 500)
                _depth_logger.error(reason)
                return None
            ticker = ib.reqMktDepth(contract, numRows=num_rows, isSmartDepth=False)
            _depth_logger.warning(f"Created ticker for {symbol}: {ticker}, contract: {contract}")
            _depth_logger.warning(f"Ticker initial state - domBids: {getattr(ticker, 'domBids', 'None')}, domAsks: {getattr(ticker, 'domAsks', 'None')}")
            return ticker, contract

        result = self.connection_manager.run_in_connection_thread_blocking(
            _subscribe,
            timeout=_SUBSCRIBE_TIMEOUT_SEC,
        )
        if not result:
            reason = f"Failed to subscribe market depth for {symbol}"
            self._set_last_error(reason, 500)
            _depth_logger.error(reason)
            return False
        ticker, contract = result

        def _on_update(*_):
            # _depth_logger.warning(f"*** _on_update CALLED for {symbol} ***")
            # _depth_logger.warning(f"Ticker domBids: {getattr(ticker, 'domBids', 'None')}")
            # _depth_logger.warning(f"Ticker domAsks: {getattr(ticker, 'domAsks', 'None')}")
            bids = [(d.price, d.size) for d in getattr(ticker, "domBids", [])]
            asks = [(d.price, d.size) for d in getattr(ticker, "domAsks", [])]
            data = {
                "bids": bids,
                "asks": asks,
                "timestamp": now_local(),
            }
            streams = self._streams.get(symbol)
            if not streams:
                return
            callbacks = list(streams.callbacks)
            executor = getattr(self, "_callback_executor", None)
            for cb in callbacks:
                payload = {
                    "bids": list(bids),
                    "asks": list(asks),
                    "timestamp": data["timestamp"],
                }
                if executor:
                    try:
                        executor.submit(self._invoke_callback, symbol, cb, payload)
                        continue
                    except RuntimeError:
                        executor = None
                self._invoke_callback(symbol, cb, payload)

        # _depth_logger.warning(f"Before binding updateEvent for {symbol}, ticker: {ticker}")
        # _depth_logger.warning(f"Ticker updateEvent before binding: {ticker.updateEvent}")
        ticker.updateEvent += _on_update
        # _depth_logger.warning(f"After binding updateEvent for {symbol}, event handlers: {len(ticker.updateEvent)}")
        with self._lock:
            self._streams[symbol] = _DepthStream(
                ticker=ticker,
                contract=contract,
                callbacks=list(callbacks),
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

        def _cancel():
            ib = self.connection_manager.get_ib_instance()
            if ib:
                try:
                    ib.cancelMktDepth(stream.contract)
                except Exception as e:
                    _depth_logger.error(f"cancelMktDepth error for {symbol}: {e}")

        self.connection_manager.run_in_connection_thread_blocking(
            _cancel,
            timeout=_CANCEL_TIMEOUT_SEC,
        )
        try:
            stream.ticker.updateEvent.clear()
        except Exception:
            pass
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

        # Best effort: update risk manager reference when available
        try:
            risk_manager = getattr(core, "risk_manager", None)
            if risk_manager and hasattr(risk_manager, "ibkr_client"):
                risk_manager.ibkr_client = client
        except Exception:  # pragma: no cover - defensive
            pass

        return client


__all__ = ["DepthSubscriber"]
