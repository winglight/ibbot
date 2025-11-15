import math
import time
from typing import List, Tuple, Dict, Optional, Any

import numpy as np

from src.dom_parameter_tuner import DOMParameterTuner, DEFAULT_FEATURE_THRESHOLDS


FEATURE_FLAG_KEYS = [
    "stacking_buy",
    "stacking_sell",
    "pulling_buy",
    "pulling_sell",
    "wall_buy",
    "wall_sell",
    "cancel_rate_buy",
    "cancel_rate_sell",
    "absorption_buy",
    "absorption_sell",
    "sweep_up",
    "sweep_down",
    "fake_breakout",
    "wall_pull_no_follow",
    "obi_ofi_reversal",
    "spread_widen",
    "spread_tighten",
]


EVENT_METADATA: Dict[str, Dict[str, Any]] = {
    "stacking_buy": {"code": "K", "label": "买方堆单", "side": "buy"},
    "stacking_sell": {"code": "K", "label": "卖方堆单", "side": "sell"},
    "pulling_buy": {"code": "P", "label": "买方撤单", "side": "buy"},
    "pulling_sell": {"code": "P", "label": "卖方撤单", "side": "sell"},
    "wall_buy": {"code": "W", "label": "买方墙单", "side": "buy"},
    "wall_sell": {"code": "W", "label": "卖方墙单", "side": "sell"},
    "cancel_rate_buy": {"code": "C", "label": "买盘撤单速率", "side": "buy"},
    "cancel_rate_sell": {"code": "C", "label": "卖盘撤单速率", "side": "sell"},
    "absorption_buy": {"code": "A", "label": "买方吸收", "side": "buy"},
    "absorption_sell": {"code": "A", "label": "卖方吸收", "side": "sell"},
    "sweep_up": {"code": "S", "label": "上行扫单"},
    "sweep_down": {"code": "S", "label": "下行扫单"},
    "fake_breakout": {"code": "F", "label": "假突破"},
    "wall_pull_no_follow": {"code": "U", "label": "撤墙无跟随"},
    "obi_ofi_reversal": {"code": "R", "label": "OBI/OFI 快速反转"},
    "spread_widen": {"code": "E", "label": "价差扩大"},
    "spread_tighten": {"code": "T", "label": "价差收窄"},
}

