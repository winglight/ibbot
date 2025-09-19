"""Signal tracking and outcome evaluation utilities for DOM service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from src.dom_cache import DomCacheManager
from src.dom_settings import (
    DEFAULT_OUTCOME_DELAY_SECONDS,
    DEFAULT_OUTCOME_THRESHOLD,
    DOMOutcomeSettings,
    get_outcome_settings,
)


def resolve_outcome_config() -> Tuple[float, float]:
    """Return the evaluation delay (seconds) and price move threshold."""

    settings: DOMOutcomeSettings = get_outcome_settings()
    delay = max(settings.evaluation_delay_seconds, 1.0)
    threshold = max(settings.price_move_threshold, 0.0)
    return delay, threshold


@dataclass
class PendingSignal:
    signal_id: str
    symbol: str
    signal_type: str
    timestamp_ms: float
    reference_price: float
    target_timestamp_ms: float


class SignalOutcomeTracker:
    """Track pending signals and annotate their outcomes once evaluable."""

    def __init__(
        self,
        cache_manager: DomCacheManager,
        *,
        evaluation_delay_seconds: float = DEFAULT_OUTCOME_DELAY_SECONDS,
        price_move_threshold: float = DEFAULT_OUTCOME_THRESHOLD,
    ) -> None:
        self.cache_manager = cache_manager
        self.evaluation_delay_ms = max(float(evaluation_delay_seconds), 1.0) * 1000.0
        self.price_move_threshold = max(float(price_move_threshold), 0.0)
        self._pending: Dict[str, PendingSignal] = {}

    # ------------------------------------------------------------------
    def register_signal(self, symbol: str, signal_type: str, metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Register a signal triggered for later outcome evaluation.

        Parameters
        ----------
        symbol:
            Trading symbol that emitted the signal.
        signal_type:
            Either ``"long"`` or ``"short"``.
        metrics:
            Metrics dictionary produced by ``DOMProcessor`` at trigger time.

        Returns
        -------
        Optional[Dict[str, Any]]
            Dictionary of fields to merge into the metrics payload (signal id,
            placeholder outcome, etc.) or ``None`` if the registration failed.
        """

        if not symbol or not signal_type:
            return None

        timestamp_ms = self._resolve_timestamp_ms(metrics)
        reference_price = self._resolve_price(metrics)
        if timestamp_ms is None or reference_price is None:
            logger.debug(
                "Skipping signal registration due to missing timestamp/price",
                symbol=symbol,
                signal_type=signal_type,
            )
            return None

        signal_id = self._generate_signal_id(symbol, signal_type, timestamp_ms)
        target_timestamp = timestamp_ms + self.evaluation_delay_ms

        self._pending[signal_id] = PendingSignal(
            signal_id=signal_id,
            symbol=symbol,
            signal_type=signal_type,
            timestamp_ms=timestamp_ms,
            reference_price=reference_price,
            target_timestamp_ms=target_timestamp,
        )

        logger.debug(
            "Registered DOM signal",
            signal_id=signal_id,
            symbol=symbol,
            signal_type=signal_type,
            reference_price=reference_price,
            evaluate_after_ms=self.evaluation_delay_ms,
        )

        return {
            "signal_id": signal_id,
            "signal_type": signal_type,
            "signal_price": reference_price,
            "outcome": None,
        }

    # ------------------------------------------------------------------
    def evaluate_on_metrics(self, symbol: str, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Evaluate pending signals when sufficient data becomes available.

        Returns a list of dictionaries describing the updates applied (each
        entry includes ``signal_id`` and the resolved ``outcome``)."""

        if not self._pending or not symbol:
            return []

        timestamp_ms = self._resolve_timestamp_ms(metrics)
        price = self._resolve_price(metrics)
        if timestamp_ms is None or price is None:
            return []

        updates: List[Dict[str, Any]] = []
        to_remove: List[str] = []

        for pending in list(self._pending.values()):
            if pending.symbol != symbol:
                continue
            if timestamp_ms < pending.target_timestamp_ms:
                continue

            outcome_details = self._compute_outcome(pending, price)
            outcome_value = outcome_details.get("outcome")
            payload: Dict[str, Any] = {
                "outcome": outcome_value,
                "outcome_price": price,
                "outcome_timestamp": timestamp_ms,
            }

            if outcome_details.get("threshold_pct") is not None:
                payload["outcome_threshold_pct"] = outcome_details["threshold_pct"]
            if outcome_details.get("price_delta") is not None:
                payload["outcome_price_delta"] = outcome_details["price_delta"]
            if outcome_details.get("pct_change") is not None:
                payload["outcome_pct_change"] = outcome_details["pct_change"]
            if outcome_details.get("pnl_currency") is not None:
                payload["outcome_pnl_currency"] = outcome_details["pnl_currency"]
            if outcome_details.get("target_amount") is not None:
                payload["outcome_target_currency"] = outcome_details["target_amount"]
            if outcome_details.get("point_value") is not None:
                payload["outcome_point_value"] = outcome_details["point_value"]
            if outcome_details.get("outcome_tier"):
                payload["outcome_tier"] = outcome_details["outcome_tier"]

            updated = self.cache_manager.update_metric(
                symbol,
                signal_id=pending.signal_id,
                timestamp=pending.timestamp_ms,
                updates=payload,
            )
            if updated:
                logger.debug(
                    "Annotated signal outcome",
                    signal_id=pending.signal_id,
                    symbol=symbol,
                    signal_type=pending.signal_type,
                    outcome=outcome_value,
                    price=price,
                )
                updates.append({"signal_id": pending.signal_id, **payload})
            else:
                logger.debug(
                    "Pending signal not found in cache during outcome update",
                    signal_id=pending.signal_id,
                    symbol=symbol,
                )
            to_remove.append(pending.signal_id)

        for signal_id in to_remove:
            self._pending.pop(signal_id, None)

        return updates

    # ------------------------------------------------------------------
    def _fetch_risk_context(self, symbol: str) -> Tuple[Optional[Any], float]:
        try:
            import core  # type: ignore
            risk_manager = getattr(core, 'risk_manager', None)
        except Exception:
            risk_manager = None

        if not risk_manager:
            return None, 1.0

        rule = None
        sym = (symbol or '').upper()
        try:
            rules = getattr(risk_manager, 'risk_rules', {}) or {}
            if isinstance(rules, dict) and rules:
                rule = rules.get(sym) or rules.get(symbol) or rules.get(sym.replace('.', ''))
        except Exception:
            rule = None

        point_value = 1.0
        try:
            pv_map = getattr(risk_manager, 'point_value_map', {}) or {}
            if isinstance(pv_map, dict) and pv_map:
                pv = pv_map.get(sym)
                if pv is None:
                    pv = pv_map.get(symbol)
                if pv is None:
                    pv = pv_map.get(sym.replace('.', ''))
                if pv is not None:
                    point_value = float(pv)
        except Exception:
            point_value = 1.0

        return rule, max(point_value, 1e-9)

    def _derive_threshold_from_rule(
        self,
        pending: PendingSignal,
        rule: Any,
        reference_price: float,
    ) -> Tuple[Optional[float], Optional[float]]:
        if not rule or reference_price <= 0:
            return None, None

        def _coerce(value: Any) -> Optional[float]:
            try:
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        diffs: List[float] = []
        tp_price = _coerce(getattr(rule, 'take_profit_price', None))
        sl_price = _coerce(getattr(rule, 'stop_loss_price', None))
        max_loss_pct = _coerce(getattr(rule, 'max_loss_percent', None))

        if pending.signal_type == 'long':
            if tp_price is not None:
                diff = tp_price - reference_price
                if diff > 0:
                    diffs.append(diff)
            if max_loss_pct is not None and max_loss_pct > 0:
                diffs.append(reference_price * max_loss_pct)
            if sl_price is not None:
                diff = reference_price - sl_price
                if diff > 0:
                    diffs.append(diff)
        else:
            if tp_price is not None:
                diff = reference_price - tp_price
                if diff > 0:
                    diffs.append(diff)
            if max_loss_pct is not None and max_loss_pct > 0:
                diffs.append(reference_price * max_loss_pct)
            if sl_price is not None:
                diff = sl_price - reference_price
                if diff > 0:
                    diffs.append(diff)

        if not diffs:
            return None, None

        diff_price = max(diffs)
        threshold_pct = diff_price / reference_price if reference_price else None
        return (max(threshold_pct, 0.0) if threshold_pct is not None else None, abs(diff_price))

    @staticmethod
    def _price_change(pending: PendingSignal, price: float) -> float:
        diff = price - pending.reference_price
        if pending.signal_type == 'short':
            diff = -diff
        return diff

    @staticmethod
    def _price_to_currency(price_diff: float, point_value: float) -> float:
        try:
            return abs(float(price_diff)) * max(float(point_value), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _compute_outcome(self, pending: PendingSignal, price: float) -> Dict[str, Any]:
        reference_price = pending.reference_price
        if reference_price <= 0 or price <= 0:
            return {"outcome": None}

        rule, point_value = self._fetch_risk_context(pending.symbol)
        threshold_pct = self.price_move_threshold
        target_currency = None

        if rule:
            derived_pct, diff_price = self._derive_threshold_from_rule(pending, rule, reference_price)
            if derived_pct is not None:
                threshold_pct = max(derived_pct, 0.0)
            if diff_price is not None:
                target_currency = self._price_to_currency(diff_price, point_value)

        if target_currency is None:
            target_currency = self._price_to_currency(reference_price * threshold_pct, point_value)

        price_delta = self._price_change(pending, price)
        pct_change = price_delta / reference_price if reference_price else 0.0
        pnl_currency = price_delta * point_value

        outcome = None
        if pending.signal_type in {'long', 'short'}:
            outcome = int(pct_change >= threshold_pct)

        outcome_tier = None
        if pnl_currency > 0 and target_currency > 0:
            ratio = pnl_currency / target_currency
            if ratio < 0.5:
                outcome_tier = 'small_win'
            elif ratio > 1.5:
                outcome_tier = 'big_win'
            else:
                outcome_tier = 'medium_win'
        elif pnl_currency < 0:
            outcome_tier = 'loss'
        elif pnl_currency == 0:
            outcome_tier = 'flat'

        return {
            "outcome": outcome,
            "threshold_pct": threshold_pct,
            "price_delta": price_delta,
            "pct_change": pct_change,
            "pnl_currency": pnl_currency,
            "target_amount": target_currency,
            "point_value": point_value,
            "outcome_tier": outcome_tier,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_timestamp_ms(metrics: Dict[str, Any]) -> Optional[float]:
        timestamp = metrics.get("timestamp")
        if timestamp is None:
            ts_ns = metrics.get("timestamp_ns")
            if isinstance(ts_ns, (int, float)):
                timestamp = float(ts_ns) / 1_000_000.0
        try:
            return float(timestamp) if timestamp is not None else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_price(metrics: Dict[str, Any]) -> Optional[float]:
        price = metrics.get("mid_price")
        try:
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _generate_signal_id(symbol: str, signal_type: str, timestamp_ms: float) -> str:
        suffix = uuid.uuid4().hex[:8]
        return f"{symbol}-{signal_type}-{int(timestamp_ms)}-{suffix}"


__all__ = [
    "SignalOutcomeTracker",
    "resolve_outcome_config",
]
