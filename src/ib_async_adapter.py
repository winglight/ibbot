"""Compatibility adapter bridging :mod:`ib_async` with existing services.

The code base historically relied on the synchronous-friendly ``ib_insync``
package.  Task 1 replaces that dependency with ``ib_async`` and introduces a
thin façade that mirrors the legacy helpers closely enough for the remaining
modules to continue operating while the migration proceeds.  Later tasks will
expand the adapter, but the implementation here already covers the connection
lifecycle, market data subscriptions, historical data queries, and basic
account/order helpers so existing call sites have usable equivalents.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

try:  # pragma: no cover - optional dependency for local development
    from ib_async import IB as NativeIB
except ImportError:  # pragma: no cover - the real implementation lands in later tasks
    NativeIB = None  # type: ignore[assignment]

try:
    from .event_loop_manager import EventLoopManager, get_safe_event_loop
except ImportError:
    # Fallback if event loop manager is not available
    EventLoopManager = None
    get_safe_event_loop = None


class AdapterEvent:
    """Minimal event helper mimicking :mod:`ib_insync.eventkit.Event` semantics."""

    def __init__(self) -> None:
        self._handlers: list[Callable[..., None]] = []

    def __iadd__(self, handler: Callable[..., None]) -> AdapterEvent:
        if handler not in self._handlers:
            self._handlers.append(handler)
        return self

    def __isub__(self, handler: Callable[..., None]) -> AdapterEvent:
        with suppress(ValueError):
            self._handlers.remove(handler)
        return self

    def emit(self, *args: Any, **kwargs: Any) -> None:
        for handler in tuple(self._handlers):
            handler(*args, **kwargs)

    def clear(self) -> None:
        self._handlers.clear()

    def __len__(self) -> int:
        return len(self._handlers)


@dataclass(slots=True)
class MarketDataSubscription:
    """Container describing a market data subscription returned by the adapter."""

    contract: Any
    price_handler: Optional[Callable[..., None]] = None
    dom_handler: Optional[Callable[..., None]] = None
    price_ticker: Optional[Any] = None
    dom_ticker: Optional[Any] = None
    price_event: Optional[Any] = None
    dom_event: Optional[Any] = None


@dataclass(slots=True)
class DepthStream:
    """Placeholder describing a depth stream handle."""

    contract: Any
    handler: Optional[Callable[..., None]] = None
    ticker: Optional[Any] = None
    event: Optional[Any] = None


class AdapterWrapper:
    """Compat wrapper used by ``ConnectionManager`` and related services."""

    def __init__(self, client: Optional[Any] = None) -> None:
        self._client = client
        self._error_handlers: list[Callable[..., None]] = []
        self._depth_handlers: list[Callable[..., None]] = []
        self._decoder_error_handler_installed = False

    def bind_client(self, client: Any) -> None:
        """Attach the underlying native client so handlers can be wired."""

        self._client = client
        self._attach_error_handlers()
        self._attach_depth_handlers()
        self._install_decoder_error_handler()

    def register_error_handler(self, handler: Callable[..., None]) -> None:
        if handler not in self._error_handlers:
            self._error_handlers.append(handler)
            self._attach_error_handlers()

    def register_depth_handler(self, handler: Callable[..., None]) -> None:
        if handler not in self._depth_handlers:
            self._depth_handlers.append(handler)
            self._attach_depth_handlers()

    def _attach_error_handlers(self) -> None:
        client = self._client
        if not client:
            return
        for handler in self._error_handlers:
            for attr in ("error_event", "errorEvent"):
                event = getattr(client, attr, None)
                if event is not None:
                    event += handler  # type: ignore[operator]

    def _attach_depth_handlers(self) -> None:
        client = self._client
        if not client:
            return
        for handler in self._depth_handlers:
            for attr in ("depth_event", "updateMktDepthL2"):
                event = getattr(client, attr, None)
                if event is not None:
                    event += handler  # type: ignore[operator]

    def _install_decoder_error_handler(self) -> None:
        """Install a monkey patch to handle decoder KeyError issues."""
        if self._decoder_error_handler_installed or not self._client:
            return
            
        try:
            import ib_async.decoder as decoder
            import ib_async.wrapper as wrapper_module
            import logging
            
            # Store original interpret method
            if hasattr(decoder, 'Decoder') and hasattr(decoder.Decoder, 'interpret'):
                original_interpret = decoder.Decoder.interpret
                
                def patched_interpret(self, fields):
                    """Patched interpret method that handles KeyError for unknown message IDs."""
                    try:
                        return original_interpret(self, fields)
                    except KeyError as e:
                        # Handle unknown message ID (like 178)
                        msg_id = str(e).strip("'\"")
                        warning_msg = f"Unknown message ID {msg_id} - possible TWS/Gateway version mismatch with ib_async library"
                        
                        # 安全地记录警告信息
                        try:
                            # 优先使用 decoder 的 logger
                            if hasattr(self, 'logger') and self.logger:
                                self.logger.warning(warning_msg)
                            else:
                                # 回退到标准 logging
                                logging.warning(warning_msg)
                        except Exception:
                            # 如果日志记录失败，至少打印到控制台
                            print(f"WARNING: {warning_msg}")
                        
                        # Return None or empty result to continue processing
                        return None
                    except Exception as e:
                        # 记录其他异常但重新抛出
                        error_msg = f"Decoder interpret error: {e}, fields: {fields[:5] if len(fields) >= 5 else fields}"
                        try:
                            if hasattr(self, 'logger') and self.logger:
                                self.logger.error(error_msg)
                            else:
                                logging.error(error_msg)
                        except Exception:
                            print(f"ERROR: {error_msg}")
                        raise
                
                # Apply the patch
                decoder.Decoder.interpret = patched_interpret
                
            # Also patch wrapper's completedOrder method to handle missing completedOrders key
            if hasattr(wrapper_module, 'Wrapper') and hasattr(wrapper_module.Wrapper, 'completedOrder'):
                original_completed_order = wrapper_module.Wrapper.completedOrder
                
                def patched_completed_order(self, contract, order, orderState):
                    """Patched completedOrder method that ensures completedOrders key exists."""
                    try:
                        # Ensure completedOrders key exists in _results
                        if hasattr(self, '_results') and 'completedOrders' not in self._results:
                            self._results['completedOrders'] = []
                        
                        return original_completed_order(self, contract, order, orderState)
                    except KeyError as e:
                        if 'completedOrders' in str(e):
                            # Initialize the missing key and retry
                            if hasattr(self, '_results'):
                                self._results['completedOrders'] = []
                                return original_completed_order(self, contract, order, orderState)
                        raise
                
                # Apply the wrapper patch
                wrapper_module.Wrapper.completedOrder = patched_completed_order
                
            self._decoder_error_handler_installed = True
            logging.info("Decoder and wrapper error handlers installed successfully")
                
        except ImportError:
            logging.warning("Could not import ib_async.decoder or ib_async.wrapper for error handling")
        except Exception as e:
            logging.error(f"Failed to install decoder error handler: {e}")


class IBAsyncAdapter:
    """High-level façade bridging ``ib_async`` with legacy call sites."""

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        if self._client_factory is None and client is None and NativeIB is not None:
            self._client_factory = NativeIB
        self.wrapper = AdapterWrapper(client)
        self._connection_args: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def native_client(self) -> Optional[Any]:
        """Return the underlying ``ib_async`` client instance."""

        return self._client

    async def connect(self, host: str, port: int, client_id: int, **kwargs: Any) -> None:
        """Establish a connection to the IBKR gateway using ``ib_async``."""

        async with self._lock:
            if self._client is None:
                if self._client_factory is None:
                    raise RuntimeError("ib_async client is unavailable in this environment")
                client = self._client_factory()
                if client is None:
                    raise RuntimeError("ib_async client factory returned None")
                self._client = client
                self.wrapper.bind_client(client)

            self._connection_args = {
                "host": host,
                "port": port,
                "clientId": client_id,
                **kwargs,
            }
            
            try:
                await self._invoke(
                    ("connect", "connectAsync"),
                    host,
                    port,
                    clientId=client_id,
                    **kwargs,
                )
            except RuntimeError as e:
                error_msg = str(e).lower()
                if "event loop is closed" in error_msg:
                    # Re-raise with more specific error message for better handling upstream
                    raise RuntimeError(f"Event loop is closed during ib_async connection: {e}") from e
                raise

    async def disconnect(self) -> None:
        """Close the active ``ib_async`` session and release resources."""

        client = self._client
        if not client:
            return
        await self._invoke(("disconnect", "close"))

    def is_connected(self) -> bool:
        """Return ``True`` if the adapter currently has an active connection."""

        client = self._client
        if not client:
            return False
        for attr in ("is_connected", "isConnected"):
            value = getattr(client, attr, None)
            if callable(value):
                return bool(value())
            if value is not None:
                return bool(value)
        conn_state = getattr(getattr(client, "client", client), "connState", None)
        if conn_state is None:
            return False
        state_value = getattr(conn_state, "value", conn_state)
        if isinstance(state_value, int):
            return state_value == 2
        return str(state_value).upper() == "CONNECTED"

    async def qualify_contracts(self, *contracts: Any) -> Sequence[Any]:
        """Qualify the provided contracts through the IBKR backend."""

        result = await self._invoke(
            (
                "qualify_contracts",
                "qualifyContracts",
                "qualifyContractsAsync",
            ),
            *contracts,
        )
        return tuple(result) if result is not None else ()

    async def request_market_data(
        self,
        contract: Any,
        *,
        snapshot: bool = False,
        regulatory_snapshot: bool = False,
        **kwargs: Any,
    ) -> MarketDataSubscription:
        """Subscribe to streaming or snapshot market data."""

        price_handler = kwargs.pop("price_handler", None)
        dom_handler = kwargs.pop("dom_handler", None)
        ticker = await self._invoke(
            (
                "request_market_data",
                "reqMarketData",
                "reqMktData",
            ),
            contract,
            "",
            snapshot=snapshot,
            regulatorySnapshot=regulatory_snapshot,
            **kwargs,
        )
        price_event = self._extract_event(
            ticker,
            (
                "update_event",
                "updateEvent",
                "tickEvent",
            ),
        )
        dom_event = self._extract_event(
            ticker,
            (
                "dom_update_event",
                "domUpdateEvent",
                "updateMktDepthL2",
            ),
        )
        if price_event is not None and price_handler is not None:
            self._subscribe(price_event, price_handler)
        if dom_event is not None and dom_handler is not None:
            self._subscribe(dom_event, dom_handler)
        return MarketDataSubscription(
            contract=contract,
            price_handler=price_handler,
            dom_handler=dom_handler,
            price_ticker=ticker,
            dom_ticker=ticker,
            price_event=price_event,
            dom_event=dom_event,
        )

    async def cancel_market_data(self, subscription: MarketDataSubscription) -> None:
        """Cancel the supplied market data subscription."""

        if subscription.price_event and subscription.price_handler:
            self._unsubscribe(subscription.price_event, subscription.price_handler)
        if subscription.dom_event and subscription.dom_handler:
            self._unsubscribe(subscription.dom_event, subscription.dom_handler)
        if subscription.price_ticker is None:
            return
        await self._invoke(
            (
                "cancel_market_data",
                "cancelMktData",
            ),
            subscription.price_ticker,
        )

    async def request_market_depth(
        self,
        contract: Any,
        *,
        num_rows: int = 10,
        is_smart_depth: bool = False,
        **kwargs: Any,
    ) -> DepthStream:
        """Subscribe to DOM/depth of market updates for the given contract."""

        handler = kwargs.pop("handler", None)
        ticker = await self._invoke(
            (
                "request_market_depth",
                "reqMktDepth",
                "reqMktDepthEx",
            ),
            contract,
            num_rows,
            isSmartDepth=is_smart_depth,
            **kwargs,
        )
        event = self._extract_event(
            ticker,
            (
                "dom_update_event",
                "domUpdateEvent",
                "updateMktDepthL2",
            ),
        )
        if event is not None and handler is not None:
            self._subscribe(event, handler)
        return DepthStream(contract=contract, handler=handler, ticker=ticker, event=event)

    async def cancel_market_depth(self, stream: DepthStream) -> None:
        """Cancel an existing market depth stream."""

        if stream.event and stream.handler:
            self._unsubscribe(stream.event, stream.handler)
        if stream.ticker is None:
            return
        await self._invoke(
            (
                "cancel_market_depth",
                "cancelMktDepth",
            ),
            stream.ticker,
        )

    async def set_market_data_type(self, market_data_type: int) -> None:
        """Configure the default market data type for subsequent requests."""

        await self._invoke(
            (
                "set_market_data_type",
                "reqMarketDataType",
            ),
            market_data_type,
        )

    async def request_historical_data(
        self,
        contract: Any,
        end_datetime: Optional[str],
        duration: str,
        bar_size: str,
        what_to_show: str,
        *,
        use_rth: bool = True,
        format_date: bool = True,
        keep_up_to_date: bool = False,
        **kwargs: Any,
    ) -> Iterable[Any]:
        """Fetch historical market data bars for the specified contract."""

        result = await self._invoke(
            (
                "request_historical_data",
                "reqHistoricalData",
            ),
            contract,
            end_datetime,
            duration,
            bar_size,
            what_to_show,
            useRTH=use_rth,
            formatDate=int(bool(format_date)),
            keepUpToDate=keep_up_to_date,
            **kwargs,
        )
        return result if result is not None else []

    async def place_order(self, contract: Any, order: Any) -> Any:
        """Place an order via the underlying ``ib_async`` client."""

        return await self._invoke(("place_order", "placeOrder"), contract, order)

    async def cancel_order(self, order_id: int) -> None:
        """Cancel the order matching ``order_id``."""

        await self._invoke(("cancel_order", "cancelOrder"), order_id)

    async def list_open_orders(self) -> Sequence[Any]:
        """Return the collection of open orders from the IBKR backend."""

        result = await self._invoke(("list_open_orders", "reqOpenOrders", "openOrders"))
        return result if result is not None else ()

    async def request_account_updates(self, account: Optional[str] = None) -> Any:
        """Fetch the latest account summary for ``account``."""

        # For ib_async, we need to handle different method signatures
        # accountSummary() - no parameters needed
        # reqAccountSummary(reqId, groupName, tags) - requires specific parameters
        
        try:
            # First try the high-level accountSummary method (no parameters needed)
            method_names = ("account_summary", "accountSummary")
            if account is None:
                return await self._invoke(method_names)
            return await self._invoke(method_names, account)
        except Exception as e:
            # If high-level method fails, try the low-level reqAccountSummary
            # with proper parameters
            try:
                import ib_async
                if hasattr(self._client, 'client') and hasattr(self._client.client, 'reqAccountSummary'):
                    # Use low-level client method with required parameters
                    req_id = self._client.client.getReqId()
                    group_name = "All" if account is None else account
                    tags = "TotalCashValue,NetLiquidation,UnrealizedPnL,RealizedPnL,GrossPositionValue"
                    
                    return await self._invoke(
                        ("reqAccountSummary",), 
                        req_id, 
                        group_name, 
                        tags
                    )
                else:
                    # Fallback to original behavior
                    method_names = ("account_summary", "accountSummary", "reqAccountSummary")
                    if account is None:
                        return await self._invoke(method_names)
                    return await self._invoke(method_names, account)
            except Exception as inner_e:
                # Log both errors and re-raise the original
                import logging
                logging.error(f"Failed to request account updates: original error: {e}, fallback error: {inner_e}")
                raise e

    async def request_portfolio(self) -> Sequence[Any]:
        """Return the current portfolio positions."""

        result = await self._invoke(("portfolio", "reqPositions"))
        return result if result is not None else ()

    async def ensure_connected(self) -> None:
        """Idempotently guarantee that the adapter maintains a live connection."""

        if self.is_connected():
            return
        if not self._connection_args:
            raise RuntimeError("No connection parameters cached; call connect() first")
        args = dict(self._connection_args)
        host = args.pop("host")
        port = args.pop("port")
        client_id = args.pop("clientId")
        await self.connect(host, port, client_id, **args)

    def _extract_event(self, source: Any, candidate_names: Sequence[str]) -> Optional[Any]:
        for name in candidate_names:
            event = getattr(source, name, None)
            if event is not None:
                return event
        return None

    @staticmethod
    def _subscribe(event: Any, handler: Callable[..., None]) -> None:
        try:
            event += handler  # type: ignore[operator]
        except TypeError:
            if hasattr(event, "append"):
                event.append(handler)  # type: ignore[attr-defined]

    @staticmethod
    def _unsubscribe(event: Any, handler: Callable[..., None]) -> None:
        with suppress(Exception):
            event -= handler  # type: ignore[operator]

    async def _invoke(self, method_names: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        """Call the first matching method on the native client."""

        client = self._client
        if client is None:
            raise RuntimeError("ib_async client has not been initialised")

        targets = list(self._iter_targets(client, method_names))
        if not targets:
            raise RuntimeError(f"Native ib_async client does not expose {method_names!r}")

        for index, target in enumerate(targets):
            try:
                result = target(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if result is not None or index == len(targets) - 1:
                    return result
            except RuntimeError as e:
                error_msg = str(e).lower()
                if "event loop is closed" in error_msg:
                    # Log the recovery attempt but don't try to fix it here
                    # The connection manager should handle this at a higher level
                    print(f"尝试使用新的事件循环恢复: {method_names}")
                    
                    # Check if this is the last target to try
                    if index == len(targets) - 1:
                        # Re-raise the event loop error for upstream handling
                        raise RuntimeError(f"Event loop is closed in ib_async method {method_names}: {e}") from e
                    # Otherwise, try the next target
                    continue
                elif "this event loop is already running" in error_msg:
                    # Handle the "event loop already running" error
                    if index == len(targets) - 1:
                        raise RuntimeError(f"This event loop is already running: {e}") from e
                    continue
                # Re-raise other RuntimeErrors
                raise
            except Exception as e:
                # For non-RuntimeError exceptions, check if it's event loop related
                error_msg = str(e).lower()
                if "event loop is closed" in error_msg:
                    if index == len(targets) - 1:
                        raise RuntimeError(f"Event loop is closed in ib_async method {method_names}: {e}") from e
                    continue
                elif "this event loop is already running" in error_msg:
                    if index == len(targets) - 1:
                        raise RuntimeError(f"This event loop is already running: {e}") from e
                    continue
                # Re-raise other exceptions
                raise

        return None

    def _iter_targets(self, client: Any, method_names: Sequence[str]) -> Iterable[Callable[..., Any]]:
        for name in method_names:
            target = getattr(client, name, None)
            if callable(target):
                yield target
        nested = getattr(client, "client", None)
        if nested is not None and nested is not client:
            yield from self._iter_targets(nested, method_names)
