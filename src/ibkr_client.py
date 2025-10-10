from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional

from loguru import logger

try:  # pragma: no cover - optional dependency in CI
    from ib_async import Contract, Future, IB, LimitOrder, MarketOrder, Order, Stock
except ImportError:  # pragma: no cover - fallback stub used in tests
    from test.ib_insync_stub import (  # type: ignore
        Contract,
        Future,
        IB,
        LimitOrder,
        MarketOrder,
        Order,
        Stock,
    )

from src.connection_manager import ConnectionManager, connection_manager as _GLOBAL_CONNECTION_MANAGER
from src.utils.ibkr_serialization import trade_to_dict

ibkr_logger = logger.bind(module="ibkr_client", request_id="-")

INDEX_FUTURE_DETAILS: Dict[str, Dict[str, Any]] = {
    "ES": {"exchange": "CME", "currency": "USD"},
    "MES": {"exchange": "CME", "currency": "USD"},
    "NQ": {"exchange": "CME", "currency": "USD"},
    "MNQ": {"exchange": "CME", "currency": "USD"},
    "YM": {"exchange": "ECBOT", "currency": "USD"},
    "MYM": {"exchange": "ECBOT", "currency": "USD"},
    "RTY": {"exchange": "CME", "currency": "USD"},
    "M2K": {"exchange": "CME", "currency": "USD"},
    "VX": {"exchange": "CFE", "currency": "USD"},
}

INDEX_FUTURE_SYMBOLS = frozenset(INDEX_FUTURE_DETAILS.keys())


class IBKRConnectionError(RuntimeError):
    """Raised when the client cannot communicate with IBKR."""


