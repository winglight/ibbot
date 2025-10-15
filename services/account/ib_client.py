from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List

from src.ib.base_client import BaseServiceIBClient
from src.ib.events import (
    AccountUpdateEvent,
    PortfolioPositionEvent,
    account_update_from_ib,
    portfolio_position_from_ib,
)
from src.ib.price_scale import resolve_price_scale


ACCOUNT_IB_HOST_ENV = "ACCOUNT_IB_HOST"
ACCOUNT_IB_PORT_ENV = "ACCOUNT_IB_PORT"
ACCOUNT_IB_CLIENT_ID_ENV = "ACCOUNT_IB_CLIENT_ID"


@dataclass(slots=True)
class _AccountSummaryRequest:
    future: asyncio.Future[dict[str, Any]]
    accounts: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(dict))
    currencies: dict[str, dict[str, str | None]] = field(
        default_factory=lambda: defaultdict(dict)
    )


@dataclass(slots=True)
class _PnLRequest:
    future: asyncio.Future[dict[str, Any]]
    daily: Decimal | None = None
    unrealized: Decimal | None = None
    realized: Decimal | None = None


@dataclass(slots=True)
class _HistoricalRequest:
    future: asyncio.Future[list[dict[str, Any]]]
    bars: list[dict[str, Any]] = field(default_factory=list)