EVENT_VALUE_FIELDS: Dict[str, str] = {
    "stacking_buy": "stacking_intensity_buy",
    "stacking_sell": "stacking_intensity_sell",
    "pulling_buy": "pulling_intensity_buy",
    "pulling_sell": "pulling_intensity_sell",
    "wall_buy": "wall_strength_buy",
    "wall_sell": "wall_strength_sell",
    "cancel_rate_buy": "cancel_rate_buy",
    "cancel_rate_sell": "cancel_rate_sell",
    "absorption_buy": "absorption_index_buy",
    "absorption_sell": "absorption_index_sell",
    "sweep_up": "sweep_intensity_up",
    "sweep_down": "sweep_intensity_down",
    "fake_breakout": "fake_breakout_prob",
    "wall_pull_no_follow": "wall_pull_no_follow_score",
    "obi_ofi_reversal": "obi_ofi_reversal_score",
    "spread_widen": "spread_delta_ticks",
    "spread_tighten": "spread_delta_ticks",
}


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

    def __init__(
        self,
        window: int = 10000,
        max_levels: int = 30,
        parameter_tuner: Optional[DOMParameterTuner] = None,
    ):
        self.buffer = DOMRingBuffer(window=window, max_levels=max_levels)
        self.parameter_tuner = parameter_tuner or DOMParameterTuner()
        self.thresholds: Dict[str, Dict[str, float]] = dict(self.parameter_tuner.thresholds)
        self.last_metrics: Dict[str, float] = {}
        self.last_signals: Dict[str, bool] = {"long": False, "short": False}
        self.last_events: List[Dict[str, Any]] = []
        self.features_history: List[Dict[str, float]] = []
        self._obi_history: List[float] = []
        self._ofi_history: List[float] = []
        self.cvd: float = 0.0

    # ------------------------------------------------------------------
    def _depth_totals(self, idx: int, side: int, levels: int = 5) -> Tuple[float, float]:
        valid = min(int(self.buffer.valid_levels[idx, side]), levels)
        if valid <= 0:
            return 0.0, 0.0
        sizes = self.buffer.book[idx, side, :valid, 1].astype(np.float64)
        return float(sizes.sum()), float(sizes.max())

    # ------------------------------------------------------------------
    def _best_price_tick(self, idx: int, side: int) -> int:
        valid = int(self.buffer.valid_levels[idx, side])
        if valid <= 0:
            return 0
        return int(self.buffer.book[idx, side, 0, 0])

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

        avg_norm_obi = (
            float(np.mean([(v - 0.5) * 2 for v in self._obi_history]))
            if self._obi_history
            else 0.0
        )
        cum_ofi = float(np.sum(self._ofi_history))
        ofi_scale = (std_ofi * len(self._ofi_history)) or 1.0
        norm_ofi = float(np.tanh(cum_ofi / ofi_scale))
        trend_score = 0.6 * avg_norm_obi + 0.4 * norm_ofi

        idx = (self.buffer._idx - 1) % self.buffer.window
        prev_exists = self.buffer._filled > 1
        prev_idx = (idx - 1) % self.buffer.window if prev_exists else None

        buy_depth, buy_max = self._depth_totals(idx, 0)
        sell_depth, sell_max = self._depth_totals(idx, 1)
        prev_buy_depth = prev_buy_max = 0.0
        prev_sell_depth = prev_sell_max = 0.0
        if prev_exists and prev_idx is not None:
            prev_buy_depth, prev_buy_max = self._depth_totals(prev_idx, 0)
            prev_sell_depth, prev_sell_max = self._depth_totals(prev_idx, 1)

        depth_delta_buy = buy_depth - prev_buy_depth
        depth_delta_sell = sell_depth - prev_sell_depth
        stacking_intensity_buy = max(depth_delta_buy, 0.0) / (prev_buy_depth + 1.0)
        stacking_intensity_sell = max(depth_delta_sell, 0.0) / (prev_sell_depth + 1.0)
        pulling_intensity_buy = max(-depth_delta_buy, 0.0) / (prev_buy_depth + 1.0)
        pulling_intensity_sell = max(-depth_delta_sell, 0.0) / (prev_sell_depth + 1.0)
        wall_strength_buy = buy_max / (buy_depth + 1.0)
        wall_strength_sell = sell_max / (sell_depth + 1.0)

        cur_ts = int(self.buffer.ts_ns[idx])
        prev_ts = int(self.buffer.ts_ns[prev_idx]) if prev_exists and prev_idx is not None else cur_ts
        dt_seconds = max((cur_ts - prev_ts) / 1_000_000_000.0, 0.2)
        cancel_rate_buy = max(-depth_delta_buy, 0.0) / ((prev_buy_depth + 1.0) * dt_seconds)
        cancel_rate_sell = max(-depth_delta_sell, 0.0) / ((prev_sell_depth + 1.0) * dt_seconds)

        price_tick = int(self.buffer.mid_tick[idx])
        price = price_tick / 100.0
        prev_price_tick = int(self.buffer.mid_tick[prev_idx]) if prev_exists and prev_idx is not None else price_tick
        price_delta_ticks = price_tick - prev_price_tick

        best_bid = self._best_price_tick(idx, 0)
        best_ask = self._best_price_tick(idx, 1)
        spread_ticks = float(best_ask - best_bid) if best_bid and best_ask else 0.0
        prev_spread_ticks = 0.0
        if prev_exists and prev_idx is not None:
            prev_bid = self._best_price_tick(prev_idx, 0)
            prev_ask = self._best_price_tick(prev_idx, 1)
            prev_spread_ticks = float(prev_ask - prev_bid) if prev_bid and prev_ask else 0.0
        spread_delta_ticks = spread_ticks - prev_spread_ticks

        sell_pressure = max(-ofi, 0.0)
        buy_pressure = max(ofi, 0.0)
        pressure_scale_buy = sell_pressure / (prev_buy_depth + 1.0)
        pressure_scale_sell = buy_pressure / (prev_sell_depth + 1.0)
        obi_component_buy = float(np.clip((obi - 0.5) * 2.0, 0.0, 1.0))
        obi_component_sell = float(np.clip((0.5 - obi) * 2.0, 0.0, 1.0))
        price_resilience_buy = 1.0 if price_delta_ticks >= 0 else max(0.0, 1.0 + price_delta_ticks / 5.0)
        price_resilience_sell = 1.0 if price_delta_ticks <= 0 else max(0.0, 1.0 - price_delta_ticks / 5.0)
        absorption_buy = float(
            np.clip(np.tanh(pressure_scale_buy) * (0.6 * obi_component_buy + 0.4 * price_resilience_buy), 0.0, 1.0)
        )
        absorption_sell = float(
            np.clip(np.tanh(pressure_scale_sell) * (0.6 * obi_component_sell + 0.4 * price_resilience_sell), 0.0, 1.0)
        )

        sweep_intensity_up = float(
            np.clip(
                np.tanh(max(buy_pressure, 0.0) / (prev_sell_depth + 1.0)) * (1.0 + max(price_delta_ticks, 0) / 5.0),
                0.0,
                1.0,
            )
        )
        sweep_intensity_down = float(
            np.clip(
                np.tanh(max(sell_pressure, 0.0) / (prev_buy_depth + 1.0)) * (1.0 + max(-price_delta_ticks, 0) / 5.0),
                0.0,
                1.0,
            )
        )

        prev_wall_buy = prev_buy_max / (prev_buy_depth + 1.0)
        prev_wall_sell = prev_sell_max / (prev_sell_depth + 1.0)
        wall_drop_buy = max(prev_wall_buy - wall_strength_buy, 0.0)
        wall_drop_sell = max(prev_wall_sell - wall_strength_sell, 0.0)
        follow_buy = float(np.clip(sell_pressure / (prev_buy_depth + 1.0), 0.0, 1.0))
        follow_sell = float(np.clip(buy_pressure / (prev_sell_depth + 1.0), 0.0, 1.0))
        wall_pull_no_follow_score = float(np.clip(max(wall_drop_buy * (1.0 - follow_buy), wall_drop_sell * (1.0 - follow_sell)), 0.0, 1.0))

        prev_obi = self._obi_history[-2] if len(self._obi_history) > 1 else obi
        prev_ofi = self._ofi_history[-2] if len(self._ofi_history) > 1 else ofi
        obi_shift = obi - 0.5
        prev_obi_shift = prev_obi - 0.5
        obi_flip = obi_shift * prev_obi_shift < 0
        ofi_flip = ofi * prev_ofi < 0
        if obi_flip and ofi_flip:
            obi_change = min(abs(obi - prev_obi) * 2.0, 1.0)
            ofi_change = min(abs(ofi - prev_ofi) / (abs(prev_ofi) + 1.0), 1.0)
            obi_ofi_reversal_score = float(np.clip(0.5 * (obi_change + ofi_change), 0.0, 1.0))
        else:
            obi_ofi_reversal_score = 0.0

        spread_component = float(np.clip(-spread_delta_ticks / 5.0, 0.0, 1.0)) if spread_delta_ticks < 0 else 0.0
        wall_pull_component = max(wall_drop_buy, wall_drop_sell)
        reversal_component = obi_ofi_reversal_score
        composite = 0.35 * wall_pull_component + 0.35 * reversal_component + 0.3 * spread_component
        fake_breakout_prob = float(1.0 / (1.0 + math.exp(-4.0 * (composite - 0.5))))

        trend_score += 0.1 * (stacking_intensity_buy - stacking_intensity_sell)
        trend_score += 0.08 * (absorption_buy - absorption_sell)
        trend_score -= 0.05 * (pulling_intensity_buy - pulling_intensity_sell)
        trend_score += 0.05 * (sweep_intensity_up - sweep_intensity_down)
        trend_score = max(min(trend_score, 1.0), -1.0)

        feature_flags = {key: False for key in FEATURE_FLAG_KEYS}

        features = {
            "obi5": obi,
            "ofi": ofi,
            "cvd": self.cvd,
            "zscore_obi5": z_obi,
            "zscore_ofi": z_ofi,
            "mid_price": price,
            "trend_score": trend_score,
            "depth_total_buy": buy_depth,
            "depth_total_sell": sell_depth,
            "depth_delta_buy": depth_delta_buy,
            "depth_delta_sell": depth_delta_sell,
            "stacking_intensity_buy": stacking_intensity_buy,
            "stacking_intensity_sell": stacking_intensity_sell,
            "pulling_intensity_buy": pulling_intensity_buy,
            "pulling_intensity_sell": pulling_intensity_sell,
            "wall_strength_buy": wall_strength_buy,
            "wall_strength_sell": wall_strength_sell,
            "cancel_rate_buy": cancel_rate_buy,
            "cancel_rate_sell": cancel_rate_sell,
            "absorption_index_buy": absorption_buy,
            "absorption_index_sell": absorption_sell,
            "sweep_intensity_up": sweep_intensity_up,
            "sweep_intensity_down": sweep_intensity_down,
            "fake_breakout_prob": fake_breakout_prob,
            "wall_pull_no_follow_score": wall_pull_no_follow_score,
            "obi_ofi_reversal_score": obi_ofi_reversal_score,
            "spread_ticks": spread_ticks,
            "spread_delta_ticks": spread_delta_ticks,
            "price_delta_ticks": price_delta_ticks,
            "feature_flags": feature_flags,
            "events": [],
        }
        return features

    # ------------------------------------------------------------------
    def detect_events(
        self,
        history: List[Dict[str, float]],
        thresholds: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Detect behavioural events and update feature flags for the latest metrics."""

        if not history:
            return [], []

        cur = history[-1]
        prev = history[-2] if len(history) > 1 else {}

        flags = {key: False for key in FEATURE_FLAG_KEYS}
        existing_flags = cur.get("feature_flags")
        if isinstance(existing_flags, dict):
            flags.update({key: False for key in FEATURE_FLAG_KEYS})
        cur["feature_flags"] = flags
        cur["events"] = []

        params = thresholds or self.thresholds
        absorb_cfg = params.get("absorb", {}) if isinstance(params, dict) else {}
        sweep_cfg = params.get("sweep", {}) if isinstance(params, dict) else {}
        feature_cfg = (
            params.get("features", DEFAULT_FEATURE_THRESHOLDS)
            if isinstance(params, dict)
            else DEFAULT_FEATURE_THRESHOLDS
        )

        events: List[str] = []
        details: List[Dict[str, Any]] = []

        def _register(event_key: str) -> None:
            if event_key not in flags:
                return
            if flags[event_key]:
                # Already recorded during a previous stage; avoid duplicates
                return
            flags[event_key] = True
            events.append(event_key)
            meta = EVENT_METADATA.get(event_key, {"code": event_key[:1].upper(), "label": event_key})
            entry: Dict[str, Any] = {
                "key": event_key,
                "code": meta.get("code"),
                "label": meta.get("label", event_key),
            }
            if "side" in meta:
                entry["side"] = meta["side"]
            value_field = EVENT_VALUE_FIELDS.get(event_key)
            if value_field and value_field in cur:
                entry["value"] = cur.get(value_field)
            details.append(entry)

        obi = cur.get("obi5", 0.5)
        ofi = cur.get("ofi", 0.0)
        buy_absorb_obi = absorb_cfg.get("obi_buy", 0.4)
        sell_absorb_obi = absorb_cfg.get("obi_sell", 0.6)
        absorb_ofi = float(absorb_cfg.get("ofi_sign", 0.0))
        if ofi > absorb_ofi and obi < buy_absorb_obi:
            _register("absorption_buy")
        if ofi < -absorb_ofi and obi > sell_absorb_obi:
            _register("absorption_sell")

        prev_ofi = prev.get("ofi") if isinstance(prev, dict) else None
        sweep_multiplier = float(sweep_cfg.get("ofi_multiplier", 2.0))
        sweep_min_abs = float(sweep_cfg.get("min_abs_ofi", 0.0))
        if prev_ofi is not None:
            if (
                ofi > 0
                and prev_ofi > 0
                and ofi > sweep_multiplier * prev_ofi
                and abs(ofi) >= sweep_min_abs
            ):
                _register("sweep_up")
            if (
                ofi < 0
                and prev_ofi < 0
                and ofi < sweep_multiplier * prev_ofi
                and abs(ofi) >= sweep_min_abs
            ):
                _register("sweep_down")

        stacking_cfg = feature_cfg.get("stacking", {}) if isinstance(feature_cfg, dict) else {}
        pulling_cfg = feature_cfg.get("pulling", {}) if isinstance(feature_cfg, dict) else {}
        wall_cfg = feature_cfg.get("wall", {}) if isinstance(feature_cfg, dict) else {}
        cancel_cfg = feature_cfg.get("cancel_rate", {}) if isinstance(feature_cfg, dict) else {}
        absorption_cfg = feature_cfg.get("absorption", {}) if isinstance(feature_cfg, dict) else {}
        feature_sweep_cfg = feature_cfg.get("sweep", {}) if isinstance(feature_cfg, dict) else {}
        fake_cfg = feature_cfg.get("fake_breakout", {}) if isinstance(feature_cfg, dict) else {}
        wall_pull_cfg = feature_cfg.get("wall_pull_no_follow", {}) if isinstance(feature_cfg, dict) else {}
        reversal_cfg = feature_cfg.get("obi_ofi_reversal", {}) if isinstance(feature_cfg, dict) else {}
        spread_cfg = feature_cfg.get("spread", {}) if isinstance(feature_cfg, dict) else {}

        if cur.get("depth_total_buy", 0.0) >= stacking_cfg.get("min_depth", 0.0) and cur.get(
            "stacking_intensity_buy", 0.0
        ) >= stacking_cfg.get("intensity", 0.0):
            _register("stacking_buy")
        if cur.get("depth_total_sell", 0.0) >= stacking_cfg.get("min_depth", 0.0) and cur.get(
            "stacking_intensity_sell", 0.0
        ) >= stacking_cfg.get("intensity", 0.0):
            _register("stacking_sell")

        if cur.get("depth_total_buy", 0.0) >= pulling_cfg.get("min_depth", 0.0) and cur.get(
            "pulling_intensity_buy", 0.0
        ) >= pulling_cfg.get("intensity", 0.0):
            _register("pulling_buy")
        if cur.get("depth_total_sell", 0.0) >= pulling_cfg.get("min_depth", 0.0) and cur.get(
            "pulling_intensity_sell", 0.0
        ) >= pulling_cfg.get("intensity", 0.0):
            _register("pulling_sell")

        if cur.get("wall_strength_buy", 0.0) >= wall_cfg.get("strength", 0.0) and cur.get(
            "depth_total_buy", 0.0
        ) >= wall_cfg.get("min_size", 0.0):
            _register("wall_buy")
        if cur.get("wall_strength_sell", 0.0) >= wall_cfg.get("strength", 0.0) and cur.get(
            "depth_total_sell", 0.0
        ) >= wall_cfg.get("min_size", 0.0):
            _register("wall_sell")

        if cur.get("cancel_rate_buy", 0.0) >= cancel_cfg.get("rate", 0.0):
            _register("cancel_rate_buy")
        if cur.get("cancel_rate_sell", 0.0) >= cancel_cfg.get("rate", 0.0):
            _register("cancel_rate_sell")

        if cur.get("absorption_index_buy", 0.0) >= absorption_cfg.get("index", 0.0):
            _register("absorption_buy")
        if cur.get("absorption_index_sell", 0.0) >= absorption_cfg.get("index", 0.0):
            _register("absorption_sell")

        if cur.get("sweep_intensity_up", 0.0) >= feature_sweep_cfg.get("intensity", 0.0):
            _register("sweep_up")
        if cur.get("sweep_intensity_down", 0.0) >= feature_sweep_cfg.get("intensity", 0.0):
            _register("sweep_down")

        if cur.get("fake_breakout_prob", 0.0) >= fake_cfg.get("prob", 1.0):
            _register("fake_breakout")

        if cur.get("wall_pull_no_follow_score", 0.0) >= wall_pull_cfg.get("score", 0.0):
            _register("wall_pull_no_follow")

        if cur.get("obi_ofi_reversal_score", 0.0) >= reversal_cfg.get("score", 0.0):
            _register("obi_ofi_reversal")

        delta_ticks = cur.get("spread_delta_ticks", 0.0)
        spread_threshold = spread_cfg.get("delta_ticks", 0.0)
        if spread_threshold and delta_ticks >= spread_threshold:
            _register("spread_widen")
        if spread_threshold and delta_ticks <= -spread_threshold:
            _register("spread_tighten")

        cur["events"] = details
        return events, details

    # ------------------------------------------------------------------
    @staticmethod
    def check_trade_signal(
        features: Dict[str, float],
        thresholds: Optional[Dict[str, Dict[str, float]]] = None,
        price_structures: Optional[Dict[str, float]] = None,
    ) -> Dict[str, bool]:
        """Return simple long/short trade signals."""
        signals = {"long": False, "short": False}
        params = thresholds or {}
        long_cfg = params.get("long", {})
        short_cfg = params.get("short", {})
        long_obi = float(long_cfg.get("obi", 0.6))
        long_ofi = float(long_cfg.get("ofi", 0.0))
        short_obi = float(short_cfg.get("obi", 0.4))
        short_ofi = float(short_cfg.get("ofi", 0.0))

        feature_cfg = params.get("features", {}) if isinstance(params, dict) else {}
        long_feature_ok = True
        short_feature_ok = True
        flags = features.get("feature_flags") or {}

        stacking_thr = float(feature_cfg.get("stacking", {}).get("intensity", 0.0))
        if stacking_thr > 0:
            if flags.get("stacking_buy"):
                long_feature_ok &= features.get("stacking_intensity_buy", 0.0) >= 0.75 * stacking_thr
            if flags.get("stacking_sell"):
                short_feature_ok &= features.get("stacking_intensity_sell", 0.0) >= 0.75 * stacking_thr

        absorption_thr = float(feature_cfg.get("absorption", {}).get("index", 0.0))
        if absorption_thr > 0:
            absorption_buy = features.get("absorption_index_buy")
            if absorption_buy not in (None, 0):
                long_feature_ok &= absorption_buy >= absorption_thr

            absorption_sell = features.get("absorption_index_sell")
            if absorption_sell not in (None, 0):
                short_feature_ok &= absorption_sell >= absorption_thr
        sweep_thr = float(feature_cfg.get("sweep", {}).get("intensity", 0.0))
        if sweep_thr > 0:
            if flags.get("sweep_up"):
                long_feature_ok &= features.get("sweep_intensity_up", 0.0) >= 0.7 * sweep_thr
            if flags.get("sweep_down"):
                short_feature_ok &= features.get("sweep_intensity_down", 0.0) >= 0.7 * sweep_thr

        fake_thr = float(feature_cfg.get("fake_breakout", {}).get("prob", 0.0))
        if fake_thr > 0 and flags.get("fake_breakout"):
            if features.get("fake_breakout_prob", 0.0) >= fake_thr:
                long_feature_ok = False
                short_feature_ok = False

        reversal_thr = float(feature_cfg.get("obi_ofi_reversal", {}).get("score", 0.0))
        if reversal_thr > 0 and flags.get("obi_ofi_reversal"):
            if features.get("obi_ofi_reversal_score", 0.0) >= reversal_thr:
                # reversals invalidate continuation trades, require a reset
                long_feature_ok = False
                short_feature_ok = False

        if (
            features.get("obi5", 0) >= long_obi
            and features.get("ofi", 0) >= long_ofi
            and long_feature_ok
        ):
            signals["long"] = True
        if (
            features.get("obi5", 0) <= short_obi
            and features.get("ofi", 0) <= short_ofi
            and short_feature_ok
        ):
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
        ts_ns = int(data.get("ts_ns", time.time_ns()))
        self.buffer.append_snapshot(bids, asks, mid_tick, ts_ns)
        features = self.compute_features()
        timestamp_ms = ts_ns // 1_000_000
        features["timestamp_ns"] = ts_ns
        features["timestamp"] = float(timestamp_ms)
        self.features_history.append(features)
        # Detect events using previous thresholds so tuner can react
        baseline_events, _ = self.detect_events(self.features_history, self.thresholds)
        self.thresholds = self.parameter_tuner.update(features, baseline_events)
        features["thresholds"] = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in self.thresholds.items()
        }
        features["session"] = self.thresholds.get("session", {}).get("active")
        _, event_details = self.detect_events(self.features_history, self.thresholds)
        self.last_events = event_details
        self.last_signals = self.check_trade_signal(features, self.thresholds)
        self.last_metrics = features


__all__ = ["DOMRingBuffer", "DOMProcessor"]
