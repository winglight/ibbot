from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Dict, List, Optional


def _safe_getattr(obj: Any, attr: str, default: Any = None) -> Any:
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def _sanitize_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        result = float(value)
    else:
        try:
            text = str(value).strip()
        except Exception:
            return None
        if not text:
            return None
        try:
            result = float(text)
        except (TypeError, ValueError):
            return None
    if math.isnan(result) or math.isinf(result):
        return None
    return float(result)


def _sanitize_str(value: Any, *, upper: bool = False) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = str(value).strip()
        except Exception:
            return None
    if not text:
        return None
    return text.upper() if upper else text


def _format_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return _sanitize_str(value)


def _first_non_empty(*values: Any) -> Optional[str]:
    for candidate in values:
        if candidate is None:
            continue
        if isinstance(candidate, str):
            text = candidate.strip()
            if text:
                return text
            continue
        try:
            return str(candidate)
        except Exception:
            continue
    return None


def is_trade_like(value: Any) -> bool:
    return hasattr(value, "order") and hasattr(value, "orderStatus")


def is_fill_like(value: Any) -> bool:
    return hasattr(value, "execution") or hasattr(value, "commissionReport")


def fill_to_dict(fill: Any) -> Dict[str, Any]:
    execution = _safe_getattr(fill, "execution")
    report = _safe_getattr(fill, "commissionReport")

    data: Dict[str, Any] = {}
    if execution is not None:
        data["exec_id"] = _first_non_empty(
            _safe_getattr(execution, "execId"),
            _safe_getattr(execution, "executionId"),
        )
        data["time"] = _format_timestamp(_safe_getattr(execution, "time"))
        data["side"] = _sanitize_str(_safe_getattr(execution, "side"))
        data["price"] = _sanitize_float(_safe_getattr(execution, "price"))
        data["avg_price"] = _sanitize_float(_safe_getattr(execution, "avgPrice"))
        quantity = _sanitize_float(
            _safe_getattr(execution, "shares")
            or _safe_getattr(execution, "quantity")
            or _safe_getattr(execution, "qty")
        )
        if quantity is not None:
            data["quantity"] = quantity
        data["exchange"] = _sanitize_str(_safe_getattr(execution, "exchange"))
        data["account"] = _sanitize_str(
            _safe_getattr(execution, "acctNumber") or _safe_getattr(execution, "account")
        )
        data["order_id"] = _first_non_empty(
            _safe_getattr(execution, "orderId"),
            _safe_getattr(execution, "permId"),
        )
    if report is not None:
        commission = _sanitize_float(_safe_getattr(report, "commission"))
        if commission is not None:
            data["commission"] = commission
        currency = _sanitize_str(_safe_getattr(report, "currency"))
        if currency:
            data["commission_currency"] = currency
        realized = _sanitize_float(
            _safe_getattr(report, "realizedPNL")
            or _safe_getattr(report, "realizedPnl")
        )
        if realized is not None:
            data["realized_pnl"] = realized
        yield_value = _sanitize_float(
            _safe_getattr(report, "yield_") or _safe_getattr(report, "yield")
        )
        if yield_value is not None:
            data["yield"] = yield_value
        redemption = _sanitize_str(_safe_getattr(report, "yieldRedemptionDate"))
        if redemption:
            data["yield_redemption_date"] = redemption
    return {key: value for key, value in data.items() if value is not None}


