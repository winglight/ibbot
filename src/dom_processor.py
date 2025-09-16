import time
from typing import List, Tuple, Dict, Optional

import numpy as np


class DOMRingBuffer:
    """NumPy-based ring buffer for DOM snapshots."""

    def __init__(self, window: int = 10000, max_levels: int = 30):
        self.window = window
        self.max_levels = max_levels
        # book[frame, side, level, field]; side:0=bids,1=asks; field:0=price_tick,1=size
        self.book = np.zeros((window, 2, max_levels, 2), dtype=np.int32)
        self.valid_levels = np.zeros((window, 2), dtype=np.int16)
        self.mid_tick = np.zeros(window, dtype=np.int32)
        self.ts_ns = np.zeros(window, dtype=np.int64)
        self._idx = 0
        self._filled = 0

    def append_snapshot(
        self,
        bids: List[Tuple[int, int]],
        asks: List[Tuple[int, int]],
        mid_tick: int,
        ts_ns: int,
    ) -> None:
        """Append a new DOM snapshot into the ring buffer."""
        i = self._idx
        n_bids = min(len(bids), self.max_levels)
        n_asks = min(len(asks), self.max_levels)

        if n_bids:
            self.book[i, 0, :n_bids, 0] = [p for p, _ in bids[:n_bids]]
            self.book[i, 0, :n_bids, 1] = [s for _, s in bids[:n_bids]]
        if n_asks:
            self.book[i, 1, :n_asks, 0] = [p for p, _ in asks[:n_asks]]
            self.book[i, 1, :n_asks, 1] = [s for _, s in asks[:n_asks]]
        if n_bids < self.max_levels:
            self.book[i, 0, n_bids:, :] = 0
        if n_asks < self.max_levels:
            self.book[i, 1, n_asks:, :] = 0

        self.valid_levels[i, 0] = n_bids
        self.valid_levels[i, 1] = n_asks
        self.mid_tick[i] = int(mid_tick)
        self.ts_ns[i] = int(ts_ns)

        self._idx = (i + 1) % self.window
        self._filled = min(self._filled + 1, self.window)

    # Backward compatible alias
    append = append_snapshot

    # ------------------------------------------------------------------
    def _resolve_idx(self, idx: Optional[int]) -> int:
        if idx is None:
            if not self._filled:
                raise IndexError("buffer empty")
            return (self._idx - 1) % self.window
        if idx < 0:
            idx = (self._idx + idx) % self.window
        return idx % self.window

    # ------------------------------------------------------------------
    def calc_obi(self, k: int = 5, idx: Optional[int] = None) -> float:
        i = self._resolve_idx(idx)
        n_bids = min(k, int(self.valid_levels[i, 0]))
        n_asks = min(k, int(self.valid_levels[i, 1]))
        bid_sum = int(self.book[i, 0, :n_bids, 1].sum())
        ask_sum = int(self.book[i, 1, :n_asks, 1].sum())
        total = bid_sum + ask_sum
        return bid_sum / total if total else 0.0

    # ------------------------------------------------------------------
    def calc_ofi(self, idx: Optional[int] = None) -> float:
        i = self._resolve_idx(idx)
        if self._filled < 2:
            return 0.0
        j = (i - 1) % self.window
        mid_i = int(self.mid_tick[i])
        mid_j = int(self.mid_tick[j])
        bid_i = int(self.book[i, 0, 0, 1])
        bid_j = int(self.book[j, 0, 0, 1])
        ask_i = int(self.book[i, 1, 0, 1])
        ask_j = int(self.book[j, 1, 0, 1])
        ofi = 0
        if mid_i >= mid_j:
            ofi += bid_i - bid_j
        if mid_i <= mid_j:
            ofi -= ask_i - ask_j
        return float(ofi)