class ServiceIBClient(BaseServiceIBClient):
    """Account service specific Interactive Brokers client."""

    def __init__(self) -> None:
        super().__init__(
            service_name="account",
            host_env=ACCOUNT_IB_HOST_ENV,
            port_env=ACCOUNT_IB_PORT_ENV,
            client_id_env=ACCOUNT_IB_CLIENT_ID_ENV,
        )
        self._subscription_accounts: set[str] = set()
        self._subscription_lock = asyncio.Lock()
        self._account_summary_requests: dict[int, _AccountSummaryRequest] = {}
        self._pnl_requests: dict[int, _PnLRequest] = {}
        self._historical_requests: dict[int, _HistoricalRequest] = {}
        self._positions_future: asyncio.Future[List[PortfolioPositionEvent]] | None = None
        self._positions_buffer: List[PortfolioPositionEvent] | None = None
        self._cache_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Subscription management

    async def subscribe_account_updates(self, account_codes: list[str]) -> None:
        """Register interest in account update callbacks for *account_codes*."""

        await self.wait_until_ready()
        cleaned = {code.strip() for code in account_codes if code and code.strip()}
        if not cleaned:
            return
        async with self._subscription_lock:
            new_accounts = [code for code in cleaned if code not in self._subscription_accounts]
            if not new_accounts:
                return
            for code in new_accounts:
                self.reqAccountUpdates(True, code)
            self._subscription_accounts.update(new_accounts)

    async def unsubscribe_account_updates(self) -> None:
        """Cancel all account update subscriptions."""

        await self.wait_until_ready()
        async with self._subscription_lock:
            if not self._subscription_accounts:
                return
            self.reqAccountUpdates(False, "")
            self._subscription_accounts.clear()

    # ------------------------------------------------------------------
    # Fetch helpers

    async def fetch_account_summary(
        self,
        tags: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        await self.wait_until_ready()
        loop = asyncio.get_running_loop()
        req_id = await self.next_request_id()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        request = _AccountSummaryRequest(future=future)
        with self._cache_lock:
            self._account_summary_requests[req_id] = request
        try:
            self.reqAccountSummary(req_id, "All", tags)
            result = await asyncio.wait_for(
                future,
                timeout if timeout is not None else self.DEFAULT_TIMEOUT,
            )
            return result
        finally:
            with self._cache_lock:
                self._account_summary_requests.pop(req_id, None)
            try:
                self.cancelAccountSummary(req_id)
            except Exception:  # pragma: no cover - defensive cleanup
                pass

    async def fetch_positions(
        self,
        *,
        timeout: float | None = None,
    ) -> List[PortfolioPositionEvent]:
        await self.wait_until_ready()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[List[PortfolioPositionEvent]] = loop.create_future()
        with self._cache_lock:
            if self._positions_future is not None:
                raise RuntimeError("Positions request already in-flight")
            self._positions_future = future
            self._positions_buffer = []
        try:
            self.reqPositions()
            result = await asyncio.wait_for(
                future,
                timeout if timeout is not None else self.DEFAULT_TIMEOUT,
            )
            return result
        finally:
            with self._cache_lock:
                self._positions_future = None
                self._positions_buffer = None
            try:
                self.cancelPositions()
            except Exception:  # pragma: no cover - defensive cleanup
                pass

    async def fetch_daily_pnl(
        self,
        account: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        await self.wait_until_ready()
        loop = asyncio.get_running_loop()
        req_id = await self.next_request_id()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        request = _PnLRequest(future=future)
        with self._cache_lock:
            self._pnl_requests[req_id] = request
        try:
            self.reqPnL(req_id, account, "")
            result = await asyncio.wait_for(
                future,
                timeout if timeout is not None else self.DEFAULT_TIMEOUT,
            )
            return result
        finally:
            with self._cache_lock:
                self._pnl_requests.pop(req_id, None)
            try:
                self.cancelPnL(req_id)
            except Exception:  # pragma: no cover - defensive cleanup
                pass

    async def fetch_historical_nav(
        self,
        contract: Any,
        duration: str,
        bar_size: str,
        *,
        what_to_show: str = "NAV",
        use_rth: int = 0,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        await self.wait_until_ready()
        loop = asyncio.get_running_loop()
        req_id = await self.next_request_id()
        future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        request = _HistoricalRequest(future=future)
        with self._cache_lock:
            self._historical_requests[req_id] = request
        try:
            self.reqHistoricalData(
                req_id,
                contract,
                "",
                duration,
                bar_size,
                what_to_show,
                use_rth,
                1,
                False,
                [],
            )
            result = await asyncio.wait_for(
                future,
                timeout if timeout is not None else self.DEFAULT_TIMEOUT,
            )
            return result
        finally:
            with self._cache_lock:
                self._historical_requests.pop(req_id, None)
            try:
                self.cancelHistoricalData(req_id)
            except Exception:  # pragma: no cover - defensive cleanup
                pass

    # ------------------------------------------------------------------
    # EWrapper callbacks

    def updateAccountValue(
        self,
        key: str,
        val: str,
        currency: str,
        accountName: str,
    ) -> None:  # noqa: N802 - IB API signature
        super().updateAccountValue(key, val, currency, accountName)
        event: AccountUpdateEvent = account_update_from_ib(
            accountName, key, val, currency
        )
        self.publish_event("account_update", event)

    def updatePortfolio(
        self,
        contract: Any,
        position: float,
        marketPrice: float,
        marketValue: float,
        averageCost: float,
        unrealizedPNL: float,
        realizedPNL: float,
        accountName: str,
    ) -> None:  # noqa: N802 - IB API signature
        super().updatePortfolio(
            contract,
            position,
            marketPrice,
            marketValue,
            averageCost,
            unrealizedPNL,
            realizedPNL,
            accountName,
        )
        symbol = str(getattr(contract, "symbol", "") or "")
        security_type = str(getattr(contract, "secType", "") or "")
        multiplier, price_scale, price_scale_source = resolve_price_scale(
            symbol,
            security_type,
            getattr(contract, "multiplier", None),
        )
        event = portfolio_position_from_ib(
            account=accountName,
            contract_id=int(getattr(contract, "conId", 0) or 0),
            symbol=symbol,
            security_type=security_type,
            position=position,
            average_cost=averageCost,
            market_price=marketPrice,
            market_value=marketValue,
            realized_pnl=realizedPNL,
            unrealized_pnl=unrealizedPNL,
            currency=getattr(contract, "currency", None),
            contract_multiplier=multiplier,
            price_scale=price_scale,
            price_scale_source=price_scale_source,
        )
        self.publish_event("portfolio", event)

    def accountSummary(
        self,
        reqId: int,
        account: str,
        tag: str,
        value: str,
        currency: str,
    ) -> None:  # noqa: N802 - IB API signature
        super().accountSummary(reqId, account, tag, value, currency)
        event: AccountUpdateEvent = account_update_from_ib(account, tag, value, currency)
        self.publish_event("account_summary", event)
        with self._cache_lock:
            request = self._account_summary_requests.get(reqId)
            if request:
                request.accounts[account][tag] = event.value
                request.currencies[account][tag] = currency

    def accountSummaryEnd(self, reqId: int) -> None:  # noqa: N802
        super().accountSummaryEnd(reqId)
        with self._cache_lock:
            request = self._account_summary_requests.get(reqId)
            loop = self._loop
        if not request or loop is None:
            return
        payload: dict[str, Any] = {
            "request_id": reqId,
            "accounts": {k: dict(v) for k, v in request.accounts.items()},
            "currencies": {k: dict(v) for k, v in request.currencies.items()},
            "received_at": datetime.now(timezone.utc),
        }
        loop.call_soon_threadsafe(self._resolve_future, request.future, payload)

    def position(
        self,
        account: str,
        contract: Any,
        position: float,
        avgCost: float,
    ) -> None:  # noqa: N802 - IB API signature
        super().position(account, contract, position, avgCost)
        symbol = str(getattr(contract, "symbol", "") or "")
        security_type = str(getattr(contract, "secType", "") or "")
        multiplier, price_scale, price_scale_source = resolve_price_scale(
            symbol,
            security_type,
            getattr(contract, "multiplier", None),
        )
        event = portfolio_position_from_ib(
            account=account,
            contract_id=int(getattr(contract, "conId", 0) or 0),
            symbol=symbol,
            security_type=security_type,
            position=position,
            average_cost=avgCost,
            market_price=None,
            market_value=None,
            realized_pnl=None,
            unrealized_pnl=None,
            currency=getattr(contract, "currency", None),
            contract_multiplier=multiplier,
            price_scale=price_scale,
            price_scale_source=price_scale_source,
        )
        with self._cache_lock:
            if self._positions_buffer is not None:
                self._positions_buffer.append(event)

    def positionEnd(self) -> None:  # noqa: N802 - IB API signature
        super().positionEnd()
        with self._cache_lock:
            future = self._positions_future
            positions = list(self._positions_buffer or [])
            loop = self._loop
        if future is None or loop is None:
            return
        loop.call_soon_threadsafe(self._resolve_future, future, positions)

    def pnl(
        self,
        reqId: int,
        dailyPnL: float,
        unrealizedPnL: float,
        realizedPnL: float,
    ) -> None:  # noqa: N802 - IB API signature
        super().pnl(reqId, dailyPnL, unrealizedPnL, realizedPnL)
        with self._cache_lock:
            request = self._pnl_requests.get(reqId)
            loop = self._loop
        if not request or loop is None:
            return
        request.daily = Decimal(str(dailyPnL))
        request.unrealized = Decimal(str(unrealizedPnL))
        request.realized = Decimal(str(realizedPnL))
        payload = {
            "request_id": reqId,
            "daily": request.daily,
            "unrealized": request.unrealized,
            "realized": request.realized,
            "received_at": datetime.now(timezone.utc),
        }
        loop.call_soon_threadsafe(self._resolve_future, request.future, payload)

    def historicalData(self, reqId: int, bar: Any) -> None:  # noqa: N802
        super().historicalData(reqId, bar)
        entry = {
            "date": getattr(bar, "date", ""),
            "open": Decimal(str(getattr(bar, "open", 0))),
            "high": Decimal(str(getattr(bar, "high", 0))),
            "low": Decimal(str(getattr(bar, "low", 0))),
            "close": Decimal(str(getattr(bar, "close", 0))),
            "volume": getattr(bar, "volume", 0),
            "average": Decimal(str(getattr(bar, "average", 0))),
            "bar_count": getattr(bar, "barCount", 0),
        }
        with self._cache_lock:
            request = self._historical_requests.get(reqId)
            if request:
                request.bars.append(entry)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802
        super().historicalDataEnd(reqId, start, end)
        with self._cache_lock:
            request = self._historical_requests.get(reqId)
            loop = self._loop
        if not request or loop is None:
            return
        payload = {
            "request_id": reqId,
            "start": start,
            "end": end,
            "bars": list(request.bars),
        }
        loop.call_soon_threadsafe(self._resolve_future, request.future, payload["bars"])


__all__ = ["ServiceIBClient"]
