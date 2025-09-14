from __future__ import annotations

"""DepthSubscriber module for IBKR DOM data streaming."""

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Callable, Dict, List

from ib_insync import Stock
from loguru import logger

from .connection_manager import ConnectionManager

# Module-specific logger
_depth_logger = logger.bind(module="depth_subscriber", request_id="-")


@dataclass
class _DepthStream:
    ticker: any
    contract: any
    callbacks: List[Callable]


class DepthSubscriber:
    """Subscribe to IBKR market depth data and forward updates via callbacks."""

    def __init__(self, connection_manager: ConnectionManager):
        self.connection_manager = connection_manager
        self.ibkr_client = None  # set later to access contract helpers
        self._streams: Dict[str, _DepthStream] = {}
        self._lock = Lock()

    def set_ibkr_client(self, ibkr_client) -> None:
        """Set IBKR client for contract creation."""
        self.ibkr_client = ibkr_client

    # ------------------------------------------------------------------
    def start_depth_stream(
        self,
        symbol: str,
        callback: Callable[[Dict[str, any]], None],
        num_rows: int = 5,
    ) -> bool:
        """Start market depth streaming for a symbol.

        Args:
            symbol: Symbol to subscribe.
            callback: Function invoked with depth updates.
            num_rows: Number of depth levels to request.
        Returns:
            True if subscription succeeded.
        """
        if not self.connection_manager.is_connected():
            _depth_logger.error("IBKR connection not active for depth stream")
            return False

        with self._lock:
            if symbol in self._streams:
                self._streams[symbol].callbacks.append(callback)
                _depth_logger.debug(f"Added callback to existing stream for {symbol}")
                return True

        def _subscribe():
            ib = self.connection_manager.get_ib_instance()
            if not ib:
                return None
            # Choose contract helper from IBKR client when available
            contract = None
            if self.ibkr_client:
                try:
                    if symbol in ["MNQ", "NQ", "ES", "MES"]:
                        contract = self.ibkr_client.get_future_contract(symbol)
                    else:
                        contract = self.ibkr_client.get_stock_contract(symbol)
                except Exception as e:
                    _depth_logger.error(f"Contract build failed for {symbol}: {e}")
                    return None
            else:
                # fallback simple stock contract
                contract = Stock(symbol, "SMART", "USD")
            ticker = ib.reqMktDepth(contract, numRows=num_rows, isSmartDepth=False)
            return ticker, contract

        result = self.connection_manager.run_in_connection_thread(_subscribe)
        if not result:
            _depth_logger.error(f"Failed to subscribe market depth for {symbol}")
            return False
        ticker, contract = result

        def _on_update(*_):
            data = {
                "bids": [(d.price, d.size) for d in getattr(ticker, "domBids", [])],
                "asks": [(d.price, d.size) for d in getattr(ticker, "domAsks", [])],
                "timestamp": datetime.now().isoformat(),
            }
            streams = self._streams.get(symbol)
            if not streams:
                return
            for cb in list(streams.callbacks):
                try:
                    cb(data)
                except Exception as e:
                    _depth_logger.warning(f"Callback error for {symbol}: {e}")

        ticker.updateEvent += _on_update
        with self._lock:
            self._streams[symbol] = _DepthStream(ticker=ticker, contract=contract, callbacks=[callback])
        _depth_logger.info(f"Started depth stream for {symbol}")
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

        self.connection_manager.run_in_connection_thread(_cancel)
        try:
            stream.ticker.updateEvent.clear()
        except Exception:
            pass
        _depth_logger.info(f"Stopped depth stream for {symbol}")
        return True


__all__ = ["DepthSubscriber"]