class IBKRClient:
    """Minimal IBKR client built directly on :mod:`ib_async`."""

    def __init__(
        self,
        *,
        connection_manager: Optional[ConnectionManager] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.connection_manager = connection_manager or _GLOBAL_CONNECTION_MANAGER
        self.metadata = dict(metadata or {})
        self._logger = ibkr_logger.bind(**self.metadata)
        self._price_listeners: List[Callable[[str, Mapping[str, Any]], None]] = []  # deprecated hook

    # ------------------------------------------------------------------
    # Connection helpers

    def connect(self) -> bool:
        """Synchronously connect to IBKR."""

        return self.connection_manager.connect()

    async def connect_async(self) -> bool:
        """Async wrapper around :meth:`connect`."""

        return await asyncio.to_thread(self.connection_manager.connect)

    def disconnect(self) -> None:
        self.connection_manager.disconnect()

    def is_connected(self) -> bool:
        return self.connection_manager.is_connected()

    def get_last_connect_error(self) -> Optional[str]:
        return self.connection_manager.get_last_connect_error()

    def get_ib_instance(self) -> Optional[IB]:
        return self.connection_manager.get_ib_instance()

    async def ensure_connection(self, reason: str | None = None) -> None:
        if self.is_connected():
            return
        connected = await self.connect_async()
        if not connected:
            error = self.get_last_connect_error() or "unknown error"
            raise IBKRConnectionError(error)

    # ------------------------------------------------------------------
    # Data retrieval helpers

    async def _invoke_ib_method(self, ib: Any, name: str, *args: Any) -> Any:
        """Invoke an ``ib_async`` method, awaiting if necessary."""

        method = getattr(ib, name, None)
        if method is None:
            raise AttributeError(f"IB client has no attribute '{name}'")

        if inspect.iscoroutinefunction(method):
            return await method(*args)

        result = await asyncio.to_thread(method, *args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _resolve_managed_account(self, ib: Any) -> Optional[str]:
        """Return the default managed account identifier."""

        for candidate in ("managedAccountsAsync", "managedAccounts"):
            try:
                managed_accounts = await self._invoke_ib_method(ib, candidate)
            except AttributeError:
                continue
            except TypeError:
                continue
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.warning(
                    "Failed to fetch managed accounts", extra={"error": str(exc)}
                )
                continue

            if managed_accounts is None:
                continue

            accounts: list[str] = []
            if isinstance(managed_accounts, str):
                accounts = [
                    item.strip()
                    for item in managed_accounts.split(",")
                    if item.strip()
                ]
            elif isinstance(managed_accounts, (list, tuple, set)):
                accounts = [str(item).strip() for item in managed_accounts if item]

            if accounts:
                return accounts[0]

        return None

    async def _call_account_method(
        self,
        ib: Any,
        method_names: tuple[str, ...],
        account: Optional[str],
    ) -> list[Any]:
        """Call an account helper with the managed account when required."""

        last_error: Optional[Exception] = None
        for name in method_names:
            try:
                if account:
                    return list(await self._invoke_ib_method(ib, name, account))
                return list(await self._invoke_ib_method(ib, name))
            except TypeError as exc:
                last_error = exc
                if account:
                    try:
                        return list(await self._invoke_ib_method(ib, name))
                    except Exception as inner_exc:  # pragma: no cover - defensive
                        last_error = inner_exc if isinstance(inner_exc, Exception) else exc
                        continue
            except AttributeError:
                continue
            except Exception as exc:  # pragma: no cover - defensive
                last_error = exc
                continue

        if last_error is not None:
            self._logger.debug(
                "Account helper invocation failed", extra={"error": str(last_error)}
            )
        return []

    async def get_account_info(self) -> Dict[str, Any]:
        """Return account summary and values using ``ib_async`` helpers."""

        await self.ensure_connection(reason="account_info")
        ib = self.get_ib_instance()
        if ib is None:
            raise IBKRConnectionError("IB session unavailable")

        account_id = await self._resolve_managed_account(ib)

        summary_items = await self._call_account_method(
            ib,
            (
                "accountSummaryAsync",
                "account_summary_async",
                "account_summary",
                "accountSummary",
            ),
            account_id,
        )
        value_items = await self._call_account_method(
            ib,
            (
                "accountValuesAsync",
                "account_values_async",
                "account_values",
                "accountValues",
            ),
            account_id,
        )

        summary_payload = [
            {
                "account": getattr(item, "account", None),
                "tag": getattr(item, "tag", None),
                "value": getattr(item, "value", None),
                "currency": getattr(item, "currency", None),
            }
            for item in summary_items
        ]
        values_payload = [
            {
                "account": getattr(item, "account", None),
                "tag": getattr(item, "tag", None),
                "value": getattr(item, "value", None),
                "currency": getattr(item, "currency", None),
            }
            for item in value_items
        ]
        return {"account_summary": summary_payload, "account_values": values_payload}

    async def get_positions(self, fast: bool = False) -> List[Dict[str, Any]]:
        """Return a simplified list of positions using ``ib.positions``."""

        await self.ensure_connection(reason="positions")
        ib = self.get_ib_instance()
        if ib is None:
            raise IBKRConnectionError("IB session unavailable")

        positions = await asyncio.to_thread(ib.positions)
        payload: List[Dict[str, Any]] = []
        for pos in positions:
            contract = pos.contract
            payload.append(
                {
                    "account": pos.account,
                    "position": pos.position,
                    "avgCost": pos.avgCost,
                    "contract": {
                        "symbol": getattr(contract, "symbol", None),
                        "secType": getattr(contract, "secType", None),
                        "exchange": getattr(contract, "exchange", None),
                        "currency": getattr(contract, "currency", None),
                    },
                }
            )
        return payload

    # ------------------------------------------------------------------
    # Order helpers

    async def place_order(
        self,
        *,
        symbol: str,
        quantity: float,
        action: str,
        order_type: str = "MARKET",
        limit_price: float | None = None,
        exchange: str | None = None,
        sec_type: str = "STK",
        currency: str | None = None,
        tif: str = "DAY",
        outside_rth: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Place an order using :mod:`ib_async` primitives."""

        if os.getenv("IBBOT_DISABLE_TRADING", "0").strip().lower() in {"1", "true", "yes", "on"}:
            return self._simulate_order(
                symbol=symbol,
                quantity=quantity,
                action=action,
                order_type=order_type,
                limit_price=limit_price,
                exchange=exchange,
                sec_type=sec_type,
            )

        await self.ensure_connection(reason="place_order")
        ib = self.get_ib_instance()
        if ib is None:
            raise IBKRConnectionError("IB session unavailable")

        contract = self._build_contract(
            symbol=symbol,
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
            **kwargs,
        )
        order = self._build_order(
            action=action,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            tif=tif,
            outside_rth=outside_rth,
            **kwargs,
        )

        trade = await asyncio.to_thread(ib.placeOrder, contract, order)
        try:
            return trade_to_dict(trade)
        except Exception:  # pragma: no cover - defensive conversion
            return {
                "status": getattr(trade.orderStatus, "status", "Submitted"),
                "order_id": getattr(trade, "orderId", None),
                "symbol": symbol,
                "exchange": getattr(contract, "exchange", exchange),
                "sec_type": getattr(contract, "secType", sec_type),
                "order_type": order_type,
                "action": action,
                "quantity": quantity,
                "remaining": getattr(trade.orderStatus, "remaining", quantity),
                "limit_price": limit_price,
                "avg_fill_price": getattr(trade.orderStatus, "avgFillPrice", None),
                "fills": [],
            }

    async def place_market_order(
        self,
        symbol: str,
        quantity: float,
        action: str,
        *,
        sec_type: str = "STK",
        exchange: str | None = None,
        currency: str | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return await self.place_order(
            symbol=symbol,
            quantity=quantity,
            action=action,
            order_type="MARKET",
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
            **kwargs,
        )

    async def submit_risk_close(
        self,
        symbol: str,
        quantity: float,
        action: str,
        *,
        sec_type: str = "STK",
        exchange: str | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Utility used by the risk engine to flatten positions."""

        return await self.place_market_order(
            symbol=symbol,
            quantity=quantity,
            action=action,
            sec_type=sec_type,
            exchange=exchange,
            **kwargs,
        )

    async def backfill_commissions(self, days: int = 7) -> int:
        """Placeholder that reports a successful no-op."""

        self._logger.info("backfill_commissions requested", days=days)
        return 0

    # ------------------------------------------------------------------
    # Helpers

    def _build_contract(
        self,
        *,
        symbol: str,
        sec_type: str,
        exchange: str | None,
        currency: str | None,
        **kwargs: Any,
    ) -> Contract:
        sec = (sec_type or "STK").upper()
        exch = exchange or INDEX_FUTURE_DETAILS.get(symbol, {}).get("exchange", "SMART")
        curr = currency or INDEX_FUTURE_DETAILS.get(symbol, {}).get("currency", "USD")

        if sec == "STK":
            return Stock(symbol, exch, curr)
        if sec == "FUT":
            expiry = kwargs.get("expiry") or kwargs.get("lastTradeDateOrContractMonth")
            return Future(symbol, exch, expiry, curr)

        contract = Contract()
        contract.symbol = symbol
        contract.secType = sec
        contract.exchange = exch
        contract.currency = curr
        if "lastTradeDateOrContractMonth" in kwargs:
            contract.lastTradeDateOrContractMonth = kwargs["lastTradeDateOrContractMonth"]
        return contract

    def _build_order(
        self,
        *,
        action: str,
        quantity: float,
        order_type: str,
        limit_price: float | None,
        tif: str,
        outside_rth: bool,
        **kwargs: Any,
    ) -> Order:
        qty = float(quantity)
        order_kind = (order_type or "MARKET").upper()
        action_normalized = (action or "BUY").upper()

        if order_kind == "MARKET":
            order = MarketOrder(action_normalized, qty)
        else:
            if limit_price is None:
                raise ValueError("limit_price required for non-market orders")
            order = LimitOrder(action_normalized, qty, float(limit_price))
        order.tif = tif
        order.outsideRth = bool(outside_rth)
        return order

    def _simulate_order(
        self,
        *,
        symbol: str,
        quantity: float,
        action: str,
        order_type: str,
        limit_price: float | None,
        exchange: str | None,
        sec_type: str,
    ) -> Dict[str, Any]:
        return {
            "status": "Simulated",
            "order_id": uuid.uuid4().hex,
            "symbol": symbol,
            "exchange": exchange or INDEX_FUTURE_DETAILS.get(symbol, {}).get("exchange", "SMART"),
            "sec_type": sec_type,
            "order_type": order_type,
            "action": action,
            "quantity": float(quantity),
            "remaining": float(quantity),
            "limit_price": limit_price,
            "avg_fill_price": None,
            "fills": [],
        }


__all__ = [
    "IBKRClient",
    "IBKRConnectionError",
    "INDEX_FUTURE_DETAILS",
    "INDEX_FUTURE_SYMBOLS",
]