class DOMProcessor:
    """Process depth updates using a ring buffer and compute features."""

    def __init__(self, window: int = 10000, max_levels: int = 30):
        self.buffer = DOMRingBuffer(window=window, max_levels=max_levels)
        self.last_metrics: Dict[str, float] = {}
        self.last_signals: Dict[str, bool] = {"long": False, "short": False}
        self.last_events: List[str] = []
        self.features_history: List[Dict[str, float]] = []
        self._obi_history: List[float] = []
        self._ofi_history: List[float] = []
        self.cvd: float = 0.0

    # ------------------------------------------------------------------
    def compute_features(self, snapshot_window: int = 20) -> Dict[str, float]:
        """Compute core features from the latest snapshot window."""
        obi = self.buffer.calc_obi(k=5)
        ofi = self.buffer.calc_ofi()
        self.cvd += ofi

        self._obi_history.append(obi)
        if len(self._obi_history) > snapshot_window:
            self._obi_history.pop(0)
        self._ofi_history.append(ofi)
        if len(self._ofi_history) > snapshot_window:
            self._ofi_history.pop(0)

        mean_obi = float(np.mean(self._obi_history)) if self._obi_history else 0.0
        std_obi = float(np.std(self._obi_history)) if len(self._obi_history) > 1 else 0.0
        z_obi = (obi - mean_obi) / std_obi if std_obi else 0.0

        mean_ofi = float(np.mean(self._ofi_history)) if self._ofi_history else 0.0
        std_ofi = float(np.std(self._ofi_history)) if len(self._ofi_history) > 1 else 0.0
        z_ofi = (ofi - mean_ofi) / std_ofi if std_ofi else 0.0

        # Compute a heuristic trend score in [-1, 1]
        avg_norm_obi = (
            float(np.mean([(v - 0.5) * 2 for v in self._obi_history]))
            if self._obi_history
            else 0.0
        )
        cum_ofi = float(np.sum(self._ofi_history))
        ofi_scale = (std_ofi * len(self._ofi_history)) or 1.0
        norm_ofi = float(np.tanh(cum_ofi / ofi_scale))
        trend_score = 0.6 * avg_norm_obi + 0.4 * norm_ofi
        trend_score = max(min(trend_score, 1.0), -1.0)

        price_tick = int(self.buffer.mid_tick[(self.buffer._idx - 1) % self.buffer.window])
        price = price_tick / 100.0
        return {
            "obi5": obi,
            "ofi": ofi,
            "cvd": self.cvd,
            "zscore_obi5": z_obi,
            "zscore_ofi": z_ofi,
            "mid_price": price,
            "trend_score": trend_score,
        }

    # ------------------------------------------------------------------
    def detect_events(self, history: List[Dict[str, float]]) -> List[str]:
        """Detect absorb/sweep events from feature history."""
        if not history:
            return []
        cur = history[-1]
        prev = history[-2] if len(history) > 1 else {}
        events: List[str] = []

        if cur.get("ofi", 0) > 0 and cur.get("obi5", 0) < 0.4:
            events.append("buy_absorb")
        if cur.get("ofi", 0) < 0 and cur.get("obi5", 0) > 0.6:
            events.append("sell_absorb")

        prev_ofi = prev.get("ofi")
        if prev_ofi is not None:
            if cur.get("ofi", 0) > 0 and prev_ofi > 0 and cur["ofi"] > 2 * prev_ofi:
                events.append("sweep_up")
            if cur.get("ofi", 0) < 0 and prev_ofi < 0 and cur["ofi"] < 2 * prev_ofi:
                events.append("sweep_down")
        return events

    # ------------------------------------------------------------------
    @staticmethod
    def check_trade_signal(
        features: Dict[str, float], price_structures: Optional[Dict[str, float]] = None
    ) -> Dict[str, bool]:
        """Return simple long/short trade signals."""
        signals = {"long": False, "short": False}
        if features.get("obi5", 0) > 0.6 and features.get("ofi", 0) > 0:
            signals["long"] = True
        if features.get("obi5", 0) < 0.4 and features.get("ofi", 0) < 0:
            signals["short"] = True
        return signals

    # ------------------------------------------------------------------
    def on_depth_update(self, data: Dict) -> None:
        def _to_levels(levels: List[Tuple[float, float]]) -> List[Tuple[int, int]]:
            normalized: List[Tuple[int, int]] = []
            for price, size in levels:
                if price is None:
                    continue
                try:
                    price_tick = int(round(float(price) * 100))
                except (TypeError, ValueError):
                    continue
                try:
                    size_int = int(size)
                except (TypeError, ValueError):
                    continue
                normalized.append((price_tick, size_int))
            return normalized

        bids = _to_levels(data.get("bids", []))
        asks = _to_levels(data.get("asks", []))
        if bids and asks:
            mid_tick = int(round((bids[0][0] + asks[0][0]) / 2))
        elif bids:
            mid_tick = bids[0][0]
        elif asks:
            mid_tick = asks[0][0]
        else:
            mid_tick = 0
        ts_ns = data.get("ts_ns", time.time_ns())
        self.buffer.append_snapshot(bids, asks, mid_tick, ts_ns)
        features = self.compute_features()
        self.features_history.append(features)
        self.last_metrics = features
        self.last_events = self.detect_events(self.features_history)
        self.last_signals = self.check_trade_signal(features)


__all__ = ["DOMRingBuffer", "DOMProcessor"]