def trade_to_dict(trade: Any, *, defaults: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    if trade is None:
        base: Dict[str, Any] = {}
    else:
        order = _safe_getattr(trade, "order")
        order_status = _safe_getattr(trade, "orderStatus")
        contract = _safe_getattr(trade, "contract")

        order_id = _first_non_empty(
            _safe_getattr(order, "orderId"),
            _safe_getattr(order, "permId"),
            _safe_getattr(order, "id"),
        )
        status = _sanitize_str(_safe_getattr(order_status, "status")) or "Submitted"
        action = _sanitize_str(_safe_getattr(order, "action"), upper=True)
        order_type = _sanitize_str(_safe_getattr(order, "orderType"), upper=True)
        limit_price = _sanitize_float(_safe_getattr(order, "lmtPrice"))
        stop_price = _sanitize_float(
            _safe_getattr(order, "auxPrice") or _safe_getattr(order, "stopPrice")
        )
        quantity = _sanitize_float(_safe_getattr(order, "totalQuantity"))
        tif = _sanitize_str(_safe_getattr(order, "tif"))
        parent_id = _first_non_empty(_safe_getattr(order, "parentId"))
        client_id = _first_non_empty(_safe_getattr(order, "clientId"))
        order_ref = _sanitize_str(_safe_getattr(order, "orderRef"))
        perm_id = _first_non_empty(_safe_getattr(order, "permId"))

        avg_fill_price = _sanitize_float(_safe_getattr(order_status, "avgFillPrice"))
        last_fill_price = _sanitize_float(_safe_getattr(order_status, "lastFillPrice"))
        filled = _sanitize_float(_safe_getattr(order_status, "filled"))
        remaining = _sanitize_float(_safe_getattr(order_status, "remaining"))
        submitted_time = _format_timestamp(_safe_getattr(order_status, "submittedTime"))
        completed_time = _format_timestamp(_safe_getattr(order_status, "completedTime"))
        warning = _sanitize_str(_safe_getattr(order_status, "warningText"))
        why_held = _sanitize_str(_safe_getattr(order_status, "whyHeld"))
        commission = _sanitize_float(_safe_getattr(order_status, "commission"))
        commission_currency = _sanitize_str(
            _safe_getattr(order_status, "commissionCurrency")
        )
        realized_pnl = _sanitize_float(
            _safe_getattr(order_status, "realizedPNL")
            or _safe_getattr(order_status, "realizedPnl")
        )

        contract_symbol = _sanitize_str(
            _safe_getattr(contract, "localSymbol") or _safe_getattr(contract, "symbol")
        )
        contract_exchange = _sanitize_str(_safe_getattr(contract, "exchange"))
        contract_sec_type = _sanitize_str(_safe_getattr(contract, "secType"), upper=True)

        base = {
            "order_id": order_id,
            "status": status,
            "symbol": contract_symbol,
            "exchange": contract_exchange,
            "sec_type": contract_sec_type,
            "action": action,
            "order_type": order_type,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "avg_fill_price": avg_fill_price,
            "last_fill_price": last_fill_price,
            "quantity": quantity,
            "filled": filled,
            "remaining": remaining,
            "time_in_force": tif,
            "parent_id": parent_id,
            "client_id": client_id,
            "order_ref": order_ref,
            "permid": perm_id,
            "commission": commission,
            "commission_currency": commission_currency,
            "realized_pnl": realized_pnl,
            "submitted_at": submitted_time,
            "completed_at": completed_time,
            "warning": warning,
            "why_held": why_held,
        }

        fills_data: List[Dict[str, Any]] = []
        fills_iterable = _safe_getattr(trade, "fills")
        if isinstance(fills_iterable, Sequence) and not isinstance(
            fills_iterable, (str, bytes, bytearray)
        ):
            for fill in fills_iterable:
                if not is_fill_like(fill):
                    continue
                fill_payload = fill_to_dict(fill)
                if fill_payload:
                    fills_data.append(fill_payload)
        elif fills_iterable is not None:
            # Some trade objects expose fills as generators/iterables
            try:
                for fill in list(fills_iterable):
                    if not is_fill_like(fill):
                        continue
                    fill_payload = fill_to_dict(fill)
                    if fill_payload:
                        fills_data.append(fill_payload)
            except Exception:
                pass
        if fills_data:
            base["fills"] = fills_data
            if commission is None:
                total_commission = 0.0
                commission_found = False
                for fill_payload in fills_data:
                    fill_commission = fill_payload.get("commission")
                    if fill_commission is None:
                        continue
                    commission_found = True
                    total_commission += float(fill_commission)
                if commission_found:
                    base["commission"] = total_commission
            if commission_currency is None:
                for fill_payload in fills_data:
                    currency = fill_payload.get("commission_currency")
                    if currency:
                        base["commission_currency"] = currency
                        break
            if realized_pnl is None:
                total_realized = 0.0
                realized_found = False
                for fill_payload in fills_data:
                    fill_realized = fill_payload.get("realized_pnl")
                    if fill_realized is None:
                        continue
                    realized_found = True
                    total_realized += float(fill_realized)
                if realized_found:
                    base["realized_pnl"] = total_realized

        log_entries = _safe_getattr(trade, "log")
        if isinstance(log_entries, Sequence) and log_entries:
            first = log_entries[0]
            timestamp = _format_timestamp(_safe_getattr(first, "time"))
            if timestamp:
                base.setdefault("submitted_at", timestamp)
        elif log_entries:
            try:
                iterator = iter(log_entries)
            except TypeError:
                iterator = None
            if iterator is not None:
                try:
                    first = next(iterator)
                except StopIteration:
                    first = None
                if first is not None:
                    timestamp = _format_timestamp(_safe_getattr(first, "time"))
                    if timestamp:
                        base.setdefault("submitted_at", timestamp)

    if defaults:
        for key, value in defaults.items():
            base.setdefault(key, value)
    return base


def coerce_ibkr_value(value: Any) -> Any:
    if is_trade_like(value):
        return trade_to_dict(value)
    if is_fill_like(value):
        return fill_to_dict(value)
    if isinstance(value, Mapping):
        return {key: coerce_ibkr_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [coerce_ibkr_value(item) for item in value]
    return value


__all__ = [
    "coerce_ibkr_value",
    "fill_to_dict",
    "is_fill_like",
    "is_trade_like",
    "trade_to_dict",
]
