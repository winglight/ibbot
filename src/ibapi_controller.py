"""Lightweight controller abstraction for the official IB API client.

This module introduces :class:`IBAPIController`, a small utility that wraps
``ibapi``'s :class:`~ibapi.wrapper.EWrapper`/:class:`~ibapi.client.EClient`
pair into an asyncio-friendly interface.  The controller is intentionally
minimal at this stage of the migration – it is focused on establishing and
tearing down connections while bridging callbacks into asyncio primitives that
the rest of the code base already understands.

The controller is designed to operate without a running TWS/Gateway during
tests.  When the real ``ibapi`` package is not importable we fall back to the
stub implementation in :mod:`test.ibapi_stub`, providing a deterministic
environment for unit tests.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Dict, List, Optional, Sequence, Set

try:  # pragma: no cover - exercised indirectly when ibapi is missing
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.common import BarData, HistoricalTick, HistoricalTickBidAsk, HistoricalTickLast, TickAttrib
    from ibapi.contract import Contract, ContractDetails
    from ibapi.order import Order
    from ibapi.order_state import OrderState
    from ibapi.execution import Execution
except ImportError:  # pragma: no cover - fallback used in CI/unit tests
    from test.ibapi_stub import (  # type: ignore
        BarData,
        Contract,
        ContractDetails,
        EClient,
        Execution,
        HistoricalTick,
        HistoricalTickBidAsk,
        HistoricalTickLast,
        EWrapper,
        Order,
        OrderState,
        TickAttrib,
    )


class IBAPIRequestError(RuntimeError):
    """Raised when the gateway rejects a specific request."""


@dataclass
class _ContractDetailsRequest:
    """Track outstanding ``reqContractDetails`` calls."""

    future: "asyncio.Future[List[ContractDetails]]"
    details: List[ContractDetails] = field(default_factory=list)


@dataclass
class _HistoricalDataRequest:
    """Track outstanding ``reqHistoricalData`` calls."""

    future: "asyncio.Future[List[BarData]]"
    bars: List[BarData] = field(default_factory=list)


@dataclass
class _HistoricalTicksRequest:
    """Track outstanding ``reqHistoricalTicks`` calls."""

    future: "asyncio.Future[List[object]]"
    ticks: List[object] = field(default_factory=list)


@dataclass
class _AccountSummaryRequest:
    """Track outstanding ``reqAccountSummary`` calls."""

    future: "asyncio.Future[List[dict[str, Any]]]"
    values: List[dict[str, Any]] = field(default_factory=list)


@dataclass
class _AccountUpdatesRequest:
    """Collect streaming updates from ``reqAccountUpdates``."""

    future: "asyncio.Future[dict[str, Any]]"
    account: str = ""
    account_values: List[dict[str, Any]] = field(default_factory=list)
    portfolio: List[dict[str, Any]] = field(default_factory=list)
    account_time: Optional[str] = None


@dataclass
class _PositionsRequest:
    """Track outstanding ``reqPositions`` calls."""

    future: "asyncio.Future[List[dict[str, Any]]]"
    positions: List[dict[str, Any]] = field(default_factory=list)


@dataclass
class OrderPlacement:
    """Details collected when the gateway acknowledges an order."""

    order_id: int
    contract: Contract
    order: Order
    state: Optional[OrderState] = None


@dataclass(frozen=True)
class ConnectionParams:
    """Connection details for an IB Gateway/TWS session."""

    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1


class IBAPIController(EWrapper):
    """Asyncio-friendly wrapper around ``ibapi``'s client/wrapper pair."""

    def __init__(
        self,
        params: Optional[ConnectionParams] = None,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__()
        self._loop = loop or asyncio.get_event_loop()
        self._params = params or ConnectionParams()
        self._client = EClient(self)

        self._run_thread: Optional[threading.Thread] = None
        self._connected_event = asyncio.Event()
        self._connection_lock = asyncio.Lock()
        self._next_valid_id_future: Optional[asyncio.Future[int]] = None
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_consumers: Set[asyncio.Queue[dict[str, Any]]] = {self._event_queue}
        self._next_request_id = 1
        self._contract_details_requests: Dict[int, _ContractDetailsRequest] = {}
        self._market_data_requests: Dict[int, Contract] = {}
        self._market_depth_requests: Dict[int, Contract] = {}
        self._historical_data_requests: Dict[int, _HistoricalDataRequest] = {}
        self._historical_ticks_requests: Dict[int, _HistoricalTicksRequest] = {}
        self._account_summary_requests: Dict[int, _AccountSummaryRequest] = {}
        self._account_updates_request: Optional[_AccountUpdatesRequest] = None
        self._positions_request: Optional[_PositionsRequest] = None
        self._order_id_lock = asyncio.Lock()
        self._next_order_id: Optional[int] = None
        self._order_id_waiters: List[asyncio.Future[int]] = []
        self._pending_order_submissions: Dict[int, asyncio.Future[OrderPlacement]] = {}
        self._pending_order_cancellations: Dict[int, asyncio.Future[str]] = {}
        self._event_consumers_lock = threading.Lock()
        self._managed_accounts: List[str] = []
        self._managed_accounts_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def client(self) -> EClient:
        """Expose the underlying ``EClient`` instance."""

        return self._client

    @property
    def event_queue(self) -> "asyncio.Queue[dict[str, Any]]":
        """Queue receiving structured events produced by callbacks."""

        return self._event_queue

    @property
    def managed_accounts(self) -> List[str]:
        """Return a snapshot of the managed accounts reported by TWS."""

        return list(self._managed_accounts)

    def register_event_consumer(self) -> "asyncio.Queue[dict[str, Any]]":
        """Create a new queue that will receive controller events."""

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._event_consumers_lock:
            self._event_consumers.add(queue)
        return queue

    def unregister_event_consumer(self, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        """Stop publishing events to ``queue``."""

        with self._event_consumers_lock:
            self._event_consumers.discard(queue)

    def _publish_event(self, event: Dict[str, Any]) -> None:
        """Broadcast ``event`` to every registered consumer."""

        with self._event_consumers_lock:
            consumers = list(self._event_consumers)

        if not consumers:
            return

        base_payload = dict(event)
        for index, queue in enumerate(consumers):
            payload = base_payload if index == 0 else dict(base_payload)
            try:
                queue.put_nowait(payload)
            except Exception:
                continue

    def is_connected(self) -> bool:
        """Return ``True`` if the underlying client reports a connection."""

        return bool(self._client.isConnected())

    async def connect(self, params: Optional[ConnectionParams] = None, *, timeout: float = 5.0) -> int:
        """Establish a connection to TWS/Gateway.

        Parameters
        ----------
        params:
            Optional override for the default connection parameters.
        timeout:
            Maximum time to wait for ``nextValidId`` before raising
            :class:`asyncio.TimeoutError`.
        """

        async with self._connection_lock:
            if self.is_connected():
                return self._client.clientId  # type: ignore[attr-defined]

            self._params = params or self._params
            self._connected_event.clear()
            self._next_valid_id_future = self._loop.create_future()
            self._managed_accounts.clear()
            self._managed_accounts_event.clear()

            # Set connection options like the working reference code
            self._client.setConnectOptions("+PACEAPI")
            
            await self._loop.run_in_executor(
                None,
                lambda: self._client.connect(
                    self._params.host,
                    self._params.port,
                    self._params.client_id,
                ),
            )

            self._start_network_loop()

            try:
                order_id = await asyncio.wait_for(self._next_valid_id_future, timeout)
            except Exception:
                await self._disconnect_locked()
                raise
            finally:
                self._next_valid_id_future = None

            return order_id

    async def disconnect(self) -> None:
        """Disconnect from TWS/Gateway and stop the network thread."""

        async with self._connection_lock:
            await self._disconnect_locked()

    async def reconnect(self, *, timeout: float = 5.0) -> int:
        """Convenience helper that performs a disconnect followed by connect."""

        await self.disconnect()
        return await self.connect(timeout=timeout)

    async def wait_until_connected(self, timeout: float = 5.0) -> None:
        """Wait until the controller observes a connection handshake."""

        await asyncio.wait_for(self._connected_event.wait(), timeout)

    async def request_contract_details(
        self,
        contract: Contract,
        *,
        timeout: float = 5.0,
    ) -> List[ContractDetails]:
        """Request contract details for ``contract`` and return the results."""

        if not self.is_connected():
            raise RuntimeError("request_contract_details requires an active connection")

        req_id = self._allocate_request_id()
        future: asyncio.Future[List[ContractDetails]] = self._loop.create_future()
        self._contract_details_requests[req_id] = _ContractDetailsRequest(future)

        await self._loop.run_in_executor(
            None,
            partial(self._client.reqContractDetails, req_id, contract),
        )

        try:
            return await asyncio.wait_for(future, timeout)
        except Exception as exc:
            await self._loop.run_in_executor(
                None,
                partial(self._client.cancelContractDetails, req_id),
            )
            raise exc
        finally:
            self._contract_details_requests.pop(req_id, None)

    async def request_market_data(
        self,
        contract: Contract,
        *,
        generic_tick_list: str = "",
        snapshot: bool = False,
        regulatory_snapshot: bool = False,
        options: Optional[Sequence[Any]] = None,
    ) -> int:
        """Submit a ``reqMktData`` request and return the request identifier."""

        if not self.is_connected():
            raise RuntimeError("request_market_data requires an active connection")

        req_id = self._allocate_request_id()
        self._market_data_requests[req_id] = contract

        await self._loop.run_in_executor(
            None,
            partial(
                self._client.reqMktData,
                req_id,
                contract,
                generic_tick_list,
                snapshot,
                regulatory_snapshot,
                list(options or []),
            ),
        )

        return req_id

    async def cancel_market_data(self, req_id: int) -> None:
        """Cancel an outstanding market data subscription."""

        if req_id in self._market_data_requests:
            await self._loop.run_in_executor(None, partial(self._client.cancelMktData, req_id))
            self._market_data_requests.pop(req_id, None)

    async def request_market_depth(
        self,
        contract: Contract,
        *,
        num_rows: int,
        is_smart_depth: bool = False,
        options: Optional[Sequence[Any]] = None,
    ) -> int:
        """Submit a ``reqMktDepth`` request and return the request identifier."""

        if not self.is_connected():
            raise RuntimeError("request_market_depth requires an active connection")

        req_id = self._allocate_request_id()
        self._market_depth_requests[req_id] = contract

        await self._loop.run_in_executor(
            None,
            partial(
                self._client.reqMktDepth,
                req_id,
                contract,
                int(num_rows),
                bool(is_smart_depth),
                list(options or []),
            ),
        )

        return req_id

    async def cancel_market_depth(self, req_id: int, *, is_smart_depth: bool = False) -> None:
        """Cancel an outstanding ``reqMktDepth`` subscription."""

        if req_id in self._market_depth_requests:
            await self._loop.run_in_executor(
                None,
                partial(self._client.cancelMktDepth, req_id, bool(is_smart_depth)),
            )
            self._market_depth_requests.pop(req_id, None)

    async def request_historical_data(
        self,
        contract: Contract,
        *,
        end_datetime: str,
        duration: str,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
        format_date: int = 1,
        keep_up_to_date: bool = False,
        chart_options: Optional[Sequence[Any]] = None,
        timeout: float = 5.0,
    ) -> List[BarData]:
        """Submit a ``reqHistoricalData`` request and await completion."""

        if not self.is_connected():
            raise RuntimeError("request_historical_data requires an active connection")

        req_id = self._allocate_request_id()
        future: asyncio.Future[List[BarData]] = self._loop.create_future()
        self._historical_data_requests[req_id] = _HistoricalDataRequest(future)

        await self._loop.run_in_executor(
            None,
            partial(
                self._client.reqHistoricalData,
                req_id,
                contract,
                end_datetime,
                duration,
                bar_size,
                what_to_show,
                use_rth,
                format_date,
                keep_up_to_date,
                list(chart_options or []),
            ),
        )

        try:
            return await asyncio.wait_for(future, timeout)
        except Exception as exc:
            await self._loop.run_in_executor(
                None,
                partial(self._client.cancelHistoricalData, req_id),
            )
            raise exc
        finally:
            self._historical_data_requests.pop(req_id, None)

    async def request_historical_ticks(
        self,
        contract: Contract,
        *,
        start_datetime: str,
        end_datetime: str,
        number_of_ticks: int,
        what_to_show: str,
        use_rth: bool,
        ignore_size: bool,
        misc_options: Optional[Sequence[Any]] = None,
        timeout: float = 5.0,
    ) -> List[object]:
        """Submit a ``reqHistoricalTicks`` request and await the snapshots."""

        if not self.is_connected():
            raise RuntimeError("request_historical_ticks requires an active connection")

        req_id = self._allocate_request_id()
        future: asyncio.Future[List[object]] = self._loop.create_future()
        self._historical_ticks_requests[req_id] = _HistoricalTicksRequest(future)

        await self._loop.run_in_executor(
            None,
            partial(
                self._client.reqHistoricalTicks,
                req_id,
                contract,
                start_datetime,
                end_datetime,
                number_of_ticks,
                what_to_show,
                use_rth,
                ignore_size,
                list(misc_options or []),
            ),
        )

        try:
            return await asyncio.wait_for(future, timeout)
        except Exception as exc:
            await self._loop.run_in_executor(
                None,
                partial(self._client.cancelHistoricalTicks, req_id),
            )
            raise exc
        finally:
            self._historical_ticks_requests.pop(req_id, None)

    async def request_account_summary(
        self,
        *,
        group: str = "All",
        tags: str = "All",
        timeout: float = 5.0,
    ) -> List[dict[str, Any]]:
        """Request an account summary snapshot and return all items."""

        if not self.is_connected():
            raise RuntimeError("request_account_summary requires an active connection")

        req_id = self._allocate_request_id()
        future: asyncio.Future[List[dict[str, Any]]] = self._loop.create_future()
        self._account_summary_requests[req_id] = _AccountSummaryRequest(future)

        await self._loop.run_in_executor(
            None,
            partial(self._client.reqAccountSummary, req_id, group, tags),
        )

        try:
            return await asyncio.wait_for(future, timeout)
        except Exception as exc:
            await self._loop.run_in_executor(
                None,
                partial(self._client.cancelAccountSummary, req_id),
            )
            raise exc
        finally:
            self._account_summary_requests.pop(req_id, None)

    async def request_account_updates(
        self,
        *,
        account: str = "",
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Request a one-shot snapshot of account values and portfolio."""

        if not self.is_connected():
            raise RuntimeError("request_account_updates requires an active connection")

        if self._account_updates_request is not None:
            raise RuntimeError("request_account_updates already in progress")

        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        request = _AccountUpdatesRequest(future=future)
        self._account_updates_request = request
        subscribed = False
        account_code = account.strip()
        start_time = self._loop.time()

        try:
            if not account_code:
                remaining = timeout - (self._loop.time() - start_time)
                try:
                    account_code = await self._await_default_account(remaining)
                except asyncio.TimeoutError:
                    # ``reqAccountUpdates`` accepts an empty string to request the
                    # default account. When TWS/IB Gateway is slow to publish the
                    # managed account list we still want to issue the subscription
                    # instead of failing fast so the streamed snapshot can arrive
                    # once the backend is ready.
                    account_code = ""
                    start_time = self._loop.time()

            request.account = account_code

            await self._loop.run_in_executor(
                None,
                partial(self._client.reqAccountUpdates, True, account_code),
            )
            subscribed = True

            remaining = timeout - (self._loop.time() - start_time)
            if remaining <= 0:
                raise asyncio.TimeoutError("account updates timed out before snapshot arrived")

            return await asyncio.wait_for(future, remaining)
        finally:
            if subscribed:
                await self._loop.run_in_executor(
                    None,
                    partial(self._client.reqAccountUpdates, False, account_code),
                )

            if self._account_updates_request is request and not future.done():
                future.cancel()

            self._account_updates_request = None

    async def request_positions(
        self,
        *,
        timeout: float = 5.0,
    ) -> List[dict[str, Any]]:
        """Request open positions via ``reqPositions``."""

        if not self.is_connected():
            raise RuntimeError("request_positions requires an active connection")

        if self._positions_request is not None:
            raise RuntimeError("request_positions already in progress")

        future: asyncio.Future[List[dict[str, Any]]] = self._loop.create_future()
        request = _PositionsRequest(future)
        self._positions_request = request

        await self._loop.run_in_executor(None, self._client.reqPositions)

        try:
            return await asyncio.wait_for(future, timeout)
        except Exception:
            raise
        finally:
            await self._loop.run_in_executor(None, self._client.cancelPositions)
            self._positions_request = None

    # ------------------------------------------------------------------
    # Order management helpers
    # ------------------------------------------------------------------
    async def allocate_order_id(self, *, timeout: float = 5.0) -> int:
        """Return the next available order identifier."""

        async with self._order_id_lock:
            if self._next_order_id is not None:
                order_id = self._next_order_id
                self._next_order_id += 1
                return order_id

            future: asyncio.Future[int] = self._loop.create_future()
            self._order_id_waiters.append(future)

        try:
            order_id = await asyncio.wait_for(future, timeout)
        except Exception:
            async with self._order_id_lock:
                if future in self._order_id_waiters:
                    self._order_id_waiters.remove(future)
            raise

        return order_id

    async def place_order(
        self,
        contract: Contract,
        order: Order,
        *,
        timeout: float = 5.0,
    ) -> OrderPlacement:
        """Submit ``order`` for ``contract`` and await acknowledgement."""

        if not self.is_connected():
            raise RuntimeError("place_order requires an active connection")

        order_id = getattr(order, "orderId", None)
        if not isinstance(order_id, int) or order_id <= 0:
            order_id = await self.allocate_order_id(timeout=timeout)
        else:
            self._reserve_order_id(order_id)

        try:
            setattr(order, "orderId", order_id)
        except Exception:
            pass

        future: asyncio.Future[OrderPlacement] = self._loop.create_future()
        self._pending_order_submissions[order_id] = future

        await self._loop.run_in_executor(
            None,
            partial(self._client.placeOrder, order_id, contract, order),
        )

        try:
            return await asyncio.wait_for(future, timeout)
        except Exception as exc:
            self._pending_order_submissions.pop(order_id, None)
            raise exc

    async def cancel_order(self, order_id: int, *, timeout: float = 5.0) -> str:
        """Cancel an outstanding order and wait for acknowledgement."""

        if not self.is_connected():
            raise RuntimeError("cancel_order requires an active connection")

        future: asyncio.Future[str] = self._loop.create_future()
        self._pending_order_cancellations[order_id] = future

        await self._loop.run_in_executor(None, partial(self._client.cancelOrder, order_id))

        try:
            return await asyncio.wait_for(future, timeout)
        except Exception as exc:
            self._pending_order_cancellations.pop(order_id, None)
            raise exc

    async def request_open_orders_snapshot(self) -> None:
        """Ask the gateway to stream the current set of open orders."""

        if not self.is_connected():
            raise RuntimeError("request_open_orders_snapshot requires an active connection")

        await self._loop.run_in_executor(None, self._client.reqOpenOrders)
        await self._loop.run_in_executor(None, self._client.reqAllOpenOrders)

    # ------------------------------------------------------------------
    # ``EWrapper`` callback implementations
    # ------------------------------------------------------------------
    def nextValidId(self, orderId: int) -> None:  # noqa: N802 (ibapi naming)
        """Mark the controller as connected once the gateway supplies an ID."""

        self._loop.call_soon_threadsafe(self._on_next_valid_id, orderId)

    def error(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        """Push errors into the asyncio queue for consumers."""

        def _handle_error() -> None:
            event = {
                "type": "error",
                "request_id": reqId,
                "code": errorCode,
                "message": errorString,
                "advanced": advancedOrderRejectJson,
            }
            self._publish_event(event)

            error = IBAPIRequestError(f"{errorCode}: {errorString}")
            if reqId in self._contract_details_requests:
                self._on_contract_details_error(reqId, error)
            if reqId in self._historical_data_requests:
                self._on_historical_data_error(reqId, error)
            if reqId in self._historical_ticks_requests:
                self._on_historical_ticks_error(reqId, error)
            if reqId in self._account_summary_requests:
                self._on_account_summary_error(reqId, error)
            if reqId in self._pending_order_submissions:
                self._on_order_error(reqId, error)
            if reqId in self._pending_order_cancellations:
                self._on_cancel_error(reqId, error)

        self._loop.call_soon_threadsafe(_handle_error)

    def connectAck(self) -> None:  # noqa: N802 (ibapi naming)
        """Bridge connection acknowledgements into the asyncio queue."""

        event = {"type": "connectAck"}
        self._loop.call_soon_threadsafe(self._publish_event, event)

    def connectionClosed(self) -> None:  # noqa: N802 (ibapi naming)
        """Reset controller state when the connection is closed."""

        def _handle_close() -> None:
            self._connected_event.clear()
            event = {"type": "connectionClosed"}
            self._publish_event(event)
            self._fail_all_requests(RuntimeError("connection closed"))
            self._reset_market_data_requests()
            self._on_account_updates_error(RuntimeError("connection closed"))
            self._on_positions_error(RuntimeError("connection closed"))
            self._reset_order_tracking()
            self._managed_accounts.clear()
            self._managed_accounts_event.clear()

        self._loop.call_soon_threadsafe(_handle_close)

    def contractDetails(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        contractDetails: ContractDetails,
    ) -> None:
        """Collect contract details for outstanding requests."""

        self._loop.call_soon_threadsafe(self._on_contract_details, reqId, contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802 (ibapi naming)
        """Resolve the outstanding request once the gateway finishes streaming."""

        self._loop.call_soon_threadsafe(self._on_contract_details_end, reqId)

    def accountSummary(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        account: str,
        tag: str,
        value: str,
        currency: str,
    ) -> None:
        event = {
            "type": "accountSummary",
            "request_id": reqId,
            "account": account,
            "tag": tag,
            "value": value,
            "currency": currency,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(
            self._on_account_summary,
            reqId,
            account,
            tag,
            value,
            currency,
        )

    def accountSummaryEnd(self, reqId: int) -> None:  # noqa: N802 (ibapi naming)
        event = {"type": "accountSummaryEnd", "request_id": reqId}
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(self._on_account_summary_end, reqId)

    def updateAccountValue(  # noqa: N802 (ibapi naming)
        self,
        key: str,
        val: str,
        currency: str,
        accountName: str,
    ) -> None:
        event = {
            "type": "updateAccountValue",
            "tag": key,
            "value": val,
            "currency": currency,
            "account": accountName,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(
            self._on_account_update_value,
            key,
            val,
            currency,
            accountName,
        )

    def updatePortfolio(  # noqa: N802 (ibapi naming)
        self,
        contract: Contract,
        position: float,
        marketPrice: float,
        marketValue: float,
        averageCost: float,
        unrealizedPNL: float,
        realizedPNL: float,
        accountName: str,
    ) -> None:
        event = {
            "type": "updatePortfolio",
            "contract": contract,
            "position": position,
            "market_price": marketPrice,
            "market_value": marketValue,
            "average_cost": averageCost,
            "unrealized_pnl": unrealizedPNL,
            "realized_pnl": realizedPNL,
            "account": accountName,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(
            self._on_account_update_portfolio,
            contract,
            position,
            marketPrice,
            marketValue,
            averageCost,
            unrealizedPNL,
            realizedPNL,
            accountName,
        )

    def accountDownloadEnd(self, account: str) -> None:  # noqa: N802 (ibapi naming)
        event = {"type": "accountDownloadEnd", "account": account}
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(self._on_account_download_end, account)

    def updateAccountTime(self, timeStamp: str) -> None:  # noqa: N802 (ibapi naming)
        event = {"type": "updateAccountTime", "timestamp": timeStamp}
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(self._on_account_update_time, timeStamp)

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802 (ibapi naming)
        accounts = [account.strip() for account in accountsList.split(",") if account.strip()]
        self._loop.call_soon_threadsafe(self._on_managed_accounts, accounts)

    def position(  # noqa: N802 (ibapi naming)
        self,
        account: str,
        contract: Contract,
        position: float,
        avgCost: float,
    ) -> None:
        event = {
            "type": "position",
            "account": account,
            "contract": contract,
            "position": position,
            "avg_cost": avgCost,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(
            self._on_position_update,
            account,
            contract,
            position,
            avgCost,
        )

    def positionEnd(self) -> None:  # noqa: N802 (ibapi naming)
        event = {"type": "positionEnd"}
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(self._on_position_end)

    def tickPrice(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        tickType: int,
        price: float,
        attrib: TickAttrib,
    ) -> None:
        event = {
            "type": "tickPrice",
            "request_id": reqId,
            "tick_type": tickType,
            "price": price,
            "attrib": {
                "can_auto_execute": getattr(attrib, "canAutoExecute", False),
                "past_limit": getattr(attrib, "pastLimit", False),
                "pre_open": getattr(attrib, "preOpen", False),
            },
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)

    def tickSize(self, reqId: int, tickType: int, size: float) -> None:  # noqa: N802 (ibapi naming)
        event = {
            "type": "tickSize",
            "request_id": reqId,
            "tick_type": tickType,
            "size": size,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802 (ibapi naming)
        event = {
            "type": "marketDataType",
            "request_id": reqId,
            "market_data_type": marketDataType,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)

    def tickSnapshotEnd(self, reqId: int) -> None:  # noqa: N802 (ibapi naming)
        event = {
            "type": "tickSnapshotEnd",
            "request_id": reqId,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)

    def updateMktDepthL2(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        position: int,
        marketMaker: str,
        operation: int,
        side: int,
        price: float,
        size: float,
        isSmartDepth: bool,
    ) -> None:
        event = {
            "type": "marketDepth",
            "request_id": reqId,
            "position": position,
            "market_maker": marketMaker,
            "operation": operation,
            "side": side,
            "price": price,
            "size": size,
            "is_smart_depth": isSmartDepth,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)

    def historicalData(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        bar: BarData,
    ) -> None:
        self._loop.call_soon_threadsafe(self._on_historical_data, reqId, bar)

    def historicalDataEnd(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        start: str,
        end: str,
    ) -> None:
        self._loop.call_soon_threadsafe(self._on_historical_data_end, reqId)

    def historicalTicks(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        ticks: List[HistoricalTick],
        done: bool,
    ) -> None:
        self._loop.call_soon_threadsafe(self._on_historical_ticks, reqId, ticks, done)

    def historicalTicksBidAsk(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        ticks: List[HistoricalTickBidAsk],
        done: bool,
    ) -> None:
        self._loop.call_soon_threadsafe(self._on_historical_ticks, reqId, ticks, done)

    def historicalTicksLast(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        ticks: List[HistoricalTickLast],
        done: bool,
    ) -> None:
        self._loop.call_soon_threadsafe(self._on_historical_ticks, reqId, ticks, done)

    def openOrder(  # noqa: N802 (ibapi naming)
        self,
        orderId: int,
        contract: Contract,
        order: Order,
        orderState: OrderState,
    ) -> None:
        event = {
            "type": "openOrder",
            "order_id": orderId,
            "contract": contract,
            "order": order,
            "order_state": orderState,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(self._on_open_order, orderId, contract, order, orderState)

    def orderStatus(  # noqa: N802 (ibapi naming)
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        event = {
            "type": "orderStatus",
            "order_id": orderId,
            "status": status,
            "filled": filled,
            "remaining": remaining,
            "avg_fill_price": avgFillPrice,
            "last_fill_price": lastFillPrice,
            "perm_id": permId,
            "parent_id": parentId,
            "client_id": clientId,
            "why_held": whyHeld,
            "mkt_cap_price": mktCapPrice,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)
        self._loop.call_soon_threadsafe(
            self._on_order_status,
            orderId,
            status,
        )

    def execDetails(  # noqa: N802 (ibapi naming)
        self,
        reqId: int,
        contract: Contract,
        execution: Execution,
    ) -> None:
        event = {
            "type": "execDetails",
            "request_id": reqId,
            "contract": contract,
            "execution": execution,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)

    def commissionReport(  # noqa: N802 (ibapi naming)
        self,
        commissionReport: CommissionReport,
    ) -> None:
        event = {
            "type": "commissionReport",
            "report": commissionReport,
        }
        self._loop.call_soon_threadsafe(self._publish_event, event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _disconnect_locked(self) -> None:
        """Internal helper that tears down the client without re-locking."""

        if not self.is_connected() and not (self._run_thread and self._run_thread.is_alive()):
            # Ensure state is cleared even if ``ibapi`` thinks we are already disconnected.
            self._run_thread = None
            self._connected_event.clear()
            self._next_order_id = None
            self._order_id_waiters.clear()
            self._managed_accounts.clear()
            self._managed_accounts_event.clear()
            return

        await self._loop.run_in_executor(None, self._client.disconnect)

        if self._run_thread and self._run_thread.is_alive():
            await self._loop.run_in_executor(None, partial(self._run_thread.join, 1.0))

        self._run_thread = None
        self._connected_event.clear()
        self._next_order_id = None
        self._order_id_waiters.clear()
        self._managed_accounts.clear()
        self._managed_accounts_event.clear()

    def _start_network_loop(self) -> None:
        if self._run_thread and self._run_thread.is_alive():
            return

        self._run_thread = threading.Thread(
            target=self._client.run,
            name="ibapi-network-thread",
            daemon=True,
        )
        self._run_thread.start()

    def _allocate_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    def _reserve_order_id(self, order_id: int) -> None:
        if order_id <= 0:
            return
        if self._next_order_id is None or self._next_order_id <= order_id:
            self._next_order_id = order_id + 1

    def _on_next_valid_id(self, order_id: int) -> None:
        if self._next_valid_id_future and not self._next_valid_id_future.done():
            self._next_valid_id_future.set_result(order_id)

        self._connected_event.set()

        if self._next_order_id is None or self._next_order_id < order_id:
            self._next_order_id = order_id

        self._resolve_order_id_waiters()

    def _resolve_order_id_waiters(self) -> None:
        if self._next_order_id is None:
            return

        pending = list(self._order_id_waiters)
        self._order_id_waiters.clear()
        for future in pending:
            if future.done():
                continue
            order_id = self._next_order_id
            self._next_order_id += 1
            future.set_result(order_id)

    # ------------------------------------------------------------------
    # Callback helpers executed in the event loop thread
    # ------------------------------------------------------------------
    def _on_contract_details(self, req_id: int, details: ContractDetails) -> None:
        request = self._contract_details_requests.get(req_id)
        if request is None:
            return
        request.details.append(details)

    def _on_contract_details_end(self, req_id: int) -> None:
        request = self._contract_details_requests.get(req_id)
        if request is None:
            return
        if not request.future.done():
            request.future.set_result(list(request.details))

    def _on_contract_details_error(self, req_id: int, error: IBAPIRequestError) -> None:
        request = self._contract_details_requests.get(req_id)
        if request is None:
            return
        if not request.future.done():
            request.future.set_exception(error)

    def _on_account_summary(
        self,
        req_id: int,
        account: str,
        tag: str,
        value: str,
        currency: str,
    ) -> None:
        request = self._account_summary_requests.get(req_id)
        if request is None:
            return
        request.values.append(
            {
                "account": account,
                "tag": tag,
                "value": value,
                "currency": currency,
            }
        )

    def _on_account_summary_end(self, req_id: int) -> None:
        request = self._account_summary_requests.get(req_id)
        if request is None:
            return
        if not request.future.done():
            request.future.set_result(list(request.values))

    def _on_account_summary_error(self, req_id: int, error: IBAPIRequestError) -> None:
        request = self._account_summary_requests.get(req_id)
        if request is None:
            return
        if not request.future.done():
            request.future.set_exception(error)

    def _on_historical_data(self, req_id: int, bar: BarData) -> None:
        request = self._historical_data_requests.get(req_id)
        if request is None:
            return
        request.bars.append(bar)

    def _on_historical_data_end(self, req_id: int) -> None:
        request = self._historical_data_requests.get(req_id)
        if request is None:
            return
        if not request.future.done():
            request.future.set_result(list(request.bars))

    def _on_historical_data_error(self, req_id: int, error: IBAPIRequestError) -> None:
        request = self._historical_data_requests.get(req_id)
        if request is None:
            return
        if not request.future.done():
            request.future.set_exception(error)

    def _on_historical_ticks(
        self,
        req_id: int,
        ticks: Sequence[object],
        done: bool,
    ) -> None:
        request = self._historical_ticks_requests.get(req_id)
        if request is None:
            return
        request.ticks.extend(list(ticks))
        if done and not request.future.done():
            request.future.set_result(list(request.ticks))

    def _on_historical_ticks_error(self, req_id: int, error: IBAPIRequestError) -> None:
        request = self._historical_ticks_requests.get(req_id)
        if request is None:
            return
        if not request.future.done():
            request.future.set_exception(error)

    def _on_account_update_value(
        self,
        tag: str,
        value: str,
        currency: str,
        account: str,
    ) -> None:
        request = self._account_updates_request
        if request is None:
            return
        if request.account and account and account != request.account:
            return
        request.account_values.append(
            {
                "tag": tag,
                "value": value,
                "currency": currency,
                "account": account,
            }
        )

    def _on_account_update_portfolio(
        self,
        contract: Contract,
        position: float,
        market_price: float,
        market_value: float,
        average_cost: float,
        unrealized_pnl: float,
        realized_pnl: float,
        account: str,
    ) -> None:
        request = self._account_updates_request
        if request is None:
            return
        if request.account and account and account != request.account:
            return
        request.portfolio.append(
            {
                "contract": contract,
                "position": position,
                "market_price": market_price,
                "market_value": market_value,
                "average_cost": average_cost,
                "unrealized_pnl": unrealized_pnl,
                "realized_pnl": realized_pnl,
                "account": account,
            }
        )

    def _on_account_update_time(self, account_time: str) -> None:
        request = self._account_updates_request
        if request is None:
            return
        request.account_time = account_time

    def _on_account_download_end(self, account: str) -> None:
        request = self._account_updates_request
        if request is None:
            return
        if request.account and account and account != request.account:
            return
        if not request.future.done():
            payload = {
                "account": account or request.account,
                "account_values": list(request.account_values),
                "portfolio": list(request.portfolio),
                "account_time": request.account_time,
            }
            request.future.set_result(payload)

    def _on_account_updates_error(self, error: Exception) -> None:
        request = self._account_updates_request
        if request and not request.future.done():
            request.future.set_exception(error)
        self._account_updates_request = None

    def _on_managed_accounts(self, accounts: Sequence[str]) -> None:
        self._managed_accounts = list(dict.fromkeys(accounts))
        if self._managed_accounts:
            self._managed_accounts_event.set()
        else:
            self._managed_accounts_event.clear()

    def _on_position_update(
        self,
        account: str,
        contract: Contract,
        position: float,
        avg_cost: float,
    ) -> None:
        request = self._positions_request
        if request is None:
            return
        request.positions.append(
            {
                "account": account,
                "contract": contract,
                "position": position,
                "avg_cost": avg_cost,
            }
        )

    def _on_position_end(self) -> None:
        request = self._positions_request
        if request is None:
            return
        if not request.future.done():
            request.future.set_result(list(request.positions))

    def _on_positions_error(self, error: Exception) -> None:
        request = self._positions_request
        if request and not request.future.done():
            request.future.set_exception(error)
        self._positions_request = None

    def _on_open_order(
        self,
        order_id: int,
        contract: Contract,
        order: Order,
        order_state: OrderState,
    ) -> None:
        future = self._pending_order_submissions.get(order_id)
        placement = OrderPlacement(order_id=order_id, contract=contract, order=order, state=order_state)
        if future and not future.done():
            future.set_result(placement)
            self._pending_order_submissions.pop(order_id, None)

    def _on_order_status(self, order_id: int, status: str) -> None:
        future = self._pending_order_cancellations.get(order_id)
        if future is None or future.done():
            return

        status_text = (status or "").strip().lower()
        if status_text in {"cancelled", "inactive"}:
            future.set_result(status)
            self._pending_order_cancellations.pop(order_id, None)

    def _on_order_error(self, order_id: int, error: IBAPIRequestError) -> None:
        future = self._pending_order_submissions.pop(order_id, None)
        if future and not future.done():
            future.set_exception(error)

    def _on_cancel_error(self, order_id: int, error: IBAPIRequestError) -> None:
        future = self._pending_order_cancellations.pop(order_id, None)
        if future and not future.done():
            future.set_exception(error)

    def _fail_all_requests(self, error: Exception) -> None:
        mappings = (
            self._contract_details_requests,
            self._historical_data_requests,
            self._historical_ticks_requests,
            self._account_summary_requests,
        )
        for pending in mappings:
            for request in list(pending.values()):
                if not request.future.done():
                    request.future.set_exception(error)
            pending.clear()

        for future in list(self._pending_order_submissions.values()):
            if not future.done():
                future.set_exception(error)
        self._pending_order_submissions.clear()

        for future in list(self._pending_order_cancellations.values()):
            if not future.done():
                future.set_exception(error)
        self._pending_order_cancellations.clear()

        for future in list(self._order_id_waiters):
            if not future.done():
                future.set_exception(error)
        self._order_id_waiters.clear()

    def _reset_market_data_requests(self) -> None:
        if self._market_data_requests:
            for req_id in list(self._market_data_requests.keys()):
                event = {"type": "marketDataCancelled", "request_id": req_id}
                self._publish_event(event)
            self._market_data_requests.clear()

        if self._market_depth_requests:
            for req_id in list(self._market_depth_requests.keys()):
                event = {"type": "marketDepthCancelled", "request_id": req_id}
                self._publish_event(event)
            self._market_depth_requests.clear()

    def _reset_order_tracking(self) -> None:
        for future in list(self._order_id_waiters):
            if not future.done():
                future.set_exception(RuntimeError("order id unavailable"))
        self._order_id_waiters.clear()
        self._pending_order_submissions.clear()
        self._pending_order_cancellations.clear()

    async def _await_default_account(self, timeout: float) -> str:
        if self._managed_accounts and self._managed_accounts_event.is_set():
            return self._managed_accounts[0]

        if timeout <= 0:
            raise asyncio.TimeoutError("managed account list not available")

        await asyncio.wait_for(self._managed_accounts_event.wait(), timeout)

        if self._managed_accounts:
            return self._managed_accounts[0]

        raise asyncio.TimeoutError("managed account list not available")
