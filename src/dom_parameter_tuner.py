"""Adaptive tuning utilities for DOM parameter thresholds.

This module implements the parameter tuning iteration described in the
DOM design specification.  It provides the :class:`DOMParameterTuner`
which keeps track of session specific statistics (regular trading hours
vs extended trading hours) and dynamically adjusts detection thresholds
based on observed order-book features.

The tuner monitors contradictory patterns – for example a high bid-side
imbalance while cumulative volume delta keeps falling – and raises the
entry thresholds for the corresponding direction.  Likewise, it tracks
false sweeps that are immediately retraced and tightens the conditions
for future sweep-driven short/long signals.  Periodic optimisation hooks
are also exposed so the service layer can iterate on the base
thresholds using historical outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from statistics import mean, pstdev
from typing import Deque, Dict, Iterable, List, MutableMapping, Optional
from collections import deque

from loguru import logger

from src.dom_settings import get_logging_settings

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fall back to UTC only
    ZoneInfo = None  # type: ignore


DEFAULT_BASE_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "long": {"obi": 0.6, "ofi": 0.0},
    "short": {"obi": 0.4, "ofi": 0.0},
    "absorb": {"obi_buy": 0.4, "obi_sell": 0.6, "ofi_sign": 0.0},
    "sweep": {"ofi_multiplier": 2.0, "min_abs_ofi": 0.0},
}

DEFAULT_FEATURE_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "stacking": {"intensity": 0.35, "min_depth": 50.0},
    "pulling": {"intensity": 0.3, "min_depth": 30.0},
    "wall": {"strength": 0.35, "min_size": 80.0},
    "cancel_rate": {"rate": 1.2},
    "absorption": {"index": 0.6},
    "sweep": {"intensity": 0.7},
    "fake_breakout": {"prob": 0.65},
    "wall_pull_no_follow": {"score": 0.6},
    "obi_ofi_reversal": {"score": 0.75},
    "spread": {"delta_ticks": 2.0},
}

RTH_START = time(9, 30)
RTH_END = time(16, 15)


@dataclass
class RollingStats:
    """Maintain a rolling window of values and expose mean/std."""

    window: int = 900
    values: Deque[float] = field(default_factory=lambda: deque(maxlen=900))

    def update(self, value: float) -> None:
        self.values.append(float(value))

    @property
    def mean(self) -> float:
        if not self.values:
            return 0.0
        return float(mean(self.values))

    @property
    def std(self) -> float:
        if len(self.values) < 2:
            return 0.0
        return float(pstdev(self.values))


@dataclass
class SessionProfile:
    """Container holding adaptive state for a trading session."""

    obi_stats: RollingStats = field(default_factory=RollingStats)
    ofi_stats: RollingStats = field(default_factory=RollingStats)
    long_penalty: float = 0.0
    short_penalty: float = 0.0
    ofi_long_penalty: float = 0.0
    ofi_short_penalty: float = 0.0
    last_timestamp_ns: Optional[int] = None


def _log_penalty_event(event: str, **kwargs: float) -> None:
    """Emit structured logs for penalty adjustments respecting verbosity flags."""

    settings = get_logging_settings()
    level = "INFO" if settings.verbose_penalty_logs else "DEBUG"
    logger.bind(dom_event="penalty_adjustment", configured_level=settings.log_level).log(
        level,
        event,
        **kwargs,
    )


class DOMParameterTuner:
    """Adaptive parameter tuner for DOM heuristics."""

    def __init__(
        self,
        base_thresholds: Optional[MutableMapping[str, Dict[str, float]]] = None,
        *,
        feature_thresholds: Optional[MutableMapping[str, Dict[str, float]]] = None,
        stats_window: int = 900,
        penalty_decay: float = 0.98,
        penalty_step: float = 0.02,
        max_penalty: float = 0.15,
        reversal_window_sec: int = 60,
        session_timezone: str = "America/New_York",
    ) -> None:
        self.base_thresholds: Dict[str, Dict[str, float]] = {
            key: dict(value) for key, value in (base_thresholds or DEFAULT_BASE_THRESHOLDS).items()
        }
        self.feature_thresholds: Dict[str, Dict[str, float]] = {
            key: {sub: float(val) for sub, val in dict(value).items()}
            for key, value in (feature_thresholds or DEFAULT_FEATURE_THRESHOLDS).items()
        }
        self.profiles: Dict[str, SessionProfile] = {
            "RTH": SessionProfile(RollingStats(stats_window), RollingStats(stats_window)),
            "ETH": SessionProfile(RollingStats(stats_window), RollingStats(stats_window)),
        }
        self.penalty_decay = float(max(min(penalty_decay, 1.0), 0.0))
        self.penalty_step = float(max(penalty_step, 0.0))
        self.max_penalty = float(max_penalty)
        self.reversal_window_ns = int(reversal_window_sec * 1_000_000_000)
        if ZoneInfo and session_timezone:
            try:
                self.session_tz = ZoneInfo(session_timezone)
            except Exception:  # pragma: no cover - fallback to UTC
                self.session_tz = timezone.utc
        else:
            self.session_tz = timezone.utc
        self._pending_sweeps: List[Dict[str, int]] = []
        self.thresholds: Dict[str, Dict[str, float]] = self._compute_thresholds("RTH", self.profiles["RTH"])

    # ------------------------------------------------------------------
    def _current_session(self, timestamp_ns: Optional[int]) -> str:
        if timestamp_ns is None:
            return "RTH"
        try:
            dt = datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):  # pragma: no cover - safeguard
            return "RTH"
        local_dt = dt if self.session_tz is timezone.utc else dt.astimezone(self.session_tz)
        local_time = local_dt.time()
        if RTH_START <= local_time <= RTH_END:
            return "RTH"
        return "ETH"

    # ------------------------------------------------------------------
    def _clamp(self, value: float, minimum: float, maximum: float) -> float:
        return max(min(value, maximum), minimum)

    # ------------------------------------------------------------------
    def _decay_penalties(self, profile: SessionProfile) -> None:
        profile.long_penalty *= self.penalty_decay
        profile.short_penalty *= self.penalty_decay
        profile.ofi_long_penalty *= self.penalty_decay
        profile.ofi_short_penalty *= self.penalty_decay

    # ------------------------------------------------------------------
    def _register_false_long(self, profile: SessionProfile) -> None:
        prev_long = profile.long_penalty
        prev_ofi = profile.ofi_long_penalty
        profile.long_penalty = min(profile.long_penalty + self.penalty_step, self.max_penalty)
        profile.ofi_long_penalty = min(profile.ofi_long_penalty + self.penalty_step, self.max_penalty)
        _log_penalty_event(
            "Registered false long signal penalty",
            previous_long_penalty=prev_long,
            previous_ofi_penalty=prev_ofi,
            long_penalty=profile.long_penalty,
            ofi_penalty=profile.ofi_long_penalty,
        )

    # ------------------------------------------------------------------
    def _register_false_short(self, profile: SessionProfile) -> None:
        prev_short = profile.short_penalty
        prev_ofi = profile.ofi_short_penalty
        profile.short_penalty = min(profile.short_penalty + self.penalty_step, self.max_penalty)
        profile.ofi_short_penalty = min(profile.ofi_short_penalty + self.penalty_step, self.max_penalty)
        _log_penalty_event(
            "Registered false short signal penalty",
            previous_short_penalty=prev_short,
            previous_ofi_penalty=prev_ofi,
            short_penalty=profile.short_penalty,
            ofi_penalty=profile.ofi_short_penalty,
        )

    # ------------------------------------------------------------------
    def _update_pending_sweeps(
        self, profile: SessionProfile, metrics: Dict[str, float], events: Iterable[str]
    ) -> None:
        timestamp_ns = int(metrics.get("timestamp_ns", 0))
        # Register newly detected sweeps
        for event in events:
            if event == "sweep_down":
                self._pending_sweeps.append({"direction": -1, "timestamp_ns": timestamp_ns})
            elif event == "sweep_up":
                self._pending_sweeps.append({"direction": 1, "timestamp_ns": timestamp_ns})

        if not self._pending_sweeps:
            return

        remaining: List[Dict[str, int]] = []
        for item in self._pending_sweeps:
            ts = item.get("timestamp_ns", 0)
            if timestamp_ns - ts > self.reversal_window_ns:
                continue
            direction = item.get("direction")
            if direction == -1:
                if metrics.get("ofi", 0.0) > 0 or metrics.get("trend_score", 0.0) > 0.1:
                    self._register_false_short(profile)
                    continue
            elif direction == 1:
                if metrics.get("ofi", 0.0) < 0 or metrics.get("trend_score", 0.0) < -0.1:
                    self._register_false_long(profile)
                    continue
            remaining.append(item)
        self._pending_sweeps = remaining

    # ------------------------------------------------------------------
    def _compute_thresholds(self, session: str, profile: SessionProfile) -> Dict[str, Dict[str, float]]:
        base = self.base_thresholds
        obi_mean = profile.obi_stats.mean
        obi_std = profile.obi_stats.std
        ofi_mean = profile.ofi_stats.mean
        ofi_std = profile.ofi_stats.std
        z = 0.5

        long_obi = max(base["long"]["obi"], obi_mean + z * obi_std) + profile.long_penalty
        long_obi = self._clamp(long_obi, 0.5, 0.95)

        short_obi = min(base["short"]["obi"], obi_mean - z * obi_std) - profile.short_penalty
        short_obi = self._clamp(short_obi, 0.05, 0.5)

        long_ofi = max(base["long"]["ofi"], ofi_mean + z * ofi_std) + profile.ofi_long_penalty
        short_ofi = min(base["short"]["ofi"], ofi_mean - z * ofi_std) - profile.ofi_short_penalty

        thresholds = {
            "session": {"active": session},
            "long": {"obi": long_obi, "ofi": long_ofi},
            "short": {"obi": short_obi, "ofi": short_ofi},
            "absorb": dict(base["absorb"]),
            "sweep": dict(base["sweep"]),
            "features": {
                key: {sub: float(val) for sub, val in value.items()}
                for key, value in self.feature_thresholds.items()
            },
        }
        return thresholds

    # ------------------------------------------------------------------
    def update(
        self,
        metrics: Dict[str, float],
        events: Iterable[str] = (),
    ) -> Dict[str, Dict[str, float]]:
        """Update rolling statistics and recompute thresholds.

        Parameters
        ----------
        metrics:
            The latest computed feature set.
        events:
            Events detected with the previous thresholds.  Used to monitor
            false sweeps and adjust penalties accordingly.
        """

        timestamp_ns = metrics.get("timestamp_ns")
        session = self._current_session(timestamp_ns if isinstance(timestamp_ns, int) else None)
        profile = self.profiles[session]
        profile.last_timestamp_ns = int(timestamp_ns) if isinstance(timestamp_ns, int) else None

        obi = float(metrics.get("obi5", 0.5))
        ofi = float(metrics.get("ofi", 0.0))
        cvd = float(metrics.get("cvd", 0.0))
        trend = float(metrics.get("trend_score", 0.0))

        self._decay_penalties(profile)
        profile.obi_stats.update(obi)
        profile.ofi_stats.update(ofi)

        base = self.base_thresholds
        if obi >= base["long"]["obi"]:
            if cvd < 0 or trend < 0:
                self._register_false_long(profile)
        if obi <= base["short"]["obi"]:
            if cvd > 0 or trend > 0:
                self._register_false_short(profile)

        self._update_pending_sweeps(profile, metrics, events)

        self.thresholds = self._compute_thresholds(session, profile)
        return self.thresholds

    # ------------------------------------------------------------------
    def optimize_with_history(
        self,
        history: Iterable[Dict[str, float]],
        *,
        obi_candidates: Optional[List[float]] = None,
        ofi_candidates: Optional[List[float]] = None,
        outcome_key: str = "outcome",
    ) -> Dict[str, Dict[str, float]]:
        """Perform a lightweight grid search using historical outcomes.

        Parameters
        ----------
        history:
            Iterable of past metric dictionaries.  Each item may include an
            ``outcome`` flag (1 for successful signal, 0 for failed).  If the
            flag is missing the item is ignored for optimisation purposes.
        obi_candidates / ofi_candidates:
            Optional override for the grid of imbalance/OFI thresholds to
            evaluate.
        outcome_key:
            Name of the key in each history entry describing whether the
            triggered signal led to a positive outcome.
        """

        entries = [item for item in history if outcome_key in item]
        if not entries:
            return self.thresholds

        obi_candidates = obi_candidates or [0.55, 0.6, 0.65, 0.7]
        ofi_candidates = ofi_candidates or [0.0, 5.0, 10.0]

        orig_long_obi = self.base_thresholds["long"]["obi"]
        orig_long_ofi = self.base_thresholds["long"]["ofi"]
        best_score = -1.0
        best_combo = (orig_long_obi, orig_long_ofi)
        best_positives = 0
        best_negatives = 0

        for obi_thr in obi_candidates:
            for ofi_thr in ofi_candidates:
                positives = 0
                negatives = 0
                for item in entries:
                    triggered = item.get("obi5", 0.0) >= obi_thr and item.get("ofi", 0.0) >= ofi_thr
                    if not triggered:
                        continue
                    if item.get(outcome_key):
                        positives += 1
                    else:
                        negatives += 1
                total = positives + negatives
                if total == 0:
                    continue
                precision = positives / total
                score = precision * positives  # emphasise both quality and quantity
                if score > best_score:
                    best_score = score
                    best_combo = (obi_thr, ofi_thr)
                    best_positives = positives
                    best_negatives = negatives

        best_obi = max(best_combo[0], orig_long_obi)
        best_ofi = max(best_combo[1], orig_long_ofi)

        self.base_thresholds["long"]["obi"] = best_obi
        self.base_thresholds["long"]["ofi"] = best_ofi
        # Mirror the adjustment for shorts to keep symmetry
        self.base_thresholds["short"]["obi"] = 1.0 - best_obi
        self.base_thresholds["short"]["ofi"] = -best_ofi
        logger.info(
            "Optimised DOM thresholds from history",
            long_obi=best_obi,
            long_ofi=best_ofi,
            positives=best_positives,
            negatives=best_negatives,
            score=best_score,
        )
        return self.base_thresholds


__all__ = [
    "DOMParameterTuner",
    "RollingStats",
    "SessionProfile",
    "DEFAULT_BASE_THRESHOLDS",
    "DEFAULT_FEATURE_THRESHOLDS",
]
