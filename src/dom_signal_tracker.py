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

            outcome = self._compute_outcome(pending, price)
            payload = {
                "outcome": outcome,
                "outcome_price": price,
                "outcome_timestamp": timestamp_ms,
            }
            updated = self.cache_manager.update_metric(
                symbol,
                signal_id=pending.signal_id,
                updates=payload,
            )
            if updated:
                logger.debug(
                    "Annotated signal outcome",
                    signal_id=pending.signal_id,
                    symbol=symbol,
                    signal_type=pending.signal_type,
                    outcome=outcome,
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
    def _compute_outcome(self, pending: PendingSignal, price: float) -> Optional[int]:
        if pending.reference_price <= 0 or price <= 0:
            return None

        pct_change = (price - pending.reference_price) / pending.reference_price
        threshold = self.price_move_threshold

        if pending.signal_type == "long":
            return int(pct_change >= threshold)
        if pending.signal_type == "short":
            return int(pct_change <= -threshold)
        return None

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
